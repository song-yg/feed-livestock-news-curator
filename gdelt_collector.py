"""
gdelt_collector.py
GDELT DOC 2.0 API를 파이썬 클라이언트(gdeltdoc)로 호출해서
키워드별 해외 언급 데이터를 수집하는 모듈.
(알고리즘 문서 "1. 수집 레이어" - gdelt_collector 스펙 참조)

naver_collector / watt_collector와 반환 형태가 다르다는 점이 핵심 차이:
그 둘은 list[dict] 하나만 반환하지만, 이 모듈은 tuple(articles, timeline)을
반환한다. 이유:
  - articles: 기사 1건 = 레코드 1건 -> 공통 스키마 그대로, 정규화/이슈그룹핑으로 감
  - timeline: 키워드 단위 시계열(timelinevol/timelinevolraw) -> 기사 단위가 아니라서
    공통 스키마에 억지로 끼워넣지 않음. 3.1 규칙대로 스코어링에는 안 들어가고
    결과물에 참고 지표로만 별도 표시됨 (저장 레이어가 알아서 분리 저장)
(2026-07-13 바람과 논의 후 확정 - "방식 A")

*** 아직 검증 전 초안입니다 — "확인 필요" 표시된 부분(특히 seendate 파싱)은
    실제 실행 결과를 보고 나서 다음 단계에서 고쳐야 함 (watt_collector와 동일한 방식) ***
"""

import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from gdeltdoc import GdeltDoc, Filters
from gdeltdoc.errors import RateLimitError

# 예시 키워드. 최종 리스트는 아직 확정 전이라 임시로 넣어둠 (naver_collector와 동일 방침).
# GDELT DOC API는 영문 검색이 기본이므로 영문 키워드로 구성 (스펙 "필요한 것" 항목 참조).
KEYWORDS_EN = [
    "avian influenza",
    "HPAI",
    "foot and mouth disease",
    "feed price",
    "livestock market",
]

# 이 프로젝트는 주 1회 실행이므로, 최근 7일 이내 기사만 남긴다 (naver/watt와 동일 방침).
DAYS_BACK = 7

# GDELT DOC API의 TIMESPAN 파라미터 포맷: 숫자+단위(min/h/d/w/m) 확인됨
# (GDELT 공식 블로그 "GDELT DOC 2.0 API Debuts!" 기준 - 검색으로 검증함)
TIMESPAN = f"{DAYS_BACK}d"

# article_search는 GDELT DOC API 자체 한계로 한 번 호출에 최대 250건까지만 반환됨
# (naver처럼 start 파라미터로 추가 페이지네이션하는 기능 자체가 없음 - API 레벨 한계)
MAX_RECORDS = 250

# 키워드 사이 요청 간격.
# GDELT는 공식적으로 "몇 초에 몇 건"인지 수치를 공개하지 않음. 다만 실제 사용기
# (2026-04, HackerNoon 사례)에 "IP당 5초에 1건 제한"이라는 보고가 있어(공식 확정
# 수치 아님, 참고치) 거기에 여유를 더해 8초로 설정.
REQUEST_INTERVAL = 8.0


# RateLimitError(HTTP 429) 전용 재시도 횟수/대기시간.
# 2026-07-13 확인됨: GDELT는 실제로 서버 사이드 요청 제한이 있음(공식 블로그에
# ElasticSearch 클러스터 보호 목적이라고 명시). 정확한 윈도우 수치는 비공개지만,
# 실사용 보고(HackerNoon, 2026-04, 공식 확정 아님·참고치)에 따르면 짧은 시간에
# 요청이 몰리면 15분가량 지속되는 차단이 걸릴 수 있다고 함 - 그래서 15초 같은
# 짧은 백오프 대신 60초 단위로 크게 잡음. 그래도 첫 시도부터 계속 429가 뜬다면
# 백오프 문제가 아니라 이미 지속 차단에 걸려있는 상태일 수 있음 - 그럴 땐 재시도
# 루프를 도는 것보다 최소 15~20분 정도 아예 요청을 멈추는 게 맞음.
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 60  # 60 -> 120 -> 240초로 증가


