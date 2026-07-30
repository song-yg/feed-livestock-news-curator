"""
main.py
사료·축산업 뉴스 큐레이션 시스템 오케스트레이션 레이어.

6단계: 수집 -> 정규화(dedup+태깅+관련성필터+카테고리재분류+이슈그룹핑) ->
스코어링(국내/해외 Top N + 카테고리별 Top N, 4차 사후재검토 포함) ->
LLM 요약 -> 저장(storage.py) -> 배포(deploy.py, 이메일).
"""

import os
from collections import defaultdict
from datetime import datetime, timezone

import gdelt_collector
import naver_collector
import scorer
import issue_grouper
import llm_summarizer
from WATT_collector import collect as watt_collect  # noqa: N813
import keyword_tagger
import category_aggregator
import relevance_filter
import storage
import deploy

# 국내/해외 Top N, 카테고리별 Top N. 환경변수로 조정 가능(기본 5/1).
TOP_N = int(os.environ.get("TOP_N") or 5)
CATEGORY_TOP_N = int(os.environ.get("CATEGORY_TOP_N") or 1)


# ---------------------------------------------------------------------------
# 1) 수집 레이어
# ---------------------------------------------------------------------------

def run_collectors() -> tuple[list[dict], dict, list[str]]:
    """watt/naver/gdelt collector 순차 실행. 소스별 독립 실행(하나 실패해도 나머지 계속).

    반환: all_articles(합친 기사 리스트), gdelt_timeline, failed_sources
    """
    all_articles: list[dict] = []
    gdelt_timeline: dict = {}
    failed_sources: list[str] = []

    try:
        watt_articles = watt_collect()
        all_articles.extend(watt_articles)
        print(f"[main] WATT 수집 완료 - {len(watt_articles)}건")
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-01] - WATT 수집 실패 (소스 전체): {type(e).__name__} - {e!r}")
        failed_sources.append("WATT")

    try:
        naver_articles = naver_collector.collect()
        all_articles.extend(naver_articles)
        print(f"[main] 네이버 수집 완료 - {len(naver_articles)}건")
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-02] - 네이버 수집 실패 (소스 전체): {type(e).__name__} - {e!r}")
        failed_sources.append("네이버")

    try:
        gdelt_articles, gdelt_timeline = gdelt_collector.collect()
        all_articles.extend(gdelt_articles)
        print(f"[main] GDELT 수집 완료 - {len(gdelt_articles)}건")
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-03] - GDELT 수집 실패 (소스 전체): {type(e).__name__} - {e!r}")
        failed_sources.append("GDELT")

    return all_articles, gdelt_timeline, failed_sources


# ---------------------------------------------------------------------------
# 2) 정규화 레이어 - 완전 동일 기사 제거
# ---------------------------------------------------------------------------

def normalize(articles: list[dict]) -> list[dict]:
    """URL 완전 동일 기사만 제거(이슈 그룹핑과 별개). 첫 등장만 유지."""
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
# 3) 스코어링
# ---------------------------------------------------------------------------

def score(articles: list[dict], model, top_n: int = TOP_N) -> tuple[list[dict], list[dict], dict, dict]:
    """
    issue_grouper.group_issues + 국내/해외 Top N + 카테고리별 Top N 계산.

    그룹핑은 전체 기사 대상으로 먼저 실행 후 국내/해외로 나눠 스코어링(교차
    매칭을 위해 축 분리 전에 그룹핑). 국내/해외 양쪽에 걸친 그룹은 각 축에
    그 축 기사만 넘기고 cross_axis_partner로 상호 표시.

    GDELT 소스 중 한국어 기사는 scorer._is_korean_gdelt_article로 국내 재분류.
    model=None이면 group_issues가 1차 결과만으로 fallback.

    국내/해외 Top N + 카테고리별 Top N 전부 issue_grouper.stage4_dedupe_and_promote로
    사후 재검토(병합+승격)를 거침. 카테고리별은 scorer.score_by_category의
    dedupe_fn 콜백으로 연결.
    """
    groups = issue_grouper.group_issues(articles, model=model)

    domestic_groups = []
    international_groups = []
    for group in groups:
        domestic_part = [
            a for a in group
            if a.get("source") == "네이버"
            or (a.get("source") == "GDELT" and scorer._is_korean_gdelt_article(a))
        ]
        international_part = [
            a for a in group
            if a.get("source") != "네이버"
            and not (a.get("source") == "GDELT" and scorer._is_korean_gdelt_article(a))
        ]
        if domestic_part and international_part:
            domestic_part[0]["_cross_axis_partner"] = international_part[0].get("title", "")
            international_part[0]["_cross_axis_partner"] = domestic_part[0].get("title", "")
        if domestic_part:
            domestic_groups.append(domestic_part)
        if international_part:
            international_groups.append(international_part)

    domestic_ranked_pool = scorer.score_and_rank(domestic_groups, top_n=None)
    international_ranked_pool = scorer.score_and_rank(international_groups, top_n=None)

    domestic_ranked = issue_grouper.stage4_dedupe_and_promote(domestic_ranked_pool, top_n=top_n, label="국내")
    international_ranked = issue_grouper.stage4_dedupe_and_promote(international_ranked_pool, top_n=top_n, label="해외")

    def _category_dedupe_fn(axis_label: str):
        def _fn(ranked_pool, n, category):
            return issue_grouper.stage4_dedupe_and_promote(ranked_pool, top_n=n, label=f"{axis_label}-{category}")
        return _fn

    domestic_category_ranked = scorer.score_by_category(
        domestic_groups, CATEGORY_TOP_N, dedupe_fn=_category_dedupe_fn("국내"))
    international_category_ranked = scorer.score_by_category(
        international_groups, CATEGORY_TOP_N, dedupe_fn=_category_dedupe_fn("해외"))

    return domestic_ranked, international_ranked, domestic_category_ranked, international_category_ranked


