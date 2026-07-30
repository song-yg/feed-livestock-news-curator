"""
llm_summarizer.py
(A) 자체 요약 생성 + (A-1) 얇은 재료 fallback 담당 모듈. (B) 그룹핑
보조는 issue_grouper.stage3_llm_assist가 담당)

** 프로바이더 설정을 issue_grouper.py에서 그대로 재사용 **
LLM_PROVIDER/모델명/API URL/X-Title 상수를 새로 정의하지 않고
issue_grouper.py에서 import해서 쓴다. 이 설정값들 자체에서 실제로 버그를
겪은 적이 있어서(① GitHub Actions에서 미등록 Variable을 참조하면 "안
넘어옴"이 아니라 "빈 문자열"로 넘어와서 os.environ.get(key, default)가
기본값을 못 쓰는 문제, ② X-Title 헤더에 한글을 넣었다가 HTTP 헤더는
latin-1만 허용돼 UnicodeEncodeError), 이미 고쳐서 검증까지 끝난 값을
그대로 재사용하면 같은 버그가 재현되는 걸 원천적으로 막을 수 있다
(issue_grouper.py 쪽 코드는 이 모듈이 전혀 건드리지 않음).

** (A)/(A-1) 안전장치 **
API 키가 없거나 LLM 호출/응답이 실패해도 요약을 생략하고 원문 제목만
노출하는 쪽으로 fallback한다 - 3차와 동일한 철학, 요약 없이 원문 링크만
발송하는 무료 모델 대응책과도 일치한다.
"""

import os
import re
import time

import requests
import trafilatura
from trafilatura.settings import use_config as _trafilatura_use_config

import issue_grouper as _ig  # LLM_PROVIDER, 모델명, API URL, X-Title 상수 재사용 (위 docstring 참고)


_SYSTEM_PROMPT = (
    "너는 사료·축산업 뉴스 큐레이션 서비스의 요약 작성자다. 주어진 이슈(같은 "
    "사건을 다루는 기사 제목들과 참고 정보)를 보고 한국어로 2~3문장의 자체 "
    "요약을 작성하라. 원문을 그대로 옮기지 말고 핵심 내용만 새로 요약한다. "
    "확실하지 않은 수치나 사실은 "
    "임의로 만들어내지 말고, 주어진 제목/참고 정보에 있는 내용만 사용한다. "
    "확실하지 않으면 요약 대신 애매함을 그대로 표현하라. 전문용어·질병명·"
    "정부기관명·제도명 등 고유명사는 임의로 한글화하지 말고 영문 원어가 "
    "있으면 괄호로 병기한다. "
    "그리고 맨 첫 줄에 'TITLE: '로 시작해서, 주어진 기사 제목을 그대로 "
    "베끼지 말고 이슈 내용을 종합한 새 한국어 헤드라인을 직접 작성해 "
    "적어라(원문 기사 제목이 외국어라도 한국어로 작성한다) - 뉴스 헤드라인 "
    "답게 간결하고 전문적인 문체로 쓴다. 그 다음 줄부터 요약 문장을 이어서 "
    "작성한다. 이 형식 외에 다른 설명은 출력하지 않는다."
)

# --- 대표 제목을 LLM이 새로 쓰게 함 (2026-07-30, 담당자 요청) ---
#
# 기존엔 그룹의 titles[0](수집 순서상 그냥 첫 번째 기사 제목)을 그대로
# 대표 제목으로 썼는데, 29건이 묶인 그룹이어도 그중 어느 게 헤드라인으로
# 제일 적절한지는 전혀 안 보고 그냥 맨 처음 걸 썼다 - "제목이 전문성
# 떨어져 보인다"는 지적의 원인. 그래서 요약과 같은 LLM 호출 안에서
# "TITLE: " 접두사로 새 헤드라인도 같이 받는다(_split_generated_title로
# 분리). 별도 호출을 안 만든 이유는 llm_summarizer 전체가 이미 429(무료
# 티어 한도) 대응에 민감해서 - 다만 이제는 유료 결제로 해결됐다고 담당자가
# 확인함.
#
# 예전엔 해외(is_international) 이슈에서만 "번역" 지시문을 조건부로
# 붙였는데, 이번엔 "해외 기사 제목은 원문 그대로 안 남긴다"는 결정과
# 맞물려서 국내/해외 구분 없이 항상 새 헤드라인을 쓰게 함 - 해외 이슈는
# 자연스럽게 "번역 + 헤드라인화"가 한 번에 되고, 원문 영어 제목은 이제
# 아예 화면에 노출되지 않는다(deploy.py 참고).
_TITLE_PATTERN = re.compile(r"\s*TITLE:\s*(.+?)\s*\n(.*)", re.DOTALL)


def _split_generated_title(text: str) -> tuple[str | None, str]:
    """
    LLM 응답에서 첫 줄의 'TITLE: ...'를 분리해 (새 헤드라인, 나머지 요약
    텍스트) 형태로 돌려준다. 형식을 안 지켰으면(모델이 지시를 무시한 경우
    등) (None, 원본 텍스트 그대로)를 돌려줘서 헤드라인 생성만 실패해도
    요약까지 잃지 않게 한다 - JSON처럼 엄격하게 형식을 강제하지 않는
    관대한 파싱. 호출부는 헤드라인이 None이면 기존 titles[0]로 fallback
    한다(deploy.py 참고) - 아주 드물게라도 화면에 뭔가는 떠야 하므로.
    """
    m = _TITLE_PATTERN.match(text)
    if not m:
        return None, text.strip()
    return m.group(1).strip(), m.group(2).strip()


