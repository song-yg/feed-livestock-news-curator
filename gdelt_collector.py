"""
gdelt_collector.py
GDELT DOC 2.0 API를 파이썬 클라이언트(gdeltdoc)로 호출해서
키워드별 해외 언급 데이터를 수집하는 모듈.
(알고리즘 문서 "1. 수집 레이어" - gdelt_collector 스펙 참조)

naver_collector / watt_collector와 반환 형태가 다르다는 점이 핵심 차이:
그 둘은 list[dict] 하나만 반환하지만, 이 모듈은 tuple(articles, timeline)을 반환한다.
이유:
  - articles: 기사 1건 = 레코드 1건 -> 공통 스키마 그대로, 정규화/이슈그룹핑으로 감
  - timeline: 키워드 단위 시계열(timelinevol/timelinevolraw) -> 기사 단위가 아니라서 공통 스키마에 억지로 끼워넣지 않음.
    3.1 규칙대로 스코어링에는 안 들어가고 결과물에 참고 지표로만 별도 표시됨 (저장 레이어가 알아서 분리 저장)

시계열 수집은 완전히 제거된 상태다: tuple(articles, timeline) 반환 형태
자체는 하위호환을 위해 그대로 유지하지만, timeline은 이제 항상 빈
딕셔너리(`{}`)다 - 실전 규모 테스트에서도 429로 오래 실패하는 등 불안정성이
해소가 안 됐고, 저장 레이어가 이 데이터를 애초에 안 쓰고 있었던 것도 제거
결정에 힘을 실었다. 상세 → `collect()` 함수 docstring 및
`_collect_timeline_for_keyword` 참고(코드는 남겨둠, 호출만 제거).

** GDELT 429(rate limit) 대응 정리 **
GDELT 429가 구조적으로 심해서(요청 대부분에서 발생, 원인이 GDELT 서버
쪽 트래픽 총량으로 추정돼 우리 코드로 완전히 해결은 불가) 여러 겹의
완화책을 적용했다:
  1. REQUEST_INTERVAL을 넉넉히 두고, 429 시 전역 공유 쿨다운으로 백오프
     (호출별 개별 대기가 아니라 - 자세한 이유는 _call_with_retry,
     _wait_for_cooldown 참고)
  2. 키워드 단위 "외부 재시도": article_search가 최종 실패한 키워드만
     모아서, 한 라운드가 끝난 뒤 별도 라운드로 최대 OUTER_RETRY_PASSES
     번 더 재시도
  3. User-Agent 헤더 주입: gdeltdoc 라이브러리 자체 이슈 트래커(#22)에서
     "User-Agent 없이 요청하면 rate limit, 추가하면 해결"이라는 보고를
     확인 - requests 기본 헤더를 오버라이드하는 방식으로 적용(아래
     "User-Agent 미기재 가설 대응" 주석 참고)
  4. 키워드를 하나의 OR 쿼리로 결합: gdeltdoc의 Filters(keyword=[...])가
     리스트를 자동으로 OR로 묶어준다는 걸 확인(공식 README) - 요청 횟수
     자체를 줄여 429에 걸릴 기회를 줄임. 시계열은 3.1 원칙상 "키워드별
     트렌드"가 의미가 있어야 하므로(지금은 항상 빈 값이라 실질적 영향은
     없음) 이건 합치지 않고 키워드별로 유지하는 구조를 남겨둠
     (_collect_articles_for_keywords 및 collect() 참고).
"""

import json
import os
import re as _re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
import requests.sessions
import requests.utils as _requests_utils

import keyword_source
from gdeltdoc import GdeltDoc, Filters
from gdeltdoc.errors import RateLimitError

# --- User-Agent 미기재 가설 대응 ---
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
_GDELT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# requests.sessions 모듈이 `from .utils import default_headers`로 자기
# 네임스페이스에 직접 바인딩해서 쓰기 때문에, requests.utils.default_headers
# 만 바꾸면 Session.__init__() 내부에서 부르는 건 여전히 원래 함수다.
# requests.sessions 쪽 바인딩도 같이 덮어써야 실제 Session 생성에 반영된다.
#
# 가드(_gdelt_ua_patched)를 두는 이유: 이 모듈이 어떤 경로로든 두 번 로드되면
# (예: importlib.reload) "원본 함수 캡처"까지 다시 실행돼서 이미 패치된
# 함수를 "원본"으로 잘못 캡처해버리고, 그걸 감싸는 새 wrapper가 자기 자신을
# 무한 호출하는 RecursionError로 이어질 수 있음 - 원본 캡처부터 최종
# 할당까지 전부 가드 안에 넣어서, 이미 패치돼 있으면 이 블록을 통째로
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



# 예시 키워드. 구글 시트(KEYWORD_SHEET_CSV_URL)가 설정돼 있으면 그쪽을
# 우선 쓰고, 없거나 읽기 실패하면 이 리스트로 대체된다(keyword_source.py
# 참고, naver_collector.py와 동일 방침). GDELT DOC API는 영문 검색이
# 기본이므로 영문 키워드로 구성한다.
#
# 시트가 나중에 바뀌면 이 fallback도 수동으로 같이 갱신해줘야 한다(자동
# 동기화 아님 - naver_collector.py KEYWORDS 갱신 시와 동일한 주의사항).
KEYWORDS_EN = [
    "foot-and-mouth disease",
    "feed price",
    "livestock movement restriction",
    "swine industry",
    "feed mill",
    "feed additive",
    "livestock import tariff",
    "poultry vertical integration",
    "smart farming",
    "smart livestock barn",
]

# --- 이미 실패가 확인된 키워드 사전 스킵 ---
#
# "HPAI"는 GDELT API가 매번 ValueError("The specified phrase is too short.")
# 로 거부하는 게 확인됨. 문제는 이 에러가 429 재시도 루프를 다 태우고
# 나서야(최대 4단계 백오프, 아래 MAX_RETRIES 참고) 도달하는 진짜 에러라서,
# 매 실행마다 어차피 실패할 키워드에 시간을 낭비하게 된다.
#
# "몇 글자부터 너무 짧다고 판단하는지"는 GDELT가 공식적으로 공개한 기준이 아니라서(추정으로 길이 임계값을 정하면 다른 정상 키워드까지 잘못 걸러낼 위험이 있음), 길이 기반 자동 필터 대신 "실제로 실패가 확인된 키워드"만 명시적으로 등록하는 스킵 리스트로 처리한다.
# 새 키워드를 추가했는데 계속 같은 ValueError로 실패하는 게 확인되면 여기 추가할 것.

SKIP_KEYWORDS = {
    "HPAI": "GDELT API가 'phrase too short'로 거부함 (재현 확인됨)",
}

