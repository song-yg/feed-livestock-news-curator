"""
relevance_filter.py
"2.5 관련성 필터" - 정규화(URL dedup + 키워드 태깅) 직후, 2.1 이슈 그룹핑
(임베딩 로드) 전에 실행한다.

배경 (2026-07-22 담당자 논의): 키워드 매칭만으로는 걸러지지 않는 오매칭
유형이 실측으로 다수 확인됨 - 동음이의어(질병명이 유튜버 닉네임인 경우),
검색어가 기관명/법안명 등 고유명사의 일부로만 등장하는 경우, 기사의 핵심
주제는 따로 있고 축산 관련 내용은 통계 나열 중 한 줄로만 등장하는 "각주성
언급" 등. 이런 유형은 키워드를 아무리 좁혀도(복합어, near() 등) 구조적으로
해결이 안 되는데, LLM이 제목/요약 맥락을 읽으면 정확히 구분 가능한 문제라
필터 단계로 신설.

설계는 issue_grouper.py의 "3차 LLM 보조"와 동일한 패턴을 그대로 재사용한다
(provider 스위치, 배치 호출, 출력 개수/형식 검증, 실패 시 안전한 기본값
fallback) - 두 모듈이 서로 다른 판정을 하지만 LLM 호출 방식 자체는 같은
철학을 공유하므로 일관성을 위해 통일함.

기본값 방향이 issue_grouper와 반대라는 점에 주의: 그룹핑 3차는 "애매하면
false(안 묶음)"가 안전하지만, 이 필터는 "애매하면 true(통과)"가 안전하다 -
관련 있는 기사를 잘못 걸러내는 것보다, 무관한 기사가 몇 개 더 통과하는 편이
손실이 적다. 배치 호출 자체가 실패해도 마찬가지 이유로 그 배치는 전부
통과시킨다(안 걸러진 채로 이후 단계로 넘어감 - 오늘까지의 동작과 동일하니
파이프라인이 더 나빠지지는 않음).

소스별로 LLM에 줄 수 있는 컨텍스트 양이 다르다는 한계가 있음 - 네이버는
description(짧은 요약), WATT는 body(본문 앞부분), GDELT는 제목뿐(GDELT는
스펙상 본문을 안 줌). GDELT 기사는 상대적으로 판정 근거가 부족하니 첫 실행
결과에서 GDELT 쪽 판정 정확도를 특히 눈여겨봐야 한다.
"""

import json
import os

import requests

# ---------------------------------------------------------------------------
# LLM 프로바이더 설정 - issue_grouper.py와 완전히 동일한 스위치 방식 재사용
# (같은 프로젝트 안에서 프로바이더 전환 기준이 파일마다 다르면 혼란스러우므로
# 통일함). LLM_PROVIDER=anthropic(기본값) 또는 openrouter.
#
# os.environ.get(key, default) 대신 or를 쓰는 이유: GitHub Actions Variables에
# 빈 문자열로 설정된 경우 os.environ.get(key, default)는 default를 못 돌려주고
# 빈 문자열을 그대로 반환하는 버그가 이미 확인된 바 있음(issue_grouper.py
# 참고) - 같은 함정을 여기서도 피하기 위해 동일한 패턴 사용.
# ---------------------------------------------------------------------------
LLM_PROVIDER = os.environ.get("LLM_PROVIDER") or "anthropic"

LLM_MODEL_ANTHROPIC = "claude-haiku-4-5-20251001"
LLM_API_URL_ANTHROPIC = "https://api.anthropic.com/v1/messages"

LLM_MODEL_OPENROUTER = os.environ.get("OPENROUTER_MODEL") or "openrouter/free"
LLM_API_URL_OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

# OpenRouter 권장 헤더(선택, ASCII 전용 - 한글 넣으면 UnicodeEncodeError남,
# issue_grouper.py에서 이미 실측 확인된 함정).
_OPENROUTER_X_TITLE = "feed-livestock-news-relevance-filter"