# --- 대용량 그룹 2단계(배치) 요약 (2026-07-30, 담당자 요청) ---
#
# "그룹 내 모든 기사를 다 LLM에 보내고 싶다"는 요청 + 기사 개수 제한을
# 없애면서, 개수가 아주 많은 그룹(예: 29건)은 한 번의 프롬프트에 다
# 욱여넣으면 무료 모델들의 컨텍스트 윈도우 제한에 걸리거나 처리 시간이
# 급격히 늘어날 위험이 있다. 그래서 _LARGE_GROUP_THRESHOLD를 넘는
# 그룹만 예외적으로 "배치별 사실 추출 -> 그 추출 결과들을 모아 최종
# 요약+헤드라인 합성"의 2단계로 처리한다(그 이하는 기존처럼 1회 호출).
#
# 임계값을 15로 잡은 이유: 담당자 요청으로 이 함수가 다루는 범위(1~15건)
# 안에서는 기사 개수 제한 없이 다 넣어도 프롬프트가 감당 가능한 수준이라
# 굳이 배치로 쪼갤 필요가 없고, 그 이상(예: 29건)만 배치 경로로 빠지게
# 해서 "웬만한 그룹은 호출 1번, 정말 큰 그룹만 호출 여러 번"이 되게 함 -
# 무조건 배치로 가면 이제 막 해결한 429(무료 티어 한도) 문제를 다시
# 키울 수 있어서, 정말 필요한 경우로 좁힘.
_LARGE_GROUP_THRESHOLD = 15
_BATCH_SIZE = 6  # 배치 하나당 기사 수 - 29건이면 배치 5개

_BATCH_EXTRACT_SYSTEM_PROMPT = (
    "너는 뉴스 기사 여러 건에서 핵심 사실만 추출하는 역할이다. 주어진 기사 "
    "제목/참고 정보들을 보고, 겹치지 않는 핵심 사실만 불릿 3~5개로 뽑아라. "
    "해석이나 의견, 요약 문장을 쓰지 말고 사실 나열만 하고, 확실하지 않은 "
    "내용은 만들어내지 말라. 각 불릿은 한 줄로, 다른 설명 없이 불릿만 "
    "출력한다."
)

# 그룹 하나에 기사가 아주 많을 때(예: 50건 이상) 제목을 전부 프롬프트에
# 넣으면 비용/속도 낭비가 크므로 상한을 둔다 - 나머지는 "외 N건 생략"으로 표시.
# 참고 컨텍스트(본문 발췌) 길이 상한. 기사 개수 자체는 더 이상 안 자르지만
# (2026-07-30, 아래 _build_user_prompt 참고), 기사 1건당 발췌 길이는
# 여전히 잘라야 프롬프트가 무한정 커지는 걸 막을 수 있어 유지 - 300자 ->
# 600자로 상향(본문 있는 기사는 맥락을 조금 더 살리되, 무제한은 아님).
_BODY_EXCERPT_CHARS = 600

