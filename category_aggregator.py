"""
category_aggregator.py
"카테고리 전체 집계" 담당 신규 모듈 (2026-07-14 세션에서 신규 기능으로 확정).

배경: 2.1 이슈 그룹핑의 정의를 "동일 사건만"으로 확정하면서(예: 한국
조류독감 발생과 미국 조류독감 발생은 별도 이슈), 큰 트렌드가 개별 이슈로
잘게 흩어져 보일 수 있는 공백이 생겼다. 이슈 그룹핑 자체를 넓히는 대신,
keyword_tagger.py의 CATEGORY_KEYWORDS를 활용해 "이 카테고리(질병명/시장가격
등)가 이번 주 전체적으로 몇 건 다뤄졌는지"를 보여주는 별도의 거친(coarse)
보조 지표를 추가하기로 함 (문서 "11. 추가 고려 기능" 2번 아이디어 계열).

** keyword_score(3번 섹션 공식, 아직 미적용)와의 차이 - 대체 관계 아님 **
문서 3번 섹션에 정의만 되고 미적용 상태인 keyword_score = keyword_hit_count
x recency_weight 는:
  - 단위: 개별 키워드 (예: "조류독감"이라는 단어 하나)
  - 목적: 카테고리별 트렌드 "시각화"와 "민감 키워드 알림"용으로 설계된 좀 더
    정밀한 지표 (문서에 "확정 시 추가 개발 필요"라고 명시 - 여전히 미구현)

이 모듈(category_aggregator.py)은:
  - 단위: 카테고리 (keyword_tagger.tag_title이 기사 1건당 매긴 category
    필드를 그대로 씀 - 개별 키워드가 아니라 기사 단위 라벨)
  - 목적: "이 카테고리가 이번 주 전반적으로 얼마나 다뤄졌는지" 한눈에
    보여주는 거친 주간 개요. 순위 경쟁(랭킹)용이 아니라 스코어링(3번
    섹션)과는 독립된 별도 지표.

입력 단위도 목적도 달라 서로 대체 관계가 아닌 별개 기능 - keyword_score는
여전히 미구현 상태로 남겨두고, 이 모듈만 이번에 구현한다.

** 집계 방식: 단순 건수 (recency_weight 미적용, 2026-07-14 결정) **
이유:
  1. 수집 자체가 이미 최근 7일로 제한돼 있어(naver_collector DAYS_BACK=7 등),
     "최신성"이 어차피 좁은 창 안에서만 움직여 recency_weight의 계단이
     주는 변별력이 이슈 단위 랭킹(3번 섹션)만큼 크지 않음
  2. 이 지표는 순위를 매기거나 무언가를 선별(Top N)하는 데 쓰이는 게
     아니라 "카테고리 전체 개요"를 보여주는 용도라, 가중치 없는 단순 건수가
     더 투명하고 설명하기 쉬움
  3. keyword_tagger.py에 이미 있던 진단용 print_category_distribution()도
     같은 원칙(단순 건수)을 쓰고 있어 일관성 유지
가중치가 필요해지면(예: "이 카테고리가 최근 며칠 새 급증했는지" 같은 요구가
생기면) keyword_score 쪽에서 별도로 다루는 게 맞다고 판단 - 이 모듈에
나중에 섞지 않는다.

** 집계 범위: 국내/해외 각각 별도 (3.1~3.2 기존 원칙과 동일, 2026-07-14 확정) **
scorer.split_domestic_international()을 그대로 재사용해서 두 축을 나눈다 -
이 프로젝트 전체가 "종합 랭킹 없음, 국내/해외 각자 원본 신호 보존"
원칙(3.2)이라 이 지표만 예외로 통합하면 다른 결과물과 앞뒤가 안 맞는다.

** 지난주 대비 증감: 구현 완료 (2026-07-25) **
저장 레이어(storage.py)가 2026-07-23에 구현 완료되면서 "지난주 값"을
가져올 데이터가 생겨, compare_with_last_week()/print_aggregate_with_
comparison()으로 구현했다(아래 함수 정의부 docstring 참고). 비교 대상은
category_distribution(카테고리별 단순 건수) - 이슈(Top N) 단위 비교는
그룹 매칭이 까다로워 범위 밖으로 유지(담당자 논의로 결정).
"""

