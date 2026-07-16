"""
gdelt_collector.py
GDELT DOC 2.0 API를 파이썬 클라이언트(gdeltdoc)로 호출해서
키워드별 해외 언급 데이터를 수집하는 모듈.
(알고리즘 문서 "1. 수집 레이어" - gdelt_collector 스펙 참조)

naver_collector / watt_collector와 반환 형태가 다르다는 점이 핵심 차이:
그 둘은 list[dict] 하나만 반환하지만, 이 모듈은 tuple(articles, timeline)을 반환한다.
이유:
  - articles: 기사 1건 = 레코드 1건 -> 공통 스키마 그대로, 정규화/이슈그룹핑으로 감
  - timeline: 키워드 단위 시계열(timelinevol/timelinevolraw) -> 기사 단위가 아니라서 공통 스키마에 억지로 끼워넣지 않음.\
    3.1 규칙대로 스코어링에는 안 들어가고 결과물에 참고 지표로만 별도 표시됨 (저장 레이어가 알아서 분리 저장)
(2026-07-13 논의 후 확정 - "방식 A")

*** 아직 검증 전 초안입니다 — "확인 필요" 표시된 부분(특히 seendate 파싱)은 실제 실행 결과를 보고 나서 다음 단계에서 고쳐야 함 (watt_collector와 동일한 방식) ***

--- 2026-07-14 실행 테스트 메모 ---
5개 키워드 중 4개(avian influenza / foot and mouth disease / feed price / livestock market) 정상 수집 확인.
"HPAI"만 GDELT API 자체 에러로 실패함 (ValueError: "The specified phrase is too short.") - 코드 버그 아니라 GDELT DOC API가 너무 짧은 검색어(약어 등)를 거부하는 것으로 추정.
KEYWORDS_EN은 어차피 최종 확정 전 단계라 지금은 그대로 두고 메모만 남김 - 추후 키워드 리스트 확정 작업 때 "HPAI" 같은 짧은 약어는 더 긴 표현으로 바꾸거나 빼는 것을 함께 검토할 것.

또한 실행 중 거의 매 호출마다 429(rate limit)가 발생해 재시도 백오프가 누적되며 총 실행 시간이 약 1시간 가까이 걸림
- 아래 두 가지로 대응:
  1. REQUEST_INTERVAL 8초 -> 15초 상향
  2. _call_with_retry의 429 처리를 "호출별 개별 대기"에서 "전역 공유 쿨다운"
     방식으로 변경 (자세한 이유는 _call_with_retry, _wait_for_cooldown 참고)

--- 2026-07-14(4차) 추가 메모 (GitHub Actions 실행 중 사용자 관찰) ---
GDELT 접속량이 예상보다 훨씬 많아 보임(사용자 관찰) - MAX_RETRIES(4단계, 최대 900초)를 다 소진하고도 실패하는 키워드가 나오면 그 키워드만 편중되게 빠지는 결과물이 나올 위험이 있어, 키워드 단위 "외부 재시도"를 추가함:
  1. article_search가 최종 실패한 키워드만 모아서, 한 라운드가 끝난 뒤 별도
     라운드로 최대 OUTER_RETRY_PASSES(기본 2)번 더 재시도 (총 최대 3회 시도)
  2. ~~429 발생 시각을 UTC로 기록해 실행 끝에 요약 출력~~ → 2026-07-15 제거함.
     실행 트리거가 사람이 수동으로 누르는 버튼(GitHub Actions workflow_dispatch)
     뿐이라, 애초에 "근무 시간대"로만 표본이 몰릴 수밖에 없어 "429가 특정
     시간대에 몰리는지" 패턴을 알아내겠다는 이 기능의 전제 자체가 성립하기
     어렵다는 판단으로 폐기 (아래 "429 시각 기록" 관련 코드 전체 삭제 -
     실행 도중 그 순간 콘솔에 시각을 찍어주는 즉시성 로그 한 줄만 유지,
     여러 실행에 걸쳐 모아서 요약하던 부분만 제거).

--- 2026-07-16 추가: 429 대응 2건 (문서 "7. 아직 결정 안 된 것들" 항목 처리) ---
실측(main.py 실행 로그)에서 GDELT 429가 여전히 심함(키워드마다 백오프
4단계를 다 소진하는 경우가 반복, 외부 재시도로 겨우 복구)이 확인돼 아래
두 가지로 대응함 - 서로 배타적이지 않아 같이 적용:
  1. User-Agent 헤더 주입: gdeltdoc 라이브러리 자체 이슈 트래커(#22)에서
     "User-Agent 없이 요청하면 rate limit, 추가하면 해결"이라는 보고를
     확인 - requests 기본 헤더를 오버라이드하는 방식으로 적용(아래
     "User-Agent 미기재 가설 대응" 주석 참고). 효과는 아직 실측 전.
  2. 키워드를 하나의 OR 쿼리로 결합: gdeltdoc의 Filters(keyword=[...])가
     리스트를 자동으로 OR로 묶어준다는 걸 확인(공식 README) - 기존엔
     키워드 4개를 각각 호출해 요청이 4번 나갔는데, 하나로 합쳐서 요청
     횟수 자체를 줄임(429에 걸릴 기회 감소, 원인이 뭐든 도움이 됨).
     시계열(timelinevol/timelinevolraw)은 3.1 원칙상 "키워드별 트렌드"가
     의미가 있어야 하므로 이건 합치지 않고 기존처럼 키워드별로 유지함
     (_collect_articles_for_keywords 및 collect() 참고).
"""