# ---------------------------------------------------------------------------
# 2.1 이슈 그룹핑용 임베딩 모델 로드
# ---------------------------------------------------------------------------

def _load_embedding_model():
    """BGE-M3 모델을 실행당 1회 로드. 실패 시 None 반환(2차 없이 fallback)."""
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("BAAI/bge-m3")
        print("[main] BGE-M3 임베딩 모델 로드 완료")
        return model
    except Exception as e:
        print(f"[main] 🟡 주의 [MN-04] - BGE-M3 모델 로드 실패 - 2차(임베딩) 매칭 없이 진행 "
              f"(1차 사전 매칭만 적용됨): {type(e).__name__} - {e!r}")
        return None


# ---------------------------------------------------------------------------
# 4) LLM 요약
# ---------------------------------------------------------------------------

def _regroup_by_category(items: list[dict]) -> dict[str, list[dict]]:
    """item["category"] 기준으로 평평한 리스트를 {카테고리: [항목,...]}로 재구성."""
    regrouped: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        regrouped[item.get("category", "미상")].append(item)
    return dict(regrouped)


def step4_category_llm_summary(domestic_category_ranked: dict[str, list[dict]],
                                international_category_ranked: dict[str, list[dict]],
                                ) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """카테고리별 Top N에 (A)/(A-1) 요약 적용. 평평한 리스트로 합쳐 처리 후 재구성."""
    domestic_flat = [item for items in domestic_category_ranked.values() for item in items]
    international_flat = [item for items in international_category_ranked.values() for item in items]

    domestic_summarized_flat = llm_summarizer.summarize_top_issues(domestic_flat, label="국내-카테고리")
    international_summarized_flat = llm_summarizer.summarize_top_issues(international_flat, label="해외-카테고리")

    return (_regroup_by_category(domestic_summarized_flat),
            _regroup_by_category(international_summarized_flat))


def step4_llm_summary(domestic_ranked: list[dict],
                       international_ranked: list[dict]) -> tuple[list[dict], list[dict]]:
    """국내/해외 Top N에 (A)/(A-1) 요약 적용. 반환값은 summary 필드 추가된 동일 형태."""
    domestic_summarized = llm_summarizer.summarize_top_issues(domestic_ranked, label="국내")
    international_summarized = llm_summarizer.summarize_top_issues(international_ranked, label="해외")
    return domestic_summarized, international_summarized


# ---------------------------------------------------------------------------
# 오케스트레이션 진입점
# ---------------------------------------------------------------------------

