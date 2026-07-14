"""
main.py
사료·축산업 뉴스 큐레이션 시스템의 오케스트레이션 레이어.
(알고리즘 문서 "6. 실행/오케스트레이션 (Runner)" 참조)

문서의 6단계 파이프라인 중 이번 작업에서 실제로 구현한 범위:
  1) 수집        -> 구현 완료 (watt/naver/gdelt collector, 기존 모듈 그대로 호출)
  2) 정규화      -> 절반만 구현: 공통 스키마 통합 + 완전 동일 기사(URL) 제거는 됨.
                    2.1 이슈 그룹핑(BGE-M3 임베딩)은 아직 미구현 - 대신
                    scorer.to_singleton_groups()로 "기사 1건 = 그룹 1개" 임시 처리
                    (2.2 키워드 태깅은 이번에 구현 완료 - keyword_tagger.py)
  3) 스코어링    -> 구현 완료 (scorer.py) - 단, 2번의 임시 처리 때문에 지금은
                    사실상 "기사 단위 점수"와 동일하게 작동함 (그룹이 다 크기 1)
                    + 카테고리 전체 집계(category_aggregator.py, 2026-07-14
                    신규) 보조 지표 추가 - 2.1 이슈 그룹핑이 "동일 사건만"
                    묶는 좁은 정의라 생기는 공백을 메우는 별개 지표 (순위와
                    무관, 국내/해외 각각 카테고리별 단순 건수만 집계)
  4) LLM 요약    -> 미구현 (TODO)
  5) 저장        -> 미구현 (TODO) - 이번엔 확인용으로 scored 결과를 콘솔 출력만 함
  6) 배포        -> 미구현 (TODO)

이번 세션 스코프(사용자 지정, 2026-07-14): "2.2 키워드 태깅과 스코어링부터".
main.py를 6단계 전부 완성형으로 만들지 않고, 위 범위까지만 실제로 동작하는
파이프라인으로 잇는다. 4/5/6은 다음 세션에서 채울 자리를 함수 스텁으로만
남겨둔다 (아래 _step4_todo 등).
"""

import gdelt_collector
import naver_collector
import scorer
from WATT_collector import collect as watt_collect  # noqa: N813 (파일명 규칙과 다르지만 기존 파일 그대로 사용)
import keyword_tagger
import category_aggregator


# ---------------------------------------------------------------------------
# 1) 수집 레이어 (섹션 1)
# ---------------------------------------------------------------------------

def run_collectors() -> tuple[list[dict], dict, list[str]]:
    """
    watt/naver/gdelt collector를 순서대로 실행한다.

    9.1 "소스별 독립 실행 구조" - 각 collector 호출을 개별 try/except로 감싸서
    하나가 완전히 죽어도(예: import 실패, 예상 밖 예외) 나머지 소스는 계속
    진행한다. 각 collector 내부에도 이미 더 세밀한 단위(WATT는 사이트별,
    naver/gdelt는 키워드별)의 방어가 있지만, 여기 main.py 레벨의 try/except는
    "collector 모듈 자체가 통째로 실패하는 경우"에 대한 마지막 방어선이다.

    반환값:
      all_articles: watt+naver+gdelt 기사를 하나로 합친 리스트 (아직 정규화 전
                    원본 스키마 - 이미 세 collector가 공통 스키마를 지키므로
                    합치기만 하면 됨)
      gdelt_timeline: GDELT 시계열 데이터 (3.1 규칙대로 스코어링에는 안 들어가고
                    참고 지표 전용 - 지금은 그냥 들고만 있음, 저장 레이어(5번,
                    아직 미구현) 완성 시 그쪽에서 사용)
      failed_sources: 실패한 소스 이름 목록 (9.2 "에러 리포트 자동화"의 재료 -
                    저장/배포 레이어가 아직 없어서 지금은 콘솔에만 출력)
    """
    all_articles: list[dict] = []
    gdelt_timeline: dict = {}
    failed_sources: list[str] = []

    try:
        watt_articles = watt_collect()
        all_articles.extend(watt_articles)
        print(f"[main] WATT 수집 완료 - {len(watt_articles)}건")
    except Exception as e:
        print(f"[main] WATT 수집 실패 (소스 전체): {type(e).__name__} - {e!r}")
        failed_sources.append("WATT")

    try:
        naver_articles = naver_collector.collect()
        all_articles.extend(naver_articles)
        print(f"[main] 네이버 수집 완료 - {len(naver_articles)}건")
    except Exception as e:
        print(f"[main] 네이버 수집 실패 (소스 전체): {type(e).__name__} - {e!r}")
        failed_sources.append("네이버")

    try:
        gdelt_articles, gdelt_timeline = gdelt_collector.collect()
        all_articles.extend(gdelt_articles)
        print(f"[main] GDELT 수집 완료 - {len(gdelt_articles)}건")
    except Exception as e:
        print(f"[main] GDELT 수집 실패 (소스 전체): {type(e).__name__} - {e!r}")
        failed_sources.append("GDELT")

    return all_articles, gdelt_timeline, failed_sources


# ---------------------------------------------------------------------------
# 2) 정규화 레이어 (섹션 2) - 완전 동일 기사 제거 + 2.2 키워드 태깅까지만
#    (2.1 이슈 그룹핑은 미구현 - scorer.to_singleton_groups로 임시 대체)
# ---------------------------------------------------------------------------

