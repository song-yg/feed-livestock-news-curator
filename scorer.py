"""
scorer.py
언급빈도 x 최신가중치 스코어링. LLM 안 씀, 순수 계산만.
입력은 이슈 그룹(list[dict]) 단위 - 그룹핑은 issue_grouper.group_issues()가 담당.
to_singleton_groups()는 미사용(테스트용 유틸로만 보존).
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone


# 경과일수 상한(포함) -> weight. 순서대로 검사, 마지막 이후는 8일 이상 취급.
RECENCY_WEIGHT_TABLE = [
    (2, 1.0),
    (5, 0.7),
    (7, 0.4),
]
RECENCY_WEIGHT_DEFAULT = 0.1  # 8일 이상


def recency_weight(days_elapsed: int) -> float:
    """경과일수 -> 계단형 가중치."""
    for max_days, weight in RECENCY_WEIGHT_TABLE:
        if days_elapsed <= max_days:
            return weight
    return RECENCY_WEIGHT_DEFAULT


def _days_elapsed(published_at: str, reference: datetime | None = None) -> int:
    """published_at ~ reference(기본 지금) 경과일수. 음수는 0으로 clamp."""
    pub_dt = datetime.fromisoformat(published_at)
    ref = reference if reference is not None else datetime.now(pub_dt.tzinfo or timezone.utc)
    if pub_dt.tzinfo is None:
        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
    delta_days = (ref - pub_dt).days
    return max(0, delta_days)


PRESS_DEDUP_CAP = 3  # 언론사당 그룹 내 최대 카운트 (잠정값)


def _press_of(article: dict) -> str:
    """언론사 식별자. naver/gdelt는 press(도메인), WATT는 source로 대체."""
    return article.get("press") or article.get("source") or "(미상)"


def dedup_group_by_press(group: list[dict], cap: int = PRESS_DEDUP_CAP) -> list[dict]:
    """그룹 내 언론사당 최신순 cap개까지만 유지. 언론사 간에는 캡 없음."""
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
    이슈 그룹 하나 스코어링.

    반환값:
      issue_score: Σ recency_weight(경과일수), dedup 이후 기준
      mention_count: dedup 이후 건수
      raw_mention_count: dedup 이전 원본 건수
      titles/urls: 그룹 내 전체 제목/링크
      press_list: 참여 언론사 목록
      cross_axis_partner_url: 국내/해외 교차 매칭 시 반대 축 대표 기사 URL
        (없으면 None). 표시용 제목은 main.py가 요약까지 끝난 뒤 이 URL로
        역참조해서 채움(여기서는 항상 None으로 초기화).
    """
    raw_mention_count = len(group)
    deduped = dedup_group_by_press(group)

    issue_score = sum(
        recency_weight(_days_elapsed(a["published_at"], reference)) for a in deduped
    )

    cross_axis_partner_url = None
    for a in group:
        if a.get("_cross_axis_partner_url"):
            cross_axis_partner_url = a["_cross_axis_partner_url"]
            break

    return {
        "issue_score": round(issue_score, 3),
        "mention_count": len(deduped),
        "raw_mention_count": raw_mention_count,
        "titles": [a["title"] for a in group],
        "urls": [a["url"] for a in group],
        "press_list": sorted({_press_of(a) for a in group}),
        "cross_axis_partner_url": cross_axis_partner_url,
        "cross_axis_partner": None,  # main.py가 최종 결과물 확정 후 역참조해서 채움
        "articles": group,
    }


def score_and_rank(groups: list[list[dict]], top_n: int | None = None,
                    reference: datetime | None = None) -> list[dict]:
    """그룹 리스트를 스코어링 후 issue_score 내림차순 정렬. top_n 지정 시 상위 N개만.
    국내/해외 통합 랭킹 없음 - 이미 한 축으로 분리된 groups만 받는 전제."""
    scored = [score_group(g, reference) for g in groups]
    scored.sort(key=lambda s: s["issue_score"], reverse=True)
    return scored[:top_n] if top_n is not None else scored


