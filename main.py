"""
main.py
사료·축산업 뉴스 큐레이션 시스템 오케스트레이션 레이어.

6단계: 수집 -> 정규화(dedup+태깅) -> 이슈그룹핑(임베딩) -> 관련성필터+카테고리재분류
(그룹 대표 1건씩만 판단) -> 스코어링(국내/해외 Top N + 카테고리별 Top N, 4차
사후재검토 포함) -> LLM 요약 -> 저장(storage.py) -> 배포(deploy.py, 이메일).
"""

import os
import time
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

# 국내/해외 Top N, 카테고리별 Top N. 환경변수로 조정 가능(기본 3/1).
TOP_N = int(os.environ.get("TOP_N") or 3)
CATEGORY_TOP_N = int(os.environ.get("CATEGORY_TOP_N") or 1)

# --- 파이프라인 시간 예산 체크포인트 (파이프라인 시작 기준 절대 분) ---
# GitHub 러너 job 하드캡(360분) 안에서 저장/배포/git커밋에 5분을 남기고,
# 각 단계가 이 시각까지는 끝나도록 강제한다. 각 단계 시작 직전에 main.py가
# "목표 시각 - 지금까지 경과 시간"을 계산해 그 단계에 남은 예산으로 넘겨준다
# (그 단계 자기 시작 시점부터 새로 재는 게 아니라, 앞 단계가 늦어지면
# 뒷 단계가 자동으로 짧아지는 방식).
GDELT_DEADLINE_MINUTES = 220        # 3:40 - GDELT 수집
GROUPING_DEADLINE_MINUTES = 280     # 4:40 - 임베딩 로드 + 이슈 그룹핑(1~3차, 필터링 전 원본 전체 대상)
RELEVANCE_DEADLINE_MINUTES = 305    # 5:05 - 관련성 필터 + 카테고리 재분류 (그룹 대표 1건씩만 판단)
STAGE4_DEADLINE_MINUTES = 310       # 5:10 - 4차 Top N 사후 재검토
SUMMARY_DEADLINE_MINUTES = 355      # 5:55 - LLM 요약


def _deadline(pipeline_start: float, minutes: int) -> float:
    """파이프라인 시작 시각(time.monotonic() 기준) + 목표 분을 절대 마감 시각으로 변환."""
    return pipeline_start + minutes * 60


# ---------------------------------------------------------------------------
# 1) 수집 레이어
# ---------------------------------------------------------------------------