# 일시적 네트워크 에러(ConnectTimeout 등) 재시도 횟수/대기시간.
# 2026-07-13 확인됨: 429(rate limit)와 ConnectTimeout이 같은 실행 안에서 섞여
# 나오는 걸 확인함 - 이건 요청 제한과는 별개로, 순간적인 접속 실패일 가능성이
# 높아(정황상 추정, GDELT 서버가 그 시점에 불안정했을 수 있음) 429보다는 훨씬
# 짧게 재시도. 이것도 다 실패하면 429 때와 마찬가지로 그 키워드는 포기.
NETWORK_ERROR_MAX_RETRIES = 2
NETWORK_ERROR_WAIT_SECONDS = 10


def _call_with_retry(func, *args, label: str = "", **kwargs):
    """
    RateLimitError(429)와 일시적 네트워크 에러(ConnectTimeout 등)를 서로 다른
    정책으로 재시도한다 (429는 길게, 네트워크 에러는 짧게). 그 외 예외는 바로
    올려보내서 (9.1 방침대로) collect()의 try/except가 그 키워드만 건너뛰게 한다.

    두 카운터(rate_limit_attempt, network_attempt)를 독립적으로 관리 - 하나의
    반복문에서 같이 세면 "429 한 번 + 네트워크에러 한 번"을 겪었을 때 429 쪽
    백오프 계산이 실제 429 횟수와 안 맞아지는 문제가 있어서 분리함.

    label: 로그에 찍을 식별자 (예: "avian influenza / article_search").
    2026-07-13 확인됨 - 키워드 하나당 API를 3번(article_search, timelinevol,
    timelinevolraw) 따로 호출하는데, 재시도 카운터가 호출마다 독립적으로 리셋되다
    보니 로그만 보면 "(1/3)"이 반복되는 것처럼 헷갈릴 수 있어서 label을 붙임.
    """
    rate_limit_attempt = 0
    network_attempt = 0

    while True:
        try:
            return func(*args, **kwargs)
        except RateLimitError:
            if rate_limit_attempt >= MAX_RETRIES:
                raise
            wait = BACKOFF_BASE_SECONDS * (2 ** rate_limit_attempt)
            rate_limit_attempt += 1
            print(f"[gdelt] {label} - 429 rate limit - {wait}초 대기 후 재시도 "
                  f"({rate_limit_attempt}/{MAX_RETRIES})")
            time.sleep(wait)
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError):
            if network_attempt >= NETWORK_ERROR_MAX_RETRIES:
                raise
            network_attempt += 1
            print(f"[gdelt] {label} - 접속 실패(ConnectTimeout 등) - "
                  f"{NETWORK_ERROR_WAIT_SECONDS}초 대기 후 재시도 "
                  f"({network_attempt}/{NETWORK_ERROR_MAX_RETRIES})")
            time.sleep(NETWORK_ERROR_WAIT_SECONDS)


def _parse_seendate(raw: str) -> datetime:
    """
    gdeltdoc article_search가 반환하는 seendate 필드를 datetime으로 변환한다.

    TODO 확인 필요: 실제 seendate 값의 정확한 포맷을 직접 실행해서 확인 안 한 상태.
    GDELT 원시 데이터에서 흔히 쓰이는 "YYYYMMDDHHMMSS" 형태(예: "20260713090000")
    또는 뒤에 "Z"가 붙는 형태를 우선 시도하고, 안 되면 ISO 8601도 시도하도록 짜둠.
    (watt_collector의 _parse_published_time과 같은 방어적 접근)
    """
    raw = raw.strip()

    # 1) "20260713090000" 또는 "20260713T090000Z" 형태 시도
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%dT%H%M%SZ"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    # 2) ISO 8601 형태 시도 (예: "2026-07-13T09:00:00Z")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass

    raise ValueError(f"seendate 파싱 실패, 형식 확인 필요: {raw!r}")


def _is_recent(dt: datetime, days: int) -> bool:
    cutoff = datetime.now(dt.tzinfo) - timedelta(days=days)
    return dt >= cutoff


def _extract_domain(url: str) -> str:
    """
    기사 원문 URL에서 도메인만 뽑아 press로 사용한다 (naver_collector의
    _extract_press와 동일한 목적 - 3.2 언론사 dedup에 사용).
    """
    if not url:
        return ""
    domain = urlparse(url).netloc
    return domain.replace("www.", "")