# --- 학습형 스킵 목록 ---
#
# 위 SKIP_KEYWORDS는 사람이 로그를 보고 손으로 채워넣는 "수동" 목록이라,
# 아직 안 등록된 짧은 키워드가 시트에 새로 추가되면 등록되기 전까지 매번
# ValueError로 요청 1번씩 계속 낭비함(길이 기반 예방적 차단은 위험해서 안 씀).
#
# 대신 "실제로 같은 키워드가 2번 연속 ValueError(쿼리 자체 거부)로 실패하면
# 자동으로 스킵 목록에 편입"하는 방식을 씀 - 사람이 손으로 안 건드려도 됨.
# GitHub Actions 러너가 매번 새 VM이라(상태가 실행 간 유지 안 됨) 이 학습
# 결과를 파일로 저장해서 리포에 git commit해야 다음 실행에도 이어짐 -
# storage.py가 raw.json/scored.json을 저장하는 것과 같은 패턴을 재사용함.
#
# data/가 아니라 별도 state/ 디렉토리에 두는 이유: data/는 "주차별 결과물
# 아카이브"라는 의미가 이미 확정돼 있는데(storage.py 참고), 이 파일은
# 특정 주차에 속하지 않고 계속 누적되는 "파이프라인 자체의 학습된 상태"라
# 성격이 달라 구분함.
#
# 2번(SKIP_STATE_FAILURE_THRESHOLD)으로 잡은 이유: 1번 실패만으로 바로
# 영구 등록하면, GDELT 쪽 일시적 문제로 어쩌다 한 번 오탐이 났을 때도
# 영구히 막혀버릴 위험이 있음 - 2번 연속 확인돼야 "진짜 이 키워드 자체의
# 문제"로 보는 게 안전하다고 판단.
SKIP_STATE_PATH = "state/gdelt_skip_keywords.json"
SKIP_STATE_FAILURE_THRESHOLD = 2

# 이번 실행 중 ValueError(쿼리 자체 거부)로 실패한 키워드를 모아두는 용도.
# collect() 시작 시 반드시 비워야 함(모듈이 재사용될 수 있는 테스트 환경
# 등에서 이전 실행의 잔여물이 안 섞이도록) - collect() 본문 참고.
_value_error_keywords_this_run: list[str] = []


def _load_skip_state(path: str = SKIP_STATE_PATH) -> dict:
    """
    {키워드: {"fail_count": N, "reason": "...", "last_seen": "..."}} 형태로
    저장된 학습된 스킵 상태를 읽어온다. 파일이 없거나(첫 실행) 읽기/파싱에
    실패하면 빈 딕셔너리로 안전하게 시작한다 - 이 파일은 어디까지나 최적화용
    보조 데이터라, 못 읽어도 파이프라인 자체가 죽으면 안 됨(9.4/9.5 원칙과
    같은 방향).
    """
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            # 파일이 파싱은 됐지만 예상 구조(키워드: 정보 dict)가 아닌 경우
            # (예: 수동 편집 실수, git 병합 충돌 잔재) - 이 파일은 학습된
            # 힌트일 뿐이라 이상하면 그냥 빈 상태로 다시 시작하는 게
            # 안전하다(아래 skip_state.get(keyword)가 dict가 아닌 값에
            # 호출되면 AttributeError로 죽을 수 있어 여기서 미리 막음).
            print(f"[gdelt] 학습된 스킵 상태 파일 구조 이상(dict 아님, 타입: "
                  f"{type(state).__name__}) - 빈 상태로 다시 시작: {path}")
            return {}
        return state
    except (OSError, json.JSONDecodeError) as e:
        print(f"[gdelt] 학습된 스킵 상태 파일 읽기 실패(처음 실행이거나 파일 없음 - "
              f"정상, 빈 상태로 시작): {path} - {type(e).__name__}: {e}")
        return {}


def _save_skip_state(state: dict, path: str = SKIP_STATE_PATH) -> None:
    """저장 실패해도 예외를 던지지 않는다 - storage.py의 파일 쓰기 실패 흡수 패턴과 동일."""
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print(f"[gdelt] 학습된 스킵 상태 저장 완료 -> {path}")
    except OSError as e:
        print(f"[gdelt] 학습된 스킵 상태 저장 실패(이번 실행 결과엔 영향 없음, "
              f"다음 실행에서 다시 학습 시도됨): {path} - {type(e).__name__}: {e}")


def _update_skip_state_after_run() -> None:
    """
    collect() 끝에서 호출 - 이번 실행 중 ValueError로 실패한 키워드들의
    fail_count를 1씩 올리고, 변경이 있었으면 파일에 저장한다.
    """
    if not _value_error_keywords_this_run:
        return

    state = _load_skip_state()
    now_str = datetime.now(timezone.utc).isoformat()
    for keyword in dict.fromkeys(_value_error_keywords_this_run):  # 중복 제거, 순서 유지
        entry = state.get(keyword)
        if not isinstance(entry, dict):
            entry = {"fail_count": 0}  # 없거나(신규) 손상된 값이면 새로 시작
        entry["fail_count"] = entry.get("fail_count", 0) + 1
        entry["reason"] = "GDELT API가 'phrase too short' 등으로 쿼리 자체를 거부함 (자동 학습됨)"
        entry["last_seen"] = now_str
        state[keyword] = entry
        if entry["fail_count"] >= SKIP_STATE_FAILURE_THRESHOLD:
            print(f"[gdelt] '{keyword}' - {entry['fail_count']}회 연속 ValueError 확인됨 "
                  f"(임계값 {SKIP_STATE_FAILURE_THRESHOLD}) - 다음 실행부터 자동 스킵 대상으로 등록")

    _save_skip_state(state)

# --- 키워드 오매칭(false positive) 필터 ---
#
# GDELT article_search는 키워드를 "부분 문자열 포함" 방식으로 매칭하기 때문에, 의도한 키워드가 더 긴 무관한 구(phrase)의 일부로 들어있는 제목도 그대로 매칭돼버리는 구조적 문제가 있다.
# 실제로 확인된 사례:
#   "foot and mouth disease"(구제역) 검색 -> "hand, foot and mouth disease"
#   (수족구병 - 어린이 질환, 전혀 다른 병)가 그대로 포함 매칭됨
#
# 길이 기반이나 정규식 기반의 일반화된 해법 대신, "실제로 오매칭이 확인된 키워드"에 한해 제외 패턴을 명시적으로 등록하는 방식을 쓴다
# (SKIP_KEYWORDS와 동일한 철학 - 추정으로 일반 규칙을 만들면 다른 정상 매칭까지 잘못 걸러낼 위험이 있음).
# 새 오매칭 패턴이 확인되면 여기 추가할 것.
#
# 형태: {검색 키워드: [제목에 이 문자열(대소문자 무시)이 포함되면 제외, ...]}
#
# 딕셔너리 키는 실제로 시트(v5)에서 쓰는 키워드 문자열과 정확히 일치해야
# 한다("foot-and-mouth disease", 하이픈 있음) - FALSE_POSITIVE_FILTERS.get
# (keyword, [])가 정확히 일치하는 문자열만 찾기 때문에, 키가 조금이라도
# 다르면(예: 하이픈 유무) 이 필터가 조용히 무력화된다.
FALSE_POSITIVE_FILTERS = {
    "foot-and-mouth disease": ["hand, foot and mouth", "hand foot and mouth"],
}

# --- 구두점 앞 공백 정규화 ---
#
# 실제 GDELT article_search가 돌려주는 제목은 쉼표/마침표 "앞"에도 공백이
# 들어간 토크나이즈된 형식이다(예: "Westmoreland sees increase in hand ,
# foot and mouth disease" - "hand SPACE , SPACE foot" 형태). 이 형식을
# 고려하지 않고 FALSE_POSITIVE_FILTERS 패턴을 등록하면 실제 제목과 안
# 맞아서 필터가 전혀 작동하지 않는다.
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
    "구두점 앞 공백" 형식(위 정규화 주석 참고) 때문에 매칭이 실패하는
    일이 없도록 한다.
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