# 한 번의 API 호출에 몇 건까지 같이 물어볼지. issue_grouper의 LLM_BATCH_SIZE(20,
# 페어 단위)와 맞춤. 원래 30으로 시작했으나 실측 결과 무료 라우터가 30건
# 배치에서 개수를 자주 못 맞춰서(29건/31건 등) 20으로 하향 - id 기반 매칭
# (아래 _call_llm 참고)으로 어긋나도 배치 전체를 안 버리게 됐지만, 애초에
# 어긋나는 빈도 자체를 줄이는 것도 유효한 보강이라 같이 적용.
BATCH_SIZE = 20

# 기사 본문/요약 스니펫을 프롬프트에 넣을 때 자를 최대 길이. 너무 길면 배치당
# 토큰이 커져서 비용/실패 위험이 늘고, 판정에는 앞부분 몇 문장이면 충분하다고
# 판단.
SNIPPET_MAX_CHARS = 150


_SYSTEM_PROMPT = (
    "너는 사료·축산업 뉴스 큐레이션 시스템의 관련성 판별기다. 아래 기준으로 "
    "각 기사가 \"사료 산업\" 또는 \"축산업\"(가축 사육·방역·유통·정책 등)을 "
    "실질적 주제로 다루는지 판단하라.\n\n"
    "관련 있음(true):\n"
    "- 가축(소/돼지/닭/오리 등) 사육·질병·방역, 축산물(고기/계란/우유 등) "
    "생산·가격·무역, 축산업 관련 정부 정책/법안을 핵심 주제로 다루는 기사\n"
    "- 사료 원료(옥수수·대두박·소맥 등 곡물, 국제 곡물 시황), 사료첨가제"
    "(아미노산·프로바이오틱스·효소제·항생제 대체제 등), 사료 제조·유통"
    "(사료공장·프리믹스·배합사료·TMR 등)을 핵심 주제로 다루는 기사도 포함한다\n\n"
    "관련 없음(false) - 아래는 실제로 반복 확인된 오매칭 유형이니 특히 주의해서 "
    "걸러라:\n"
    "1. 동음이의어: 검색어와 철자는 같지만 다른 대상을 가리킴 (예: 질병명이 "
    "사람의 이름/닉네임인 경우, \"사료\"가 반려동물 사료 산업을 가리키는 경우 "
    "- 반려동물 사료는 이 프로젝트 범위 밖이다)\n"
    "2. 고유명사의 일부로만 등장: 검색어가 기관명·법안명 등 고유명사의 일부일 "
    "뿐 (예: 국회 상임위원회 이름 안에 포함된 경우 - 기사 주제 자체는 무관한 "
    "정치 뉴스)\n"
    "3. 각주성 언급: 기사의 핵심 주제는 따로 있고(예: 반도체 수출, 종합 "
    "물가지수), 축산 관련 내용은 여러 통계/품목 나열 중 한 줄로만 잠깐 등장\n"
    "4. 곡물이 사료 용도가 아닌 경우: 옥수수·소맥·대두 등 곡물 관련 기사가 "
    "사료용이 아니라 식용·바이오에너지(에탄올)용·일반 농산물 시장 전반을 "
    "다루고, 사료·축산 관련 언급이 전혀 없는 경우 (예: 옥수수 가격 기사가 "
    "팝콘·시리얼 등 식품 원료 수급이나 에탄올 원료 얘기뿐인 경우)\n\n"
    "함께 주어지는 \"카테고리\" 값은 사전 키워드 매칭으로 자동 분류된 참고용 "
    "힌트일 뿐, 확정된 정답이 아니다 - 실제 제목/요약 내용을 보고 최종 "
    "판단하라.\n\n"
    "판단이 애매하면 true로 답한다(보수적 기본값 - 관련 있는 기사를 잘못 "
    "걸러내는 것보다, 무관한 기사가 몇 개 더 통과하는 편이 안전하다).\n\n"
    "제목 언어가 서로 다를 수 있다(한국어/영어/기타 언어 혼재) - 언어와 "
    "무관하게 같은 기준으로 판단한다.\n\n"
    "다른 설명 없이 JSON 배열만 출력한다. 각 원소는 {\"id\": 번호, \"relevant\": "
    "true|false} 형태이며, id는 입력받은 기사의 번호와 정확히 일치해야 한다."
)


