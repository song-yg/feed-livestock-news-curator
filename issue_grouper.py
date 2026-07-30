"""
issue_grouper.py
이슈 그룹핑 모듈. 1차(사전 매칭) -> 2차(임베딩) -> 3차(LLM 보조) -> 4차(Top N 재검토)
순서로 group_issues()가 실행. 1차는 ISSUE_SYNONYM_GROUPS가 비어있어 항상 no-op,
2차(BGE-M3 임베딩)에만 의존.
"""

import csv
import json
import os
from itertools import combinations

import requests

import scorer  # 4차 병합 그룹 재스코어링용

from keyword_tagger import EXCLUDED_TERMS  # 1차 매칭 제외어, keyword_tagger와 기준 통일


# ---------------------------------------------------------------------------
# 1차: KR<->EN 키워드 사전 매칭
# ---------------------------------------------------------------------------
# 완전 동일 사건 매칭용 사전. 국가/지역 구분이 안 돼 항상 빈 리스트로 둠 -
# 실질적으로 모든 기사가 2차(임베딩)로 넘어감. 인프라만 보존.
ISSUE_SYNONYM_GROUPS: list[set[str]] = []


def _stage1_match_keys(title: str) -> set[int]:
    """제목이 ISSUE_SYNONYM_GROUPS의 몇 번 묶음에 걸리는지(대소문자 무시 부분 매칭)."""
    title_lower = title.lower()
    matched = set()
    for idx, synonyms in enumerate(ISSUE_SYNONYM_GROUPS):
        usable_synonyms = synonyms - EXCLUDED_TERMS
        if any(syn.lower() in title_lower for syn in usable_synonyms):
            matched.add(idx)
    return matched


# ---------------------------------------------------------------------------
# Union-Find (그룹 병합용)
# ---------------------------------------------------------------------------
class UnionFind:
    """find(i): i의 그룹 대표 조회. union(i, j): 같은 그룹으로 병합."""

    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]  # 경로 압축
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        root_i, root_j = self.find(i), self.find(j)
        if root_i != root_j:
            self.parent[root_j] = root_i

    def groups(self) -> list[list[int]]:
        """[[인덱스, ...], ...] 형태로 최종 그룹 반환."""
        buckets: dict[int, list[int]] = {}
        for i in range(len(self.parent)):
            root = self.find(i)
            buckets.setdefault(root, []).append(i)
        return list(buckets.values())


# ---------------------------------------------------------------------------
# 1차 매칭 그룹 생성
# ---------------------------------------------------------------------------

def stage1_group(articles: list[dict]) -> tuple[list[list[dict]], list[dict]]:
    """
    1차 사전 매칭 그룹 생성.
    반환: grouped(2건 이상 매칭된 그룹들), unmatched(2차로 넘길 나머지)
    """
    n = len(articles)
    uf = UnionFind(n)

    key_to_article_indices: dict[int, list[int]] = {}
    for i, article in enumerate(articles):
        keys = _stage1_match_keys(article.get("title", ""))
        for key in keys:
            key_to_article_indices.setdefault(key, []).append(i)

    for indices in key_to_article_indices.values():
        for a, b in combinations(indices, 2):
            uf.union(a, b)

    grouped = []
    unmatched = []
    for indices in uf.groups():
        if len(indices) >= 2:
            grouped.append([articles[i] for i in indices])
        else:
            unmatched.append(articles[indices[0]])

    return grouped, unmatched


# ---------------------------------------------------------------------------
# 2차: BGE-M3 임베딩 코사인 유사도 매칭
# ---------------------------------------------------------------------------
THRESHOLD = 0.7  # 잠정값, export_similarity_scores() 디버그 CSV로 튜닝

BORDERLINE_MARGIN = 0.06  # THRESHOLD-MARGIN ~ THRESHOLD가 애매 구간(3차 대상)

# 임계값 튜닝용 디버그 CSV 저장 스위치. 기본 OFF, SIMILARITY_DEBUG_CSV=1로 켬.
SIMILARITY_DEBUG_CSV = (os.environ.get("SIMILARITY_DEBUG_CSV") or "").strip().lower() in ("1", "true", "yes", "on")


def _embedding_text(article: dict) -> str:
    """임베딩 입력 텍스트 - 제목 + (있으면) 본문 앞 200자."""
    title = article.get("title", "")
    body = article.get("body")
    if body:
        return f"{title} {body[:200]}"
    return title


def _cosine_similarity_matrix(vectors):
    """벡터 리스트 -> N x N 코사인 유사도 행렬."""
    import numpy as np

    vectors = np.array(vectors)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    normalized = vectors / norms
    return normalized @ normalized.T