def run_collectors(pipeline_start: float) -> tuple[list[dict], dict, list[str]]:
    """watt/naver/gdelt collector 순차 실행. 소스별 독립 실행(하나 실패해도 나머지 계속).
    GDELT에는 pipeline_start 기준 GDELT_DEADLINE_MINUTES 절대 마감을 넘김
    (WATT/네이버가 먼저 쓴 시간이 자동으로 반영됨).

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
        gdelt_deadline = _deadline(pipeline_start, GDELT_DEADLINE_MINUTES)
        gdelt_articles, gdelt_timeline = gdelt_collector.collect(deadline=gdelt_deadline)
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

def score(groups: list[list[dict]], top_n: int = TOP_N,
          stage4_deadline: float | None = None) -> tuple[list[dict], list[dict], dict, dict]:
    """
    이미 그룹핑된 groups(issue_grouper.group_issues 결과)를 국내/해외로 나눠
    Top N + 카테고리별 Top N을 계산한다.

    ** 그룹핑은 이 함수 밖(run())에서 미리 끝나 있어야 함 (2026-07-31) **
    예전엔 이 함수 안에서 issue_grouper.group_issues()를 직접 호출했는데,
    관련성 필터를 "그룹 대표 1건만 판단"하는 방식으로 바꾸면서 그룹핑 자체가
    관련성 필터보다 먼저 실행돼야 하는 순서가 됐다(run() 참고) - 그래서
    그룹핑은 run()이 score() 호출 전에 끝내고, 여기서는 이미 필터링까지
    끝난 groups만 받아 국내/해외 분리·랭킹만 담당한다.

    국내/해외 양쪽에 걸친 그룹은 각 축에 그 축 기사만 넘기고 반대 축 대표
    기사 URL을 _cross_axis_partner_url로 상호 표시 - 실제 표시용 제목은
    아직 안 정함(Top N 확정 전이라 파트너가 실제로 Top N에 들지 모름).
    main.py의 _resolve_cross_axis_partners()가 4차/요약까지 다 끝난 뒤
    실제로 이메일에 남은 항목인지 확인해서 제목을 채운다.

    GDELT 소스 중 한국어 기사는 scorer._is_korean_gdelt_article로 국내 재분류.

    stage4_deadline: 국내/해외 Top N + 카테고리별 Top N 4차 재검토에 전파
    (파이프라인 기준 절대 마감, time.monotonic() - main.py가 계산해서 넘김).

    국내/해외 Top N + 카테고리별 Top N 전부 issue_grouper.stage4_dedupe_and_promote로
    사후 재검토(병합+승격)를 거침. 카테고리별은 scorer.score_by_category의
    dedupe_fn 콜백으로 연결.
    """
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
            domestic_part[0]["_cross_axis_partner_url"] = international_part[0].get("url", "")
            international_part[0]["_cross_axis_partner_url"] = domestic_part[0].get("url", "")
        if domestic_part:
            domestic_groups.append(domestic_part)
        if international_part:
            international_groups.append(international_part)

    domestic_ranked_pool = scorer.score_and_rank(domestic_groups, top_n=None)
    international_ranked_pool = scorer.score_and_rank(international_groups, top_n=None)

    domestic_ranked = issue_grouper.stage4_dedupe_and_promote(
        domestic_ranked_pool, top_n=top_n, label="국내", deadline=stage4_deadline)
    international_ranked = issue_grouper.stage4_dedupe_and_promote(
        international_ranked_pool, top_n=top_n, label="해외", deadline=stage4_deadline)

    def _category_dedupe_fn(axis_label: str):
        def _fn(ranked_pool, n, category):
            return issue_grouper.stage4_dedupe_and_promote(
                ranked_pool, top_n=n, label=f"{axis_label}-{category}", deadline=stage4_deadline)
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
                                deadline: float | None = None,
                                ) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """카테고리별 Top N에 (A)/(A-1) 요약 적용. 평평한 리스트로 합쳐 처리 후 재구성.
    deadline은 국내/해외 LLM 요약(step4_llm_summary)과 같은 파이프라인 기준
    절대 마감을 그대로 씀 - 둘 다 [4]/[4-보조] 단계에서 같은 예산을 나눠 쓰는 셈."""
    domestic_flat = [item for items in domestic_category_ranked.values() for item in items]
    international_flat = [item for items in international_category_ranked.values() for item in items]

    domestic_summarized_flat = llm_summarizer.summarize_top_issues(
        domestic_flat, label="국내-카테고리", deadline=deadline)
    international_summarized_flat = llm_summarizer.summarize_top_issues(
        international_flat, label="해외-카테고리", deadline=deadline)

    return (_regroup_by_category(domestic_summarized_flat),
            _regroup_by_category(international_summarized_flat))


def step4_llm_summary(domestic_ranked: list[dict],
                       international_ranked: list[dict],
                       deadline: float | None = None) -> tuple[list[dict], list[dict]]:
    """국내/해외 Top N에 (A)/(A-1) 요약 적용. 반환값은 summary 필드 추가된 동일 형태.
    deadline: 파이프라인 기준 절대 마감(SUMMARY_DEADLINE_MINUTES)."""
    domestic_summarized = llm_summarizer.summarize_top_issues(domestic_ranked, label="국내", deadline=deadline)
    international_summarized = llm_summarizer.summarize_top_issues(
        international_ranked, label="해외", deadline=deadline)
    return domestic_summarized, international_summarized


# ---------------------------------------------------------------------------
# 오케스트레이션 진입점
# ---------------------------------------------------------------------------

def _resolve_cross_axis_partners(domestic_summarized: list[dict], international_summarized: list[dict],
                                  domestic_category_summarized: dict[str, list[dict]],
                                  international_category_summarized: dict[str, list[dict]]) -> None:
    """
    score()의 group()에서 미리 붙여둔 cross_axis_partner_url을, [4]/[4-보조]
    요약까지 전부 끝나 최종 확정된 시점에 실제로 반대 축 결과물(Top N +
    카테고리별 Top N)에 남아있는지 재검증한다. 있으면 그 항목의 최종 표시용
    제목(generated_title 우선, 없으면 titles[0])으로 cross_axis_partner를
    채우고, 없으면(Top N/카테고리 Top N 어디에도 안 남았으면) None으로 둬서
    이메일/summary.md에서 자동으로 안 보이게 한다.

    in-place로 각 항목의 "cross_axis_partner" 필드를 채운다(반환값 없음) -
    네 개 리스트/딕셔너리 전부 main.py 안에서만 도는 참조라 안전.
    """
    def _rep_title(item: dict) -> str | None:
        return item.get("generated_title") or (item["titles"][0] if item.get("titles") else None)

    def _build_url_to_title(main_list: list[dict], category_dict: dict[str, list[dict]]) -> dict[str, str]:
        url_to_title: dict[str, str] = {}
        all_items = list(main_list) + [item for items in category_dict.values() for item in items]
        for item in all_items:
            rep_title = _rep_title(item)
            if not rep_title:
                continue
            for url in item.get("urls", []):
                url_to_title[url] = rep_title
        return url_to_title

    domestic_url_to_title = _build_url_to_title(domestic_summarized, domestic_category_summarized)
    international_url_to_title = _build_url_to_title(international_summarized, international_category_summarized)

    def _resolve(items: list[dict], opposite_url_to_title: dict[str, str]) -> None:
        for item in items:
            partner_url = item.get("cross_axis_partner_url")
            item["cross_axis_partner"] = opposite_url_to_title.get(partner_url) if partner_url else None

    _resolve(domestic_summarized, international_url_to_title)
    _resolve(international_summarized, domestic_url_to_title)
    for items in domestic_category_summarized.values():
        _resolve(items, international_url_to_title)
    for items in international_category_summarized.values():
        _resolve(items, domestic_url_to_title)


def run() -> None:
    pipeline_start = time.monotonic()

    print("=== [1] 수집 시작 ===")
    articles, gdelt_timeline, failed_sources = run_collectors(pipeline_start)

    print("\n=== [2] 정규화 ===")
    try:
        articles = normalize(articles)
        keyword_tagger.tag_articles(articles)
        keyword_tagger.print_category_distribution(articles)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-05] - [2] 정규화/태깅 단계에서 예상 못 한 오류 발생 - 원본 기사 그대로 다음 단계로 진행: "
              f"{type(e).__name__} - {e!r}")

    print("\n=== [2.1] 이슈 그룹핑 임베딩 모델 로드 ===")
    embedding_model = _load_embedding_model()

    grouping_deadline = _deadline(pipeline_start, GROUPING_DEADLINE_MINUTES)

    print("\n=== [2.2] 이슈 그룹핑 ===")
    # 관련성 필터보다 먼저 실행(2026-07-31) - 필터를 "그룹 대표 1건만 판단"
    # 방식으로 바꾸면서 그룹핑이 먼저 끝나 있어야 함. 필터링 전 원본 전체가
    # 대상이라 예전보다 입력 규모가 커짐 - stage2_group의 임계값 분류를
    # numpy로 벡터화해둔 덕에 수천 건이 들어와도 안전하게 처리됨.
    try:
        groups = issue_grouper.group_issues(articles, model=embedding_model, deadline=grouping_deadline)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-16] - [2.2] 이슈 그룹핑 단계에서 예상 못 한 오류 발생 - "
              f"그룹핑 없이(기사 1건 = 그룹 1개) 다음 단계로 진행: {type(e).__name__} - {e!r}")
        groups = scorer.to_singleton_groups(articles)

    relevance_deadline = _deadline(pipeline_start, RELEVANCE_DEADLINE_MINUTES)

    print("\n=== [2.5] 관련성 필터 (그룹 대표 1건씩 판단) ===")
    try:
        groups = relevance_filter.filter_groups(groups, deadline=relevance_deadline)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-06] - [2.5] 관련성 필터 단계에서 예상 못 한 오류 발생 - 필터링 없이 다음 단계로 진행: "
              f"{type(e).__name__} - {e!r}")

    print("\n=== [2.6] 카테고리 재분류 (그룹 대표 1건씩 판단) ===")
    try:
        groups = relevance_filter.recategorize_uncategorized_groups(groups, deadline=relevance_deadline)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-07] - [2.6] 카테고리 재분류 단계에서 예상 못 한 오류 발생 - 재분류 없이 다음 단계로 진행: "
              f"{type(e).__name__} - {e!r}")

    # 카테고리 집계([3-보조])/raw.json 저장([5])은 개별 기사 단위 리스트가
    # 필요해서, 필터링/재분류까지 끝난 groups를 다시 평평한 리스트로 펼침.
    articles = [a for g in groups for a in g]

    stage4_deadline = _deadline(pipeline_start, STAGE4_DEADLINE_MINUTES)

    print("\n=== [3] 스코어링 ===")
    try:
        (domestic_ranked, international_ranked,
         domestic_category_ranked, international_category_ranked) = score(
            groups, top_n=TOP_N, stage4_deadline=stage4_deadline)
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

    summary_deadline = _deadline(pipeline_start, SUMMARY_DEADLINE_MINUTES)

    print("\n=== [4] 국내/해외 LLM 요약 생성 ===")
    try:
        domestic_summarized, international_summarized = step4_llm_summary(
            domestic_ranked, international_ranked, deadline=summary_deadline)
        llm_summarizer.print_summaries("국내", domestic_summarized)
        llm_summarizer.print_summaries("해외", international_summarized)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-10] - [4] LLM 요약 단계에서 예상 못 한 오류 발생 - 요약 없이(원문 제목만) 진행: "
              f"{type(e).__name__} - {e!r}")
        domestic_summarized, international_summarized = domestic_ranked, international_ranked

    print("\n=== [4-보조] 카테고리별 LLM 요약 생성 ===")
    try:
        domestic_category_summarized, international_category_summarized = step4_category_llm_summary(
            domestic_category_ranked, international_category_ranked, deadline=summary_deadline)
        for category, items in domestic_category_summarized.items():
            llm_summarizer.print_summaries(f"국내-{category}", items)
        for category, items in international_category_summarized.items():
            llm_summarizer.print_summaries(f"해외-{category}", items)
    except Exception as e:
        print(f"[main] 🔴 조치필요 [MN-11] - [4-보조] 카테고리별 LLM 요약 단계에서 예상 못 한 오류 발생 - 요약 없이(원문 제목만) 진행: "
              f"{type(e).__name__} - {e!r}")
        domestic_category_summarized, international_category_summarized = (
            domestic_category_ranked, international_category_ranked)

    # cross_axis_partner 최종 확정 (2026-07-31) - Top N/카테고리별 Top N +
    # 요약까지 전부 끝난 시점이라야 "반대 축에 실제로 남아있는지"를 정확히
    # 알 수 있음. [4]/[4-보조]의 콘솔 출력(print_summaries)은 이 호출보다
    # 앞서 실행되므로 이번 실행에서는 🔗 표시 없이 찍힘(진단용 로그라 감수) -
    # 실제 산출물인 이메일/summary.md는 이 아래 [5]/[6]에서 만들어지므로 문제 없음.
    try:
        _resolve_cross_axis_partners(domestic_summarized, international_summarized,
                                      domestic_category_summarized, international_category_summarized)
    except Exception as e:
        print(f"[main] 🟡 주의 [MN-15] - cross_axis_partner 최종 확정 단계에서 예상 못 한 오류 발생 - "
              f"이번 실행은 🔗 표시 없이 진행(다른 내용엔 영향 없음): {type(e).__name__} - {e!r}")

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