def _snippet(article: dict) -> str | None:
    """
    기사에서 판정 근거로 쓸 스니펫을 뽑는다. 네이버는 description(짧은 요약),
    WATT는 body(본문 앞부분)를 쓰고, GDELT는 스펙상 본문이 아예 없어(body가
    항상 None) None을 반환한다 - 이 경우 프롬프트에 "요약 없음"으로 명시해
    모델이 없는 정보를 있는 척 채워 넣지 않게 한다(_build_user_prompt 참고).
    """
    text = article.get("description") or article.get("body")
    if not text:
        return None
    return text[:SNIPPET_MAX_CHARS]


def _build_user_prompt(batch: list[dict]) -> str:
    lines = ["다음 기사들이 사료·축산업 뉴스로서 관련이 있는지 판단해줘.\n"]
    for idx, article in enumerate(batch, start=1):
        title = article.get("title", "")
        category = article.get("category", "기타")
        snippet = _snippet(article)
        snippet_part = f'"{snippet}"' if snippet else "(없음 - 제목만으로 판단)"
        lines.append(
            f'{idx}. 제목: "{title}" / 카테고리: {category} / 요약: {snippet_part}'
        )
    lines.append(
        f'\n총 {len(batch)}건이다. 각 원소에 위 번호를 "id"로 그대로 포함해서 '
        f'JSON 배열로만 답하라 (예: [{{"id": 1, "relevant": true}}, '
        f'{{"id": 2, "relevant": false}}, ...]). id를 빠뜨리거나 순서를 바꾸지 마라.'
    )
    return "\n".join(lines)