# --- 재료 부족 기사 본문 추가 수집 (2026-07-28, 담당자 요청 + 같은 날 확장) ---
#
# 네이버/GDELT는 본문을 아예 못 가져와서(주요 문장/메타데이터만) "재료
# 부족"인 경우가 대부분이었다. 처음엔 단독 기사(그룹 크기 1)에서만
# 시도했는데, 네이버는 "주요 문장"만 주는 짧은 스니펫이라 같은 이슈를
# 네이버 기사 여러 건이 다뤄서 그룹이 됐어도(예: 3건) 그룹 전체를
# 통틀어 재료가 다 얇은 경우가 실제로 있어서(담당자 확인), 그룹 크기와
# 무관하게 재료 부족이면 시도하도록 확장함. WATT처럼 사이트 구조를
# 하드코딩한 전용 스크레이퍼를 만들 수도 있지만, 네이버/GDELT는 매번 다른
# 언론사 사이트로 연결되기 때문에(WATT는 사이트 하나 고정) 사이트마다
# 전용 스크레이퍼를 만드는 건 현실적이지 않다고 판단.
#
# 대신 trafilatura(범용 본문 추출 라이브러리)로 "일단 시도라도" 해본다.
# 사이트별 맞춤 셀렉터 없이 페이지에서 본문으로 보이는 영역을 추측해서
# 뽑아주는 방식이라 성공률은 사이트마다 들쭉날쭉하다(광고/관련기사를
# 본문으로 착각하거나, JS로 내용을 그리는 사이트는 애초에 못 뽑음 -
# trafilatura는 순수 HTTP 요청 방식이라 WATT가 403 먹혀서 Playwright로
# 바꿨던 것과 같은 부류의 사이트는 여기서도 막힐 수 있음). 실패해도
# 기존처럼 "재료 부족 -> 요약 생략(단독 기사) 또는 있는 재료로 진행(그룹)"
# 으로 안전하게 fallback하니 지금보다 나빠지는 경우는 없음 - 담당자도 이
# 트레이드오프를 감수하기로 결정함(전용 스크레이퍼 vs 범용 추출 중 범용
# 선택).
#
# --- 그룹당 최대 20건으로 확장 (2026-07-30, 담당자 요청) ---
#
# 기존엔 "그룹 전체를 통틀어 재료 충분한 기사가 하나도 없을 때만" 대표
# 기사(articles[0]) 1건만 보강했다. 그 사이 _build_user_prompt가 그룹
# 내 모든 기사의 본문/설명을 다 프롬프트에 반영하도록 이미 바뀌어서
# (기사 개수 상한 제거, 위 주석 참고), "대표 기사 하나만 본문이 있고
# 나머지는 여전히 재료가 얇은 채로 프롬프트에 들어가는" 불균형이 생겼다.
# 그래서 트리거 조건을 "그룹 전체 상태" 판정에서 "기사 단위" 판정으로
# 바꿨다: 다른 기사가 이미 충분한 재료를 갖고 있어도 무관하게, 그룹 안에서
# 재료가 얇은 기사 각각을 대상으로 보강을 시도한다 - "그룹 전체를 다
# LLM에 보낸다"는 기존 확장 방향과 더 맞는다고 판단(담당자 확인 없이
# 합리적 기본값으로 진행, 문제 있으면 조건 되돌리기 쉬움). 그룹당 최대
# _BODY_FETCH_MAX_ARTICLES_PER_GROUP(20)건까지만 시도해서 호출 폭증을
# 막고, 그 20건은 재료 얇은 기사를 원래 수집/그룹핑 순서 그대로 앞에서부터
# 고른다(이미 Top N으로 추려진 그룹이라 그 안에서 추가로 우열을 가릴
# 근거가 약해 순위를 따로 매기지 않음).
#
# 시간 통제: trafilatura 타임아웃(10초) 기준 최악의 경우 20건 x 10초 =
# 200초/그룹이 걸릴 수 있어, GDELT collector의 TIME_BUDGET_SECONDS
# 패턴을 참고해 그룹 하나당 총 소요 시간 상한
# (_BODY_FETCH_GROUP_TIME_BUDGET_SECONDS)을 별도로 둔다. 예산을 넘기면
# 남은 기사는 건너뛰고 그때까지 확보한 재료로 진행한다 - "부분 성공이
# 완전 포기보다 낫다"는 이 프로젝트 전반의 원칙과 동일.
#
# 비용/부담 통제를 위해, 이미 순위(Top N)로 추려진 기사에 대해서만 시도한다
# (summarize_top_issues 호출 시점엔 이미 main.py의 TOP_N으로 걸러진 상태 -
# 전체 수집분 수백 건에 대해 매번 시도하는 게 아님).
_BODY_FETCH_TIMEOUT_SECONDS = 10
_BODY_FETCH_MIN_LENGTH = 200  # has_substantial_material의 본문 기준과 동일
_BODY_FETCH_MAX_ARTICLES_PER_GROUP = 20  # 그룹당 최대 보강 시도 건수
_BODY_FETCH_GROUP_TIME_BUDGET_SECONDS = 60  # 그룹 하나당 보강 시도 총 소요 시간 상한(초)

# trafilatura 기본 타임아웃(30초)은 Top N 몇 건에 대해서만 시도한다 해도
# 사이트가 응답 없이 매달리면 그만큼 전체 실행이 늘어질 수 있어, 위
# 상수값으로 짧게 맞춰둔다.
_TRAFILATURA_CONFIG = _trafilatura_use_config()
_TRAFILATURA_CONFIG.set("DEFAULT", "DOWNLOAD_TIMEOUT", str(_BODY_FETCH_TIMEOUT_SECONDS))


def _build_user_prompt(item: dict) -> str:
    """
    이슈 하나(scorer.score_group() 결과 dict - titles/urls/press_list/articles
    필드를 가짐)를 LLM 입력 프롬프트로 만든다.

    제목 + (본문 확보된 경우) 본문에서
    뽑은 핵심 문장 + (네이버 소스인 경우) description을 참고 컨텍스트로
    추가한다(그대로 인용하지 않고 참고용으로만 사용).

    ** 기사 개수 상한 제거 (2026-07-30) **: 이 함수는 그룹 크기가
    _LARGE_GROUP_THRESHOLD(15) 이하일 때만 호출된다(그 이상은
    summarize_issue가 _summarize_large_group의 배치 경로로 보냄) - 이
    범위 안에서는 "그룹 내 모든 기사를 다 LLM에 보내고 싶다"는 요청대로
    제목/기사 모두 자르지 않고 전부 포함한다. 기사 1건당 발췌 길이만
    _BODY_EXCERPT_CHARS로 제한(무제한으로 이어붙이면 프롬프트가
    쓸데없이 커짐).
    """
    titles = item.get("titles", [])
    lines = ["다음은 같은 이슈를 다룬 기사 제목들이다:"]
    for title in titles:
        lines.append(f"- {title}")

    context_lines = []
    for article in item.get("articles", []):
        source = article.get("source", "?")
        body = article.get("body")
        if body:
            context_lines.append(f"[{source}] 본문 일부: {body[:_BODY_EXCERPT_CHARS]}")
        description = article.get("description")
        if description:
            context_lines.append(f"[{source}] 설명: {description}")

    if context_lines:
        lines.append("\n참고 정보(그대로 인용하지 말고 참고만 할 것):")
        lines.extend(context_lines)

    lines.append("\n위 내용을 바탕으로 한국어 2~3문장 자체 요약을 작성하라.")
    return "\n".join(lines)


