"""
llm_summarizer.py
"4. LLM 사용 지점" 중 (A) 자체 요약 생성 + (A-1) 얇은 재료 fallback 담당 모듈.
(알고리즘 문서 "4. LLM 사용 지점" 표 참조. (B) 그룹핑 보조는
issue_grouper.stage3_llm_assist로 이미 구현 완료 - 2026-07-15)

** 프로바이더 설정을 issue_grouper.py에서 그대로 재사용 **
LLM_PROVIDER/모델명/API URL/X-Title 상수를 새로 정의하지 않고
issue_grouper.py에서 import해서 쓴다. 이유: 3차(그룹핑 보조)를 만들면서
이 설정값들 자체에서 실제로 버그를 세 번 겪었다(① GitHub Actions에서
미등록 Variable을 참조하면 "안 넘어옴"이 아니라 "빈 문자열"로 넘어와서
os.environ.get(key, default)가 기본값을 못 쓰는 문제, ② X-Title 헤더에
한글을 넣었다가 HTTP 헤더는 latin-1만 허용돼 UnicodeEncodeError). 이미
고쳐서 검증까지 끝난 값을 그대로 재사용하면 여기서 또 같은 버그가
재현되는 걸 원천적으로 막을 수 있다 (issue_grouper.py 쪽 코드는 이 모듈이
전혀 건드리지 않음 - 이미 동작 확인된 3차 로직에 영향 없음).

** (A)/(A-1) 안전장치 (9.4 원칙 재사용) **
API 키가 없거나 LLM 호출/응답이 실패해도 요약을 생략하고 원문 제목만
노출하는 쪽으로 fallback한다 - 3차와 동일한 철학, "요약 없이 원문 링크만
발송"이 9.5 섹션이 이미 권장한 무료 모델 대응책과도 일치한다.
"""

import os

import requests

import issue_grouper as _ig  # LLM_PROVIDER, 모델명, API URL, X-Title 상수 재사용 (위 docstring 참고)


_SYSTEM_PROMPT = (
    "너는 사료·축산업 뉴스 큐레이션 서비스의 요약 작성자다. 주어진 이슈(같은 사건을 다루는 기사 제목들과 참고 정보)를 보고 한국어로 2~3문장의 자체 요약을 작성하라. 원문을 그대로 옮기지 말고 핵심 내용만 새로 요약한다. (9.4 안전장치 1번 - 없는 내용 추가 금지). 확실하지 않은 수치나 사실은 임의로 만들어내지 말고, 주어진 제목/참고 정보에 있는 내용만 사용한다. 확실하지 않으면 요약 대신 애매함을 그대로 표현하라(9.5 섹션 - 확실하지 않으면 요약 대신 제목만 나열하는 원칙과 같은 결). 전문용어·질병명·정부기관명·제도명 등 고유명사는 임의로 한글화하지 말고 영문 원어가 있으면 괄호로 병기한다(12.2 언어 처리 방침). 다른 설명 없이 요약 문장만 출력한다."
)

# 그룹 하나에 기사가 아주 많을 때(예: 50건 이상) 제목을 전부 프롬프트에
# 넣으면 비용/속도 낭비가 크므로 상한을 둔다 - 나머지는 "외 N건 생략"으로 표시.
_MAX_TITLES_IN_PROMPT = 15
# 참고 컨텍스트(본문 발췌/description)도 기사 몇 건까지만 볼지 상한
_MAX_CONTEXT_ARTICLES = 5
_MAX_BODY_EXCERPT_CHARS = 300


def _build_user_prompt(item: dict) -> str:
    """
    이슈 하나(scorer.score_group() 결과 dict - titles/urls/press_list/articles
    필드를 가짐)를 LLM 입력 프롬프트로 만든다.

    문서 4번 섹션 (A) 입력 정의 그대로: 제목 + (본문 확보된 경우) 본문에서
    뽑은 핵심 문장 + (네이버 소스인 경우) description을 참고 컨텍스트로 추가
    (2026-07-13 확정 - 그대로 인용하지 않고 참고용으로만 사용).
    """
    titles = item.get("titles", [])
    lines = ["다음은 같은 이슈를 다룬 기사 제목들이다:"]
    for title in titles[:_MAX_TITLES_IN_PROMPT]:
        lines.append(f"- {title}")
    if len(titles) > _MAX_TITLES_IN_PROMPT:
        lines.append(f"(외 {len(titles) - _MAX_TITLES_IN_PROMPT}건 제목 생략)")

    context_lines = []
    for article in item.get("articles", [])[:_MAX_CONTEXT_ARTICLES]:
        source = article.get("source", "?")
        body = article.get("body")
        if body:
            context_lines.append(f"[{source}] 본문 일부: {body[:_MAX_BODY_EXCERPT_CHARS]}")
        description = article.get("description")
        if description:
            context_lines.append(f"[{source}] 설명: {description}")

    if context_lines:
        lines.append("\n참고 정보(그대로 인용하지 말고 참고만 할 것):")
        lines.extend(context_lines)

    lines.append("\n위 내용을 바탕으로 한국어 2~3문장 자체 요약을 작성하라.")
    return "\n".join(lines)