# --- 시계열 전용 기간 (article_search와 분리) ---
#
# GDELT 공식 문서 확인: timeline_search는 요청 기간 길이에 따라 해상도가
# 자동으로 바뀐다 - 72시간 미만이면 15분 단위, 72시간~1주일이면 1시간
# 단위, "1주일 초과"면 1일 단위. TIMESPAN(7d, 정확히 "1주일")을 그대로
# 쓰면 1시간 단위로 나오는데, 이 프로젝트는 주 1회 실행되는 주간
# 다이제스트라 시간 단위 디테일이 애초에 필요 없어 8일(1주일 "초과")로
# 바꿔서 일 단위로 받도록 함.
#
# ** article_search와 상수를 공유하지 않는 이유 **: TIMESPAN을 그냥
# 8d로 바꾸면 article_search도 8일치를 가져오게 되는데, 그러면 (a) "최근
# 7일" 원칙이 깨지고 (b) 어차피 나중에 DAYS_BACK 기준으로 걸러질 여분의
# 기사가 GDELT 250건 상한 경쟁에 불필요하게 끼어들어 크라우딩만 심해짐.
# 그래서 시계열만 쓰는 별도 상수로 분리.
#
# ** 429 감소 효과는 미확인 **: GDELT의 "함대 전체 QPS" 가설이 맞다면
# 429는 응답 데이터量이 아니라 요청 "빈도" 때문에 걸리는 거라, 해상도를
# 낮춘다고 429 자체가 줄어든다는 보장은 없음 - 다만 "필요한 만큼만
# 받는다"는 것 자체는 429와 무관하게 맞는 방향이라 반영함.
TIMELINE_TIMESPAN = "8d"

# article_search는 GDELT DOC API 자체 한계로 한 번 호출에 최대 250건까지만 반환됨
# (naver처럼 start 파라미터로 추가 페이지네이션하는 기능 자체가 없음 - API 레벨 한계)
MAX_RECORDS = 250

# --- 적응형 배치 수집 ---
#
# 배경: 키워드를 전부 OR로 합치면 250건 상한을 광범위한 키워드 하나가
# 독차지하는 문제가 생길 수 있다(예: `vaccination`이 250건 중 상당 비율을
# 차지). 반대로 키워드를 전부 개별 요청하면 크라우딩은 안 생기지만
# 키워드 수가 늘수록 요청 횟수가 그대로 비례해서 늘어나 429/런타임
# 부담이 커진다.
#
# 검토했다가 기각한 대안: ① 키워드를 미리 "위험/안전"으로 수동 분류해서
# 위험한 것만 개별 처리 - "지금은 널널한데 갑자기 뜨는 키워드"를 못 잡고
# 사람이 계속 재분류해야 하는 유지보수 부담이 있음. ② 무작위로 고정
# 묶음을 나눠서 그냥 돌리기 - 하필 두 인기 키워드가 같은 고정 묶음에
# 우연히 들어가면 그 조합이 계속 나쁜 채로 반복됨.
#
# 채택한 방식: 일단 작게 묶어서(BATCH_SIZE) 보내보고, 그 배치 결과에서
# 상한(MAX_RECORDS) 근처까지 찼는데 특정 키워드가 결과의 상당 비율을
# 차지했으면("크라우딩 감지") 그 배치의 나머지 키워드만 그 자리에서
# 개별로 추가 요청해서 보충한다. 사람이 미리 분류할 필요도 없고, 그
# 실행에서 실제로 터진 키워드를 즉시 감지해서 대응하므로 "갑자기 뜬
# 키워드"도 놓치지 않는다 (아래 collect()/_detect_crowded_keywords 참고).
BATCH_SIZE = 5  # 잠정값 - 실측하면서 조정 (작을수록 크라우딩 적지만 요청 많아짐)

# 배치 결과가 상한(MAX_RECORDS)의 이 비율 이상 찼을 때만 크라우딩 검사를
# 한다 - 상한에 안 걸렸으면애초에 뭔가 밀려났을 리가 없으므로 검사 자체가
# 무의미함(불필요한 오탐 방지).
CROWDING_CAP_TRIGGER_RATIO = 0.9

# 배치 결과 중 한 키워드가(제목 기준 근사치) 이 비율 이상을 차지하면
# "크라우딩"으로 판단해 나머지 키워드를 개별 재요청한다. 잠정값 - 실측
# 필요.
CROWDING_SHARE_THRESHOLD = 0.4

# 키워드 사이 요청 간격.
# GDELT는 공식적으로 "몇 초에 몇 건"인지 수치를 공개하지 않는다. 경험적으로
# 조정한 값 - 너무 짧으면 거의 매 호출마다 429가 발생해 재시도 백오프가
# 누적되는 문제가 있다.
REQUEST_INTERVAL = 15.0


# RateLimitError(HTTP 429) 전용 재시도 횟수/대기시간.
# GDELT는 실제로 서버 사이드 요청 제한이 있음(공식 블로그에 ElasticSearch
# 클러스터 보호 목적이라고 명시). 정확한 윈도우 수치는 비공개지만, 실사용
# 보고(공식 확정 아님·참고치)에 따르면 짧은 시간에 요청이 몰리면 15분가량
# 지속되는 차단이 걸릴 수 있다고 함 - 4단계(60→120→240→480초, 누적
# 900초=15분)로 이 참고치와 누적 대기시간을 맞췄다. 그래도 계속 429가
# 뜬다면 이건 백오프 튜닝으로 해결할 문제가 아니라 실행 자체를 미루는 게
# 맞다는 신호.
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 60  # 60 -> 120 -> 240 -> 480초로 증가 (누적 900초)


# 일시적 네트워크 에러(ConnectTimeout 등) 재시도 횟수/대기시간.
# 429(rate limit)와 ConnectTimeout이 같은 실행 안에서 섞여 나올 수 있다.
# 이건 요청 제한과는 별개로 순간적인 접속 실패일 가능성이 높아 429보다는
# 훨씬 짧게 재시도한다. 이것도 다 실패하면 429 때와 마찬가지로 그
# 키워드는 포기.
NETWORK_ERROR_MAX_RETRIES = 2
NETWORK_ERROR_WAIT_SECONDS = 10


# 429 발생 시각을 모아뒀다가 실행 끝에 "시간대별 패턴 요약"으로 출력하던
# 기능은 제거했다. 실행 트리거가 사람이 수동으로 누르는 버튼뿐이라 표본이
# 근무 시간대로만 몰릴 수밖에 없어, 애초에 "시간대 패턴을 알아낸다"는
# 목적을 달성할 수 없다는 판단. 429가 발생한 그 순간 콘솔에 UTC 시각을
# 찍는 즉시성 로그(아래 _call_with_retry 안의 print)는 지금 실행 중인
# 로그를 읽을 때 유용해서 그대로 둔다 - 여러 실행에 걸쳐 모아서
# 분석하려던 부분만 제거한 것.