import json
import os
from collections import Counter

from keyword_tagger import CATEGORY_KEYWORDS
import scorer
import storage


# CATEGORY_KEYWORDS의 카테고리 순서 그대로 + "기타"를 마지막에 추가해서
# 출력 순서를 고정한다 (키워드표 카테고리 번호 순 - keyword_tagger.py의
# 동점 처리 결정적 규칙과 같은 이유: 실행마다 순서가 흔들리지 않도록)
_CATEGORY_ORDER = list(CATEGORY_KEYWORDS.keys()) + ["기타"]


def count_by_category(articles: list[dict]) -> Counter:
    """
    기사 리스트 하나를 받아 카테고리별 건수를 센다.

    articles는 keyword_tagger.tag_articles()가 이미 "category" 필드를
    채워둔 상태여야 한다 (2.2 태깅이 먼저 실행돼 있어야 함 - 이 함수 자체는
    태깅을 하지 않고, 이미 붙어있는 category 필드만 집계한다).
    """
    return Counter(a.get("category", "기타") for a in articles)


def aggregate(articles: list[dict]) -> dict[str, Counter]:
    """
    3.1 원칙대로 국내/해외 두 축으로 나눠서 각각 카테고리별 건수를 집계한다.

    반환값: {"국내": Counter, "해외": Counter}
    (scorer.split_domestic_international과 동일한 축 정의 - 네이버=국내,
    WATT/GDELT=해외)
    """
    domestic, international = scorer.split_domestic_international(articles)
    return {
        "국내": count_by_category(domestic),
        "해외": count_by_category(international),
    }


def print_aggregate(aggregated: dict[str, Counter]) -> None:
    """
    국내/해외 카테고리 집계를 사람이 읽기 좋은 표 형태로 출력한다.

    (keyword_tagger.print_category_distribution과 같은 톤 - 이번 주 결과물
    콘솔 확인용. 저장 레이어 완성 전까지는 이 출력이 유일한 확인 방법.)
    """
    for axis in ("국내", "해외"):
        counter = aggregated.get(axis, Counter())
        total = sum(counter.values())
        print(f"\n=== 카테고리 집계 - {axis} (전체 {total}건) ===")
        if total == 0:
            print("  (기사 없음)")
            continue
        for category in _CATEGORY_ORDER:
            count = counter.get(category, 0)
            if count == 0:
                continue
            pct = count / total * 100
            print(f"  {category:15s} {count:4d}건 ({pct:.1f}%)")


# ---------------------------------------------------------------------------
# 지난주 대비 증감 (2026-07-25 신규 구현 - 모듈 docstring "지난주 대비 증감:
# 이번 범위 밖" 문단에 예고해뒀던 자리를 채움. 저장 레이어(storage.py)가
# 2026-07-23에 구현 완료돼서, 이제 지난주 scored.json을 읽어올 수 있음.)
# ---------------------------------------------------------------------------

