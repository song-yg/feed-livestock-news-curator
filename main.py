"""
main.py
사료·축산업 뉴스 큐레이션 시스템의 오케스트레이션 레이어.
(알고리즘 문서 "6. 실행/오케스트레이션 (Runner)" 참조)

문서의 6단계 파이프라인 중 이번 작업에서 실제로 구현한 범위:
  1) 수집        -> 구현 완료 (watt/naver/gdelt collector, 기존 모듈 그대로 호출)
  2) 정규화      -> 구현 완료: 공통 스키마 통합 + 완전 동일 기사(URL) 제거
                    + 2.2 키워드 태깅(keyword_tagger.py)
                    + 2.1 이슈 그룹핑(issue_grouper.py, 2026-07-15 연결 완료 -
                    아래 score() 함수 참고. 1차 사전 매칭 + 2차 BGE-M3 임베딩 +
                    3차 LLM 보조(2026-07-15, issue_grouper.stage3_llm_assist)
                    까지 전부 연결됨. 3차는 기본적으로 ANTHROPIC_API_KEY 환경변수가
                    필요하나, Anthropic 키 발급 승인 전까지는 LLM_PROVIDER=openrouter
                    +OPENROUTER_API_KEY로 임시 대체 가능(issue_grouper.py 프로바이더
                    스위치 참조). GitHub Actions에서 돌리려면 리포 Secrets/Variables에
                    추가 등록 필요(8번 섹션 체크리스트 참조). 둘 다 없으면 3차만
                    안전하게 생략되고 나머지 파이프라인은 계속 동작함. **GitHub
                    Actions 실행 검증(2026-07-15)**: 최초 실행 시 "No space left on
                    device"로 실패 → 원인은 sentence-transformers가 끌어오는 GPU용
                    PyTorch + BGE-M3 + Playwright 용량이 러너 보장 여유공간(14GB)을
                    초과한 것으로 확인 → workflow에 프리인스톨 툴 정리(dotnet/
                    android/ghc) + PyTorch CPU 전용 선설치를 추가해 해결, 이후
                    main.py 실행 진입까지 확인됨(.github/workflows/run-pipeline.yml
                    참조)
  3) 스코어링    -> 구현 완료 (scorer.py) - 2.1이 실제로 연결되면서 이제
                    "이슈(여러 기사 묶음) 단위 점수"로 정상 작동함 (기존엔
                    to_singleton_groups 임시 처리로 사실상 기사 단위 점수와
                    동일했음)
                    + 카테고리 전체 집계(category_aggregator.py, 2026-07-14
                    신규) 보조 지표 추가 - 2.1 이슈 그룹핑이 "동일 사건만"
                    묶는 좁은 정의라 생기는 공백을 메우는 별개 지표 (순위와
                    무관, 국내/해외 각각 카테고리별 단순 건수만 집계)
  4) LLM 요약    -> 구현 완료 (2026-07-17, llm_summarizer.py): (A) 자체 요약
                    + (A-1) 얇은 재료 fallback. (B) 그룹핑 보조는 2.1에서
                    이미 구현 완료(issue_grouper.stage3_llm_assist). 프로바이더
                    설정(LLM_PROVIDER, 모델명, X-Title 등)은 issue_grouper.py
                    에서 그대로 재사용 - 3차 개발 중 겪은 버그(빈 문자열 env
                    var, 헤더 인코딩)가 이 설정값 자체의 문제였어서, 새로
                    베껴 쓰지 않고 이미 검증된 값을 공유해 재발을 막음.
                    API 키가 없거나 LLM 호출이 실패해도 그 이슈는 "요약 생략,
                    원문 제목만 노출"로 안전하게 fallback(9.4/9.5 원칙 재사용)
  5) 저장        -> 구현 완료 (2026-07-23, storage.py) - data/YYYY-WW/에
                    raw.json(정규화+필터링된 최종 기사 데이터)/scored.json
                    (스코어링+요약 결과, articles 필드는 raw.json과 중복이라
                    제외)/summary.md(사람이 읽을 배포용 요약본) 저장. WATT
                    body는 저작권상 raw.json에서도 제외(storage.py docstring
                    참고). git 커밋/푸시는 워크플로(run-pipline.yml) 책임 -
                    이 단계는 파일 생성까지만.
  6) 배포        -> 미구현 (TODO)

main.py를 6단계 전부 완성형으로 만들지 않고, 위 범위까지만 실제로 동작하는
파이프라인으로 잇는다. 6은 다음 세션에서 채울 자리를 함수 스텁으로만
남겨둔다 (아래 _step6_deploy_todo).
"""