# --- 키워드 단위 외부 재시도 (outer retry) ---
#
# MAX_RETRIES=4(최대 900초 대기)를 다 소진하고도 article_search가 실패하는
# 키워드가 나올 수 있는데, 이 경우 그 키워드가 그대로 기사 0건으로 끝나면
# "이번 주엔 유독 어떤 키워드만 몰린" 편중된 결과물이 나올 위험이 있다.
# 이건 실제 이슈 분포가 아니라 순전히 그 시점 레이트리밋 운에 좌우되는
# 편중이라 문제.
#
# 대응: 한 라운드(전체 키워드 순회)가 끝난 뒤, article_search가 최종 실패한 키워드만 모아서 별도 라운드로 재시도한다.
# 이미 성공한 키워드는 다시 건드리지 않음(중복 수집 방지 + 불필요한 API 호출 절약).
#
# 라운드 사이엔 REQUEST_INTERVAL과 별개로 OUTER_RETRY_WAIT_SECONDS만큼 추가 대기를 둔다.
# MAX_RETRIES 소진이 항상 900초 대기 후에 일어나는 건 아니라서(예: 네트워크 에러로 더 일찍 포기한 경우),
# 쿨다운이 실제로는 덜 지난 채 다음 라운드가 시작될 수 있어 안전 마진으로 추가함.
#
# 총 시도 횟수 = 1(최초) + OUTER_RETRY_PASSES(추가 라운드) = 기본 3회.
# 이 프로젝트는 주 1회 배치라 실행 시간이 늘어나는 것보다 "쏠린 결과물"을 피하는 쪽을 우선함.
OUTER_RETRY_PASSES = 2
OUTER_RETRY_WAIT_SECONDS = 90


# --- 전역(프로세스 공유) 쿨다운 ---
#
# _call_with_retry가 429를 만났을 때 "그 호출 안에서만" 대기하면, 이
# 프로젝트가 키워드 하나당 API를 여러 번(article_search, timelinevol,
# timelinevolraw) 따로 호출하는 구조상 재시도 카운터가 호출마다 완전히
# 독립이라 같은 차단 구간 안에서도 호출마다 처음부터 다시 60초부터
# 백오프를 시작하는 낭비가 발생한다.
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


def _parse_retry_after(response) -> float | None:
    """
    gdeltdoc의 RateLimitError는 requests.HTTPError를 상속해서 원본 응답을
    response 속성에 그대로 담고 있음(gdeltdoc/errors.py의 `raise
    RateLimitError(response=response)`) - 이 함수는 거기서 `Retry-After`
    헤더를 읽어본다.

    ** 중요 - GDELT가 실제로 이 헤더를 주는지는 미확인 **: 헤더가 있으면
    그 값을 쓰고, 없으면(또는 파싱 실패하면) None을 반환해서 호출부가
    기존 방식(BACKOFF_BASE_SECONDS 지수 백오프 추측)으로 안전하게
    fallback하도록 설계 - 있으면 이득, 없어도 손해 없는 변경.

    HTTP 표준상 Retry-After는 초 단위 정수 또는 HTTP-date 형식일 수 있어
    (RFC 9110) 둘 다 시도한다.
    """
    if response is None:
        return None
    value = response.headers.get("Retry-After")
    if not value:
        return None

    try:
        return float(value)
    except ValueError:
        pass

    try:
        from email.utils import parsedate_to_datetime
        retry_dt = parsedate_to_datetime(value)
        if retry_dt.tzinfo is None:
            retry_dt = retry_dt.replace(tzinfo=timezone.utc)
        seconds = (retry_dt - datetime.now(timezone.utc)).total_seconds()
        return max(seconds, 0.0)
    except (TypeError, ValueError):
        return None


def _call_with_retry(func, *args, label: str = "", **kwargs):
    """
    RateLimitError(429)와 일시적 네트워크 에러(ConnectTimeout 등)를 서로 다른
    정책으로 재시도한다 (429는 길게, 네트워크 에러는 짧게). 그 외 예외는 바로
    올려보낸다 - 단수형 경로(_collect_articles_for_keyword)에서는 그 키워드만
    건너뛰지만, 결합 경로(_collect_articles_for_keywords)에서는 ValueError를
    별도로 잡아 키워드별 개별 요청으로 격리한다(아래
    _collect_articles_individually 참고).

    429는 이 함수 호출 하나만 기다리고 마는 게 아니라, 전역 쿨다운
    (_trigger_cooldown)을 걸어서 이후에 오는 모든 호출(다른 키워드/다른
    엔드포인트 포함)이 같은 차단 구간을 공유해서 기다리게 함. 호출 시작
    시 _wait_for_cooldown()으로 남아있는 쿨다운을 먼저 소진한다.

    두 카운터(rate_limit_attempt, network_attempt)를 독립적으로 관리 - 하나의
    반복문에서 같이 세면 "429 한 번 + 네트워크에러 한 번"을 겪었을 때 429 쪽
    백오프 계산이 실제 429 횟수와 안 맞아지는 문제가 있어서 분리함.

    label: 로그에 찍을 식별자 (예: "avian influenza / article_search").
    키워드 하나당 API를 여러 번(article_search, timelinevol, timelinevolraw)
    따로 호출하는데, 재시도 카운터가 호출마다 독립적으로 리셋되다 보니
    로그만 보면 "(1/3)"이 반복되는 것처럼 헷갈릴 수 있어서 label을 붙였다.

    429 응답에 `Retry-After` 헤더가 실려 있으면(_parse_retry_after 참고)
    그 값을 BACKOFF_BASE_SECONDS 지수 백오프 추측보다 우선 사용한다 -
    서버가 정확히 알려준 시간이라면 우리 추측보다 정확할 수 있음. 헤더가
    없으면(GDELT가 실제로 이 헤더를 주는지 여전히 미확인 상태) 기존 방식
    그대로 fallback - 있으면 이득, 없어도 손해 없는 변경.
    """
    rate_limit_attempt = 0
    network_attempt = 0

    while True:
        _wait_for_cooldown()  # 다른 호출이 걸어둔 전역 쿨다운이 있으면 먼저 대기
        try:
            return func(*args, **kwargs)
        except RateLimitError as e:
            if rate_limit_attempt >= MAX_RETRIES:
                raise
            server_wait = _parse_retry_after(getattr(e, "response", None))
            if server_wait is not None:
                wait = server_wait
                wait_source = "서버가 Retry-After로 알려준 값"
            else:
                wait = BACKOFF_BASE_SECONDS * (2 ** rate_limit_attempt)
                wait_source = "서버가 안 알려줘서 우리 쪽 추정값"
            rate_limit_attempt += 1
            now_str = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(f"[gdelt] {now_str} - {label} - 429 rate limit - {wait:.0f}초 전역 쿨다운 설정 "
                  f"({wait_source}) ({rate_limit_attempt}/{MAX_RETRIES})")
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