def export_similarity_scores(articles: list[dict], sim_matrix, threshold: float,
                              borderline_margin: float, path: str = "similarity_debug/similarity_scores.csv",
                              min_score: float = 0.4) -> str | None:
    """
    유사도 행렬 전체를 CSV로 내보냄(임계값 튜닝용). min_score 미만 쌍은 제외.
    status 컬럼은 현재 THRESHOLD/BORDERLINE_MARGIN 기준 merged/borderline/none 표시.
    실패해도 예외 안 던지고 로그만 남김(진단용 부가기능).
    """
    n = len(articles)
    rows = []
    for i, j in combinations(range(n), 2):
        sim = float(sim_matrix[i][j])
        if sim < min_score:
            continue
        if sim >= threshold:
            status = "merged"
        elif threshold - borderline_margin <= sim < threshold:
            status = "borderline"
        else:
            status = "none"
        rows.append((sim, articles[i], articles[j], status))

    rows.sort(key=lambda r: r[0], reverse=True)  # 유사도 높은 순 - 경계선을 위아래로 훑기 편하게

    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            # utf-8-sig: 엑셀에서 한글 CSV 열 때 깨지는 문제 방지(BOM 포함)
            writer = csv.writer(f)
            writer.writerow(["similarity", "status", "title_a", "title_b",
                              "category_a", "category_b", "source_a", "source_b"])
            for sim, a, b, status in rows:
                writer.writerow([
                    f"{sim:.4f}", status,
                    a.get("title", ""), b.get("title", ""),
                    a.get("category", ""), b.get("category", ""),
                    a.get("source", ""), b.get("source", ""),
                ])
    except OSError as e:
        print(f"[issue_grouper] 🟡 주의 [IG-01] - 유사도 디버그 CSV 저장 실패(그룹핑 결과에는 영향 없음): "
              f"{path} - {type(e).__name__}: {e}")
        return None

    print(f"[issue_grouper] 유사도 디버그 CSV 저장 완료 ({len(rows)}쌍, "
          f"min_score={min_score} 이상만) -> {path}")
    return path


def stage2_group(
    articles: list[dict],
    model=None,
    threshold: float = THRESHOLD,
    borderline_margin: float = BORDERLINE_MARGIN,
) -> tuple[list[list[dict]], list[dict], list[tuple[dict, dict, float]]]:
    """
    1차에서 못 잡은 기사들을 BGE-M3 임베딩으로 매칭.
    model: SentenceTransformer 인스턴스(호출부에서 1회 로드해 주입).

    반환:
      grouped: 2차에서 새로 묶인 그룹들
      still_unmatched: 2차에서도 못 잡은 단독 기사
      borderline_pairs: 애매 구간 (기사A, 기사B, 유사도) - 3차 LLM 보조 대상
    """
    if not articles:
        return [], [], []

    texts = [_embedding_text(a) for a in articles]
    vectors = model.encode(texts, normalize_embeddings=True)
    sim_matrix = _cosine_similarity_matrix(vectors)

    if SIMILARITY_DEBUG_CSV:
        export_similarity_scores(articles, sim_matrix, threshold, borderline_margin)

    n = len(articles)
    uf = UnionFind(n)

    # 2-pass: union 먼저 전부 확정한 뒤 borderline 후보를 걸러야, 간접
    # 연결로 이미 확정된 쌍이 borderline에 중복 기록되지 않음.
    for i, j in combinations(range(n), 2):
        sim = float(sim_matrix[i][j])
        if sim >= threshold:
            uf.union(i, j)

    borderline_pairs = []
    for i, j in combinations(range(n), 2):
        if uf.find(i) == uf.find(j):
            continue
        sim = float(sim_matrix[i][j])
        if threshold - borderline_margin <= sim < threshold:
            borderline_pairs.append((articles[i], articles[j], sim))

    grouped = []
    still_unmatched = []
    for indices in uf.groups():
        if len(indices) >= 2:
            grouped.append([articles[i] for i in indices])
        else:
            still_unmatched.append(articles[indices[0]])

    return grouped, still_unmatched, borderline_pairs


# ---------------------------------------------------------------------------
# 3차: LLM 그룹핑 보조 (임계값 애매 구간)
# ---------------------------------------------------------------------------
# borderline_pairs만 대상(전수 호출 아님). "같은 사건" 기준: 질병/주제가
# 같아도 국가·장소·시점이 다르면 별개.
#
# LLM_PROVIDER=anthropic(기본, ANTHROPIC_API_KEY) 또는 openrouter(로컬 검증
# 전용, OPENROUTER_API_KEY). 운영 환경은 기본값(anthropic) 유지할 것.
#
# 무료 모델 하나를 못 박지 않고 OpenRouter의 자체 무료 라우터(openrouter/free)
# 를 기본값으로 쓴 이유: 개별 :free 모델은 공급사가 예고 없이 무료 태그를
# 뗄 수 있어 코드가 조용히 깨질 수 있는데,
# openrouter/free는 그 라우팅 자체를 OpenRouter가 대신 처리해준다. 특정
# 모델을 고정하고 싶으면 OPENROUTER_MODEL 환경변수로 덮어쓸 수 있다.
#
# ** os.environ.get(key, default) 대신 or를 쓰는 이유 **
# GitHub Actions에서 리포에 등록 안 된 Variable을 `${{ vars.X }}`로 참조하면
# "아예 안 넘어옴"이 아니라 "빈 문자열로 채워진 환경변수"가 된다(GitHub 공식
# 문서: "설정 안 된 configuration variable을 참조하면 빈 문자열로 평가됨").
# `os.environ.get(key, default)`의 default는 키가 "아예 없을 때"만 적용되고
# 빈 문자열이 있으면 그 빈 문자열을 그대로 돌려주므로, OPENROUTER_MODEL을
# Variables에 등록 안 한 상태로 두면 LLM_MODEL_OPENROUTER가 빈 문자열이
# 되어 OpenRouter API가 "model" 필드 없음으로 400 Bad Request를 던진다.
# `or` 연산자를 쓰면 빈 문자열도 falsy라 기본값으로 자연스럽게 대체된다.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER") or "anthropic"