def _request_openrouter(system_prompt: str, user_prompt: str, api_key: str,
                         session: requests.Session, model_name: str) -> tuple[str, dict]:
    """반환값: (응답 텍스트, 원본 응답 dict) - dict는 실패 시 로그에 남기기 위함."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "content-type": "application/json",
        # ASCII 전용이어야 함 - HTTP 헤더는 latin-1만 허용되므로
        # 한글이 섞이면 UnicodeEncodeError. issue_grouper의
        # 검증된 상수를 그대로 재사용
        "X-Title": _ig._OPENROUTER_X_TITLE,
    }
    body = {
        "model": model_name,
        "temperature": 0.3,  # 자연어 생성이라 3차(판정, temperature=0)보다는 살짝 여유
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    resp = session.post(_ig.LLM_API_URL_OPENROUTER, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip(), data


def _request_anthropic(system_prompt: str, user_prompt: str, api_key: str, session: requests.Session) -> tuple[str, dict]:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": _ig.LLM_MODEL_ANTHROPIC,
        "max_tokens": 300,
        "temperature": 0.3,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    resp = session.post(_ig.LLM_API_URL_ANTHROPIC, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    ).strip()
    return text, data


def _call_llm(system_prompt: str, user_prompt: str, api_key: str, session: requests.Session) -> str | None:
    """
    issue_grouper.py의 프로바이더 설정을 재사용해 LLM을 한 번 호출하고 응답
    텍스트를 반환한다. 실패 시 None (호출부가 "요약 생략, 원문 제목만"으로
    fallback).

    3차(_call_llm, issue_grouper.py)와 요청 형식은 거의 같지만, 3차는 JSON
    배열을 기대하는 반면 이건 자연어 문장 하나를 기대한다는 점이 달라서
    별도 함수로 둔다 - 파싱 방식이 다른데 억지로 하나로 합치면 오히려
    코드가 더 헷갈림. temperature/max_tokens도 요약 생성 용도에 맞게
    다르게 줘야 해서(자연어 생성이라 판정보다 살짝 여유), issue_grouper의
    요청 헬퍼를 그대로 재사용하지 않고 이 파일에 따로 둔다.

    session: summarize_top_issues가 이슈마다 반복 호출하므로, 세션을
    재사용해 커넥션 오버헤드를 줄인다(relevance_filter.py/issue_grouper.py
    와 동일한 방식).

    ** 지정 모델 실패 시 다음 후보 모델로 자동 재시도 **
    openrouter인 경우 _ig._LLM_MODEL_CHAIN_OPENROUTER_ROLES(1순위 -> 2순위 ->
    3순위 -> 최종 안전망(openrouter/free), issue_grouper.py 상수 선언부
    참고)를 순서대로 시도한다. 앞 모델이 실패하면(이름 오타, 무료 티어에서
    빠짐 등) 다음 후보로 자동 재시도 - 특정 모델 하나에 고정한 설정이 그
    모델만의 문제 때문에 이번 실행의 요약 기능 전체를 막는 걸 방지. 마지막
    "최종 안전망"까지 실패해야 최종 실패로 처리.

    ** 오류 코드를 역할(순위)별로 고정해서 분리한 이유(2026-07-28) **:
    issue_grouper.py/relevance_filter.py와 동일 - LS-02(1순위)/LS-03(2순위)/
    LS-04(3순위)/LS-05(최종 안전망)로 분리해서 어느 순위가 실패했는지 로그
    grep만으로 구분 가능. 최종 실패는 아래 바깥 except가 기존처럼 LS-01로
    한 번 더 종합 로그를 남김(이건 "요약 자체가 결국 실패했다"는 상위
    레벨 신호, LS-05는 "그 원인이 최종 안전망까지 다 막혀서였다"는 하위
    레벨 신호 - 역할이 다름).
    """
    data = None  # 응답 자체를 못 받았을 수도 있으니 미리 초기화(로그에서 안전하게 참조용)
    try:
        if _ig.LLM_PROVIDER == "openrouter":
            chain = _ig._LLM_MODEL_CHAIN_OPENROUTER_ROLES
            role_codes = {"1순위": "LS-02", "2순위": "LS-03", "3순위": "LS-04", "최종 안전망": "LS-05"}
            last_error: Exception | None = None
            for idx, (role, model_name) in enumerate(chain):
                try:
                    if idx > 0:
                        print(f"[llm_summarizer] 🟡 주의 - 요약 생성 {role} 모델('{model_name}')로 재시도 "
                              f"({idx + 1}/{len(chain)})")
                    text, data = _request_openrouter(system_prompt, user_prompt, api_key, session, model_name)
                    return text
                except Exception as e:
                    last_error = e
                    code = role_codes[role]
                    is_final = idx == len(chain) - 1
                    level = "🔴 조치필요" if is_final else "🟡 주의"
                    next_note = "더 시도할 모델 없음" if is_final else "다음 후보 모델로 재시도"
                    print(f"[llm_summarizer] {level} [{code}] - 요약 생성 {role} 모델('{model_name}') "
                          f"호출 실패 - {next_note}: {type(e).__name__} - {e!r}")
            raise last_error
        else:
            text, data = _request_anthropic(system_prompt, user_prompt, api_key, session)
            return text
    except Exception as e:
        # data가 있으면(HTTP 호출 자체는 성공했는데 그 안에서 예상한 필드를
        # 못 찾은 경우, 예: API 응답 스키마 변경) 실제로 뭘 받았는지 잘라서
        # 같이 남긴다 - relevance_filter.py/issue_grouper.py와 동일한 이유.
        snippet = (" ".join(str(data).split())[:200] + "...") if data is not None else "(응답을 아예 못 받음 - 요청/인증 단계에서 실패)"
        print(f"[llm_summarizer] 🔴 조치필요 [LS-01] - LLM({_ig.LLM_PROVIDER}) 호출 실패: {type(e).__name__} - {e!r} "
              f"| 실제 응답: {snippet}")
        return None


def _build_batch_extract_prompt(batch_articles: list[dict]) -> str:
    """
    대용량 그룹의 배치 하나(_BATCH_SIZE건)를 사실 추출용 프롬프트로 만든다.
    _build_user_prompt와 달리 요약 대신 "사실만 나열"을 요청하는 용도라
    별도 함수로 둠(_summarize_large_group 1단계에서 사용).
    """
    lines = ["다음은 하나의 이슈를 다룬 기사들 중 일부(배치)이다:"]
    for article in batch_articles:
        title = article.get("title") or "(제목 없음)"
        source = article.get("source", "?")
        lines.append(f"- [{source}] {title}")
        body = article.get("body")
        if body:
            lines.append(f"  본문 일부: {body[:_BODY_EXCERPT_CHARS]}")
        description = article.get("description")
        if description:
            lines.append(f"  설명: {description}")
    lines.append("\n위 기사들에서 겹치지 않는 핵심 사실만 불릿 3~5개로 뽑아라.")
    return "\n".join(lines)


def _build_final_synthesis_prompt(item: dict, batch_facts: list[str]) -> str:
    """
    _summarize_large_group 1단계(배치별 사실 추출)의 결과들을 모아 최종
    요약+헤드라인 생성용 프롬프트로 만든다. 대표 제목 몇 개만 참고용으로
    곁들이고(전체 제목 나열은 이미 배치 단계에서 다 반영됨), 핵심은 배치
    사실 목록.
    """
    titles = item.get("titles", [])
    rep_titles = titles[:5]
    lines = ["다음은 같은 이슈를 다룬 기사 제목 일부(참고용):"]
    for t in rep_titles:
        lines.append(f"- {t}")
    if len(titles) > len(rep_titles):
        lines.append(f"(총 {len(titles)}건 중 일부만 표시, 전체 내용은 아래 사실 요약에 반영됨)")

    lines.append(f"\n아래는 전체 기사(총 {len(item.get('articles', []))}건)를 배치로 나눠 뽑은 핵심 사실 목록이다:")
    for i, facts in enumerate(batch_facts, start=1):
        lines.append(f"[배치 {i}]\n{facts}")

    lines.append("\n위 내용을 종합해서 한국어 2~3문장 자체 요약을 작성하라.")
    return "\n".join(lines)


def _summarize_large_group(item_for_prompt: dict, api_key: str, session: requests.Session) -> str | None:
    """
    그룹 크기가 _LARGE_GROUP_THRESHOLD를 넘을 때만 쓰이는 2단계 요약
    (2026-07-30, 담당자 요청 - "요약을 두 번 하자: 배치로 나눠서 요약하고,
    그 요약들을 모아 최종본을 만들자"). 기사를 _BATCH_SIZE개씩 나눠
    배치별로 핵심 사실만 뽑고(_build_batch_extract_prompt), 그 결과들을
    모아 최종 요약+헤드라인을 한 번 더 생성한다(_build_final_synthesis_prompt).

    배치 하나가 실패해도(호출 실패 등) 나머지 배치 결과만으로 최종 합성을
    진행한다(LS-07 로그) - "부분 정보로라도 만드는 게 완전 포기보다 낫다"는
    이 프로젝트 전반의 원칙과 같은 방향. 배치가 전부 실패하면 None을
    반환해(LS-08) 호출부가 기존처럼 "요약 생략"으로 처리하게 한다.

    담당자가 429(무료 티어 한도)를 유료 결제로 해결했다고 확인해서, 배치
    호출이 여러 번 늘어나는 것 자체에 대한 우려는 지금은 크지 않음 - 그래도
    혹시 모를 상황을 대비해 부분 실패 허용 원칙은 유지.
    """
    articles = item_for_prompt.get("articles", [])
    batches = [articles[i:i + _BATCH_SIZE] for i in range(0, len(articles), _BATCH_SIZE)]

    batch_facts = []
    for batch_idx, batch in enumerate(batches, start=1):
        prompt = _build_batch_extract_prompt(batch)
        text = _call_llm(_BATCH_EXTRACT_SYSTEM_PROMPT, prompt, api_key, session)
        if text:
            batch_facts.append(text)
        else:
            print(f"[llm_summarizer] 🟡 주의 [LS-07] - 대용량 그룹({len(articles)}건) 배치 {batch_idx}/{len(batches)} "
                  f"사실 추출 실패 - 이 배치는 제외하고 나머지로 계속 진행")

    if not batch_facts:
        print(f"[llm_summarizer] 🔴 조치필요 [LS-08] - 대용량 그룹({len(articles)}건) 배치 전부 실패 - 요약 생략")
        return None

    final_prompt = _build_final_synthesis_prompt(item_for_prompt, batch_facts)
    return _call_llm(_SYSTEM_PROMPT, final_prompt, api_key, session)


def _is_suspicious_summary(text: str) -> bool:
    """
    오픈라우터 무료 라우터(openrouter/free)로 요약을 생성하면, 정상 요약
    대신 콘텐츠 안전성 판정 결과로 보이는 텍스트("User Safety: safe")를
    그대로 반환하는 경우가 있다. 정확한 원인은 미확인(오픈라우터 무료
    라우터가 요청마다 다른 실제 모델로 라우팅될 수 있어, 그중 일부가
    콘텐츠 안전성 필터 응답을 요약 대신 반환하는 것으로 추정할 뿐).

    "확실히 요약이 아니라고 판단할 수 있는 좁은 패턴만" 걸러낸다 - 과도하게
    넓히면 정상 요약도 걸러질 위험이 있어, 실제로 관측된 문구("user
    safety")만 좁게 대응한다. 품질이 낮거나 짧은 요약까지 거르는 건 범위 밖.
    """
    return "user safety" in text.lower()


def _fetch_body_via_trafilatura(url: str) -> str | None:
    """
    주어진 URL에서 범용으로 본문을 추출 시도한다. 사이트별 맞춤 셀렉터가
    없어서 성공률은 사이트마다 다르다 - 위 상수 선언부의 트레이드오프
    설명 참고. 실패(다운로드 실패/추출 실패/타임아웃/예외)하면 조용히
    None을 반환한다 - 호출부가 기존 "재료 부족" 경로로 안전하게 흡수한다.
    """
    try:
        downloaded = trafilatura.fetch_url(url, no_ssl=True, config=_TRAFILATURA_CONFIG)
    except Exception:
        return None
    if not downloaded:
        return None
    try:
        extracted = trafilatura.extract(downloaded, favor_precision=True, include_comments=False,
                                         include_tables=False)
    except Exception:
        return None
    return extracted or None


def summarize_issue(item: dict, session: requests.Session | None = None) -> dict:
    """
    이슈 하나(scorer.score_group() 결과 dict)에 (A)/(A-1) 로직을 적용해
    요약을 붙인다. 원본 item은 변경하지 않고 얕은 복사본을 반환한다
    (호출부가 리스트를 여러 번 다룰 수 있어 부작용 없는 편이 안전).

    session: 안 넘기면(예: 이 함수를 단독으로 부를 때) 호출 하나짜리 임시
    세션을 만들어 안전하게 동작 - summarize_top_issues처럼 여러 건을
    반복 처리할 때만 세션을 만들어 넘겨주면 재사용 이득이 있다.

    반환값에 추가되는 필드:
      summary: LLM이 생성한 2~3문장 요약, 또는 None(요약 생략된 경우)
      summary_skipped_reason: 요약을 생략한 이유. 정상 요약됐으면 None.
      generated_title: 요약이 성공했을 때 LLM이 같이 지어준 새 헤드라인
        (2026-07-30, titles[0]을 그냥 쓰는 대신 그룹 전체 내용을 종합해서
        새로 씀 - 국내/해외 구분 없이 항상 시도). 요약 실패/생략이거나
        모델이 형식을 안 지켰으면 None - 호출부(deploy.py)가 이 경우
        titles[0]로 fallback한다.
    """
    result = dict(item)
    result["generated_title"] = None  # 아래에서 요약 성공 + 헤드라인 파싱 성공 시에만 채워짐
    titles = item.get("titles", [])
    articles = item.get("articles", [])
    item_for_prompt = item  # 기본은 원본 그대로 - 본문 추가 수집 성공 시에만 아래서 교체

    # (A-1) 얇은 재료 fallback: 이슈 그룹핑이 안 되고
    # (그룹 크기 1) 언론사 1곳만 보도한 단독 기사면, 실제로 요약할 재료가
    # 없을 때 생략한다 - 재료가 얇아서 생략하는 거지, "단독 기사"라서
    # 생략하는 게 아니다. 그룹(여러 언론사가 같이 보도)은 재료가 얇아도
    # 스킵하지 않고 있는 재료로나마 요약을 시도한다(기존 동작 그대로).
    #
    # ** 재료 부족 판정과 trafilatura 보강 시도는 그룹 크기 무관 (2026-07-28
    # 확장) ** 처음엔 단독 기사(titles==1)에서만 시도했는데, 네이버는
    # "주요 문장"만 주는 짧은 스니펫이라 같은 이슈를 여러 네이버 기사가
    # 다뤄서 그룹이 여러 건이어도(예: 3건) 그룹 전체를 통틀어 재료가
    # 다 얇은 경우가 실제로 있었다(담당자 확인) - "그룹이니까 재료가
    # 충분하다"고 볼 수 없었음. 그래서 재료 충분 여부 판정과 trafilatura
    # 보강 시도는 그룹 크기와 무관하게 항상 하되, "재료 부족하면 아예
    # 스킵"하는 건 여전히 단독 기사(titles==1)에서만 적용한다 - 그룹은
    # 원래도 재료가 얇아도 스킵 안 하던 동작이라 그 부분은 안 건드림.
    # ** 네이버는 description 기준을 적용하지 않음 (2026-07-30) **
    # 원래는 body 200자 또는 description 50자 중 하나만 넘으면 "재료
    # 충분"으로 봤는데, 네이버 "주요 문장"은 한 문장만 잘라줘도 50자는
    # 웬만하면 넘겨서(문장 하나 = 60~100자가 흔함), 실제로는 요약할 재료가
    # 부족한데도 기준을 통과해 trafilatura 보강 시도 자체가 걸리지 않는
    # 경우가 있었다(담당자 확인). 그래서 네이버 소스는 body만 인정하고
    # description 길이는 무시 - body가 없으면(네이버는 원래 body를 안 줌)
    # 무조건 재료 부족으로 보고 아래에서 보강을 시도한다. WATT/GDELT 등
    # 다른 소스는 기존 기준(body 200자 또는 description 50자) 그대로 유지.
    def _article_has_substantial_material(article: dict) -> bool:
        if len(article.get("body") or "") >= _BODY_FETCH_MIN_LENGTH:
            return True
        if article.get("source") == "네이버":
            return False
        return len(article.get("description") or "") >= 50

    has_substantial_material = any(_article_has_substantial_material(a) for a in articles)

    # ** 대표 기사 1건 -> 그룹당 최대 20건으로 확장 (2026-07-30, 담당자
    # 요청) ** 트리거 조건을 "그룹 전체에 재료 충분한 기사가 하나도 없을
    # 때만"에서 "기사 하나하나가 재료 부족이면(다른 기사가 이미 충분해도
    # 무관하게) 보강 시도"로 바꿨다 - 위 상수 선언부 주석 참고. 재료가
    # 얇은 기사만 원래 순서대로 골라 최대 _BODY_FETCH_MAX_ARTICLES_PER_GROUP
    # (20)건까지 시도한다.
    needing_indices = [i for i, a in enumerate(articles) if not _article_has_substantial_material(a)]
    targets = needing_indices[:_BODY_FETCH_MAX_ARTICLES_PER_GROUP]

    if targets:
        label = "단독 기사" if len(titles) == 1 else f"그룹({len(titles)}건)"
        print(f"[llm_summarizer] 🟡 주의 [LS-06] - {label} 재료 부족 기사 {len(targets)}건 "
              f"(최대 {_BODY_FETCH_MAX_ARTICLES_PER_GROUP}건 한도) - 본문 추가 수집 시도")

        # item은 원본이라 직접 안 건드리고, 프롬프트 생성에만 쓸 얕은
        # 복사 리스트를 따로 만든다(함수 docstring의 "원본 미변경" 약속
        # 유지) - 보강 성공한 인덱스만 교체.
        enriched_articles = list(articles)
        attempted = 0
        succeeded = 0
        group_urls = item.get("urls", [])
        start_time = time.monotonic()

        for idx in targets:
            if time.monotonic() - start_time >= _BODY_FETCH_GROUP_TIME_BUDGET_SECONDS:
                remaining = len(targets) - attempted
                print(f"[llm_summarizer] 🟡 주의 - {label} 본문 보강 시간 예산"
                      f"({_BODY_FETCH_GROUP_TIME_BUDGET_SECONDS}초) 초과 - 남은 {remaining}건 "
                      f"건너뛰고 지금까지 확보한 재료로 진행")
                break

            article = articles[idx]
            url = article.get("url") or (group_urls[idx] if idx < len(group_urls) else None)
            if not url:
                continue

            attempted += 1
            fetched_body = _fetch_body_via_trafilatura(url)
            if fetched_body and len(fetched_body) >= _BODY_FETCH_MIN_LENGTH:
                succeeded += 1
                enriched_article = dict(article)
                enriched_article["body"] = fetched_body
                enriched_articles[idx] = enriched_article

        print(f"[llm_summarizer] 본문 추가 수집 결과 [LS-09] - {label} {attempted}건 시도, "
              f"{succeeded}건 성공")

        if succeeded > 0:
            item_for_prompt = dict(item)
            item_for_prompt["articles"] = enriched_articles
            has_substantial_material = any(
                _article_has_substantial_material(a) for a in enriched_articles
            )

    if len(titles) == 1 and not has_substantial_material:
        result["summary"] = None
        result["summary_skipped_reason"] = (
            "단독 기사(이슈 그룹핑 안 됨) - 본문/설명 재료가 얇아 요약 생략, "
            "원문 제목만 노출 (범용 본문 추가 수집도 실패/미시도)"
        )
        return result
    # 재료(본문 등)가 충분하면(원래 있었거나, 방금 보강했거나) 단독
    # 기사여도 아래 정상 요약 경로로 진행. 그룹은 재료가 여전히 부족해도
    # (위에서 skip 안 하므로) 이 시점 이후로 그대로 진행한다.

    key_env_var = "OPENROUTER_API_KEY" if _ig.LLM_PROVIDER == "openrouter" else "ANTHROPIC_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        result["summary"] = None
        result["summary_skipped_reason"] = (
            f"{key_env_var} 없음(LLM_PROVIDER={_ig.LLM_PROVIDER}) - 요약 생략, 원문 제목만 노출"
        )
        return result

    # ** 대용량 그룹은 배치 2단계로 (2026-07-30) **: _LARGE_GROUP_THRESHOLD를
    # 넘으면 _summarize_large_group(위 함수 선언부 참고)으로 보내고, 그
    # 이하는 기존처럼 _build_user_prompt로 만든 프롬프트 1번 호출로 끝낸다.
    total_articles = len(item_for_prompt.get("articles", []))
    if total_articles > _LARGE_GROUP_THRESHOLD:
        print(f"[llm_summarizer] 대용량 그룹({total_articles}건, 임계값 {_LARGE_GROUP_THRESHOLD} 초과) "
              f"- 배치로 나눠 2단계 요약 진행")
        if session is not None:
            summary_text = _summarize_large_group(item_for_prompt, api_key, session)
        else:
            with requests.Session() as temp_session:
                summary_text = _summarize_large_group(item_for_prompt, api_key, temp_session)
    else:
        user_prompt = _build_user_prompt(item_for_prompt)
        if session is not None:
            summary_text = _call_llm(_SYSTEM_PROMPT, user_prompt, api_key, session)
        else:
            with requests.Session() as temp_session:
                summary_text = _call_llm(_SYSTEM_PROMPT, user_prompt, api_key, temp_session)

    if not summary_text:
        result["summary"] = None
        result["summary_skipped_reason"] = "LLM 호출/응답 실패 - 요약 생략, 원문 제목만 노출"
        return result

    generated_title, summary_text = _split_generated_title(summary_text)

    if _is_suspicious_summary(summary_text):
        result["summary"] = None
        result["summary_skipped_reason"] = (
            "LLM 응답이 요약이 아닌 것으로 추정됨(안전성 필터 오작동 등, 원인 미확인) - "
            "본문 요약 생성 실패, 원문 제목만 노출"
        )
        return result

    result["summary"] = summary_text
    result["summary_skipped_reason"] = None
    result["generated_title"] = generated_title
    return result


def summarize_top_issues(ranked_items: list[dict], label: str = "") -> list[dict]:
    """
    scorer.score_and_rank()가 만든 상위 이슈 리스트 전체에 summarize_issue를
    적용한다. main.py의 4단계 호출부에서 국내/해외 각각 부른다.

    이슈 하나당 LLM 호출이 몇 초~몇십 초 걸릴 수 있어(특히 무료 모델은
    느리거나 대기열이 걸릴 수 있음), 전체가 끝난 뒤 한꺼번에 출력하지 않고
    GDELT/WATT collector처럼 항목 하나 처리할 때마다 바로바로 로그를
    찍는다 - 실행 상태를 실시간으로 볼 수 있게 함.
    """
    results = []
    total = len(ranked_items)
    with requests.Session() as session:
        for i, item in enumerate(ranked_items, start=1):
            titles = item.get("titles", [])
            rep_title = titles[0] if titles else "(제목 없음)"
            prefix = f"[llm_summarizer] {label} " if label else "[llm_summarizer] "
            # "요약 요청 중..." 대신 "처리 중..."으로 표현 (2026-07-28) - 이 시점엔
            # 아직 LLM을 호출할지, 재료가 얇아 코드가 곧바로 생략 처리할지
            # 결정되지 않았다(summarize_issue 내부의 (A-1) 재료 부족 체크가
            # LLM 호출보다 먼저 실행됨). "요약 요청 중"이라고 찍으면 마치
            # 매번 LLM을 호출하는 것처럼 보여 오해의 소지가 있었음 - 실제로는
            # 재료 부족/API 키 없음 등으로 LLM을 아예 안 부르고 생략되는
            # 경우가 흔함(바로 아래 결과 로그에서 "요약 완료"인지
            # "요약 생략"인지, 그리고 생략이면 왜 생략됐는지 사유가 남는다).
            print(f"{prefix}({i}/{total}) '{rep_title}' (그룹 {len(titles)}건) - 처리 중...")

            result = summarize_issue(item, session)

            if result.get("summary"):
                print(f"{prefix}({i}/{total}) 요약 완료")
            else:
                print(f"{prefix}({i}/{total}) 요약 생략 - {result.get('summary_skipped_reason', '사유 불명')}")

            results.append(result)
    return results


def print_summaries(label: str, summarized: list[dict]) -> None:
    """
    결과를 사람이 읽기 좋게 콘솔에 출력한다(scorer.print_top_n과 같은 톤 -
    storage.py 저장 결과와 별개로 실행 중 진행 확인용).

    요약이 있든 없든 원문 링크는 항상 같이 보여준다.
    """
    print(f"\n=== {label} - LLM 요약 ===")
    for i, item in enumerate(summarized, start=1):
        titles = item.get("titles", [])
        rep_title = titles[0] if titles else "(제목 없음)"
        print(f"{i}. {rep_title}")
        if item.get("summary"):
            print(f"   요약: {item['summary']}")
        else:
            print(f"   (요약 생략 - {item.get('summary_skipped_reason', '사유 불명')})")
        urls = item.get("urls", [])
        shown_urls = ", ".join(urls[:3])
        more_note = f" 외 {len(urls) - 3}건" if len(urls) > 3 else ""
        print(f"   원문 링크: {shown_urls}{more_note}")