def _collect_articles_for_keyword(gd: "GdeltDoc", keyword: str) -> tuple[bool, list[dict], str | None]:
    """
    한 키워드에 대해 article_search만 수행하고 (성공 여부, 기사 리스트,
    실패 사유)를 반환한다. 실패 사유는 성공 시 None, 실패 시
    "{예외타입}: {메시지}" 형태 - 호출부(collect())가 "최종 실패 키워드"
    로그에 원인까지 같이 남길 수 있도록 함(운영자가 429/네트워크 오류/
    그 외 예외 중 무엇 때문인지 로그만 보고 구분할 수 있게).
    외부 재시도 라운드(OUTER_RETRY_PASSES)에서 실패한 키워드만 다시 돌릴
    수 있도록 collect()와는 별도 함수로 분리돼 있다.

    성공 여부는 article_search 호출 자체가 예외 없이 끝났는지 기준 (기사가
    0건이어도 호출 자체가 정상이면 True - 그 키워드는 그냥 최근 기사가 없는
    것이므로 재시도 대상이 아님).

    ** collect()의 정식 실행 경로에서는 더 이상 안 씀 **: 요청 횟수를
    줄이려고 아래 _collect_articles_for_keywords(복수형, OR 결합)로
    교체됨. 이 단수형 함수는 키워드 하나만 따로 확인해보고 싶은
    진단/디버깅 용도로 남겨둠 - 삭제하지 않음.
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
                    # 언어/국가 분포 진단용. gdeltdoc이 원래 반환하는
                    # 필드인데 지금까지 받아만 놓고 안 쓰고 있었음.
                    # GDELT 공식 문서 확인 - JSON/article_search 모드에서는 title이 번역 없이 원문 그대로 옴 (TRANS 옵션은 HTML 모드 전용 위젯이라 여기 안 해당).
                    # 즉 이 title 값은 중국어/독일어 등 원문 스크립트 그대로일 수 있음. (12번 언어처리방침에서 다뤄야 할 지점.)
                    "language": row.get("language", ""),
                    "sourcecountry": row.get("sourcecountry", ""),
                })

        fp_note = f" (오매칭 필터로 {false_positive_count}건 제외)" if false_positive_count else ""
        print(f"[gdelt] '{keyword}' article_search -> 최근 {DAYS_BACK}일 이내 "
              f"{len(keyword_articles)}건 수집 완료{fp_note}")
        return True, keyword_articles, None

    except ValueError as e:
        # 쿼리 자체가 거부된 경우(예: "phrase too short")는 시간이 지나도
        # 안 풀리는 확정적 실패라, 위 SKIP_KEYWORDS/학습형 스킵 목록
        # (_update_skip_state_after_run 참고)의 대상이 됨 - 여기서 발생
        # 시점에 바로 기록해둔다. 일반 Exception과 구분해서 잡는 이유는
        # 네트워크 오류 같은 일시적 실패까지 "이 키워드가 문제"로 학습되면
        # 안 되기 때문(오탐 방지).
        print(f"[gdelt] '{keyword}' article_search 실패(쿼리 자체 거부로 추정 - "
              f"{type(e).__name__}: {e})")
        _value_error_keywords_this_run.append(keyword)
        return False, [], f"{type(e).__name__}: {e}"

    except Exception as e:
        # 기사 수집 자체가 실패한 경우 - 호출부(collect())가 이 키워드를
        # failed_keywords에 담아 외부 재시도 라운드로 넘긴다.
        print(f"[gdelt] '{keyword}' article_search 실패: {type(e).__name__} - {e!r}")
        return False, [], f"{type(e).__name__}: {e}"


def _collect_articles_for_keywords(gd: "GdeltDoc", keywords: list[str]) -> tuple[bool, list[dict]]:
    """
    여러 키워드를 하나의 OR 쿼리로 묶어서 article_search를 "한 번만" 호출한다.
    (모듈 docstring "GDELT 429 대응 정리" 참고)

    gdeltdoc의 Filters(keyword=[...])는 리스트를 넘기면 값들을 boolean OR로
    묶어준다(공식 README 확인) - 기존엔 키워드 개수만큼 요청이 나갔는데
    이걸 1번으로 줄여서 429에 걸릴 기회 자체를 줄인다.

    collect()의 외부 재시도 로직이 그대로 재사용되도록, 단수형
    _collect_articles_for_keyword와 동일한 반환 형태(성공 여부, 기사 리스트)
    를 유지한다.

    ** MAX_RECORDS 상한이 이제 "키워드 조합 전체"에 걸린다는 점 주의 **:
    키워드 하나당 최대 250건씩 받던 개별 요청과 달리, OR로 합치면 이
    조합 전체에서 최대 250건까지만 돌아온다. 키워드가 늘거나 특정
    키워드가 급증하면 조용히 잘려나갈 수 있다는 걸 염두에 둘 것.

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

        # --- 키워드별 매칭 현황 (제목 기준 근사치) ---
        # 배경: OR로 합친 뒤로는 "각 키워드가 실제로 얼마나 수집됐는지"
        # 로그로 전혀 구분이 안 된다. 추가 API 호출 없이(429 부담을 다시
        # 늘리지 않으려고), 이미 받아온 기사 제목에 각 키워드가 부분
        # 문자열로 들어있는지만 사후 대조해서 근사치를 보여준다.
        #
        # ** 주의 - 이건 정확한 집계가 아니라 근사치다 **: GDELT 검색은
        # 제목뿐 아니라 본문 전체를 대상으로 매칭하는데, 우리는 본문을
        # 안 받아오므로(GDELT는 본문 미제공) 제목에 키워드가 없어도 실제로는
        # 그 키워드로 매칭됐을 수 있다 - 그래서 아래 숫자의 합이 전체 수집
        # 건수보다 작게 나올 수 있다("본문에만 있던 매칭"은 집계에서 빠짐).
        # 그래도 "이 키워드가 최소 이만큼은 확실히 걸렸다"는 하한선 확인
        # 용도로는 충분하다 - 목적(키워드 하나가 나머지를 잠식하는지 확인)에
        # 필요한 만큼의 신뢰도는 됨.
        print(f"[gdelt] 키워드별 매칭 현황(제목 기준 근사치 - 본문에만 있는 매칭은 " 
              f"집계 안 됨, 하나의 기사가 여러 키워드에 동시 매칭될 수 있어 합계가 "
              f"전체 건수와 다를 수 있음):")
        for kw in keywords:
            kw_lower = kw.lower()
            count = sum(1 for a in combined_articles if kw_lower in a["title"].lower())
            print(f"  - '{kw}': {count}건")
        return True, combined_articles

    except ValueError as e:
        # "phrase too short" 등 GDELT가 쿼리 자체를 거부하는 에러
        # (RateLimitError/네트워크 에러와 달리 시간이 지나도 절대 안 풀림)는
        # 결합 쿼리 안에 문제 키워드가 "섞여 있는 한" 재시도해도 100% 같은
        # 이유로 또 실패한다 - collect()의 외부 재시도 로직은 원래 429처럼
        # "시간이 지나면 풀리는 실패"를 염두에 두고 만든 거라, 이 경우엔
        # 재시도를 그대로 낭비하고 결국 GDELT 기사 전체가 0건으로 끝나버릴
        # 수 있다(키워드를 OR로 합친 구조에서 생기는 위험 - 개별 요청이면
        # 문제 키워드 하나만 실패로 끝났을 것). 재시도 대신 그 즉시 키워드별
        # 개별 요청으로 내려가서 문제 키워드만 격리하고, 나머지는 정상적으로 살린다.
        print(f"[gdelt] '{label}' article_search 실패(쿼리 자체 거부로 추정 - "
              f"{type(e).__name__}: {e}) - 재시도 대신 키워드별 개별 요청으로 즉시 전환")
        return _collect_articles_individually(gd, keywords)

    except Exception as e:
        print(f"[gdelt] '{label}' article_search 실패: {type(e).__name__} - {e!r}")
        return False, []