LLM_MODEL_ANTHROPIC = "claude-haiku-4-5-20251001"
LLM_API_URL_ANTHROPIC = "https://api.anthropic.com/v1/messages"

LLM_MODEL_OPENROUTER = os.environ.get("OPENROUTER_MODEL") or "openrouter/free"
LLM_API_URL_OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

# 2/3순위 폴백 모델(선택, 비우면 건너뜀)
LLM_MODEL_OPENROUTER_2 = os.environ.get("OPENROUTER_MODEL_2") or ""
LLM_MODEL_OPENROUTER_3 = os.environ.get("OPENROUTER_MODEL_3") or ""

# 시도 순서: 1순위 -> 2순위 -> 3순위 -> 최종 안전망(openrouter/free, 항상 자동 추가)
_LLM_MODEL_CHAIN_OPENROUTER_ROLES: list[tuple[str, str]] = []
if LLM_MODEL_OPENROUTER:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("1순위", LLM_MODEL_OPENROUTER))
if LLM_MODEL_OPENROUTER_2:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("2순위", LLM_MODEL_OPENROUTER_2))
if LLM_MODEL_OPENROUTER_3:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("3순위", LLM_MODEL_OPENROUTER_3))
if "openrouter/free" not in [m for _, m in _LLM_MODEL_CHAIN_OPENROUTER_ROLES]:
    _LLM_MODEL_CHAIN_OPENROUTER_ROLES.append(("최종 안전망", "openrouter/free"))

# 순위별 오류 코드(체인 길이 무관하게 역할 고정)
_LLM_MODEL_ROLE_ERROR_CODE = {
    "1순위": "IG-07",
    "2순위": "IG-08",
    "3순위": "IG-09",
    "최종 안전망": "IG-10",
}

LLM_MODEL_CHAIN_OPENROUTER = [m for _, m in _LLM_MODEL_CHAIN_OPENROUTER_ROLES]  # 하위호환용

LLM_BATCH_SIZE = 20  # 배치당 최대 쌍 수

# HTTP 헤더는 ASCII만 허용 - 한글 넣으면 UnicodeEncodeError
_OPENROUTER_X_TITLE = "feed-livestock-news-issue-grouping-stage3"

_LLM_SYSTEM_PROMPT = (
    "You are a judge that assists news issue grouping. Given two article "
    "titles, decide whether the two articles cover \"exactly the same "
    "event\". "
    "Even if they cover the same disease/topic, judge them as separate "
    "events if the country, location, or timing differs (e.g. an article "
    "about an avian influenza outbreak in Korea and one in the US are "
    "separate events even though the disease name is the same). "
    "This applies not only at the country level but also to different "
    "domestic regions (province/city/county) within the same country "
    "(e.g. an article about heatwave damage to livestock in Gyeongnam and "
    "one in Jeju are both domestic and cover the same kind of issue, but "
    "are separate events if the affected region differs). "
    "Also judge them as separate events when one article covers a single "
    "issue (A) alone and the other article covers several issues "
    "including that one (A, B, C, D, etc.) together - overlapping on one "
    "issue does not make them the same event, because the scope and focus "
    "covered are different (e.g. a standalone article on \"Gyeongnam "
    "livestock heatwave damage\" and an article on \"Gyeongnam livestock "
    "damage from heatwave, flooding, and typhoon combined\" are not the "
    "same event even though the heatwave damage overlaps, because the "
    "latter is a separate roundup-style article covering multiple "
    "disasters together). "
    "Titles may be in different languages (Korean/English/other languages "
    "mixed) - judge them as the same event if they refer to the same "
    "event, regardless of language. "
    "If you are not certain, you must answer false (conservative default "
    "- it is safer not to group than to group incorrectly). "
    "Output only a JSON array with no other explanation. Each element must "
    "be in the form {\"id\": number, \"same_event\": true|false}, and id must "
    "exactly match the number of the input pair."
)


def _build_llm_user_prompt(pairs: list[tuple[dict, dict, float]]) -> str:
    lines = ["Judge whether each of the following pairs of article titles covers exactly the same event.\n"]
    for idx, (a, b, _sim) in enumerate(pairs, start=1):
        lines.append(f"{idx}. A: \"{a.get('title', '')}\" / B: \"{b.get('title', '')}\"")
    lines.append(
        f'\nThere are {len(pairs)} pairs total. Include the number above as "id" in each '
        f'element and answer with a JSON array only (e.g. [{{"id": 1, "same_event": true}}, '
        f'{{"id": 2, "same_event": false}}, ...]). Do not omit any id or change the order.'
    )
    return "\n".join(lines)