def run() -> None:
    print("=== [1] 수집 시작 ===")
    articles, gdelt_timeline, failed_sources = run_collectors()

    print("\n=== [2] 정규화 ===")
    try:
        articles = normalize(articles)
        keyword_tagger.tag_articles(articles)
        keyword_tagger.print_category_distribution(articles)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-05] - [2] 정규화/태깅 단계에서 예상 못 한 오류 발생 - 원본 기사 그대로 다음 단계로 진행: "
              f"{type(e).__name__} - {e!r}")

    print("\n=== [2.5] 관련성 필터 ===")
    try:
        articles = relevance_filter.filter_articles(articles)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-06] - [2.5] 관련성 필터 단계에서 예상 못 한 오류 발생 - 필터링 없이 다음 단계로 진행: "
              f"{type(e).__name__} - {e!r}")

    print("\n=== [2.6] 카테고리 재분류 ===")
    try:
        articles = relevance_filter.recategorize_uncategorized(articles)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-07] - [2.6] 카테고리 재분류 단계에서 예상 못 한 오류 발생 - 재분류 없이 다음 단계로 진행: "
              f"{type(e).__name__} - {e!r}")

    print("\n=== [2.1] 이슈 그룹핑 임베딩 모델 로드 ===")
    embedding_model = _load_embedding_model()

    print("\n=== [3] 스코어링 ===")
    try:
        (domestic_ranked, international_ranked,
         domestic_category_ranked, international_category_ranked) = score(articles, embedding_model, top_n=TOP_N)
        scorer.print_top_n("국내", domestic_ranked, n=TOP_N)
        scorer.print_top_n("해외", international_ranked, n=TOP_N)
        scorer.print_category_top_n("국내", domestic_category_ranked, n=CATEGORY_TOP_N)
        scorer.print_category_top_n("해외", international_category_ranked, n=CATEGORY_TOP_N)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-08] - [3] 스코어링 단계에서 예상 못 한 오류 발생 - 이번 주는 Top N 없이 진행"
              f"(저장 단계에서 raw.json은 그대로 남음): {type(e).__name__} - {e!r}")
        domestic_ranked, international_ranked = [], []
        domestic_category_ranked, international_category_ranked = {}, {}

    print("\n=== [3-보조] 카테고리 전체 집계 ===")
    try:
        category_distribution = category_aggregator.aggregate(articles)
        category_comparison = category_aggregator.compare_with_last_week(category_distribution)
        category_aggregator.print_aggregate_with_comparison(category_distribution, category_comparison)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-09] - [3-보조] 카테고리 집계 단계에서 예상 못 한 오류 발생 - 이번 주는 집계 없이 진행: "
              f"{type(e).__name__} - {e!r}")
        category_distribution, category_comparison = {}, None

    print("\n=== [4] 국내/해외 LLM 요약 생성 ===")
    try:
        domestic_summarized, international_summarized = step4_llm_summary(
            domestic_ranked, international_ranked)
        llm_summarizer.print_summaries("국내", domestic_summarized)
        llm_summarizer.print_summaries("해외", international_summarized)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-10] - [4] LLM 요약 단계에서 예상 못 한 오류 발생 - 요약 없이(원문 제목만) 진행: "
              f"{type(e).__name__} - {e!r}")
        domestic_summarized, international_summarized = domestic_ranked, international_ranked

    print("\n=== [4-보조] 카테고리별 LLM 요약 생성 ===")
    try:
        domestic_category_summarized, international_category_summarized = step4_category_llm_summary(
            domestic_category_ranked, international_category_ranked)
        for category, items in domestic_category_summarized.items():
            llm_summarizer.print_summaries(f"국내-{category}", items)
        for category, items in international_category_summarized.items():
            llm_summarizer.print_summaries(f"해외-{category}", items)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-11] - [4-보조] 카테고리별 LLM 요약 단계에서 예상 못 한 오류 발생 - 요약 없이(원문 제목만) 진행: "
              f"{type(e).__name__} - {e!r}")
        domestic_category_summarized, international_category_summarized = (
            domestic_category_ranked, international_category_ranked)

    print("\n=== [5] 저장 ===")
    # 수동 실행(workflow_dispatch)은 저장 생략 - 지난주 대비 증감 비교 기준 오염 방지
    is_manual_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if is_manual_run:
        print("[main] 수동 실행(workflow_dispatch)이라 저장을 건너뜁니다 - "
              "'지난주 대비 증감' 비교 기준 오염 방지(콘솔에 출력된 이번 실행 결과는 "
              "그대로 확인 가능, data/에는 안 남음)")
        saved_dir = None
    else:
        try:
            saved_dir = storage.save_week(articles, domestic_summarized, international_summarized,
                                           domestic_category_summarized, international_category_summarized,
                                           gdelt_timeline, failed_sources, category_distribution,
                                           category_comparison)
        except Exception as e:
            print(f"[main] 🔴 조치필요 [MN-12] - 저장 단계에서 예상 못 한 오류 발생(콘솔 로그의 결과는 그대로 유효함): "
                  f"{type(e).__name__} - {e!r}")
            saved_dir = None
    print("\n=== [6] 배포 ===")
    try:
        week_label = os.path.basename(saved_dir) if saved_dir else datetime.now(timezone.utc).strftime("%G-%V")
        deploy.send_weekly_email(week_label, domestic_summarized, international_summarized,
                                  domestic_category_summarized, international_category_summarized,
                                  failed_sources, category_comparison)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-13] - 배포 단계에서 예상 못 한 오류 발생(콘솔 로그의 결과는 그대로 유효함): "
              f"{type(e).__name__} - {e!r}")

    if failed_sources:
        saved_dir_note = f"{saved_dir}/scored.json에도" if saved_dir else "(data/ 파일에는 안 남았지만)"
        print(f"\n[main] 🔴 조치필요 [MN-14] - 이번 실행 실패 소스: {failed_sources} "
              f"({saved_dir_note} failed_sources로 같이 저장됨)")


if __name__ == "__main__":
    run()