def _collect_articles_individually(gd: "GdeltDoc", keywords: list[str]) -> tuple[bool, list[dict]]:
    """
    결합 쿼리(_collect_articles_for_keywords)가 "phrase too short"류 - 재시도로
    해결 안 되는 - 에러로 실패했을 때 호출되는 격리 폴백.

    키워드를 하나씩 따로 요청해서(기존 _collect_articles_for_keyword 재사용)
    문제 있는 키워드만 그 자리에서 실패로 남기고, 나머지는 정상적으로
    결과에 포함시킨다 - 결합 요청 방식이 가진 "하나가 전체를 잠식하는"
    위험을 이 경로에서만 다시 키워드 1개 단위로 좁혀서 상쇄한다.

    구글 시트(keyword_source.py)로 키워드를 등록하는 구조라, 사람이
    모르는 사이 짧은 영문 키워드가 추가돼 이 상황이 발생할 수 있다 - 이
    폴백이 없으면 그 실행의 GDELT 기사가 전부 0건으로 끝나버릴 수 있음.
    """
    all_articles = []
    any_success = False
    for keyword in keywords:
        success, keyword_articles, _reason = _collect_articles_for_keyword(gd, keyword)
        if success:
            any_success = True
            all_articles.extend(keyword_articles)
        time.sleep(REQUEST_INTERVAL)
    return any_success, all_articles


def _detect_crowded_keywords(articles: list[dict], keywords: list[str]) -> list[str]:
    """
    배치(OR 결합) 요청 결과에서 특정 키워드가 결과를 과도하게 차지해서,
    상한(MAX_RECORDS) 때문에 같은 배치의 다른 키워드가 밀려났을 가능성이
    있는지 감지한다.

    반환값: "크라우딩을 일으킨 것으로 보이는" 키워드 리스트. 호출부는 이
    리스트에 없는(=밀려났을 가능성이 있는) 나머지 배치 키워드를 개별로
    추가 요청해서 보충한다.

    ** 판단 기준 **
    1. 배치 결과 건수가 MAX_RECORDS의 CROWDING_CAP_TRIGGER_RATIO(90%)
       이상이어야 검사 자체를 시작한다 - 상한에 안 걸렸으면 애초에 밀려난
       기사가 있을 수 없으므로 검사가 무의미함(오탐 방지).
    2. 제목 기준 부분 문자열 매칭으로 키워드별 건수를 세고(근사치 - 아래
       한계 참고), 배치 전체 대비 비율이 CROWDING_SHARE_THRESHOLD(40%)
       이상이면 그 키워드를 크라우딩 원인으로 판단한다.

    ** 한계 (기존 "키워드별 매칭 현황" 로그와 같은 이유) **
    GDELT는 제목뿐 아니라 본문 전체로 매칭하는데 본문은 안 받아오므로,
    이 함수는 "제목에 명시적으로 드러난 크라우딩"만 잡아낸다 - 본문에만
    있던 매칭으로 크라우딩이 생겨도 이 함수는 못 잡을 수 있다(과소 탐지
    쪽으로 치우침 - 놓치는 게 있을 수 있지만, 반대로 불필요한 추가 요청을
    남발하지는 않는다는 뜻이라 안전한 방향의 편향으로 판단).
    """
    total = len(articles)
    if total < MAX_RECORDS * CROWDING_CAP_TRIGGER_RATIO:
        return []

    crowders = []
    for keyword in keywords:
        keyword_lower = keyword.lower()
        count = sum(1 for a in articles if keyword_lower in a["title"].lower())
        if count / total >= CROWDING_SHARE_THRESHOLD:
            crowders.append(keyword)
    return crowders


def _collect_timeline_for_keyword(gd: "GdeltDoc", keyword: str) -> dict | None:
    """
    한 키워드에 대해 timelinevol/timelinevolraw를 수집한다. 실패 시 None.
    (collect() 안에 있던 로직을 분리한 별도 함수)

    ** `collect()`에서 더 이상 호출 안 함 **
    시계열 수집 자체를 완전히 제거하기로 결정(실전 규모 테스트에서도 429
    백오프를 다 쓰고 실패하는 등 불안정성이 해소가 안 됨, 저장 레이어가
    이 데이터를 애초에 아무 데도 안 쓰고 있었던 것도 근거). 함수 자체는
    참고/향후 복원 가능성을 위해 코드에 남겨둠 - 죽은 코드지만 실행되지
    않으므로 부작용은 없음.

    article_search용 TIMESPAN(7d) 대신 TIMELINE_TIMESPAN(8d)을 쓴다 -
    "1주일 초과"부터 GDELT가 일 단위 해상도로 전환해주는 걸 이용해, 이
    프로젝트에 필요 없는 시간 단위 디테일 대신 일 단위로 받는다 - 위
    TIMELINE_TIMESPAN 정의 주석 참고.
    """
    f = Filters(keyword=keyword, timespan=TIMELINE_TIMESPAN, num_records=MAX_RECORDS)
    try:
        vol_df = _call_with_retry(gd.timeline_search, "timelinevol", f,
                                   label=f"{keyword} / timelinevol")
        time.sleep(REQUEST_INTERVAL)
        volraw_df = _call_with_retry(gd.timeline_search, "timelinevolraw", f,
                                      label=f"{keyword} / timelinevolraw")
        print(f"[gdelt] '{keyword}' 시계열 수집 완료")
        return {
            # 실제 컬럼 구조:
            #   vol:    {"datetime": Timestamp, "Volume Intensity": float}
            #   volraw: {"datetime": Timestamp, "Article Count": int, "All Articles": int}
            # Timestamp 객체는 그대로 JSON 직렬화가 안 되므로, 저장 레이어
            # 쪽에서 raw.json에 넣기 전 문자열로 변환하는 처리가 필요함.
            "vol": vol_df.to_dict("records") if vol_df is not None else [],
            "volraw": volraw_df.to_dict("records") if volraw_df is not None else [],
        }
    except Exception as e:
        # 시계열만 실패한 경우 - 기사는 이미 확보돼 있으므로(성공했다면)
        # 데이터 손실 없음. 결과물에서는 참고 지표(3.1)가 빠지는 것뿐이라
        # 외부 재시도 대상에는 넣지 않는다(article_search만큼 치명적이지 않음).
        print(f"[gdelt] '{keyword}' 시계열 수집 실패: {type(e).__name__} - {e!r}")
        return None