def _snippet_for_log(text: str, limit: int = 200) -> str:
    """LLM 원본 응답을 로그용으로 잘라서 반환."""
    if not text:
        return "(빈 응답)"
    flat = " ".join(text.split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _request_openrouter(system_prompt: str, user_prompt: str, api_key: str,
                         session: requests.Session, model_name: str) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "content-type": "application/json",
        "X-Title": _OPENROUTER_X_TITLE,
    }
    body = {
        "model": model_name,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    resp = session.post(LLM_API_URL_OPENROUTER, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def _request_anthropic(system_prompt: str, user_prompt: str, api_key: str, session: requests.Session) -> str:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": LLM_MODEL_ANTHROPIC,
        "max_tokens": 1024,
        "temperature": 0,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    resp = session.post(LLM_API_URL_ANTHROPIC, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    ).strip()


def _request_llm_text(system_prompt: str, user_prompt: str, api_key: str, session: requests.Session,
                       validate=None):
    """
    LLM_PROVIDER 경로로 텍스트 응답을 받아온다.
    openrouter면 _LLM_MODEL_CHAIN_OPENROUTER_ROLES를 순서대로 시도, 실패 시
    다음 후보로 재시도. 최종 안전망까지 실패하면 예외를 그대로 올림.
    validate(text, is_final) 콜백을 넘기면 응답 형식 이상도 재시도 대상으로 취급.
    """
    if LLM_PROVIDER != "openrouter":
        text = _request_anthropic(system_prompt, user_prompt, api_key, session)
        return validate(text, True) if validate else text

    chain = _LLM_MODEL_CHAIN_OPENROUTER_ROLES
    last_error: Exception | None = None
    for idx, (role, model_name) in enumerate(chain):
        is_final = idx == len(chain) - 1
        try:
            if idx > 0:
                print(f"[issue_grouper] 🟡 주의 - 3차 그룹핑 {role} 모델('{model_name}')로 재시도 "
                      f"({idx + 1}/{len(chain)})")
            text = _request_openrouter(system_prompt, user_prompt, api_key, session, model_name)
            return validate(text, is_final) if validate else text
        except Exception as e:
            last_error = e
            code = _LLM_MODEL_ROLE_ERROR_CODE[role]
            level = "🔴 조치필요" if is_final else "🟡 주의"
            next_note = "더 시도할 모델 없음 - 이 배치 전체 '안 묶음' fallback" if is_final else "다음 후보 모델로 재시도"
            print(f"[issue_grouper] {level} [{code}] - 3차 그룹핑 {role} 모델('{model_name}') "
                  f"호출/응답 검증 실패 - {next_note}: {type(e).__name__} - {e!r}")
    raise last_error


def _call_llm(pairs: list[tuple[dict, dict, float]], api_key: str, session: requests.Session) -> list[bool] | None:
    """
    LLM 1회 호출로 pairs 각각의 same_event 판정을 받는다.
    입출력 개수 불일치/파싱 실패/API 에러 등 신뢰 불가 응답이면 None 반환
    (fallback은 stage3_llm_assist에서 "안 묶음" 처리).
    """
    user_prompt = _build_llm_user_prompt(pairs)

    def _validate(text: str, is_final: bool) -> list:
        cleaned = text
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
        parsed = json.loads(cleaned.strip())

        if not isinstance(parsed, list) or not parsed:
            actual = len(parsed) if isinstance(parsed, list) else type(parsed).__name__
            raise ValueError(f"리스트가 아니거나 비어있음(실제 {actual}) | 실제 응답: {_snippet_for_log(text)}")
        return parsed

    try:
        parsed = _request_llm_text(_LLM_SYSTEM_PROMPT, user_prompt, api_key, session, validate=_validate)
    except Exception as e:
        print(f"[issue_grouper] 🔴 조치필요 [IG-02] - 3차 LLM({LLM_PROVIDER}) 호출/파싱 실패 - 이 배치({len(pairs)}쌍)는 "
              f"전부 '안 묶음' fallback: {type(e).__name__} - {e!r}")
        return None

    # id 기반 부분 복구 - 애매하면 안 묶음(false)이 안전한 기본값
    by_id: dict[int, bool] = {}
    for item in parsed:
        try:
            by_id[int(item["id"])] = bool(item["same_event"])
        except (KeyError, TypeError, ValueError):
            continue

    results = []
    missing = []
    for idx in range(1, len(pairs) + 1):
        if idx in by_id:
            results.append(by_id[idx])
        else:
            missing.append(idx)
            results.append(False)

    if missing:
        print(f"[issue_grouper] 🟡 주의 [IG-03] - 3차 LLM({LLM_PROVIDER}) 출력에서 id {missing} 누락"
              f"(기대 {len(pairs)}쌍 중 {len(missing)}쌍) - 그 쌍들만 '안 묶음' 기본값 처리, "
              f"나머지 {len(pairs) - len(missing)}쌍은 정상 판정 사용")

    return results


def stage3_llm_assist(borderline_pairs: list[tuple[dict, dict, float]]) -> list[tuple[dict, dict, float]]:
    """
    애매 구간 쌍을 LLM에 물어 "같은 사건"으로 확정된 쌍만 반환.
    LLM_PROVIDER에 따라 ANTHROPIC_API_KEY 또는 OPENROUTER_API_KEY 사용.
    키 없거나 전부 실패하면 안 묶음으로 안전하게 fallback.
    """
    if not borderline_pairs:
        return []

    key_env_var = "OPENROUTER_API_KEY" if LLM_PROVIDER == "openrouter" else "ANTHROPIC_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        print(f"[issue_grouper] 🟡 주의 [IG-04] - {key_env_var} 없음(LLM_PROVIDER={LLM_PROVIDER}) - 3차 LLM 보조 생략, "
              f"애매 구간 {len(borderline_pairs)}쌍 전부 '안 묶음' 기본값 유지")
        return []

    model_desc = " -> ".join(LLM_MODEL_CHAIN_OPENROUTER) if LLM_PROVIDER == "openrouter" else LLM_MODEL_ANTHROPIC
    print(f"[issue_grouper] 3차 LLM 보조 시작 - provider={LLM_PROVIDER}, model={model_desc}, "
          f"대상 {len(borderline_pairs)}쌍")

    confirmed = []
    with requests.Session() as session:
        for i in range(0, len(borderline_pairs), LLM_BATCH_SIZE):
            batch = borderline_pairs[i:i + LLM_BATCH_SIZE]
            results = _call_llm(batch, api_key, session)
            if results is None:
                continue  # 이 배치만 안 묶음 유지, 다음 배치는 계속 시도
            for (a, b, sim), same_event in zip(batch, results):
                if same_event:
                    confirmed.append((a, b, sim))

    print(f"[issue_grouper] 3차 LLM 보조 완료 - 애매 구간 {len(borderline_pairs)}쌍 중 "
          f"{len(confirmed)}쌍 '같은 사건'으로 최종 병합")
    return confirmed


# ---------------------------------------------------------------------------
# 최종 진입점: 1차 + 2차 + 3차를 합쳐서 scorer.py에 바로 넘길 수 있는 형태로 반환
# ---------------------------------------------------------------------------

def group_issues(articles: list[dict], model=None) -> list[list[dict]]:
    """
    1~3차를 순서대로 실행해 최종 이슈 그룹 리스트 반환(main.py의 score()가 호출).

    3차 병합: stage2_grouped/still_unmatched를 "구성요소"로 보고, 확정된
    쌍만 구성요소 단위로 union. 연쇄 병합 방지 - 3개 이상 연결된 컴포넌트는
    모든 쌍이 실제로 LLM에 직접 확인됐는지(클리크인지) 검증하고, 아니면
    빠진 쌍만 추가로 재확인한 뒤에도 안 되면 개별 유지.
    """
    stage1_grouped, stage1_unmatched = stage1_group(articles)

    if model is None:
        print("[issue_grouper] 🟡 주의 [IG-05] - 임베딩 모델이 없어 2차(임베딩) 매칭 생략 - 1차 결과만 사용")
        singleton = [[a] for a in stage1_unmatched]
        return stage1_grouped + singleton

    stage2_grouped, still_unmatched, borderline_pairs = stage2_group(stage1_unmatched, model=model)

    confirmed_pairs: list[tuple[dict, dict, float]] = []
    if borderline_pairs:
        print(f"[issue_grouper] 임계값 애매 구간 {len(borderline_pairs)}쌍 발견 - 3차 LLM 보조로 최종 판단")
        confirmed_pairs = stage3_llm_assist(borderline_pairs)

    components = stage2_grouped + [[a] for a in still_unmatched]
    components = _merge_confirmed_components(components, confirmed_pairs, extra_confirm=stage3_llm_assist)

    return stage1_grouped + components


def _merge_confirmed_components(components: list[list[dict]],
                                 confirmed_pairs: list[tuple[dict, dict, float]],
                                 extra_confirm=None) -> list[list[dict]]:
    """
    확정된 쌍으로 components를 추가 병합. 단순 Union-Find는 "A~B, B~C 확정"만으로
    A~C를 직접 확인한 적 없이 A+B+C를 묶는 연쇄 문제가 있어, 3개 이상 연결된
    컴포넌트는 모든 쌍이 직접 확정됐는지(클리크) 검증한다.
      - 클리크면 병합
      - 아니면 빠진 쌍만 대표 기사로 extra_confirm에 재확인, 그래도 안 되면 개별 유지

    extra_confirm: callable(pairs) -> confirmed pairs. group_issues가 stage3_llm_assist를
    넘김. None이면 재확인 없이 바로 개별 유지.
    """
    if not confirmed_pairs:
        return components

    url_to_component: dict[str, int] = {}
    for idx, comp in enumerate(components):
        for article in comp:
            url_to_component[article.get("url")] = idx

    edges: set[tuple[int, int]] = set()
    for a, b, _sim in confirmed_pairs:
        idx_a = url_to_component.get(a.get("url"))
        idx_b = url_to_component.get(b.get("url"))
        if idx_a is not None and idx_b is not None and idx_a != idx_b:
            edges.add((min(idx_a, idx_b), max(idx_a, idx_b)))

    if not edges:
        return components

    comp_uf = UnionFind(len(components))
    for idx_a, idx_b in edges:
        comp_uf.union(idx_a, idx_b)

    merged_components = []
    pending_recheck: list[tuple[int, ...]] = []
    for indices in comp_uf.groups():
        if len(indices) <= 2:
            merged_group = []
            for idx in indices:
                merged_group.extend(components[idx])
            merged_components.append(merged_group)
            continue

        is_clique = all(
            (min(x, y), max(x, y)) in edges
            for x, y in combinations(indices, 2)
        )
        if is_clique:
            merged_group = []
            for idx in indices:
                merged_group.extend(components[idx])
            merged_components.append(merged_group)
        else:
            pending_recheck.append(indices)

    if pending_recheck and extra_confirm is not None:
        extra_candidates: list[tuple[dict, dict, float]] = []
        pair_lookup: dict[tuple, tuple[int, int]] = {}
        for indices in pending_recheck:
            for x, y in combinations(indices, 2):
                key = (min(x, y), max(x, y))
                if key in edges:
                    continue
                rep_a, rep_b = components[x][0], components[y][0]
                extra_candidates.append((rep_a, rep_b, 0.0))
                pair_lookup[(rep_a.get("url"), rep_b.get("url"))] = key

        if extra_candidates:
            print(f"[issue_grouper] 사슬로만 연결된 컴포넌트 {len(pending_recheck)}개 발견 - "
                  f"빠진 쌍 {len(extra_candidates)}개만 대표 기사로 3차 LLM에 추가 확인")
            newly_confirmed = extra_confirm(extra_candidates)
            for a, b, _sim in newly_confirmed:
                key = pair_lookup.get((a.get("url"), b.get("url")))
                if key:
                    edges.add(key)

    for indices in pending_recheck:
        is_clique_now = all(
            (min(x, y), max(x, y)) in edges
            for x, y in combinations(indices, 2)
        )
        if is_clique_now:
            print(f"[issue_grouper] 사슬 컴포넌트 재확인으로 클리크 완성(컴포넌트 {len(indices)}개) - 병합")
            merged_group = []
            for idx in indices:
                merged_group.extend(components[idx])
            merged_components.append(merged_group)
        else:
            recheck_note = ("빠진 쌍을 추가로 재확인했지만 여전히 일부는 직접 확인 안 됨"
                             if extra_confirm is not None else "일부 쌍은 LLM에 직접 확인된 적 없음")
            print(f"[issue_grouper] 🟡 주의 [IG-06] - 3차 확정 쌍이 사슬로만 연결됨(컴포넌트 "
                  f"{len(indices)}개 - {recheck_note}) - 연쇄 병합 방지로 안 묶고 개별 유지")
            for idx in indices:
                merged_components.append(components[idx])

    return merged_components


# ---------------------------------------------------------------------------
# 4차: Top N 사후 재검토 + 병합 + 순위 승격
# ---------------------------------------------------------------------------
# Top N 후보끼리만 다시 "같은 사건인지" LLM 확인 후 병합, 빈 자리는 다음 순위로 승격.
# 3차와 판정 기준이 다름 - "시점이 달라도 같은 발병의 후속 보도면 같은 사건"으로 처리.
_STAGE4_SYSTEM_PROMPT = (
    "You are a judge that assists final de-duplication of a news issue "
    "ranking. Given two issue summaries (each made of one or more article "
    "titles about the same underlying story), decide whether they are "
    "actually reporting on the same real-world event or outbreak, even if "
    "worded very differently or reported on different days. "
    "Judge them as the SAME event if they describe the same disease "
    "outbreak or incident continuing, escalating, or being confirmed over "
    "time in the same country/region (e.g. an initial suspected-case "
    "report and a later article confirming wider spread of that same "
    "outbreak are the SAME event, even though the specific facts and "
    "wording changed as the story developed). "
    "Judge them as separate events if the country/region differs, or if "
    "they are genuinely unrelated incidents (different disease, different "
    "outbreak) that merely share a topic. "
    "When genuinely unsure, prefer NOT merging (same_event: false) - a "
    "missed merge is safer than an incorrect one. "
    "Output only a JSON array with no other explanation. Each element must "
    "be in the form {\"id\": number, \"same_event\": true|false}, and id "
    "must exactly match the number of the input pair."
)


def _build_stage4_user_prompt(pairs: list[tuple[dict, dict]]) -> str:
    """item(그룹)당 대표 제목 최대 5개를 A/B로 나열해 같은 사건인지 판정 요청."""
    lines = ["Judge whether each of the following pairs of issue summaries covers the same real-world event.\n"]
    for idx, (item_a, item_b) in enumerate(pairs, start=1):
        titles_a = " / ".join(item_a.get("titles", [])[:5]) or "(제목 없음)"
        titles_b = " / ".join(item_b.get("titles", [])[:5]) or "(제목 없음)"
        lines.append(f'{idx}. A: "{titles_a}" / B: "{titles_b}"')
    lines.append(
        f'\nThere are {len(pairs)} pairs total. Include the number above as "id" in each '
        f'element and answer with a JSON array only (e.g. [{{"id": 1, "same_event": true}}, '
        f'{{"id": 2, "same_event": false}}, ...]). Do not omit any id or change the order.'
    )
    return "\n".join(lines)


def _call_stage4_llm_batch(pairs: list[tuple[dict, dict]], api_key: str,
                            session: requests.Session) -> list[bool] | None:
    """단일 배치 호출 - _call_llm과 동일한 파싱/부분 복구 패턴. 배치 전체 실패 시 None."""
    user_prompt = _build_stage4_user_prompt(pairs)

    def _validate(text: str, is_final: bool) -> list:
        cleaned = text
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
        parsed = json.loads(cleaned.strip())
        if not isinstance(parsed, list) or not parsed:
            actual = len(parsed) if isinstance(parsed, list) else type(parsed).__name__
            raise ValueError(f"리스트가 아니거나 비어있음(실제 {actual}) | 실제 응답: {_snippet_for_log(text)}")
        return parsed

    try:
        parsed = _request_llm_text(_STAGE4_SYSTEM_PROMPT, user_prompt, api_key, session, validate=_validate)
    except Exception as e:
        print(f"[issue_grouper] 🔴 조치필요 [IG-11] - 4차 재검토 LLM({LLM_PROVIDER}) 호출/파싱 실패 - "
              f"이 배치({len(pairs)}쌍)는 전부 '병합 안 함' fallback: {type(e).__name__} - {e!r}")
        return None

    by_id: dict[int, bool] = {}
    for item in parsed:
        try:
            by_id[int(item["id"])] = bool(item["same_event"])
        except (KeyError, TypeError, ValueError):
            continue

    results = []
    missing = []
    for idx in range(1, len(pairs) + 1):
        if idx in by_id:
            results.append(by_id[idx])
        else:
            missing.append(idx)
            results.append(False)  # 안전한 기본값 - 병합 안 함

    if missing:
        print(f"[issue_grouper] 🟡 주의 [IG-12] - 4차 재검토 LLM({LLM_PROVIDER}) 출력에서 id {missing} 누락"
              f"(기대 {len(pairs)}쌍 중 {len(missing)}쌍) - 그 쌍들만 '병합 안 함' 기본값 처리")

    return results


def _call_stage4_llm(pairs: list[tuple[dict, dict]], api_key: str, session: requests.Session) -> list[bool]:
    """LLM_BATCH_SIZE 단위로 배치 호출 후 결과 병합. 배치 실패분은 False(병합 안 함)로 채움."""
    results: list[bool] = []
    for i in range(0, len(pairs), LLM_BATCH_SIZE):
        batch = pairs[i:i + LLM_BATCH_SIZE]
        batch_results = _call_stage4_llm_batch(batch, api_key, session)
        results.extend(batch_results if batch_results is not None else [False] * len(batch))
    return results


def stage4_dedupe_and_promote(ranked_pool: list[dict], top_n: int, label: str = "") -> list[dict]:
    """
    ranked_pool(top_n 제한 없는 전체 순위 풀) 상위 top_n을 후보로 삼아 같은
    사건 쌍을 LLM으로 재확인, 병합하고 빈 자리는 다음 순위로 채운다.
    회차당 병합 1건만 적용 후 재판단, 최대 3회. API 키 없으면 기존 순위 그대로 반환.
    반환값은 scorer.score_and_rank(top_n=N)과 동일한 형태.
    """
    if top_n is None:
        top_n = len(ranked_pool)
    candidates = list(ranked_pool[:top_n])
    reserve = list(ranked_pool[top_n:])  # 승격 후보 풀 - 이미 점수순 정렬된 상태 유지

    if len(candidates) < 2:
        return candidates

    key_env_var = "OPENROUTER_API_KEY" if LLM_PROVIDER == "openrouter" else "ANTHROPIC_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        print(f"[issue_grouper] 🟡 주의 [IG-13] - {key_env_var} 없음(LLM_PROVIDER={LLM_PROVIDER}) - "
              f"4차 Top N 재검토 생략(기존 순위 그대로 사용)")
        return candidates

    prefix = f"[issue_grouper] {label} " if label else "[issue_grouper] "
    max_rounds = 3
    merged_any = False

    with requests.Session() as session:
        for round_idx in range(1, max_rounds + 1):
            if len(candidates) < 2:
                break

            index_pairs = list(combinations(range(len(candidates)), 2))
            llm_pairs = [(candidates[i], candidates[j]) for i, j in index_pairs]
            print(f"{prefix}4차 Top N 재검토 {round_idx}회차 - 후보 {len(candidates)}건({len(index_pairs)}쌍) 확인")
            results = _call_stage4_llm(llm_pairs, api_key, session)

            merge_at = next((k for k, same in enumerate(results) if same), None)
            if merge_at is None:
                break  # 이번 회차에 병합할 쌍 없음 - 더 볼 것 없으니 종료

            i, j = index_pairs[merge_at]
            item_a, item_b = candidates[i], candidates[j]
            rep_a = item_a["titles"][0] if item_a.get("titles") else "(제목 없음)"
            rep_b = item_b["titles"][0] if item_b.get("titles") else "(제목 없음)"
            print(f"{prefix}🔗 같은 사건으로 판정돼 병합: '{rep_a}' + '{rep_b}'")

            merged_articles = item_a.get("articles", []) + item_b.get("articles", [])
            merged_item = scorer.score_group(merged_articles)
            merged_any = True

            candidates = [c for k, c in enumerate(candidates) if k not in (i, j)]
            candidates.append(merged_item)

            if reserve:
                promoted = reserve.pop(0)
                rep_p = promoted["titles"][0] if promoted.get("titles") else "(제목 없음)"
                print(f"{prefix}⬆️ 빈 자리에 다음 순위 후보 승격: '{rep_p}'")
                candidates.append(promoted)

            candidates.sort(key=lambda c: c["issue_score"], reverse=True)
            candidates = candidates[:top_n]
        else:
            print(f"{prefix}🟡 주의 - 4차 재검토가 최대 {max_rounds}회 한도에 도달함 - "
                  f"남은 중복이 더 있을 수 있음(다음 실행에서 다시 확인됨)")

    if merged_any:
        print(f"{prefix}4차 Top N 재검토 완료 - 최종 {len(candidates)}건")

    return candidates