import re as _re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
import requests.sessions
import requests.utils as _requests_utils
from gdeltdoc import GdeltDoc, Filters
from gdeltdoc.errors import RateLimitError

# --- 2026-07-16 추가: User-Agent 미기재 가설 대응 (7번 섹션 미결정 항목) ---
#
# 배경: gdeltdoc 라이브러리 자체 GitHub 이슈(#22, alex9smith/gdelt-doc-api)에서
# "요청 수가 적었는데도 rate limit 에러를 겪었고, 브라우저 요청은 정상 동작
# 했다 - api_client.py에 User-Agent 헤더를 추가했더니 해결됐다"는 보고를
# 실제로 확인함(우리가 쓰는 바로 그 라이브러리의 이슈 트래커). 이 패치가
# 라이브러리에 병합됐다는 근거는 못 찾았고, README에도 헤더/세션을
# 커스터마이징하는 공식 옵션이 없어 - 지금도 gdeltdoc이 User-Agent 없이
# 요청을 보내고 있을 가능성이 높다고 판단.
#
# gdeltdoc이 헤더 주입 방법을 공식 제공하지 않으므로, requests 라이브러리의
# 기본 헤더(default_headers()) 자체를 오버라이드하는 방식으로 우회한다 -
# Session()이 생성될 때마다 이 함수가 호출되므로, gdeltdoc이 내부적으로
# 만드는 Session에도 자연히 적용된다.
#
# ** 주의 - 이건 프로세스 전역에 영향을 준다 **: 이 모듈을 import하는 순간
# requests 기본 헤더가 바뀌어서, 같은 프로세스에서 실행되는 naver_collector의
# requests.get 호출에도 이 User-Agent가 섞여 들어간다(naver_collector는
# X-Naver-Client-Id 등 자기 인증 헤더만 명시적으로 쓰고 User-Agent는 따로
# 지정 안 하므로 - 인증은 헤더 값 기반이라 UA가 섞여도 인증 자체엔 영향 없음,
# 다만 "전역 부작용"이라는 점은 유지보수 시 반드시 인지할 것). WATT_collector
# 가 이미 쓰고 있는 것과 동일한 UA 문자열을 재사용해 일관성을 맞춤.
#
# ** 효과 미확인 - 검증 필요 **: 위 이슈 보고는 신뢰도 높은 근거지만, 이
# 프로젝트의 GDELT 429가 정확히 같은 원인인지는 아직 실측 안 됨. 다음
# 실행에서 429 발생 빈도가 눈에 띄게 줄어드는지 로그로 직접 확인할 것.
_GDELT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 2026-07-16 버그 수정: requests.sessions 모듈이 `from .utils import
# default_headers`로 자기 네임스페이스에 직접 바인딩해서 쓰기 때문에,
# requests.utils.default_headers만 바꾸면 Session.__init__() 내부에서 부르는
# 건 여전히 원래 함수다 (실측 확인 - Session().headers["User-Agent"]가
# patch 후에도 그대로 "python-requests/x.y.z"였음). requests.sessions 쪽
# 바인딩도 같이 덮어써야 실제 Session 생성에 반영된다.
#
# 가드(_gdelt_ua_patched)를 두는 이유: 이 모듈이 어떤 경로로든 두 번 로드되면
# (예: importlib.reload) "원본 함수 캡처"까지 다시 실행돼서 이미 패치된
# 함수를 "원본"으로 잘못 캡처해버리고, 그걸 감싸는 새 wrapper가 자기 자신을
# 무한 호출하는 RecursionError가 실제로 재현됨(실측 확인) - 원본 캡처부터
# 최종 할당까지 전부 가드 안에 넣어서, 이미 패치돼 있으면 이 블록을 통째로
# 건너뛴다.
if not getattr(requests.utils, "_gdelt_ua_patched", False):
    _original_default_headers = _requests_utils.default_headers

    def _default_headers_with_gdelt_ua():
        headers = _original_default_headers()
        headers["User-Agent"] = _GDELT_USER_AGENT
        return headers

    requests.utils.default_headers = _default_headers_with_gdelt_ua
    requests.sessions.default_headers = _default_headers_with_gdelt_ua
    requests.utils._gdelt_ua_patched = True



# 예시 키워드. 최종 리스트는 아직 확정 전이라 임시로 넣어둠 (naver_collector와 동일 방침).
# GDELT DOC API는 영문 검색이 기본이므로 영문 키워드로 구성 (스펙 "필요한 것" 항목 참조).
#
# 2026-07-14 확인 - "HPAI"는 GDELT API가 "너무 짧은 검색어"로 거부함 (ValueError: "The specified phrase is too short.").
# 키워드 리스트를 본격적으로 확정할 때 더 긴 표현으로 교체하거나 제외할 것 - 지금은 리스트 자체가 임시라 그대로 둠.
KEYWORDS_EN = [
    "avian influenza",
    "HPAI",
    "foot and mouth disease",
    "feed price",
    "livestock market",
]