def collect() -> tuple[list[dict], dict]:
    """
    KEYWORDS_EN을 순서대로 돌면서 GDELT에서 기사 메타데이터와 언급 시계열을
    함께 수집한다. 이 함수가 gdelt_collector의 '진입점'.

    반환값:
      articles: 공통 스키마 리스트. 다음 단계(정규화/이슈그룹핑)로 그대로 전달됨
      timeline: {키워드: {"vol": [...], "volraw": [...]}} 형태.
                스코어링에는 안 들어가고 결과물에 참고 지표로만 별도 표시 (3.1 규칙)
    """
    gd = GdeltDoc()

    all_articles = []
    timeline_by_keyword = {}

    for keyword in KEYWORDS_EN:
        try:
            f = Filters(
                keyword=keyword,
                timespan=TIMESPAN,
                num_records=MAX_RECORDS,
            )

            # 1) 기사 메타데이터 수집
            articles_df = _call_with_retry(gd.article_search, f, label=f"{keyword} / article_search")
            keyword_articles = []
            if articles_df is not None and not articles_df.empty:
                for _, row in articles_df.iterrows():
                    try:
                        published_at = _parse_seendate(str(row["seendate"]))
                    except ValueError as e:
                        # 날짜 파싱 실패한 기사 1건만 건너뜀 (전체 중단 아님, 9.1 방침)
                        print(f"[gdelt] '{keyword}' 기사 스킵 - {e}")
                        continue

                    if not _is_recent(published_at, DAYS_BACK):
                        continue

                    keyword_articles.append({
                        "source": "GDELT",
                        "title": row["title"],
                        "url": row["url"],
                        "published_at": published_at.isoformat(),
                        "category": None,   # 정제 단계에서 채움 (naver_collector와 동일 방침)
                        "body": None,        # GDELT는 본문 미제공 (스펙에 명시된 그대로)
                        "press": _extract_domain(row["url"]),  # naver의 press 필드와 동일 목적
                    })

            all_articles.extend(keyword_articles)

            time.sleep(REQUEST_INTERVAL)

            # 2) 언급 시계열 수집 (timelinevol, timelinevolraw 둘 다 - 스펙 62번 줄 기준)
            vol_df = _call_with_retry(gd.timeline_search, "timelinevol", f,
                                       label=f"{keyword} / timelinevol")
            time.sleep(REQUEST_INTERVAL)
            volraw_df = _call_with_retry(gd.timeline_search, "timelinevolraw", f,
                                          label=f"{keyword} / timelinevolraw")

            timeline_by_keyword[keyword] = {
                # 2026-07-13 확인됨(실행 결과로 검증) - 실제 컬럼 구조:
                #   vol:    {"datetime": Timestamp, "Volume Intensity": float}
                #   volraw: {"datetime": Timestamp, "Article Count": int, "All Articles": int}
                # Timestamp 객체는 그대로 JSON 직렬화가 안 되므로, 저장 레이어(5번 섹션)
                # 쪽 raw.json에 넣기 전 문자열로 변환하는 처리가 필요함 (다음 단계 작업)
                "vol": vol_df.to_dict("records") if vol_df is not None else [],
                "volraw": volraw_df.to_dict("records") if volraw_df is not None else [],
            }

            print(f"[gdelt] '{keyword}' -> 최근 {DAYS_BACK}일 이내 기사 {len(keyword_articles)}건 "
                  f"+ 시계열 수집 완료")

        except Exception as e:
            # 키워드 하나 실패했다고 전체를 멈추지 않는다 (naver/watt와 동일한 9.1 방침)
            # 2026-07-13: 에러 메시지가 빈 문자열로 나오는 경우가 있어 예외 타입도 같이 로그
            print(f"[gdelt] '{keyword}' 수집 실패: {type(e).__name__} - {e!r}")
            continue

        time.sleep(REQUEST_INTERVAL)

    return all_articles, timeline_by_keyword


if __name__ == "__main__":
    # 터미널에서 python gdelt_collector.py 로 직접 실행했을 때만 동작.
    articles, timeline = collect()
    print(f"\n총 {len(articles)}건 기사 수집 완료")
    for a in articles[:3]:
        print(a)
    print(f"\n시계열 수집된 키워드: {list(timeline.keys())}")
    if timeline:
        first_keyword = next(iter(timeline))
        print(f"\n'{first_keyword}' 시계열 샘플 (vol 앞 2건): {timeline[first_keyword]['vol'][:2]}")
        print(f"'{first_keyword}' 시계열 샘플 (volraw 앞 2건): {timeline[first_keyword]['volraw'][:2]}")