def normalize(articles: list[dict]) -> list[dict]:
    """
    "완전 동일 기사 제거": 같은 URL이 중복 수집된 경우만 제거 (2번 섹션 명시 -
    이슈 그룹핑과는 다른 개념. 여긴 정말 똑같은 기사가 두 번 들어온 경우만
    거른다 - 예: 페이지네이션 겹침, 재실행 등).

    첫 번째로 본 URL을 유지하고 이후 중복은 버린다 (순서 유지를 위해 dict를
    순서 보존 집합처럼 사용).
    """
    seen_urls: set[str] = set()
    deduped = []
    for article in articles:
        url = article.get("url")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        deduped.append(article)

    removed = len(articles) - len(deduped)
    if removed:
        print(f"[main] 완전 동일 기사(URL 중복) {removed}건 제거")

    return deduped


# ---------------------------------------------------------------------------
# 3) 스코어링 (섹션 3) - scorer.py 그대로 사용
# ---------------------------------------------------------------------------

def score(articles: list[dict], top_n: int = 5) -> tuple[list[dict], list[dict]]:
    """
    3.1 국내/해외 개별 집계 + 3.2 개별 랭킹(Top N)까지 수행.

    ** 2.1 이슈 그룹핑 미구현 상태의 한계 **: 원래는 같은 이슈를 다루는 여러
    기사가 하나의 그룹으로 묶여서 issue_score가 "그 이슈에 대한 언급 총량"을
    반영해야 하는데(3번 섹션 수식), 지금은 to_singleton_groups로 기사 1건=
    그룹 1건이라 사실상 "그 기사 자체의 최신성 점수"만 나온다 - 여러 매체가
    같은 사건을 다뤄도 따로따로 집계되어 순위에 제대로 안 뭉쳐 나타남.
    2.1이 구현되면 이 함수의 to_singleton_groups 호출부만 실제 그룹 리스트로
    바꾸면 되고, 그 아래(score_and_rank) 로직은 그대로 재사용된다.
    """
    domestic, international = scorer.split_domestic_international(articles)

    domestic_groups = scorer.to_singleton_groups(domestic)
    international_groups = scorer.to_singleton_groups(international)

    domestic_ranked = scorer.score_and_rank(domestic_groups, top_n=top_n)
    international_ranked = scorer.score_and_rank(international_groups, top_n=top_n)

    return domestic_ranked, international_ranked


# ---------------------------------------------------------------------------
# 4)~6) 아직 미구현 - 자리만 표시
# ---------------------------------------------------------------------------

def _step4_llm_summary_todo(top_issues: list[dict]) -> None:
    """
    TODO (섹션 4): 상위 이슈들의 제목(+본문 핵심/description)을 LLM에 넘겨
    2~3문장 자체 요약 생성. (A) 자체 요약, (A-1) 단독기사 fallback, (B) 그룹핑
    보조 세 지점 중 (B)는 2.1이 있어야 의미가 생기므로 2.1과 같이 붙일 것.
    지금은 아무 것도 하지 않음 - 다음 세션 작업.
    """


def _step5_storage_todo(domestic_ranked: list[dict], international_ranked: list[dict],
                         gdelt_timeline: dict, failed_sources: list[str],
                         category_distribution: dict) -> None:
    """
    TODO (섹션 5): data/YYYY-WW/raw.json, scored.json, summary.md 저장.
    9.2 에러 리포트(failed_sources)도 이 단계 결과물에 자동으로 붙여야 함.
    category_distribution(카테고리 전체 집계, 2026-07-14 신규)도 scored.json에
    같이 저장해야 다음 주 "지난주 대비 증감"(category_aggregator.py 모듈
    docstring의 "이번 범위 밖" 항목) 비교가 가능해진다 - 저장 레이어 구현 시
    함께 반영할 것.
    지금은 저장 대신 콘솔 출력으로 대체 (아래 print_summary 참고).
    """


def _step6_deploy_todo() -> None:
    """TODO (섹션 6, 7번 섹션): 1단계는 이메일 본문(HTML) 배포로 확정돼 있음."""


# ---------------------------------------------------------------------------
# 오케스트레이션 진입점
# ---------------------------------------------------------------------------

def run() -> None:
    print("=== [1] 수집 시작 ===")
    articles, gdelt_timeline, failed_sources = run_collectors()

    print("\n=== [2] 정규화 (스키마 통일은 collector가 이미 함 / URL dedup + 키워드 태깅) ===")
    articles = normalize(articles)
    keyword_tagger.tag_articles(articles)
    keyword_tagger.print_category_distribution(articles)

    print("\n=== [3] 스코어링 (국내/해외 개별 Top N) ===")
    domestic_ranked, international_ranked = score(articles, top_n=5)
    scorer.print_top_n("국내", domestic_ranked, n=5)
    scorer.print_top_n("해외", international_ranked, n=5)

    print("\n=== [3-보조] 카테고리 전체 집계 (국내/해외, 2026-07-14 신규) ===")
    # 2.1 이슈 그룹핑이 "동일 사건만" 묶는 좁은 정의라 큰 트렌드가 개별
    # 이슈로 흩어져 보이는 공백을 메우는 거친(coarse) 보조 지표 - 순위(Top N)와는
    # 별개로, 카테고리 자체가 이번 주 몇 건 다뤄졌는지만 보여준다.
    # (category_aggregator.py 모듈 docstring 참고)
    category_distribution = category_aggregator.aggregate(articles)
    category_aggregator.print_aggregate(category_distribution)

    # 4~6단계는 아직 자리만 (TODO)
    _step4_llm_summary_todo(domestic_ranked + international_ranked)
    _step5_storage_todo(domestic_ranked, international_ranked, gdelt_timeline,
                         failed_sources, category_distribution)
    _step6_deploy_todo()

    if failed_sources:
        print(f"\n[main] 이번 실행 실패 소스: {failed_sources} (9.2 에러 리포트 - "
              f"저장/배포 레이어 완성 전까지는 콘솔 로그로만 확인 가능)")


if __name__ == "__main__":
    run()