# --- 2026-07-14(2차) 추가: 이미 실패가 확인된 키워드 사전 스킵 ---
#
# "HPAI"는 2026-07-14 실행에서 GDELT API가 매번 ValueError("The specified phrase is too short.")로 거부하는 게 확인됨.
# 문제는 이 에러가 429 재시도 루프를 다 태우고 나서야(최대 4단계 백오프, 아래 MAX_RETRIES 참고) 도달하는 진짜 에러라서, 매 실행마다 어차피 실패할 키워드에 수 분~십수 분을 낭비하고 있었음 (실측: 약 7분/키워드).
#
# "몇 글자부터 너무 짧다고 판단하는지"는 GDELT가 공식적으로 공개한 기준이 아니라서(추정으로 길이 임계값을 정하면 다른 정상 키워드까지 잘못 걸러낼 위험이 있음), 길이 기반 자동 필터 대신 "실제로 실패가 확인된 키워드"만 명시적으로 등록하는 스킵 리스트로 처리한다.
# 새 키워드를 추가했는데 계속 같은 ValueError로 실패하는 게 확인되면 여기 추가할 것.

SKIP_KEYWORDS = {
    "HPAI": "GDELT API가 'phrase too short'로 거부함 (2026-07-14 확인, 재현됨)",
}

# --- 2026-07-14 추가: 키워드 오매칭(false positive) 필터 ---
#
# GDELT article_search는 키워드를 "부분 문자열 포함" 방식으로 매칭하기 때문에, 의도한 키워드가 더 긴 무관한 구(phrase)의 일부로 들어있는 제목도 그대로 매칭돼버리는 구조적 문제가 있다.
# 실제로 확인된 사례:
#   "foot and mouth disease"(구제역) 검색 -> "hand, foot and mouth disease"
#   (수족구병 - 어린이 질환, 전혀 다른 병)가 그대로 포함 매칭됨
#   (test_gdelt_collector.py 진단 실행, 2026-07-14)
#
# 길이 기반이나 정규식 기반의 일반화된 해법 대신, "실제로 오매칭이 확인된 키워드"에 한해 제외 패턴을 명시적으로 등록하는 방식을 쓴다
# (SKIP_KEYWORDS와 동일한 철학 - 추정으로 일반 규칙을 만들면 다른 정상 매칭까지 잘못 걸러낼 # 위험이 있음).
# 새 오매칭 패턴이 확인되면 여기 추가할 것.
#
# 형태: {검색 키워드: [제목에 이 문자열(대소문자 무시)이 포함되면 제외, ...]}
FALSE_POSITIVE_FILTERS = {
    "foot and mouth disease": ["hand, foot and mouth", "hand foot and mouth"],
}

# --- 2026-07-15 버그 수정: 구두점 앞 공백 정규화 ---
#
# FALSE_POSITIVE_FILTERS를 처음 등록했을 때(2026-07-14)는 "hand, foot and mouth"(쉼표 뒤에만 공백)와 "hand foot and mouth"(쉼표 없음) 두 형태만 가정했다.
# 그런데 실제 GDELT article_search가 돌려주는 제목은 쉼표/마침표 "앞"에도 공백이 들어간 토크나이즈된 형식이었다.
# (실측 확인, 2026-07-15 재검증 - calibration_raw_2026-W29.json): "Westmoreland sees increase in hand , foot and mouth disease"
# 즉 실제 제목은 "hand SPACE , SPACE foot" 형태라, 등록해둔 두 패턴 중 어느 것과도 안 맞아서 필터가 만들어진 이후 단 한 번도 실제로 걸러낸 적이 없었다.
# (같은 재검증에서 "hand , foot and mouth disease" 오매칭 3건이 필터를 그대로 통과해 원본 데이터에 남아있는 게 확인됨)
#
# 패턴을 계속 늘리는 대신(제목 형식이 또 달라지면 또 놓칠 위험), 비교 전에 "구두점 앞 공백"을 없애는 정규화를 한 번 거치는 쪽을 택함.
# 이러면 "hand, foot and mouth"/"hand , foot and mouth"/"hand  , foot and mouth" 등 공백 개수가 달라져도 전부 같은 문자열로 취급돼 안전하다.
_SPACE_BEFORE_PUNCT = _re.compile(r"\s+([,.;:!?])")


def _normalize_spacing(text: str) -> str:
    """구두점 앞의 공백을 제거해 비교용으로 정규화한다 (예: "hand , foot" -> "hand, foot")."""
    return _SPACE_BEFORE_PUNCT.sub(r"\1", text)


def _is_false_positive(keyword: str, title: str) -> bool:
    """
    해당 키워드의 등록된 제외 패턴이 제목에 포함돼 있으면 True.
    (예: "foot and mouth disease" 검색 결과 중 제목에 "hand, foot and mouth"가
    있으면 수족구병 오매칭으로 판단해 제외)

    비교 전 제목과 패턴 양쪽 다 _normalize_spacing을 거쳐, GDELT 제목의
    "구두점 앞 공백" 형식(위 2026-07-15 버그 수정 주석 참고) 때문에 매칭이
    실패하는 일이 없도록 한다.
    """
    patterns = FALSE_POSITIVE_FILTERS.get(keyword, [])
    if not patterns or not title:
        return False
    title_normalized = _normalize_spacing(title.lower())
    return any(_normalize_spacing(p.lower()) in title_normalized for p in patterns)