class _FakeEmbeddingModel:
    """
    테스트 전용 가짜 임베딩 모델. 매칭돼야 할 텍스트끼리는 비슷한 벡터,
    나머지는 서로 직교하는 벡터를 부여해 우연한 유사도 충돌을 방지.
    model.encode(texts, normalize_embeddings=True) 인터페이스만 흉내냄.
    """

    def encode(self, texts: list[str], normalize_embeddings: bool = True):
        import numpy as np

        rng = np.random.default_rng(seed=42)
        dim = len(texts) + 1

        vectors = []
        next_free_axis = 1  # 0번 축은 곡물/사료 그룹 전용
        for text in texts:
            vec = np.zeros(dim)
            if "옥수수" in text or "grain" in text.lower() or "feed price" in text.lower():
                vec[0] = 1.0
            else:
                vec[next_free_axis] = 1.0
                next_free_axis += 1
            vec += rng.normal(scale=0.02, size=dim)
            vectors.append(vec)
        return np.array(vectors)


if __name__ == "__main__":
    # === 1차만 단독 확인 (무거운 설치 없이) ===
    sample_articles = [
        {"title": "전북서 고병원성 조류독감 추가 발생", "url": "https://a.com/1"},
        {"title": "USDA reports new avian flu outbreak in Iowa", "url": "https://b.com/1"},
        {"title": "구제역 확산에 한우 수출 잠정 중단", "url": "https://a.com/2"},
        {"title": "Feed prices expected to rise amid grain shortage", "url": "https://b.com/2"},  # 매칭 안 되어야 함
        {"title": "옥수수 국제가격 상승, 배합사료 원가 부담 커져", "url": "https://a.com/3"},  # 같은 이슈, 2차가 잡아야 함
    ]

    grouped, unmatched = stage1_group(sample_articles)
    print(f"=== 1차 매칭으로 묶인 그룹: {len(grouped)}개 ===")
    for g in grouped:
        print(f"  - {len(g)}건: {[a['title'] for a in g]}")
    print(f"\n=== 1차에서 못 잡은 기사(2차로 넘길 대상): {len(unmatched)}건 ===")
    for a in unmatched:
        print(f"  - {a['title']}")

    print("\n\n=== group_issues() 전체 실행 (가짜 임베딩 모델) ===")
    fake_model = _FakeEmbeddingModel()
    final_groups = group_issues(sample_articles, model=fake_model)

    print(f"\n최종 그룹 수: {len(final_groups)}개")
    for g in final_groups:
        titles = [a["title"] for a in g]
        tag = "그룹" if len(g) >= 2 else "단독"
        print(f"  [{tag}, {len(g)}건] {titles}")

    merged_ok = any(
        len(g) == 2 and
        {"옥수수 국제가격 상승, 배합사료 원가 부담 커져", "Feed prices expected to rise amid grain shortage"}
        == {a["title"] for a in g}
        for g in final_groups
    )
    print(f"\n[검증] 2차 임베딩으로 사료가격 이슈 그룹핑 성공: {merged_ok}")
    assert merged_ok, "2차 임베딩 그룹핑 로직에 문제가 있음"

    def _same_group(title_a: str, title_b: str) -> bool:
        return any({title_a, title_b} <= {a["title"] for a in g} for g in final_groups)

    unrelated_pairs = [
        ("전북서 고병원성 조류독감 추가 발생", "구제역 확산에 한우 수출 잠정 중단"),
        ("USDA reports new avian flu outbreak in Iowa", "구제역 확산에 한우 수출 잠정 중단"),
        ("USDA reports new avian flu outbreak in Iowa", "옥수수 국제가격 상승, 배합사료 원가 부담 커져"),
    ]
    for title_a, title_b in unrelated_pairs:
        wrongly_merged = _same_group(title_a, title_b)
        print(f"[검증] '{title_a[:20]}...' <-> '{title_b[:20]}...' 안 묶임: {not wrongly_merged}")
        assert not wrongly_merged, f"무관한 기사가 잘못 묶임: {title_a} <-> {title_b}"
    print("\n[검증] 무관한 기사 오탐 없음 - 전부 통과")

    print("\n\n=== 3차 LLM 보조 배선 확인 (mock API, 실제 네트워크 호출 없음) ===")
    import os as _os
    import requests as _requests

    _mock_calls = []

    def _mock_session_post(self, url, headers=None, json=None, timeout=None):
        _mock_calls.append(url)
        pairs_count = json["messages"][0]["content"].count('A: "')
        results = [{"id": i, "same_event": True} for i in range(1, pairs_count + 1)]
        text = __import__("json").dumps(results)

        class _MockResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"content": [{"type": "text", "text": text}]}
        return _MockResp()

    _original_session_post = _requests.Session.post
    _requests.Session.post = _mock_session_post
    _os.environ["ANTHROPIC_API_KEY"] = "sk-ant-smoke-test-dummy-key"
    try:
        fake_borderline = [
            (
                {"title": "전북 조류독감 추가 확진", "url": "https://smoke/1"},
                {"title": "Jeonbuk confirms new avian flu case", "url": "https://smoke/2"},
                0.72,
            )
        ]
        confirmed = stage3_llm_assist(fake_borderline)
        assert len(confirmed) == 1, "mock 응답이 전부 True인데 confirmed가 1개가 아님 - 배선 문제"
        assert _mock_calls, "session.post가 한 번도 호출 안 됨 - stage3가 실제로 API를 안 부름"
        print(f"[검증] stage3_llm_assist 배선 정상 - mock 호출 {len(_mock_calls)}회, "
              f"confirmed {len(confirmed)}쌍")
    finally:
        _requests.Session.post = _original_session_post
        del _os.environ["ANTHROPIC_API_KEY"]

    # 헤더 latin-1 인코딩 검증(한글 섞이면 UnicodeEncodeError)
    for header_name, header_value in {
        "Authorization": "Bearer dummy-key-for-header-encoding-check",
        "content-type": "application/json",
        "X-Title": _OPENROUTER_X_TITLE,
    }.items():
        header_value.encode("latin-1")
    print(f"[검증] OpenRouter 요청 헤더(X-Title='{_OPENROUTER_X_TITLE}') latin-1 인코딩 가능 확인 - 통과")

    print("\n[issue_grouper] 자체 점검 전체 통과 (1차/2차 그룹핑 + 음성 검증 + 3차 배선 확인 + 헤더 인코딩 확인)")