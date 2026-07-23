"""
scorer.py
"3. 언급빈도 x 최신가중치 계산 (Scoring)" 담당 모듈.
(알고리즘 문서 "3. 언급빈도 x 최신가중치 계산" 참조)

이 레이어는 순수 계산만 한다 - LLM 안 씀 (문서에 "이 레이어도 LLM 안 씀. 순수
계산." 이라고 3.2 끝에 명시돼 있음).

** 중요 - 2.1 이슈 그룹핑과의 의존 관계 **
이 모듈의 함수들은 전부 "이슈 그룹"(같은 사건을 다루는 기사 묶음, list[dict])을
입력으로 받도록 설계했다 - issue_score 공식 자체가 "그룹 내 기사 각각 계산 후
합산"이기 때문에(3번 섹션 수식 참조) 애초에 그룹 단위가 자연스러운 입력이다.

문제는 2.1(BGE-M3 임베딩 기반 이슈 그룹핑)이 아직 구현 전이라, 지금 당장은
"진짜 이슈 그룹"을 넘겨줄 수 있는 곳이 없다는 것. 그래서 임시로
`to_singleton_groups()`를 하나 만들어 "기사 1건 = 그룹 1개"로 취급하게 했다.
이렇게 하면:
  - recency_weight, issue_score 계산 로직 자체는 지금 바로 검증 가능
  - 3.2의 "동일 언론사 도배 dedup"은 그룹 크기가 항상 1이라 사실상 작동할
    일이 없음(도배가 성립하려면 같은 그룹 안에 같은 언론사 기사가 여러 건
    있어야 하는데, 지금은 애초에 그룹이 기사 1건뿐이라 캡에 걸릴 대상이 없음)
  - 3.2의 국내-해외 교차 매칭 🔗 태그도 2.1이 있어야 성립하는 기능이라 지금은
    구현하지 않음 (아래 rank_top_n에 태그 필드 없음 - TODO로 남김)
2.1이 실제로 붙으면 `to_singleton_groups` 대신 진짜 그룹 리스트를 넘기기만
하면 되고, score_group/score_and_rank 쪽 로직은 바꿀 필요 없다.
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone


# --- recency_weight: 계단형(문턱 함수) 가중치, 3번 섹션 표 그대로 ---
#
# 문서에 "나중에 담당자가 값을 조정할 때, 수식 이해 없이 표의 숫자만 바꾸면
# 되도록" 계단형으로 확정했다고 명시돼 있음 - 이 표만 수정하면 조정 가능.
RECENCY_WEIGHT_TABLE = [
    # (경과일수 상한(포함), weight)  - 순서대로 검사, 마지막 항목이 "8일 이상"
    (2, 1.0),
    (5, 0.7),
    (7, 0.4),
]
RECENCY_WEIGHT_DEFAULT = 0.1  # 8일 이상


def recency_weight(days_elapsed: int) -> float:
    """발행일로부터 경과일수에 따른 계단형 가중치 (3번 섹션 표/코드 그대로 이식)."""
    for max_days, weight in RECENCY_WEIGHT_TABLE:
        if days_elapsed <= max_days:
            return weight
    return RECENCY_WEIGHT_DEFAULT


def _days_elapsed(published_at: str, reference: datetime | None = None) -> int:
    """
    published_at(ISO 8601 문자열)과 기준 시각(reference, 기본값 지금) 사이의
    경과일수를 정수로 반환한다. 음수(미래 날짜 등 이상치)는 0으로 clamp.
    """
    pub_dt = datetime.fromisoformat(published_at)
    ref = reference if reference is not None else datetime.now(pub_dt.tzinfo or timezone.utc)
    if pub_dt.tzinfo is None:
        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
    delta_days = (ref - pub_dt).days
    return max(0, delta_days)


# --- 동일 언론사 도배 dedup (3.2) ---
#
# "같은 언론사가 같은 이슈를 반복 게재하는 경우만 mention_count 집계에서
# 제한한다 (예: 언론사당 상위 N건까지만 카운트). 서로 다른 언론사가 각자
# 취재해서 다루는 경우는 캡을 걸지 않고 원본 그대로 반영"
PRESS_DEDUP_CAP = 3  # 언론사당 그룹 내 최대 카운트 (정확한 수치는 미확정 - 잠정값)


def _press_of(article: dict) -> str:
    """
    언론사 식별자를 꺼낸다. naver/gdelt는 "press"(도메인) 필드가 이미 있고,
    WATT는 "press" 필드가 없는 대신 "source"(예: "WATTAgNet")가 사실상
    언론사 역할을 한다 - 사이트 자체가 하나의 매체이므로 source를 대체값으로 씀.
    """
    return article.get("press") or article.get("source") or "(미상)"


def dedup_group_by_press(group: list[dict], cap: int = PRESS_DEDUP_CAP) -> list[dict]:
    """
    그룹(같은 이슈로 묶인 기사들) 안에서, 같은 언론사 기사가 cap건을 넘으면
    그 언론사 몫만 잘라낸다. 어떤 기사를 남길지는 최신순(발행일 내림차순)으로
    정렬해 앞에서부터 cap개만 유지 - 오래된 반복 게재보다 최근 것을 우선함.

    서로 다른 언론사는 캡을 걸지 않는다 (3.2 원칙 - "서로 다른 언론사가 각자
    취재해서 다루는 경우는... 원본 그대로 반영, 이게 실제 화제성 신호이기 때문")
    """
    by_press: dict[str, list[dict]] = defaultdict(list)
    for article in group:
        by_press[_press_of(article)].append(article)

    kept = []
    for press, press_articles in by_press.items():
        press_articles.sort(key=lambda a: a.get("published_at", ""), reverse=True)
        kept.extend(press_articles[:cap])
    return kept


def score_group(group: list[dict], reference: datetime | None = None) -> dict:
    """
    이슈 그룹 하나를 스코어링한다.

    반환값:
      issue_score: Σ recency_weight(경과일수) - dedup 이후 기사 기준 (3.2:
                   "dedup은 점수 계산과 화면 노출 숫자 양쪽에 동일하게 적용")
      mention_count: dedup 이후 건수 (화면 노출용)
      raw_mention_count: dedup 이전 원본 건수 (data/scored.json에만 남김, 3.2 참고)
      titles: 그룹에 속한 기사 제목 전부 (LLM 요약 단계에서 사용, 2.1 7번 항목)
      urls: 그룹에 속한 기사 원문 링크 전부
      press_list: 참여 언론사 목록 (발행매체 다양성 참고용, 11번 섹션 아이디어 2)
    """
    raw_mention_count = len(group)
    deduped = dedup_group_by_press(group)

    issue_score = sum(
        recency_weight(_days_elapsed(a["published_at"], reference)) for a in deduped
    )

    return {
        "issue_score": round(issue_score, 3),
        "mention_count": len(deduped),
        "raw_mention_count": raw_mention_count,
        "titles": [a["title"] for a in group],
        "urls": [a["url"] for a in group],
        "press_list": sorted({_press_of(a) for a in group}),
        "articles": group,  # 하위 단계(LLM 요약 등)에서 원본 기사 접근이 필요할 수 있어 보존
    }


def score_and_rank(groups: list[list[dict]], top_n: int | None = None,
                    reference: datetime | None = None) -> list[dict]:
    """
    이슈 그룹 리스트 전체를 스코어링하고 issue_score 내림차순으로 정렬한다.
    top_n이 지정되면 상위 N개만 반환 (7번 섹션 - 초기엔 "주간 Top 5"로 제한
    운영하기로 돼 있으니 main.py에서 LLM 요약 호출 전 top_n=5로 넘기면 됨).

    3.2 원칙대로 정규화·환산 없이 각 축(국내/해외)의 원본 issue_score를 그대로
    비교해 순위만 매긴다 - 국내/해외를 하나로 합치는 종합 랭킹은 만들지 않음
    (이 함수는 이미 한 축으로 분리된 groups만 받는다는 전제, main.py에서
    호출부가 국내/해외를 나눠서 각각 이 함수를 부른다).
    """
    scored = [score_group(g, reference) for g in groups]
    scored.sort(key=lambda s: s["issue_score"], reverse=True)
    return scored[:top_n] if top_n is not None else scored


def to_singleton_groups(articles: list[dict]) -> list[list[dict]]:
    """
    ** 임시 placeholder - 2.1 이슈 그룹핑이 구현되기 전까지만 쓴다 **

    "기사 1건 = 그룹 1개"로 취급해서, 진짜 그룹핑이 아직 없어도 스코어링
    로직(score_group/score_and_rank)을 지금 바로 검증할 수 있게 해준다.
    2.1이 실제로 구현되면 이 함수 대신 임베딩 매칭 결과(진짜 그룹 리스트)를
    score_and_rank에 바로 넘기면 되고, scorer.py 쪽 코드는 안 바꿔도 된다.
    """
    return [[a] for a in articles]


def _is_korean_title(title: str, threshold: float = 0.2) -> bool:
    """
    2026-07-22 추가: GDELT는 번역 인덱싱 기능이 있어서, 영어 키워드로 검색해도
    한국어 기사가 걸려 들어오는 경우가 있음(예: `foot-and-mouth disease` 검색에
    "구제역"이라는 단어를 쓴 순수 국내 기사 - 유튜버 닉네임 동음이의 사건이
    실제로 "해외" 랭킹에 노출된 사례로 실측 확인됨). 제목에서 한글 비율이
    threshold 이상이면 국내 기사로 판단한다 - langdetect 같은 언어 감지
    라이브러리는 제목처럼 짧은 텍스트에서 신뢰도가 떨어지는 것으로 알려져
    있어, 대신 결정론적이고 의존성 없는 한글 유니코드(가~힣, U+AC00-D7A3)
    비율 체크로 충분하다고 판단(담당자 논의로 결정).
    """
    if not title:
        return False
    hangul_count = sum(1 for ch in title if "\uac00" <= ch <= "\ud7a3")
    non_space_count = sum(1 for ch in title if not ch.isspace())
    if non_space_count == 0:
        return False
    return (hangul_count / non_space_count) >= threshold


def split_domestic_international(articles: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    3.1 "국내/해외 개별 집계" 축 분리. 네이버 = 국내, WATT/GDELT = 해외
    (알고리즘 문서 1번 섹션 소스 표 기준).

    2026-07-22 예외 추가: GDELT로 수집됐지만 제목이 한국어인 기사는 "해외"가
    아니라 "국내"로 재분류한다(`_is_korean_title` 참고) - WATT는 원래
    영어권 업계지라 이 문제가 없어 GDELT 소스에만 적용.
    """
    domestic = []
    international = []
    for a in articles:
        if a.get("source") == "네이버":
            domestic.append(a)
        elif a.get("source") == "GDELT" and _is_korean_title(a.get("title", "")):
            domestic.append(a)
        else:
            international.append(a)
    return domestic, international


def _majority_category(group: list[dict]) -> str:
    """
    그룹(같은 사건으로 묶인 기사들) 안에서 가장 많이 나온 category를 대표
    카테고리로 정한다. 이슈 그룹핑이 "같은 사건"을 묶는 거라 보통 카테고리가
    일관되지만, 사전 매칭이 기사마다 미묘하게 다르게 걸려 섞이는 경우가
    있을 수 있어 다수결로 정함. 동률이면 먼저 나온 걸 쓴다 - Counter는
    삽입 순서를 보존하는 dict 서브클래스이고 most_common()의 정렬은
    안정정렬(stable sort)이라, 동률인 항목들은 원래 순서(그룹 안에서 먼저
    나온 순서) 그대로 유지된다.
    """
    counts = Counter(a.get("category", "기타") for a in group)
    return counts.most_common(1)[0][0]


def score_by_category(groups: list[list[dict]], top_n: int,
                       exclude: tuple[str, ...] = ("기타",)) -> dict[str, list[dict]]:
    """
    카테고리별 Top N (2026-07-23 신규, 담당자 설계 확정: "국내/해외 축과
    독립적으로 카테고리 축도 만들되, 국내/해외 구분은 유지" - 즉 이 함수는
    domestic_groups/international_groups 중 하나를 받아서 그 축 "안에서"
    카테고리별로 나눈다. 호출부(main.py)가 국내용/해외용으로 각각 한 번씩
    불러써야 함).

    "기타"는 기본적으로 제외한다 - 카테고리별 Top N의 목적(질병명/무역·관세
    처럼 명확한 주제별로 뭐가 중요한지 보기)과 "카테고리 안 걸린 잡다한 것들
    중 언급 많은 것"은 성격이 다르기 때문(담당자 논의로 결정).

    이슈가 없는 카테고리는 결과 dict에 아예 키로 안 남는다(빈 리스트를
    안 만듦) - 호출부에서 "이 카테고리는 이번 주 이슈 없음"과 "이 카테고리
    자체가 없음"을 구분할 필요가 없게 하기 위함.
    """
    by_category: dict[str, list[list[dict]]] = defaultdict(list)
    for group in groups:
        category = _majority_category(group)
        if category in exclude:
            continue
        by_category[category].append(group)

    result = {}
    for category, cat_groups in by_category.items():
        ranked = score_and_rank(cat_groups, top_n=top_n)
        if ranked:
            # 2026-07-23 추가: 이후 단계(LLM 요약, storage 저장)에서 여러
            # 카테고리 결과를 하나의 평평한 리스트로 합쳐 처리할 일이 있는데,
            # 그때도 "이 항목이 원래 어느 카테고리였는지" 추적할 수 있도록
            # 항목 자체에 category를 남겨둔다.
            for item in ranked:
                item["category"] = category
            result[category] = ranked
    return result


def print_category_top_n(label: str, category_ranked: dict[str, list[dict]], n: int) -> None:
    """카테고리별 Top N을 사람이 읽기 좋은 형태로 출력 (진단/확인용)."""
    if not category_ranked:
        print(f"\n=== {label} 카테고리별 Top {n} === (해당 카테고리 이슈 없음)")
        return
    print(f"\n=== {label} 카테고리별 Top {n} ===")
    for category, ranked in category_ranked.items():
        print(f"\n[{category}]")
        for i, item in enumerate(ranked, start=1):
            rep_title = item["titles"][0] if item["titles"] else "(제목 없음)"
            print(f"  {i}. [{item['issue_score']:.2f}점, 언급 {item['mention_count']}건] {rep_title}")
            if len(item["titles"]) > 1:
                print(f"     (그룹 내 추가 {len(item['titles']) - 1}건 생략)")


def print_top_n(label: str, ranked: list[dict], n: int = 5) -> None:
    """상위 N개 이슈를 사람이 읽기 좋은 형태로 출력 (진단/확인용)."""
    print(f"\n=== {label} Top {min(n, len(ranked))} ===")
    for i, item in enumerate(ranked[:n], start=1):
        rep_title = item["titles"][0] if item["titles"] else "(제목 없음)"
        print(f"{i}. [{item['issue_score']:.2f}점, 언급 {item['mention_count']}건] {rep_title}")
        if len(item["titles"]) > 1:
            print(f"   (그룹 내 추가 {len(item['titles']) - 1}건 생략)")


if __name__ == "__main__":
    # 자체 점검용 - 그룹핑 없이 recency_weight/score_group만 확인
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    sample_articles = [
        {"title": "구제역 확산", "url": "https://a.com/1", "source": "네이버",
         "press": "yna.co.kr", "published_at": now.isoformat()},
        {"title": "구제역 추가 발생", "url": "https://a.com/2", "source": "네이버",
         "press": "yna.co.kr", "published_at": (now - timedelta(days=1)).isoformat()},
        {"title": "구제역 3주째", "url": "https://a.com/3", "source": "네이버",
         "press": "chosun.com", "published_at": (now - timedelta(days=6)).isoformat()},
    ]
    group_result = score_group(sample_articles)
    print(group_result)
    assert group_result["mention_count"] == 3  # cap=3 안 넘었으니 dedup 없음

    for d in (0, 2, 3, 5, 6, 7, 8, 30):
        print(f"경과 {d}일 -> weight {recency_weight(d)}")