# 이 프로젝트는 주 1회 실행이므로, 최근 7일 이내 기사만 남긴다 (naver/watt와 동일 방침).
DAYS_BACK = 7

# GDELT DOC API의 TIMESPAN 파라미터 포맷: 숫자+단위(min/h/d/w/m) 확인됨
# (GDELT 공식 블로그 "GDELT DOC 2.0 API Debuts!" 기준 - 검색으로 검증함)
TIMESPAN = f"{DAYS_BACK}d"

# article_search는 GDELT DOC API 자체 한계로 한 번 호출에 최대 250건까지만 반환됨
# (naver처럼 start 파라미터로 추가 페이지네이션하는 기능 자체가 없음 - API 레벨 한계)
MAX_RECORDS = 250

# 키워드 사이 요청 간격.
# GDELT는 공식적으로 "몇 초에 몇 건"인지 수치를 공개하지 않음. 기존엔 8초로 설정했었으나 (2026-07-14 확인) 실제 실행에서 거의 매 호출마다 429가 발생해 재시도 백오프가 누적되는 문제가 있어 15초로 상향.
# 그래도 429가 잦으면 추가 상향 검토 필요 (정확한 공식 수치는 여전히 비공개라 경험적으로 조정하는 값).
REQUEST_INTERVAL = 15.0


# RateLimitError(HTTP 429) 전용 재시도 횟수/대기시간.
# 2026-07-13 확인됨: GDELT는 실제로 서버 사이드 요청 제한이 있음(공식 블로그에 ElasticSearch 클러스터 보호 목적이라고 명시).
# 정확한 윈도우 수치는 비공개지만, 실사용 보고(HackerNoon, 2026-04, 공식 확정 아님·참고치)에 따르면 짧은 시간에 요청이 몰리면 15분가량 지속되는 차단이 걸릴 수 있다고 함.
#
# 2026-07-14(2차) 변경: 기존 3단계(60→120→240초, 누적 7분)로는 실측 429 로그를 보면 여전히 거의 매 키워드마다 재시도가 소진되는 걸 확인함.
# 위에서 언급한 "15분가량 지속 차단" 참고치보다 누적 대기시간이 짧아서, 실제 차단이 안 풀린 시점에 재시도를 반복하고 있었을 가능성이 있음.
# 4단계(60→120→240→480초, 누적 900초=15분)로 늘려서 참고치와 누적 대기시간을 맞춤.
# 그래도 계속 429가 뜬다면 이건 백오프 튜닝으로 해결할 문제가 아니라 실행 자체를 미루는 게 맞다는 신호.
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 60  # 60 -> 120 -> 240 -> 480초로 증가 (누적 900초)


# 일시적 네트워크 에러(ConnectTimeout 등) 재시도 횟수/대기시간.
# 2026-07-13 확인됨: 429(rate limit)와 ConnectTimeout이 같은 실행 안에서 섞여 나오는 걸 확인함.
# 이건 요청 제한과는 별개로, 순간적인 접속 실패일 가능성이 높아(정황상 추정, GDELT 서버가 그 시점에 불안정했을 수 있음) 429보다는 훨씬 짧게 재시도.
# 이것도 다 실패하면 429 때와 마찬가지로 그 키워드는 포기.
NETWORK_ERROR_MAX_RETRIES = 2
NETWORK_ERROR_WAIT_SECONDS = 10


# 429 발생 시각을 모아뒀다가 실행 끝에 "시간대별 패턴 요약"으로 출력하던
# 기능(_rate_limit_log, _print_rate_limit_summary)은 2026-07-15 제거함.
# 실행 트리거가 사람이 수동으로 누르는 버튼뿐이라 표본이 근무 시간대로만
# 몰릴 수밖에 없어,애초에 "시간대 패턴을 알아낸다"는 목적을 달성할 수 없다는
# 판단. 429가 발생한 그 순간 콘솔에 UTC 시각을 찍는 즉시성 로그(아래
# _call_with_retry 안의 print)는 지금 실행 중인 로그를 읽을 때 유용해서 그대로 둠 -
# 여러 실행에 걸쳐 모아서 분석하려던 부분만 제거한 것.