import gdelt_collector
import naver_collector
import scorer
import issue_grouper
import llm_summarizer
from WATT_collector import collect as watt_collect  # noqa: N813 (파일명 규칙과 다르지만 기존 파일 그대로 사용)
import keyword_tagger
import category_aggregator
import relevance_filter
import storage


# ---------------------------------------------------------------------------
# 1) 수집 레이어 (섹션 1)
# ---------------------------------------------------------------------------

def run_collectors() -> tuple[list[dict], dict, list[str]]:
    """
    watt/naver/gdelt collector를 순서대로 실행한다.

    9.1 "소스별 독립 실행 구조" - 각 collector 호출을 개별 try/except로 감싸서 하나가 완전히 죽어도(예: import 실패, 예상 밖 예외) 나머지 소스는 계속 진행한다.
    각 collector 내부에도 이미 더 세밀한 단위(WATT는 사이트별, naver/gdelt는 키워드별)의 방어가 있지만, 여기 main.py 레벨의 try/except는 "collector 모듈 자체가 통째로 실패하는 경우"에 대한 마지막 방어선이다.

    반환값:
      all_articles: watt+naver+gdelt 기사를 하나로 합친 리스트 (아직 정규화 전 원본 스키마 - 이미 세 collector가 공통 스키마를 지키므로 합치기만 하면 됨)
      gdelt_timeline: GDELT 시계열 데이터 (3.1 규칙대로 스코어링에는 안 들어가고 참고 지표 전용- 지금은 그냥 들고만 있음, 저장 레이어(5번, 아직 미구현) 완성 시 그쪽에서 사용)
      failed_sources: 실패한 소스 이름 목록 (9.2 "에러 리포트 자동화"의 재료 - 저장/배포 레이어가 아직 없어서 지금은 콘솔에만 출력)
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
    "완전 동일 기사 제거": 같은 URL이 중복 수집된 경우만 제거.
    (2번 섹션 명시 - 이슈 그룹핑과는 다른 개념. 여긴 정말 똑같은 기사가 두 번 들어온 경우만 거른다 - 예: 페이지네이션 겹침, 재실행 등).

    첫 번째로 본 URL을 유지하고 이후 중복은 버린다 (순서 유지를 위해 dict를 순서 보존 집합처럼 사용).
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

def score(articles: list[dict], model, top_n: int = 5) -> tuple[list[dict], list[dict]]:
    """
    2.1 이슈 그룹핑(issue_grouper.group_issues) + 3.1/3.2 국내/해외 개별 랭킹(Top N)까지 수행.
    (2026-07-15, to_singleton_groups 임시 처리를 실제 그룹핑으로 교체 - 배경 문서 "진행할 것 — 최우선" 참조)

    ** 그룹핑을 먼저, 축 분리는 그 다음 (설계 결정) **
    알고리즘 문서 2.1 "작동 방식" 3번은 매칭 범위를 "전체 기사 벡터 x 전체 기사 벡터"(국내-국내 / 해외-해외 / 국내-해외 전부 포함)로 명시하고 있다.
    그런데 기존 임시 코드는 split_domestic_international()을 먼저 호출해 국내/해외를 나눈 뒤 각각 따로 to_singleton_groups()를 적용하고 있었다.
    이 순서 그대로 group_issues만 바꿔치기하면 국내/해외가 애초에 분리된 채로 그룹핑되어 버려서 국내-해외 교차 매칭이 구조적으로 아예 발생할 수 없게 된다 (문서 정의와 어긋남).
    그래서 이번 연결 작업에서 순서를 뒤집었다:
    ① 전체 기사를 대상으로 group_issues()를 한 번 호출 -> ② 그 결과 그룹들을 국내/해외 축으로 "나눠서" scorer에 넘긴다.

    ** 국내-해외 교차 매칭된 그룹의 처리 **
    group_issues가 만든 그룹 하나가 국내(네이버)·해외(WATT/GDELT) 기사를 동시에 포함할 수 있다.
    3.2 원칙("양쪽 리스트 모두에서 노출... 하나의 점수로 합치지는 않음, 정규화·환산 없이 각 축의 원본 신호를 그대로 보존")대로,
    이런 그룹은 국내 축 스코어링엔 그 그룹 안의 네이버 기사만, 해외 축 스코어링엔 그 그룹 안의 WATT/GDELT 기사만 걸러서 넘긴다 - 각 축의 issue_score가 그 축 안에서의 원본 신호만 반영하게 하기 위함이다.
    다만 "이 이슈가 다른 축에서도 다뤄졌다"를 화면에 🔗로 표시해주는 기능 자체는 scorer.py 상단 docstring에 이미 명시된 대로 여전히 미구현.
    이번 연결 작업 범위 밖(다음 세션에서 배포 포맷 확정 시 추가할 것).

    model: sentence_transformers.SentenceTransformer 인스턴스, 또는 None.
           None이면 issue_grouper.group_issues가 2차(임베딩) 없이 1차 결과만으로 안전하게 fallback한다 (아래 _load_embedding_model 참고).
    """
    groups = issue_grouper.group_issues(articles, model=model)

    domestic_groups = []
    international_groups = []
    for group in groups:
        domestic_part = [a for a in group if a.get("source") == "네이버"]
        international_part = [a for a in group if a.get("source") != "네이버"]
        if domestic_part:
            domestic_groups.append(domestic_part)
        if international_part:
            international_groups.append(international_part)

    domestic_ranked = scorer.score_and_rank(domestic_groups, top_n=top_n)
    international_ranked = scorer.score_and_rank(international_groups, top_n=top_n)

    return domestic_ranked, international_ranked


# ---------------------------------------------------------------------------
# 2.1 이슈 그룹핑용 임베딩 모델 로드 (2026-07-15 신규)
# ---------------------------------------------------------------------------

def _load_embedding_model():
    """
    BGE-M3 임베딩 모델을 실행당 한 번만 로드해서 score() 단계에 주입한다.

    한 번만 로드하는 이유: issue_grouper.stage2_group의 docstring에 이미 명시돼 있음 - "모델 로드 자체가 무거운 작업이라, 기사 배치마다 매번 새로 로드하면 안 되기 때문...
    호출하는 쪽(main.py)에서 한 번만 로드해서 넘겨주는 구조로 설계".
    main.py에서는 국내/해외 두 축을 스코어링하지만 그룹핑 자체는 score() 안에서 한 번만(전체 기사 대상) 일어나므로, 이 함수도 run() 전체를 통틀어 딱 한 번만 호출하면 된다.

    모델 로드 실패(최초 실행 시 다운로드 실패, 패키지 미설치, 캐시 문제 등) 시에도 전체 파이프라인이 죽지 않도록 여기서 예외를 잡아 None을 반환한다.
    issue_grouper.group_issues(articles, model=None)이 이미 "2차(임베딩) 생략, 1차 사전 매칭 결과만 사용"으로 안전하게 fallback하도록 설계돼 있으므로(issue_grouper.py group_issues 참고),
    이 함수의 실패가 9.1 "소스별 독립 실행 구조"와 같은 철학으로 전체 중단 없이 흡수된다.
    다만 이 경우 2.1의 2차(임베딩) 매칭 없이 1차 사전 매칭(현재 빈 리스트라 사실상 매칭 없음)만 적용되므로 사실상 이번 실행은 to_singleton_groups와 같은 결과가 된다는 점은 감안해야 한다.
    (완전한 자동 복구는 아님 - 다음 실행에서 모델 로드가 다시 성공하길 기대하는 정도의 완화책).
    """
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("BAAI/bge-m3")
        print("[main] BGE-M3 임베딩 모델 로드 완료")
        return model
    except Exception as e:
        print(f"[main] BGE-M3 모델 로드 실패 - 2차(임베딩) 매칭 없이 진행 "
              f"(1차 사전 매칭만 적용됨): {type(e).__name__} - {e!r}")
        return None


# ---------------------------------------------------------------------------
# 4)~6) 아직 미구현 - 자리만 표시
# ---------------------------------------------------------------------------

def step4_llm_summary(domestic_ranked: list[dict],
                       international_ranked: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    섹션 4 (A) 자체 요약 + (A-1) 얇은 재료 fallback 구현 완료 (2026-07-17).
    실제 로직은 llm_summarizer.py에 있고, 이 함수는 국내/해외 축을 각각
    넘겨주는 얇은 호출부다. (B) 그룹핑 보조는 2.1에서 이미 처리됨
    (issue_grouper.stage3_llm_assist) - 여기서는 (A)/(A-1)만 다룬다.

    domestic_ranked/international_ranked는 score()에서 이미 top_n=5로 제한된
    상태로 들어온다 (7번 섹션 "초기엔 주간 Top 5로 제한 운영" 방침 그대로 -
    여기서 추가로 자르지 않음).

    반환값은 입력과 같은 형태(list[dict])에 "summary"/"summary_skipped_reason"
    필드가 추가된 것 - 저장 레이어(5단계, 아직 미구현)에 그대로 넘길 수 있다.
    """
    domestic_summarized = llm_summarizer.summarize_top_issues(domestic_ranked, label="국내")
    international_summarized = llm_summarizer.summarize_top_issues(international_ranked, label="해외")
    return domestic_summarized, international_summarized


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
    keyword_tagger.print_uncategorized_sample(articles, sample_size=30)

    print("\n=== [2.5] 관련성 필터 (LLM - 사료·축산업 뉴스가 아닌 기사 제외) ===")
    # 2026-07-22 신설. 키워드 매칭만으로는 못 거르는 오매칭(동음이의어, 기관명
    # 일부로만 등장, 각주성 언급 등)을 LLM이 제목/요약 맥락으로 판단해 걸러낸다
    # - 이후 단계(임베딩 계산, 3차 LLM 그룹핑 보조)의 대상도 함께 줄어드는
    # 효과가 있음. 자세한 설계 배경은 relevance_filter.py 모듈 docstring 참고.
    articles = relevance_filter.filter_articles(articles)

    print("\n=== [2.1] 이슈 그룹핑 임베딩 모델 로드 (BGE-M3, 실행당 1회) ===")
    embedding_model = _load_embedding_model()

    print("\n=== [3] 스코어링 (2.1 이슈 그룹핑 + 국내/해외 개별 Top N) ===")
    domestic_ranked, international_ranked = score(articles, embedding_model, top_n=5)
    scorer.print_top_n("국내", domestic_ranked, n=5)
    scorer.print_top_n("해외", international_ranked, n=5)

    print("\n=== [3-보조] 카테고리 전체 집계 (국내/해외, 2026-07-14 신규) ===")
    # 2.1 이슈 그룹핑이 "동일 사건만" 묶는 좁은 정의라 큰 트렌드가 개별
    # 이슈로 흩어져 보이는 공백을 메우는 거친(coarse) 보조 지표 - 순위(Top N)와는
    # 별개로, 카테고리 자체가 이번 주 몇 건 다뤄졌는지만 보여준다.
    # (category_aggregator.py 모듈 docstring 참고)
    category_distribution = category_aggregator.aggregate(articles)
    category_aggregator.print_aggregate(category_distribution)

    # 4~6단계
    print("\n=== [4] LLM 요약 생성 (상위 이슈, 국내/해외 각각) ===")
    domestic_summarized, international_summarized = step4_llm_summary(
        domestic_ranked, international_ranked)
    llm_summarizer.print_summaries("국내", domestic_summarized)
    llm_summarizer.print_summaries("해외", international_summarized)

    print("\n=== [5] 저장 (data/YYYY-WW/raw.json, scored.json, summary.md) ===")
    # 2026-07-23 추가: storage.py 내부는 이미 파일 단위로 안전하게 실패를
    # 흡수하도록 만들었지만(storage.py docstring 참고), 예상 못 한 예외까지
    # 완벽히 막을 순 없으므로 9.1 "소스별 독립 실행 구조"와 같은 방향으로
    # 마지막 방어선을 하나 더 둔다 - 저장이 통째로 실패해도 이미 콘솔에
    # 다 출력된 이번 실행 결과(수집/스코어링/요약)는 그대로 남는다.
    try:
        saved_dir = storage.save_week(articles, domestic_summarized, international_summarized,
                                       gdelt_timeline, failed_sources, category_distribution)
    except Exception as e:
        print(f"[main] 저장 단계에서 예상 못 한 오류 발생(콘솔 로그의 결과는 그대로 유효함): "
              f"{type(e).__name__} - {e!r}")
        saved_dir = None
    _step6_deploy_todo()

    if failed_sources:
        saved_dir_note = f"{saved_dir}/scored.json에도" if saved_dir else "(저장 실패로 파일에는 못 남았지만)"
        print(f"\n[main] 이번 실행 실패 소스: {failed_sources} (9.2 에러 리포트 - "
              f"{saved_dir_note} failed_sources로 같이 저장됨, "
              f"배포 레이어 완성 전까지는 콘솔 로그로도 확인 가능)")


if __name__ == "__main__":
    run()