def _call_llm(batch: list[dict], api_key: str) -> list[bool] | None:
    """
    LLM API를 한 번 호출해서 batch 각각에 대한 relevant 판정을 받아온다.
    실패(호출 에러/JSON 파싱 실패/개수 불일치)하면 None을 반환한다 - 호출부
    (filter_articles)는 None이면 그 배치를 전부 통과시킨다.
    """
    user_prompt = _build_user_prompt(batch)

    try:
        if LLM_PROVIDER == "openrouter":
            headers = {
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
                "X-Title": _OPENROUTER_X_TITLE,
            }
            body = {
                "model": LLM_MODEL_OPENROUTER,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            }
            resp = requests.post(LLM_API_URL_OPENROUTER, headers=headers, json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()
        else:
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body = {
                "model": LLM_MODEL_ANTHROPIC,
                "max_tokens": 1024,
                "temperature": 0,
                "system": _SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_prompt}],
            }
            resp = requests.post(LLM_API_URL_ANTHROPIC, headers=headers, json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            text = "".join(
                block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
            ).strip()

        # 코드 펜스(```json ... ```)로 감싸서 올 때가 있어 방어적으로 벗겨낸다
        # (issue_grouper.py와 동일한 방어 로직 - 무료 모델은 이런 포맷 이탈이
        # Haiku보다 잦을 수 있어 특히 중요).
        if text.startswith("```"):
            text = text.split("```")[1]
            text = text[4:] if text.startswith("json") else text
        parsed = json.loads(text.strip())
    except Exception as e:
        print(f"[relevance_filter] LLM({LLM_PROVIDER}) 호출/파싱 실패 - 이 배치"
              f"({len(batch)}건) 전부 통과 처리: {type(e).__name__} - {e!r}")
        return None

    if not isinstance(parsed, list) or not parsed:
        actual = len(parsed) if isinstance(parsed, list) else type(parsed).__name__
        print(f"[relevance_filter] LLM({LLM_PROVIDER}) 출력 형식 이상(리스트가 "
              f"아니거나 비어있음, 실제 {actual}) - 이 배치({len(batch)}건) 전부 통과 처리")
        return None

    # 2026-07-22 변경: 기존엔 "출력 개수 != 입력 개수"면 배치 전체를 버렸는데,
    # 실측 결과 30건 중 1건만 빠지거나 하나 더 생기는 경미한 어긋남에도
    # 배치 전체(29~31건)가 통째로 낭비되는 게 확인됨(담당자 지적). id를
    # 명시적으로 주고받게 해서, 어긋나도 "일치하는 것만 살리고, 안 맞는 것만
    # 개별적으로 안전한 기본값(통과)으로 처리"하도록 개선.
    by_id: dict[int, bool] = {}
    for item in parsed:
        try:
            by_id[int(item["id"])] = bool(item["relevant"])
        except (KeyError, TypeError, ValueError):
            continue  # id/relevant 형식이 이상한 개별 항목만 무시하고 계속 진행

    results = []
    missing = []
    for idx in range(1, len(batch) + 1):
        if idx in by_id:
            results.append(by_id[idx])
        else:
            missing.append(idx)
            results.append(True)  # 안전한 기본값 - 통과

    if missing:
        print(f"[relevance_filter] LLM({LLM_PROVIDER}) 출력에서 id {missing} 누락"
              f"(기대 {len(batch)}건 중 {len(missing)}건) - 그 항목들만 통과 처리, "
              f"나머지 {len(batch) - len(missing)}건은 정상 판정 사용")

    return results


def filter_articles(articles: list[dict]) -> list[dict]:
    """
    정규화+태깅이 끝난 articles를 받아, LLM이 "사료·축산업 뉴스가 아니다"라고
    확실히 판단한 것만 걸러내고 나머지를 반환한다.

    API 키가 없거나(LLM_PROVIDER에 맞는 키 환경변수 미설정) 모든 배치 호출이
    실패하면, 안전하게 원본 articles를 그대로 반환한다(필터를 그냥 안 거친
    것과 동일 - 9.4/9.5 원칙과 같은 방향의 안전한 기본값).
    """
    if not articles:
        return articles

    key_env_var = "OPENROUTER_API_KEY" if LLM_PROVIDER == "openrouter" else "ANTHROPIC_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        print(f"[relevance_filter] {key_env_var} 없음(LLM_PROVIDER={LLM_PROVIDER}) - "
              f"관련성 필터 생략, {len(articles)}건 전부 통과")
        return articles

    model_name = LLM_MODEL_OPENROUTER if LLM_PROVIDER == "openrouter" else LLM_MODEL_ANTHROPIC
    print(f"[relevance_filter] 관련성 필터 시작 - provider={LLM_PROVIDER}, "
          f"model={model_name}, 대상 {len(articles)}건")

    kept = []
    dropped_samples = []
    for i in range(0, len(articles), BATCH_SIZE):
        batch = articles[i:i + BATCH_SIZE]
        results = _call_llm(batch, api_key)
        if results is None:
            kept.extend(batch)  # 이 배치는 전부 통과 (안전한 기본값)
            continue
        for article, relevant in zip(batch, results):
            if relevant:
                kept.append(article)
            else:
                dropped_samples.append(article.get("title", ""))

    dropped_count = len(articles) - len(kept)
    print(f"[relevance_filter] 관련성 필터 완료 - {len(articles)}건 중 "
          f"{dropped_count}건 제외, {len(kept)}건 유지")
    if dropped_samples:
        sample_n = min(10, len(dropped_samples))
        print(f"[relevance_filter] 제외된 기사 샘플 (최대 {sample_n}건):")
        for title in dropped_samples[:sample_n]:
            print(f"   - {title}")

    return kept