def collect(keywords: list[str] | None = None) -> tuple[list[dict], dict]:
    """
    KEYWORDS_EN을 대상으로 GDELT에서 기사 메타데이터를 수집한다.
    이 함수가 gdelt_collector의 '진입점'.

    keywords: 지정하면 모듈 기본값(KEYWORDS_EN) 대신 이 리스트로 순회한다
              - 테스트 스크립트에서 키워드 수를 줄여 빠르게 확인해보기
              위한 용도. main.py의 정식 실행은 인자 없이 collect()를
              호출하므로 기존 동작과 완전히 동일함.

    ** 시계열 수집은 완전히 제거된 상태 **
    반환값의 timeline은 항상 빈 딕셔너리(`{}`)다. `main.py`가 여전히
    `(articles, timeline)` 튜플 형태로 언패킹하므로 반환값 개수는 그대로
    유지 - 호출부 수정 없이 안전하게 제거하려는 의도.

    ** 적응형 배치 수집 **
    키워드를 전부 OR로 묶으면 250건 상한을 한 키워드가 독차지하는 문제가
    생길 수 있고, 전부 개별 요청하면 요청 횟수가 키워드 수에 비례해서
    늘어난다. 검토했다가 기각한 대안: ① 위험 키워드를 미리 수동으로
    분류 - "갑자기 뜬 키워드"를 못 잡고 계속 재분류해야 함. ② 무작위
    고정 묶음 - 나쁜 조합이 우연히 고정되면 계속 반복될 수 있음.

    대신 채택한 방식은 사람이 미리 분류할 필요 없이 **그 실행 안에서
    실시간으로 크라우딩을 감지해서 보정**한다:

      1단계: 활성 키워드를 BATCH_SIZE(기본 5)개씩 묶어 각 배치를 OR로
             결합해 요청(_collect_articles_for_keywords 재사용) - 배치가
             많아도 요청 횟수는 "키워드 수 / BATCH_SIZE"로 완만하게 늘어남.
      2단계: 각 배치 결과에 _detect_crowded_keywords를 적용해, 상한
             (MAX_RECORDS) 근처까지 찼는데 특정 키워드가 결과의 상당
             비율(CROWDING_SHARE_THRESHOLD)을 차지했으면 "크라우딩"으로
             판단하고, 그 배치의 **나머지** 키워드만 개별 재요청 대상으로
             남겨둔다.
      3단계: 배치 자체가 실패한 경우(429 등)는 **같은 배치로** 외부 재시도
             라운드(OUTER_RETRY_PASSES)를 돈다. 크라우딩으로 밀려난
             키워드(진짜 "상한 초과"류 문제)만 개별 재요청 대상으로 모아
             별도로 재시도한다.

    _collect_articles_for_keywords(복수형, OR 결합)와
    _collect_articles_individually(전체 개별 폴백)는 이 배치 로직에서
    그대로 재사용된다.

    반환값:
      articles: 공통 스키마 리스트. 다음 단계(정규화/이슈그룹핑)로 그대로 전달됨
      timeline: 시계열 수집을 완전히 제거해서 **항상 빈 딕셔너리(`{}`)**.
    """
    gd = GdeltDoc()
    # keywords 인자로 명시적으로 넘겨준 게 있으면(테스트용) 그걸 그대로 쓰고,
    # 없으면 구글 시트에 등록된 활성 키워드를 우선 사용, 시트 미설정/읽기
    # 실패 시 KEYWORDS_EN(하드코딩)으로 안전하게 대체(keyword_source.py
    # 참고 - 이 함수는 예외를 던지지 않음)
    target_keywords = keywords if keywords is not None else keyword_source.get_keywords("en", KEYWORDS_EN)

    # 이번 실행의 학습 기록을 새로 시작 (모듈이 테스트 등에서 재사용될 수
    # 있어 이전 실행의 잔여물이 안 섞이도록 매번 비움)
    _value_error_keywords_this_run.clear()
    skip_state = _load_skip_state()

    active_keywords = []
    for keyword in target_keywords:
        if keyword in SKIP_KEYWORDS:
            print(f"[gdelt] '{keyword}' 스킵 - {SKIP_KEYWORDS[keyword]}")
            continue
        learned_entry = skip_state.get(keyword)
        if isinstance(learned_entry, dict) and learned_entry.get("fail_count", 0) >= SKIP_STATE_FAILURE_THRESHOLD:
            print(f"[gdelt] '{keyword}' 스킵 - 학습형 스킵 목록 등재됨 "
                  f"({learned_entry.get('fail_count')}회 연속 ValueError 확인, "
                  f"{SKIP_STATE_PATH} 참고)")
            continue
        active_keywords.append(keyword)

    all_articles = []
    timeline_by_keyword = {}

    if not active_keywords:
        return all_articles, timeline_by_keyword

    # --- 1단계: 배치 단위 수집 + 크라우딩 감지 ---
    batches = [active_keywords[i:i + BATCH_SIZE] for i in range(0, len(active_keywords), BATCH_SIZE)]
    pending_individual: list[str] = []  # 크라우딩/상한 근접으로 밀려나 개별 확인이 필요한 키워드
    pending_batches: list[list[str]] = []  # 배치 요청 자체가 실패해서 배치로 재시도할 것들

    for batch in batches:
        if len(batch) == 1:
            # 배치 크기 1은 그냥 개별 요청과 같으므로 굳이 OR로 묶지 않고 바로 개별 처리 대상으로 보냄
            pending_individual.append(batch[0])
            continue

        success, batch_articles = _collect_articles_for_keywords(gd, batch)
        if not success:
            # 배치 요청 자체가 실패하면(429 등) 바로 개별 요청(5배 요청)으로
            # 전환하지 않는다 - 429는 쿼리 복잡도가 아니라 "요청을 얼마나
            # 자주 보내는지"에 걸리는 것으로 추정돼서(GDELT가 공식적으로
            # 밝힌 기준은 아니지만 관측 패턴과 일치), 실패했다고 요청 수를
            # 5배로 늘리면 상황을 악화시킬 뿐이다. 대신 같은 배치를 그대로
            # pending_batches에 담아 두고, 아래에서 배치 단위로 재시도한다 -
            # 정말 "250건 상한을 넘어서" 문제가 되는 경우(크라우딩)만 여전히
            # 개별로 격리한다(바로 아래 크라우딩 분기는 그대로 유지).
            print(f"[gdelt] 배치 {batch} 요청 실패 - 같은 배치로 재시도 예정 "
                  f"(개별 전환 아님)")
            pending_batches.append(batch)
        else:
            all_articles.extend(batch_articles)
            crowders = _detect_crowded_keywords(batch_articles, batch)
            if crowders:
                others = [kw for kw in batch if kw not in crowders]
                print(f"[gdelt] 배치 {batch} 내 크라우딩 감지({crowders}가 결과의 "
                      f"{int(CROWDING_SHARE_THRESHOLD * 100)}% 이상 차지 추정) - "
                      f"나머지 키워드 {others} 개별 재요청으로 보충 예정")
                pending_individual.extend(others)
            elif len(batch_articles) >= MAX_RECORDS * CROWDING_CAP_TRIGGER_RATIO:
                # 특정 키워드 하나가 독차지한 건 아니지만(그래서
                # _detect_crowded_keywords는 아무도 못 잡음), 배치 결과가 상한
                # 근처까지 찼다는 사실 자체는 여전히 위험 신호다 - 여러 키워드가
                # "골고루" 상한에 밀려서 다들 조금씩 손실을 봤을 수 있는데, 이
                # 경우 제목 기준 근사치 비율로는 아무도 40%(CROWDING_SHARE_THRESHOLD)를
                # 못 넘어서 단순 비율 판정만으로는 "크라우딩 없음"으로 오판할 수 있다.
                # 특정 원인 키워드를 지목할 수 없으므로, 안전하게 배치 전체를
                # 개별 재요청 대상으로 보충한다.
                print(f"[gdelt] 배치 {batch} 결과가 상한 근처까지 참({len(batch_articles)}건) - "
                      f"골고루 밀렸을 위험 있어 배치 전체 개별 재요청으로 보충 예정")
                pending_individual.extend(batch)
        time.sleep(REQUEST_INTERVAL)

    # --- 1-보조단계: 배치 요청 자체가 실패한 것들을 "배치 그대로" 재시도 ---
    # 크라우딩(상한 초과류 문제)과 달리 이건 단순히 그 순간 요청이 안 됐던
    # 것뿐이라, 개별로 쪼개서 요청 수를 5배로 늘리는 대신 같은 배치로 다시
    # 시도한다. 여기서도 계속 실패하면(OUTER_RETRY_PASSES를 다 써도 안
    # 되면) 마지막 안전망으로만 개별 전환한다 - 무한정 배치로만 매달리다
    # 데이터를 아예 못 건지는 것보다는 낫다는 판단.
    # pending_batches는 이미 1단계에서 1차 시도가 끝난 뒤 실패한 것들이라,
    # 여기서는 재시도 라운드를 1부터 센다(0부터 다시 세면 원래 개별 재시도
    # 섹션(아래)의 "OUTER_RETRY_PASSES+1번 시도"와 총 시도 횟수 기준이
    # 어긋남 - 개별 섹션은 round_keywords가 "아직 한 번도 개별로 안 뚫려본
    # 것"부터 세는 반면, 여기는 이미 1회 시도가 끝난 것부터 세는 차이).
    batch_round = pending_batches
    for round_num in range(1, OUTER_RETRY_PASSES + 1):
        if not batch_round:
            break
        print(f"[gdelt] --- 배치 재시도 라운드 {round_num}/{OUTER_RETRY_PASSES} - "
              f"이전 라운드 실패 배치 {len(batch_round)}개 ---")
        print(f"[gdelt] 라운드 간 안전 대기 {OUTER_RETRY_WAIT_SECONDS}초")
        time.sleep(OUTER_RETRY_WAIT_SECONDS)

        still_failed_batches = []
        for batch in batch_round:
            success, batch_articles = _collect_articles_for_keywords(gd, batch)
            if success:
                all_articles.extend(batch_articles)
                # 재시도로 살아난 배치도 똑같이 크라우딩 체크 (일관성 유지)
                crowders = _detect_crowded_keywords(batch_articles, batch)
                if crowders:
                    others = [kw for kw in batch if kw not in crowders]
                    pending_individual.extend(others)
                elif len(batch_articles) >= MAX_RECORDS * CROWDING_CAP_TRIGGER_RATIO:
                    pending_individual.extend(batch)
            else:
                still_failed_batches.append(batch)
            time.sleep(REQUEST_INTERVAL)
        batch_round = still_failed_batches

    if batch_round:
        # 이 경로는 위 "배치 내 크라우딩 감지"/"상한 근처까지 참"과는 다른
        # 상황이다 - 그쪽은 요청 자체는 성공했는데 결과가 상한에 밀렸을
        # 위험이 있어 개별로 보강하는 것이고, 여기는 배치 요청 자체가
        # 여러 번(1차 + 재시도 라운드) 계속 실패해서 더 이상 배치로는
        # 시도할 수단이 없어 마지막 수단으로 키워드 단위로 쪼개는 것이다.
        print(f"[gdelt] 배치 재시도 {OUTER_RETRY_PASSES}회 소진 - 개별 요청 전환: {batch_round}")
        for batch in batch_round:
            pending_individual.extend(batch)

    # --- 2단계: 개별 보충 요청 - 실패한 것만 외부 재시도 라운드 ---
    round_keywords = list(dict.fromkeys(pending_individual))  # 순서 유지하며 중복 제거
    failed_keywords: list[str] = []
    # 키워드별 마지막 실패 사유(예외 타입+메시지) - 라운드를 거듭할수록
    # 최신 시도의 사유로 덮어써진다. "최종 실패 키워드" 로그에 원인까지
    # 같이 남겨서, 429(서버 혼잡) 때문인지 다른 이유(네트워크 오류, 예상
    # 못 한 예외 등)인지 운영자가 로그만 보고 구분할 수 있게 한다.
    failure_reasons: dict[str, str] = {}

    for round_num in range(OUTER_RETRY_PASSES + 1):
        if round_num > 0:
            if not failed_keywords:
                break  # 지난 라운드에서 실패한 키워드가 없으면 더 돌 필요 없음
            print(f"[gdelt] --- 기사 수집 외부 재시도 라운드 {round_num}/{OUTER_RETRY_PASSES} - "
                  f"이전 라운드 실패 키워드 {len(failed_keywords)}개: {failed_keywords} ---")
            print(f"[gdelt] 라운드 간 안전 대기 {OUTER_RETRY_WAIT_SECONDS}초")
            time.sleep(OUTER_RETRY_WAIT_SECONDS)
            round_keywords = failed_keywords

        if not round_keywords:
            break

        failed_keywords = []

        for keyword in round_keywords:
            success, keyword_articles, reason = _collect_articles_for_keyword(gd, keyword)
            if success:
                all_articles.extend(keyword_articles)
                failure_reasons.pop(keyword, None)  # 재시도로 살아났으면 이전 실패 기록 제거
            else:
                failed_keywords.append(keyword)
                failure_reasons[keyword] = reason or "사유 불명"
            time.sleep(REQUEST_INTERVAL)

    if failed_keywords:
        detail = ", ".join(f"{kw} ({failure_reasons.get(kw, '사유 불명')})" for kw in failed_keywords)
        print(f"[gdelt] 최종 실패 키워드 (총 {OUTER_RETRY_PASSES + 1}회 시도 후에도 실패, "
              f"기사 0건으로 처리됨): {detail}")

    # --- 시계열 수집 완전 제거 결정 ---
    # timelinevol 단독(키워드 5개) 실전 규모 테스트에서도 429 백오프 4단계를
    # 다 쓰고 실패하는 사례가 나옴 - 시계열은 원래도 무거운 편이었는데
    # 8일/일 단위 해상도(TIMELINE_TIMESPAN)로 완화를 시도했음에도 여전히
    # 불안정하다고 판단해 최종적으로 제거함. 저장 레이어가 이 데이터를
    # 애초에 아무 데도 안 쓰고 있었던 것도 제거 결정에 힘을 실음(안 쓰는
    # 기능에 429/런타임 부담을 계속 감수할 이유가 없음).
    #
    # _collect_timeline_for_keyword 함수 자체는 코드에 남겨둔다(아래 참고) -
    # 필요시 이 블록에서 다시 호출하기만 하면 복원 가능.

    # 이번 실행에서 ValueError로 실패한 키워드들의 학습 상태를 파일에
    # 반영(2번 연속되면 다음 실행부터 자동 스킵). git commit/push는
    # main.py/run-pipline.yml 쪽 책임 - 여기는 파일 생성까지만.
    _update_skip_state_after_run()

    return all_articles, timeline_by_keyword


def _print_distribution(articles: list[dict]) -> None:
    """
    진단용 전용 함수.

    목적: "영어 키워드만으로 중국/유럽 기사가 실제로 얼마나 잡히는지"를 확인하기 위한 진단.
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
    # 시계열 수집을 완전히 제거해서(collect() 자체가 항상 timeline={}를
    # 반환) 여기서도 시계열 관련 출력을 뺐다.
    articles, timeline = collect()
    print(f"\n총 {len(articles)}건 기사 수집 완료")
    for a in articles[:3]:
        print(a)

    _print_distribution(articles)