def to_singleton_groups(articles: list[dict]) -> list[list[dict]]:
    """기사 1건 = 그룹 1개 변환. 현재 파이프라인에서는 미사용, 테스트용."""
    return [[a] for a in articles]


def _is_korean_title(title: str, threshold: float = 0.2) -> bool:
    """제목의 한글 유니코드(가~힣) 비율로 국내 기사 여부 추정."""
    if not title:
        return False
    hangul_count = sum(1 for ch in title if "\uac00" <= ch <= "\ud7a3")
    non_space_count = sum(1 for ch in title if not ch.isspace())
    if non_space_count == 0:
        return False
    return (hangul_count / non_space_count) >= threshold


def _is_korean_gdelt_article(article: dict) -> bool:
    """GDELT 기사의 국내(한국어) 여부. language 필드 우선, 없으면 제목 한글 비율로 fallback."""
    language = article.get("language")
    if language:
        return language == "Korean"
    return _is_korean_title(article.get("title", ""))


def split_domestic_international(articles: list[dict]) -> tuple[list[dict], list[dict]]:
    """국내/해외 분리. 네이버=국내, WATT/GDELT=해외(GDELT는 한국어면 국내로 재분류)."""
    domestic = []
    international = []
    for a in articles:
        if a.get("source") == "네이버":
            domestic.append(a)
        elif a.get("source") == "GDELT" and _is_korean_gdelt_article(a):
            domestic.append(a)
        else:
            international.append(a)
    return domestic, international


def _majority_category(group: list[dict]) -> str:
    """그룹 내 다수결 카테고리. 동률이면 먼저 나온 순서 유지(Counter 안정정렬)."""
    counts = Counter(a.get("category", "기타") for a in group)
    return counts.most_common(1)[0][0]


def score_by_category(groups: list[list[dict]], top_n: int,
                       exclude: tuple[str, ...] = ("기타",),
                       dedupe_fn=None) -> dict[str, list[dict]]:
    """
    카테고리별 Top N. 국내/해외 축과 독립적, "기타"는 기본 제외.
    dedupe_fn 넘기면 카테고리별 전체 풀을 dedupe_fn(풀, top_n, category)에 태워 결과를 씀(issue_grouper.stage4_dedupe_and_promote 용도)
      - scorer.py는 순환참조 방지로 issue_grouper를 직접 import 안 해서 콜백 방식.
    """
    by_category: dict[str, list[list[dict]]] = defaultdict(list)
    for group in groups:
        category = _majority_category(group)
        if category in exclude:
            continue
        by_category[category].append(group)

    result = {}
    for category, cat_groups in by_category.items():
        if dedupe_fn is not None:
            full_pool = score_and_rank(cat_groups, top_n=None)
            ranked = dedupe_fn(full_pool, top_n, category)
        else:
            ranked = score_and_rank(cat_groups, top_n=top_n)
        if ranked:
            for item in ranked:
                item["category"] = category
            result[category] = ranked
    return result


def print_category_top_n(label: str, category_ranked: dict[str, list[dict]], n: int) -> None:
    """카테고리별 Top N 콘솔 출력(진단용)."""
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
            if item.get("cross_axis_partner"):
                print(f"     🔗 반대 축에서도 다뤄짐: {item['cross_axis_partner']}")


def print_top_n(label: str, ranked: list[dict], n: int = 5) -> None:
    """상위 N개 이슈 콘솔 출력(진단용)."""
    print(f"\n=== {label} Top {min(n, len(ranked))} ===")
    for i, item in enumerate(ranked[:n], start=1):
        rep_title = item["titles"][0] if item["titles"] else "(제목 없음)"
        print(f"{i}. [{item['issue_score']:.2f}점, 언급 {item['mention_count']}건] {rep_title}")
        if len(item["titles"]) > 1:
            print(f"   (그룹 내 추가 {len(item['titles']) - 1}건 생략)")
        if item.get("cross_axis_partner"):
            print(f"   🔗 반대 축에서도 다뤄짐: {item['cross_axis_partner']}")