def _call_llm(system_prompt: str, user_prompt: str, api_key: str) -> str | None:
    """
    issue_grouper.py의 프로바이더 설정을 재사용해 LLM을 한 번 호출하고 응답
    텍스트를 반환한다. 실패 시 None (호출부가 "요약 생략, 원문 제목만"으로
    fallback).

    3차(_call_llm, issue_grouper.py)와 요청 형식은 거의 같지만, 3차는 JSON
    배열을 기대하는 반면 이건 자연어 문장 하나를 기대한다는 점이 달라서
    별도 함수로 둔다 - 파싱 방식이 다른데 억지로 하나로 합치면 오히려
    코드가 더 헷갈림.
    """
    try:
        if _ig.LLM_PROVIDER == "openrouter":
            headers = {
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
                # ASCII 전용이어야 함 - 2026-07-16 X-Title 한글 인코딩 버그 교훈,
                # issue_grouper의 검증된 상수를 그대로 재사용
                "X-Title": _ig._OPENROUTER_X_TITLE,
            }
            body = {
                "model": _ig.LLM_MODEL_OPENROUTER,
                "temperature": 0.3,  # 자연어 생성이라 3차(판정, temperature=0)보다는 살짝 여유
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            resp = requests.post(_ig.LLM_API_URL_OPENROUTER, headers=headers, json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        else:
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
            resp = requests.post(_ig.LLM_API_URL_ANTHROPIC, headers=headers, json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return "".join(
                block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
            ).strip()
    except Exception as e:
        print(f"[llm_summarizer] LLM({_ig.LLM_PROVIDER}) 호출 실패: {type(e).__name__} - {e!r}")
        return None


def summarize_issue(item: dict) -> dict:
    """
    이슈 하나(scorer.score_group() 결과 dict)에 (A)/(A-1) 로직을 적용해
    요약을 붙인다. 원본 item은 변경하지 않고 얕은 복사본을 반환한다
    (호출부가 리스트를 여러 번 다룰 수 있어 부작용 없는 편이 안전).

    반환값에 추가되는 필드:
      summary: LLM이 생성한 2~3문장 요약, 또는 None(요약 생략된 경우)
      summary_skipped_reason: 요약을 생략한 이유. 정상 요약됐으면 None.
    """
    result = dict(item)
    titles = item.get("titles", [])

    # (A-1) 얇은 재료 fallback (문서 4번 섹션 (A-1), 2026-07-13 확정):
    # 이슈 그룹핑이 안 되고(그룹 크기 1) 언론사 1곳만 보도한 단독 기사는
    # 재료(제목 하나뿐)가 너무 얇아 LLM이 요약을 만들면 사실상 제목을
    # 부풀리는 것과 다름없으므로, 애초에 LLM 호출 자체를 생략한다.
    if len(titles) == 1:
        result["summary"] = None
        result["summary_skipped_reason"] = (
            "단독 기사(이슈 그룹핑 안 됨) - 재료가 얇아 요약 생략, 원문 제목만 노출 (문서 4번 섹션 (A-1))"
        )
        return result

    key_env_var = "OPENROUTER_API_KEY" if _ig.LLM_PROVIDER == "openrouter" else "ANTHROPIC_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        result["summary"] = None
        result["summary_skipped_reason"] = (
            f"{key_env_var} 없음(LLM_PROVIDER={_ig.LLM_PROVIDER}) - 요약 생략, 원문 제목만 노출"
        )
        return result

    user_prompt = _build_user_prompt(item)
    summary_text = _call_llm(_SYSTEM_PROMPT, user_prompt, api_key)

    if not summary_text:
        result["summary"] = None
        result["summary_skipped_reason"] = "LLM 호출/응답 실패 - 요약 생략, 원문 제목만 노출 (9.4 fallback)"
        return result

    result["summary"] = summary_text
    result["summary_skipped_reason"] = None
    return result


def summarize_top_issues(ranked_items: list[dict], label: str = "") -> list[dict]:
    """
    scorer.score_and_rank()가 만든 상위 이슈 리스트 전체에 summarize_issue를
    적용한다. main.py의 4단계 호출부에서 국내/해외 각각 부른다.

    ** 2026-07-17 진행상황 로그 추가 **: 원래는 전체가 끝난 뒤
    print_summaries로 한꺼번에만 출력했는데, 이슈 하나당 LLM 호출이 몇 초~
    몇십 초 걸릴 수 있어(특히 오픈라우터 무료 모델은 느리거나 대기열이 걸릴
    수 있음) 그 사이 로그가 조용해서 "멈춘 건지 도는 건지 구분이 안 된다"는
    문제가 실제로 있었음. GDELT/WATT collector처럼 항목 하나 처리할 때마다
    바로바로 로그를 찍도록 수정 - 실행 상태를 실시간으로 볼 수 있게 함.
    """
    results = []
    total = len(ranked_items)
    for i, item in enumerate(ranked_items, start=1):
        titles = item.get("titles", [])
        rep_title = titles[0] if titles else "(제목 없음)"
        prefix = f"[llm_summarizer] {label} " if label else "[llm_summarizer] "
        print(f"{prefix}({i}/{total}) '{rep_title}' (그룹 {len(titles)}건) - 요약 요청 중...")

        result = summarize_issue(item)

        if result.get("summary"):
            print(f"{prefix}({i}/{total}) 요약 완료")
        else:
            print(f"{prefix}({i}/{total}) 요약 생략 - {result.get('summary_skipped_reason', '사유 불명')}")

        results.append(result)
    return results


def print_summaries(label: str, summarized: list[dict]) -> None:
    """
    결과를 사람이 읽기 좋게 콘솔에 출력한다 (저장 레이어(5단계, 아직
    미구현) 완성 전까지의 확인용 - scorer.print_top_n과 같은 톤).

    9.4 안전장치 2번("출력에 항상 원문 링크 강제 부착") 그대로 적용 -
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


if __name__ == "__main__":
    # 자체 점검용 - API 키 없이도 (A-1) fallback 경로와 다건 그룹의 fallback
    # 경로(키 없음)가 정상 동작하는지 확인. 실제 LLM 호출 성공 경로는 진짜
    # 키가 있어야 확인 가능하므로 여기선 검증 안 함(수동으로 키 넣고 확인할 것).
    single_article_issue = {
        "issue_score": 1.0,
        "mention_count": 1,
        "raw_mention_count": 1,
        "titles": ["단독 보도 - 테스트용 제목"],
        "urls": ["https://example.com/1"],
        "press_list": ["testpress.com"],
        "articles": [{"source": "네이버", "title": "단독 보도 - 테스트용 제목",
                      "url": "https://example.com/1", "description": "테스트 설명"}],
    }
    multi_article_issue = {
        "issue_score": 3.0,
        "mention_count": 3,
        "raw_mention_count": 3,
        "titles": ["기사 A", "기사 B", "기사 C"],
        "urls": ["https://example.com/a", "https://example.com/b", "https://example.com/c"],
        "press_list": ["press1.com", "press2.com", "press3.com"],
        "articles": [
            {"source": "네이버", "title": "기사 A", "url": "https://example.com/a", "description": "설명 A"},
            {"source": "네이버", "title": "기사 B", "url": "https://example.com/b", "description": "설명 B"},
            {"source": "WATTAgNet", "title": "기사 C", "url": "https://example.com/c", "body": "본문 발췌 C" * 50},
        ],
    }

    result1 = summarize_issue(single_article_issue)
    print("[검증1] 단독 기사(A-1) - summary:", result1["summary"],
          "/ reason:", result1["summary_skipped_reason"])
    assert result1["summary"] is None
    assert "단독 기사" in result1["summary_skipped_reason"]

    # API 키를 일부러 지운 상태에서 다건 그룹을 넣어 "키 없음" fallback 확인
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("OPENROUTER_API_KEY", None)
    result2 = summarize_issue(multi_article_issue)
    print("[검증2] 다건 그룹, API 키 없음 - summary:", result2["summary"],
          "/ reason:", result2["summary_skipped_reason"])
    assert result2["summary"] is None
    assert "없음" in result2["summary_skipped_reason"]

    print_summaries("테스트", [result1, result2])
    print("\n[llm_summarizer] 자체 점검 통과 (A-1 fallback + 키 없음 fallback)")