def compare_with_last_week(current: dict[str, Counter], base_dir: str = "data",
                            reference=None) -> dict[str, dict[str, dict]] | None:
    """
    이번 주 카테고리 집계(aggregate()의 반환값)를 지난주 scored.json의
    category_distribution과 비교한다.

    비교 대상을 category_distribution(카테고리별 단순 건수)으로 정한 이유:
    이 값이 애초에 "다음 주 실행에서 지난주 대비 증감을 계산하려면 지난주
    집계 결과가 파일로 남아있어야 한다"는 목적으로 저장돼온 데이터라
    (storage.save_scored docstring 참고) 가장 자연스러운 1차 비교 대상.
    이슈(Top N) 단위 비교("이 이슈가 지난주에도 있었나")는 그룹 매칭이 훨씬
    까다로워서(제목이 매주 조금씩 다름, 그룹핑 자체가 매주 새로 돎) 범위
    밖으로 남겨둠(담당자 논의로 결정).

    지난주 파일이 없으면(첫 실행, 혹은 지난주 저장이 실패했던 경우) 예외를
    던지지 않고 None을 반환한다 - 호출부가 이걸 보고 "지난주 데이터 없음"
    으로 안전하게 표시하면 됨(9.4/9.5 원칙과 같은 방향).

    반환값 (지난주 데이터가 있는 경우):
      {"국내": {카테고리: {"this_week": int, "last_week": int, "delta": int}, ...},
       "해외": {...}}
    두 주 중 어느 한쪽에만 등장한 카테고리도 전부 포함(없는 쪽은 0으로 취급).
    """
    path = os.path.join(storage.previous_week_dir(base_dir, reference), "scored.json")
    try:
        with open(path, encoding="utf-8") as f:
            last_week_payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        print(f"[category_aggregator] 지난주 데이터 없음/읽기 실패({path}) - "
              f"증감 비교 생략: {type(e).__name__}")
        return None

    last_week_distribution = last_week_payload.get("category_distribution", {})

    comparison = {}
    for axis in ("국내", "해외"):
        this_counter = current.get(axis, Counter())
        last_counter = last_week_distribution.get(axis, {})
        categories = set(this_counter) | set(last_counter)
        axis_result = {}
        for category in categories:
            this_count = this_counter.get(category, 0)
            last_count = last_counter.get(category, 0)
            axis_result[category] = {
                "this_week": this_count,
                "last_week": last_count,
                "delta": this_count - last_count,
            }
        comparison[axis] = axis_result

    return comparison


def print_aggregate_with_comparison(aggregated: dict[str, Counter],
                                     comparison: dict[str, dict[str, dict]] | None) -> None:
    """
    print_aggregate()와 같은 표를 만들되, comparison이 있으면(지난주 데이터가
    있으면) 각 줄에 지난주 대비 증감을 같이 붙인다. comparison이 None이면
    print_aggregate()와 동일하게 동작(증감 표시 없이).
    """
    for axis in ("국내", "해외"):
        counter = aggregated.get(axis, Counter())
        total = sum(counter.values())
        suffix = "" if comparison is None else ", 지난주 대비"
        print(f"\n=== 카테고리 집계 - {axis} (전체 {total}건{suffix}) ===")
        if total == 0:
            print("  (기사 없음)")
            continue
        axis_comparison = None if comparison is None else comparison.get(axis, {})
        for category in _CATEGORY_ORDER:
            count = counter.get(category, 0)
            if count == 0:
                continue
            pct = count / total * 100
            line = f"  {category:15s} {count:4d}건 ({pct:.1f}%)"
            if axis_comparison is not None and category in axis_comparison:
                delta = axis_comparison[category]["delta"]
                last_count = axis_comparison[category]["last_week"]
                sign = "+" if delta >= 0 else ""
                line += f" [지난주 {last_count}건, {sign}{delta}]"
            print(line)


if __name__ == "__main__":
    # 자체 점검용 - keyword_tagger 없이도(카테고리 미리 붙여서) 집계 로직만 확인
    sample_articles = [
        {"source": "네이버", "category": "질병명"},
        {"source": "네이버", "category": "질병명"},
        {"source": "네이버", "category": "시장/가격 용어"},
        {"source": "WATTAgNet", "category": "질병명"},
        {"source": "GDELT", "category": "기타"},
    ]
    result = aggregate(sample_articles)
    print_aggregate(result)
    assert result["국내"]["질병명"] == 2
    assert result["해외"]["질병명"] == 1
    assert result["해외"]["기타"] == 1
    print("\n[category_aggregator] 자체 점검 통과")