# --- 2026-07-14(4차) 추가: 키워드 단위 외부 재시도 (outer retry) ---
#
# 사용자 관찰(2026-07-14, GitHub Actions 실행 중): GDELT 자체 접속량이 예상보다 많아 보임.
#  MAX_RETRIES=4(최대 900초 대기)를 다 소진하고도 article_search가 실패하는 키워드가 나올 수 있는데, 기존엔 이 경우 그 키워드가 그대로 기사 0건으로 끝나버렸음.
# 9.1 "소스 실패 대응" 원칙상 키워드 하나 실패가 전체 실행을 막진 않지만, 그 결과 "이번 주엔 유독 어떤 키워드만 몰린" 편중된 결과물이 나올 위험이 있음.
# 이건 실제 이슈 분포가 아니라 순전히 그 시점 레이트리밋 운에 좌우되는 편중이라 문제.
#
# 대응: 한 라운드(전체 키워드 순회)가 끝난 뒤, article_search가 최종 실패한 키워드만 모아서 별도 라운드로 재시도한다.
# 이미 성공한 키워드는 다시 건드리지 않음(중복 수집 방지 + 불필요한 API 호출 절약).
#
# 라운드 사이엔 REQUEST_INTERVAL과 별개로 OUTER_RETRY_WAIT_SECONDS만큼 추가 대기를 둔다.
# MAX_RETRIES 소진이 항상 900초 대기 후에 일어나는 건 아니라서(예: 네트워크 에러로 더 일찍 포기한 경우),
# 쿨다운이 실제로는 덜 지난 채 다음 라운드가 시작될 수 있어 안전 마진으로 추가함.
#
# 총 시도 횟수 = 1(최초) + OUTER_RETRY_PASSES(추가 라운드) = 기본 3회.
# 이 프로젝트는 주 1회 배치라 실행 시간이 늘어나는 것보다 "쏠린 결과물"을 피하는 쪽을 우선함 (사용자 판단, 2026-07-14).
OUTER_RETRY_PASSES = 2
OUTER_RETRY_WAIT_SECONDS = 90


# --- 2026-07-14 추가: 전역(프로세스 공유) 쿨다운 ---
#
# 기존엔 _call_with_retry가 429를 만나면 "그 호출 안에서만" time.sleep으로 대기했음.
# 문제는 이 프로젝트가 키워드 하나당 API를 3번(article_search, timelinevol, timelinevolraw) 따로 호출하는데,
# 재시도 카운터가 호출마다 완전히 독립이라 같은 차단 구간 안에서도 호출마다 처음부터(1/3) 다시 60초부터 백오프를 시작하는 낭비가 발생함 (2026-07-14 실행에서 총 실행 시간이 1시간 가까이 걸린 주된 원인으로 추정).
#
# 그래서 429를 한 번이라도 만나면 "지금부터 N초간은 전체 프로세스가 아예 요청을 안 보낸다"는 정보를 전역으로 공유하도록 변경.
# 이후의 모든 호출은 실행 전에 먼저 이 쿨다운이 끝났는지 확인하고, 안 끝났으면 그만큼 먼저 기다린 뒤에 실제 요청을 시도함.
_cooldown_until = 0.0
_cooldown_lock = threading.Lock()


def _wait_for_cooldown():
    """쿨다운 중이면 그 시점까지 대기. 아니면 즉시 반환."""
    with _cooldown_lock:
        remaining = _cooldown_until - time.time()
    if remaining > 0:
        print(f"[gdelt] 전역 쿨다운 중 - {remaining:.0f}초 남음, 대기")
        time.sleep(remaining)


def _trigger_cooldown(seconds: float):
    """
    429를 만났을 때 전역 쿨다운을 세팅(또는 연장)한다.
    이미 더 긴 쿨다운이 걸려있다면 그걸 덮어쓰지 않는다(연장만 함) -
    여러 호출이 거의 동시에 429를 만나도 쿨다운이 짧아지는 방향으로
    잘못 갱신되지 않도록.
    """
    global _cooldown_until
    with _cooldown_lock:
        new_until = time.time() + seconds
        if new_until > _cooldown_until:
            _cooldown_until = new_until


def _call_with_retry(func, *args, label: str = "", **kwargs):
    """
    RateLimitError(429)와 일시적 네트워크 에러(ConnectTimeout 등)를 서로 다른
    정책으로 재시도한다 (429는 길게, 네트워크 에러는 짧게). 그 외 예외는 바로
    올려보내서 (9.1 방침대로) collect()의 try/except가 그 키워드만 건너뛰게 한다.

    2026-07-14 변경: 429는 이 함수 호출 하나만 기다리고 마는 게 아니라,
    전역 쿨다운(_trigger_cooldown)을 걸어서 이후에 오는 모든 호출(다른
    키워드/다른 엔드포인트 포함)이 같은 차단 구간을 공유해서 기다리게 함.
    호출 시작 시 _wait_for_cooldown()으로 남아있는 쿨다운을 먼저 소진한다.

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
        _wait_for_cooldown()  # 다른 호출이 걸어둔 전역 쿨다운이 있으면 먼저 대기
        try:
            return func(*args, **kwargs)
        except RateLimitError:
            if rate_limit_attempt >= MAX_RETRIES:
                raise
            wait = BACKOFF_BASE_SECONDS * (2 ** rate_limit_attempt)
            rate_limit_attempt += 1
            now_str = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(f"[gdelt] {now_str} - {label} - 429 rate limit - {wait}초 전역 쿨다운 설정 "
                  f"({rate_limit_attempt}/{MAX_RETRIES})")
            _trigger_cooldown(wait)
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


def _collect_articles_for_keyword(gd: "GdeltDoc", keyword: str) -> tuple[bool, list[dict]]:
    """
    한 키워드에 대해 article_search만 수행하고 (성공 여부, 기사 리스트)를 반환한다.
    2026-07-14(4차) - 외부 재시도 라운드(OUTER_RETRY_PASSES)에서 실패한 키워드만
    다시 돌릴 수 있도록 기존 collect() 안에 있던 로직을 분리함.

    성공 여부는 article_search 호출 자체가 예외 없이 끝났는지 기준 (기사가
    0건이어도 호출 자체가 정상이면 True - 그 키워드는 그냥 최근 기사가 없는
    것이므로 재시도 대상이 아님).

    ** 2026-07-16 - collect()의 정식 실행 경로에서는 더 이상 안 씀 **:
    요청 횟수를 줄이려고 아래 _collect_articles_for_keywords(복수형, OR 결합)
    로 교체됨(모듈 docstring "429 대응 2건" 참고). 이 단수형 함수는 키워드
    하나만 따로 확인해보고 싶은 진단/디버깅 용도로 남겨둠 - 삭제하지 않음.
    """
    f = Filters(keyword=keyword, timespan=TIMESPAN, num_records=MAX_RECORDS)
    keyword_articles = []
    false_positive_count = 0

    try:
        articles_df = _call_with_retry(gd.article_search, f, label=f"{keyword} / article_search")

        if articles_df is not None and not articles_df.empty:
            for _, row in articles_df.iterrows():
                if _is_false_positive(keyword, str(row["title"])):
                    # 예: "foot and mouth disease" 검색인데 제목이
                    # "hand, foot and mouth disease"(수족구병)인 경우 -
                    # 구조적 오매칭이라 이 기사만 조용히 제외 (9.1 방침대로
                    # 기사 1건 스킵일 뿐 키워드 전체를 중단하지 않음)
                    false_positive_count += 1
                    continue

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
                    # 2026-07-14 추가: 언어/국가 분포 진단용.
                    # gdeltdoc이 원래 반환하는 필드인데 지금까지 받아만 놓고 안 쓰고 있었음.
                    # GDELT 공식 문서 확인 - JSON/article_search 모드에서는 title이 번역 없이 원문 그대로 옴 (TRANS 옵션은 HTML 모드 전용 위젯이라 여기 안 해당).
                    # 즉 이 title 값은 중국어/독일어 등 원문 스크립트 그대로일 수 있음. (12번 언어처리방침에서 다뤄야 할 지점.)
                    "language": row.get("language", ""),
                    "sourcecountry": row.get("sourcecountry", ""),
                })

        fp_note = f" (오매칭 필터로 {false_positive_count}건 제외)" if false_positive_count else ""
        print(f"[gdelt] '{keyword}' article_search -> 최근 {DAYS_BACK}일 이내 "
              f"{len(keyword_articles)}건 수집 완료{fp_note}")
        return True, keyword_articles

    except Exception as e:
        # 기사 수집 자체가 실패한 경우 - 호출부(collect())가 이 키워드를 failed_keywords에 담아 외부 재시도 라운드로 넘긴다 (2026-07-14(4차))
        print(f"[gdelt] '{keyword}' article_search 실패: {type(e).__name__} - {e!r}")
        return False, []


def _collect_articles_for_keywords(gd: "GdeltDoc", keywords: list[str]) -> tuple[bool, list[dict]]:
    """
    여러 키워드를 하나의 OR 쿼리로 묶어서 article_search를 "한 번만" 호출한다.
    (2026-07-16 추가 - 위 모듈 docstring "429 대응 2건" 참고)

    gdeltdoc의 Filters(keyword=[...])는 리스트를 넘기면 값들을 boolean OR로
    묶어준다(공식 README 확인) - 기존엔 키워드 개수만큼 요청이 나갔는데
    이걸 1번으로 줄여서 429에 걸릴 기회 자체를 줄인다.

    collect()의 외부 재시도 로직이 그대로 재사용되도록, 단수형
    _collect_articles_for_keyword와 동일한 반환 형태(성공 여부, 기사 리스트)
    를 유지한다.

    ** MAX_RECORDS 상한이 이제 "키워드 조합 전체"에 걸린다는 점 주의 **:
    기존엔 키워드 하나당 최대 250건씩(키워드 4개면 이론상 최대 1000건)
    받을 수 있었는데, OR로 합치면 이 조합 전체에서 최대 250건까지만
    돌아온다. 지금 실측 키워드별 건수(avian influenza ~53, foot and mouth
    disease ~49, feed price ~12, livestock market ~26 - 합쳐도 250 미만)로는
    문제가 없지만, 나중에 키워드가 늘거나 특정 키워드가 급증하면 조용히
    잘려나갈 수 있다는 걸 염두에 둘 것 (7번 섹션 "키워드 리스트 확정" 작업
    때 같이 재검토 권장).

    ** 오매칭 필터를 여러 키워드에 걸쳐 확인 **: OR로 묶으면 반환된 기사
    하나가 정확히 어느 키워드에 매칭돼서 나온 건지 API가 알려주지 않는다.
    그래서 keywords에 포함된 키워드 전부의 FALSE_POSITIVE_FILTERS 패턴을
    모아 확인한다 - 오매칭 자체가 "부분 문자열 포함 매칭" 때문에 생기는
    구조적 문제라, 어떤 키워드가 트리거했든 같은 필터를 적용하는 게
    자연스럽다(위 FALSE_POSITIVE_FILTERS 섹션 설명 참고).
    """
    label = " OR ".join(keywords)
    f = Filters(keyword=keywords, timespan=TIMESPAN, num_records=MAX_RECORDS)
    combined_articles = []
    false_positive_count = 0

    try:
        articles_df = _call_with_retry(gd.article_search, f, label=f"{label} / article_search")

        if articles_df is not None and not articles_df.empty:
            for _, row in articles_df.iterrows():
                title = str(row["title"])
                if any(_is_false_positive(kw, title) for kw in keywords):
                    false_positive_count += 1
                    continue

                try:
                    published_at = _parse_seendate(str(row["seendate"]))
                except ValueError as e:
                    print(f"[gdelt] '{label}' 기사 스킵 - {e}")
                    continue

                if not _is_recent(published_at, DAYS_BACK):
                    continue

                combined_articles.append({
                    "source": "GDELT",
                    "title": row["title"],
                    "url": row["url"],
                    "published_at": published_at.isoformat(),
                    "category": None,
                    "body": None,
                    "press": _extract_domain(row["url"]),
                    "language": row.get("language", ""),
                    "sourcecountry": row.get("sourcecountry", ""),
                })

        fp_note = f" (오매칭 필터로 {false_positive_count}건 제외)" if false_positive_count else ""
        print(f"[gdelt] '{label}' article_search -> 최근 {DAYS_BACK}일 이내 "
              f"{len(combined_articles)}건 수집 완료{fp_note}")
        return True, combined_articles

    except Exception as e:
        print(f"[gdelt] '{label}' article_search 실패: {type(e).__name__} - {e!r}")
        return False, []


def _collect_timeline_for_keyword(gd: "GdeltDoc", keyword: str) -> dict | None:
    """
    한 키워드에 대해 timelinevol/timelinevolraw를 수집한다. 실패 시 None.
    (2026-07-14(4차) - collect() 안에 있던 로직을 분리, 동작은 기존과 동일)
    """
    f = Filters(keyword=keyword, timespan=TIMESPAN, num_records=MAX_RECORDS)
    try:
        vol_df = _call_with_retry(gd.timeline_search, "timelinevol", f,
                                   label=f"{keyword} / timelinevol")
        time.sleep(REQUEST_INTERVAL)
        volraw_df = _call_with_retry(gd.timeline_search, "timelinevolraw", f,
                                      label=f"{keyword} / timelinevolraw")
        print(f"[gdelt] '{keyword}' 시계열 수집 완료")
        return {
            # 2026-07-13 확인됨(실행 결과로 검증) - 실제 컬럼 구조:
            #   vol:    {"datetime": Timestamp, "Volume Intensity": float}
            #   volraw: {"datetime": Timestamp, "Article Count": int, "All Articles": int}
            # Timestamp 객체는 그대로 JSON 직렬화가 안 되므로, 저장 레이어(5번 섹션) 쪽 raw.json에 넣기 전 문자열로 변환하는 처리가 필요함 (다음 단계 작업)
            "vol": vol_df.to_dict("records") if vol_df is not None else [],
            "volraw": volraw_df.to_dict("records") if volraw_df is not None else [],
        }
    except Exception as e:
        # 시계열만 실패한 경우 - 기사는 이미 확보돼 있으므로(성공했다면) 데이터 손실 없음. 
        # 과물에서는 참고 지표(3.1)가 빠지는 것뿐이라 외부 재시도 대상에는 넣지 않는다. (article_search만큼 치명적이지 않음, 2026-07-14(4차))
        print(f"[gdelt] '{keyword}' 시계열 수집 실패: {type(e).__name__} - {e!r}")
        return None


def collect(keywords: list[str] | None = None, skip_timeline: bool = True) -> tuple[list[dict], dict]:
    """
    KEYWORDS_EN을 대상으로 GDELT에서 기사 메타데이터와 언급 시계열을 함께
    수집한다. 이 함수가 gdelt_collector의 '진입점'.

    keywords: 지정하면 모듈 기본값(KEYWORDS_EN) 대신 이 리스트로 순회한다.
              (2026-07-14(3차) 추가 - 테스트 스크립트에서 키워드 수를 줄여
              빠르게 확인해보기 위한 용도. main.py의 정식 실행은 인자 없이
              collect()를 호출하므로 기존 동작과 완전히 동일함)
    skip_timeline: True면 timeline_search(timelinevol/timelinevolraw) 호출을
              건너뛰고 article_search만 수행한다. (2026-07-14(3차) 추가 -
              language/sourcecountry 분포 확인이 목적일 땐 시계열 데이터가
              필요 없고, API 호출이 훨씬 줄어 빠름. 정식 운영에서는 시계열이
              필요하므로 기본값 False 유지)

    ** 2026-07-16 재구성 - 기사 수집과 시계열 수집을 분리된 단계로 처리 **
    (모듈 docstring "429 대응 2건" 참고):
      1단계(기사 수집): 대상 키워드를 하나의 OR 쿼리로 묶어
         _collect_articles_for_keywords를 "한 번만" 호출 - 요청 횟수를
         최소화해 429에 걸릴 기회 자체를 줄인다. 실패하면(전역 쿨다운을
         다 소진해도 안 되면) 같은 조합을 통째로 최대 OUTER_RETRY_PASSES
         번 재시도한다(기존엔 "실패한 키워드만" 재시도했지만, 이제 기사
         수집이 키워드 단위가 아니라 조합 단위이므로 재시도도 조합 단위).
      2단계(시계열 수집): 3.1 원칙("해외에서 이 이슈가 지금 뜨는지/식는지"
         키워드별로 봐야 함)에 따라 이건 합치지 않고 기존처럼 키워드별로
         순회한다 - _collect_timeline_for_keyword 그대로 재사용, 이 부분은
         동작 변경 없음(외부 재시도 대상도 기존처럼 아님, article_search만큼
         치명적이지 않다는 기존 판단 유지).

    반환값:
      articles: 공통 스키마 리스트. 다음 단계(정규화/이슈그룹핑)로 그대로 전달됨
      timeline: {키워드: {"vol": [...], "volraw": [...]}} 형태. skip_timeline=True면
                항상 빈 딕셔너리.
                스코어링에는 안 들어가고 결과물에 참고 지표로만 별도 표시 (3.1 규칙)
    """
    gd = GdeltDoc()
    target_keywords = keywords if keywords is not None else KEYWORDS_EN

    active_keywords = []
    for keyword in target_keywords:
        if keyword in SKIP_KEYWORDS:
            print(f"[gdelt] '{keyword}' 스킵 - {SKIP_KEYWORDS[keyword]}")
            continue
        active_keywords.append(keyword)

    all_articles = []
    timeline_by_keyword = {}

    if not active_keywords:
        return all_articles, timeline_by_keyword

    # --- 1단계: 기사 수집 - 키워드를 하나의 OR 쿼리로 묶어 요청 횟수 최소화 ---
    remaining = list(active_keywords)
    article_search_ok = False

    for round_num in range(OUTER_RETRY_PASSES + 1):
        if round_num > 0:
            print(f"[gdelt] --- 기사 수집 외부 재시도 라운드 {round_num}/{OUTER_RETRY_PASSES} "
                  f"- 대상 키워드 {len(remaining)}개: {remaining} ---")
            print(f"[gdelt] 라운드 간 안전 대기 {OUTER_RETRY_WAIT_SECONDS}초")
            time.sleep(OUTER_RETRY_WAIT_SECONDS)

        success, articles = _collect_articles_for_keywords(gd, remaining)
        if success:
            all_articles.extend(articles)
            article_search_ok = True
            break
        # 실패하면 remaining을 그대로 유지한 채 다음 라운드에서 같은 조합을 재시도

    if not article_search_ok:
        print(f"[gdelt] 기사 수집 최종 실패 (총 {OUTER_RETRY_PASSES + 1}회 시도 후에도 실패, "
              f"이번 실행은 GDELT 기사 0건으로 처리됨): {remaining}")

    # --- 2단계: 시계열 수집 - 3.1 원칙대로 키워드별 개별 수집 유지 (합치지 않음) ---
    if skip_timeline:
        for keyword in active_keywords:
            print(f"[gdelt] '{keyword}' 시계열 수집 스킵 (skip_timeline=True, 테스트 모드)")
    else:
        time.sleep(REQUEST_INTERVAL)
        for keyword in active_keywords:
            timeline_entry = _collect_timeline_for_keyword(gd, keyword)
            if timeline_entry is not None:
                timeline_by_keyword[keyword] = timeline_entry
            time.sleep(REQUEST_INTERVAL)

    return all_articles, timeline_by_keyword


def _print_distribution(articles: list[dict]) -> None:
    """
    2026-07-14 추가 - 진단용 전용 함수.

    목적: "영어 키워드만으로 중국/유럽 기사가 실제로 얼마나 잡히는지"를 확인하기 위한 1회성 진단.
    이슈 그룹핑이나 스코어링에는 안 들어가고, 사람이 눈으로 보고 "키워드 사전을 다국어로 확장할 필요가 있는지" 판단하는 데만 씀.
    language/sourcecountry 값이 비어있는 항목이 있을 수 있음(gdeltdoc이 항상 채워주는지 확인 안 된 상태)
    - 그런 경우 "(미상)"으로 묶어서 표시.
    """
    from collections import Counter

    lang_counter = Counter(a["language"] or "(미상)" for a in articles)
    country_counter = Counter(a["sourcecountry"] or "(미상)" for a in articles)

    print(f"\n=== 언어 분포 (전체 {len(articles)}건) ===")
    for lang, count in lang_counter.most_common():
        pct = count / len(articles) * 100 if articles else 0
        print(f"  {lang:20s} {count:4d}건 ({pct:.1f}%)")

    print(f"\n=== 국가 분포 (전체 {len(articles)}건) ===")
    for country, count in country_counter.most_common(15):
        pct = count / len(articles) * 100 if articles else 0
        print(f"  {country:20s} {count:4d}건 ({pct:.1f}%)")

    # 중국/유럽 비중을 바로 눈에 띄게 별도 표시 (이번 진단의 핵심 질문)
    china_related = sum(
        1 for a in articles
        if a["sourcecountry"] in ("China", "Hong Kong", "Taiwan")
        or a["language"] in ("chi", "zh", "zh-cn", "zh-tw")
    )
    print(f"\n중국/홍콩/대만 관련: {china_related}건 "
          f"({china_related / len(articles) * 100 if articles else 0:.1f}%)")


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

    _print_distribution(articles)
