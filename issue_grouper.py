"""
issue_grouper.py
"2.1 이슈 그룹핑" 담당 모듈 (알고리즘 문서 "2.1 이슈 그룹핑" 참조)

문서에 정의된 하이브리드 파이프라인 중 지금 이 파일에서 구현하는 범위:
  1차 - KR<->EN 키워드 사전 매칭         -> 구현은 됐으나 현재 사전을 비워둠
                                          (2026-07-14, ISSUE_SYNONYM_GROUPS 주석 참조)
  2차 - BGE-M3 임베딩 코사인 유사도       -> 구현
  3차 - LLM 그룹핑 보조 (임계값 애매 구간) -> 구현 완료 (2026-07-15, 아래
                                          "3차: LLM 그룹핑 보조" 섹션 참조).
                                          Anthropic API(Haiku 4.5)를 호출해
                                          borderline_pairs만 최종 판단한다.

3차까지 전부 구현되면서 group_issues()가 1~3차를 순서대로 실행해 최종
그룹 리스트를 만든다 (아래 group_issues 참조).
"""

import json
import os
from itertools import combinations

import requests

# keyword_tagger.py가 이미 "AI"를 매칭에서 제외하기로 확정한 이유와 완전히
# 똑같은 문제가 여기서도 재현됐다 (아래 ISSUE_SYNONYM_GROUPS 주석 및
# _stage1_match_keys 참고) - 같은 판단 기준을 두 파일에 따로 관리하면
# 나중에 하나만 고치고 하나는 놓치는 사고가 나기 쉬우므로, keyword_tagger의
# EXCLUDED_TERMS를 그대로 재사용해 기준을 한 곳으로 통일한다.
from keyword_tagger import EXCLUDED_TERMS


# ---------------------------------------------------------------------------
# 1차: KR<->EN 키워드 사전 매칭
# ---------------------------------------------------------------------------
#
# 문서 예시: "조류독감"/"AI" <-> "avian flu"/"HPAI" 처럼, 같은 이슈를 가리키는
# 한글/영어 표현들을 하나의 묶음(set)으로 등록해둔다. 제목 안에 같은 묶음의
# 단어가 들어있는 두 기사는 "1차에서 바로 매칭"된다.
#
# 주의 - keyword_tagger.py의 CATEGORY_KEYWORDS와는 목적이 다르다:
#   - keyword_tagger: "이 기사가 질병명/시장가격/제도 중 어떤 카테고리인가"
#     (넓은 카테고리 분류용, 이미 구현됨)
#   - 여기(ISSUE_SYNONYM_GROUPS): "이 두 기사가 완전히 같은 사건을 다루는가"
#     (개별 이슈 매칭용, 카테고리보다 훨씬 좁고 구체적인 단위)
# 예를 들어 keyword_tagger는 "조류독감"과 "구제역"을 둘 다 "질병명" 카테고리
# 하나로 묶지만, 여기서는 서로 다른 이슈이므로 별개 그룹이어야 한다.
#
# ** 2026-07-14 세션 결정: 이슈 그룹핑의 정의를 "동일 사건만"으로 확정 **
# (예: 한국 조류독감 발생과 미국 조류독감 발생은 같은 질병이어도 별도 이슈)
# 이유: 세계적으로 큰 이슈면 각 발생건이 각자 랭킹에 자연스럽게 올라올 테니
# 억지로 합칠 필요 없음.
#
# 이 정의 확정으로 아래처럼 "질병명" 단위로 묶던 기존 1차 사전 매칭은
# 국가/사건을 구분 못해서 정의와 안 맞는다는 게 실제 재검증(2026-07-14,
# calibrate_issue_grouper.py 실행)에서 확인됨 - 예를 들어 "조류독감" 묶음은
# 국내 기사 1건과 필리핀/캄보디아/호주/미국 등 전혀 다른 나라의 bird flu
# 기사들을 전부 한 그룹으로 묶어버렸고, "구제역" 묶음도 한국 예천 발생
# 기사들과 South Africa의 FMD 백신 관련 기사가 섞여버렸다 (실측 로그
# calibration_log.txt 참조). 그래서 이 사전은 비워둔다 - "완전 동일 사건"
# 매칭은 국가/장소/시점까지 구분해야 하는 훨씬 좁은 단위라 질병명 키워드
# 매칭으로는 애초에 표현이 불가능함. 2차(BGE-M3 임베딩)에만 의존하는
# 구조로 전환 - 임베딩은 제목 전체의 의미를 보므로 "어느 나라 사건인지"
# 같은 맥락도 (완벽하진 않아도) 어느 정도 반영됨.
#
# 아래 함수(_stage1_match_keys, stage1_group)는 인프라 자체는 남겨둔다 -
# 나중에 "완전 동일 사건"을 표현할 수 있는 더 구체적인 키(예: 특정 지명+
# 특정 발생 시점 조합)로 다시 채울 가능성이 있을 때 재사용 가능. 지금은
# 빈 리스트라 이 함수들이 항상 매칭 없음(빈 set)을 반환 - 즉 모든 기사가
# 2차(임베딩)로 그대로 넘어감.
ISSUE_SYNONYM_GROUPS: list[set[str]] = []


def _stage1_match_keys(title: str) -> set[int]:
    """
    제목 하나가 ISSUE_SYNONYM_GROUPS의 몇 번 묶음(들)에 걸리는지 반환한다.
    (대소문자 무시 부분 문자열 매칭 - keyword_tagger.py의 tag_title과 같은 방식)

    반환값은 "걸린 묶음의 인덱스 집합" - 한 제목이 여러 묶음에 걸릴 수도
    있어서(예: "조류독감과 구제역 동시 발생" 같은 드문 제목) 인덱스 하나가
    아니라 set으로 돌려준다.
    """
    title_lower = title.lower()
    matched = set()
    for idx, synonyms in enumerate(ISSUE_SYNONYM_GROUPS):
        usable_synonyms = synonyms - EXCLUDED_TERMS
        if any(syn.lower() in title_lower for syn in usable_synonyms):
            matched.add(idx)
    return matched


# ---------------------------------------------------------------------------
# 그룹 병합 로직 (Union-Find, 일명 "합집합-찾기" 구조)
# ---------------------------------------------------------------------------
#
# 왜 필요한가: "A와 B가 매칭", "B와 C가 매칭"이라는 개별 판정 결과가 나왔을 때,
# 최종적으로는 "A, B, C가 전부 한 그룹"이어야 한다 (B를 통해 A와 C도 간접
# 연결됨). 이렇게 "여러 개의 쌍(pair) 매칭 결과를 하나의 그룹으로 합쳐주는"
# 표준 자료구조가 Union-Find다. 코드 자체는 짧지만 동작 원리가 직관적이지
# 않을 수 있어서 아래에 최대한 풀어서 주석을 달았다.
class UnionFind:
    """
    각 기사(인덱스)를 하나의 "그룹 대표"에 연결해두는 구조.
    - find(i): i가 속한 그룹의 "대표"가 누구인지 찾는다
    - union(i, j): i와 j를 같은 그룹으로 합친다
    """

    def __init__(self, n: int):
        # 처음엔 모두가 "자기 자신"을 대표로 하는 독립된 그룹
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        # i의 대표를 찾아 올라간다. 대표가 자기 자신이 아니면 계속 위로.
        while self.parent[i] != i:
            # 경로 압축: 다음에 더 빨리 찾도록 중간 노드를 대표에 바로 연결
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        root_i, root_j = self.find(i), self.find(j)
        if root_i != root_j:
            self.parent[root_j] = root_i

    def groups(self) -> list[list[int]]:
        """최종 그룹들을 [[기사 인덱스, ...], ...] 형태로 뽑아낸다."""
        buckets: dict[int, list[int]] = {}
        for i in range(len(self.parent)):
            root = self.find(i)
            buckets.setdefault(root, []).append(i)
        return list(buckets.values())


# ---------------------------------------------------------------------------
# 1차 매칭만으로 그룹 만들기 (2차 임베딩은 다음 스텝에서 추가)
# ---------------------------------------------------------------------------

def stage1_group(articles: list[dict]) -> tuple[list[list[dict]], list[dict]]:
    """
    1차 키워드 사전 매칭만으로 그룹을 만든다.

    반환값:
      grouped: 1차에서 이미 묶인 그룹들 (list[list[기사]])
                - 묶였다는 건 "같은 묶음(ISSUE_SYNONYM_GROUPS)에 걸린 기사가
                  2건 이상"이라는 뜻. 딱 1건만 걸린 경우는 "그룹"이 아니라
                  그냥 매칭 안 된 기사와 동일하게 취급해 unmatched로 보낸다
                  (그룹은 최소 2건부터 의미가 있음).
      unmatched: 1차에서 못 잡은 기사들 -> 다음 스텝(2차 임베딩)으로 넘길 대상
    """
    n = len(articles)
    uf = UnionFind(n)

    # 각 묶음(synonym group) 인덱스에 걸린 기사들끼리 전부 union
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
#
# 문서 2.1 "작동 방식" 2~5번 그대로: 1차에서 못 잡은 기사만 대상으로,
# 전체 기사끼리(국내x해외 뿐 아니라 국내-국내/해외-해외도 포함) 코사인
# 유사도를 계산해서 threshold 이상이면 그룹핑한다.
#
# 임계값(THRESHOLD)은 문서 "7. 아직 결정 안 된 것들"에 명시된 대로 아직
# 미확정 값이다 (예상 범위 0.7~0.8대). 여기 0.75는 시범 운영 중 조정할
# 잠정값 - scorer.py의 PRESS_DEDUP_CAP과 같은 성격의 "잠정 상수".
THRESHOLD = 0.75

# threshold 근처 "애매한 구간"의 폭. 예를 들어 THRESHOLD=0.75,
# BORDERLINE_MARGIN=0.05면 0.70~0.75 사이가 애매 구간 -> 문서 3차(LLM 보조)
# 대상. 2026-07-15부터 3차(stage3_llm_assist)가 실제로 이 borderline_pairs를
# 입력받아 처리한다 - LLM이 "같은 사건"으로 확정한 쌍만 병합되고, 그 외
# (API 키 없음/호출 실패 등)는 여전히 "안 묶는" 보수적 기본값으로 fallback.
BORDERLINE_MARGIN = 0.05


def _embedding_text(article: dict) -> str:
    """
    임베딩에 넣을 텍스트를 만든다. 문서 2.1: "제목 + 있으면 본문 요약 일부".
    본문(body)이 있는 소스(WATT)는 앞부분 200자만 덧붙인다 - 본문 전체를
    넣으면 계산량만 늘고, 어차피 "같은 이슈인지" 판단엔 도입부만으로 충분한
    경우가 대부분이라 문서 취지("일부")에 맞춰 짧게 자른다.
    """
    title = article.get("title", "")
    body = article.get("body")
    if body:
        return f"{title} {body[:200]}"
    return title


def _cosine_similarity_matrix(vectors):
    """
    벡터 리스트 전체에 대한 N x N 코사인 유사도 행렬을 계산한다.
    numpy만 있으면 되고, BGE-M3가 이미 정규화된 벡터를 주는 편이지만
    혹시 몰라 직접 정규화도 한 번 해준다 (이중으로 해도 결과는 동일 -
    이미 정규화된 벡터를 다시 정규화해도 크기가 안 변하므로 안전).
    """
    import numpy as np

    vectors = np.array(vectors)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    normalized = vectors / norms
    return normalized @ normalized.T


def stage2_group(
    articles: list[dict],
    model=None,
    threshold: float = THRESHOLD,
    borderline_margin: float = BORDERLINE_MARGIN,
) -> tuple[list[list[dict]], list[dict], list[tuple[dict, dict, float]]]:
    """
    1차에서 못 잡은 기사들을 BGE-M3 임베딩으로 매칭한다.

    model: sentence_transformers.SentenceTransformer 인스턴스를 주입받는다
           (함수 안에서 직접 모델을 로드하지 않는 이유: 모델 로드 자체가
           무거운 작업이라, 기사 배치마다 매번 새로 로드하면 안 되기 때문 -
           호출하는 쪽(main.py)에서 한 번만 로드해서 넘겨주는 구조로 설계)

    반환값:
      grouped: 2차에서 새로 묶인 그룹들
      still_unmatched: 2차에서도 못 잡은 기사들 (혼자 남는 "단독 기사" -
                        4번 섹션 (A-1) fallback 대상이 됨)
      borderline_pairs: threshold 근처 애매 구간에 걸린 (기사A, 기사B, 유사도)
                        쌍 목록 - 3차 LLM 보조가 아직 없어서 지금은 기록만
                        해두고 그룹핑엔 반영하지 않음 (보수적 기본값)
    """
    if not articles:
        return [], [], []

    texts = [_embedding_text(a) for a in articles]
    vectors = model.encode(texts, normalize_embeddings=True)
    sim_matrix = _cosine_similarity_matrix(vectors)

    n = len(articles)
    uf = UnionFind(n)

    # ** 2026-07-14 버그 수정: 2-pass로 분리 **
    # 기존엔 한 pass 안에서 "threshold 이상이면 union, 아니면 borderline"을
    # 같이 처리했는데, 이러면 i-j가 서로 직접은 threshold 미만이라도 다른
    # 기사 k를 거쳐 간접적으로(transitively) 이미 같은 그룹으로 묶인 경우까지
    # borderline에 중복으로 기록되는 문제가 있었다. 실제 재검증(2026-07-14,
    # calibrate_issue_grouper.py)에서 확인된 사례: "농협 2200억" 기사 54건이
    # 이미 다른 엣지들로 전부 한 그룹에 묶였는데도, 그 54건 내부의 쌍들이
    # threshold 바로 아래(0.70~0.75)라는 이유만으로 446개 borderline 쌍 중
    # 342개(77%)를 차지함 - 이미 그룹핑 결과가 확정된 쌍인데도 LLM 보조
    # 대상으로 잡혀서, 4번 섹션 "전수 호출 아님, 비용 고려" 설계 의도가
    # 실제 스케일에서 깨지는 원인이 됐다.
    #
    # 그래서 1st pass에서 union만 먼저 전부 끝내고(간접 연결까지 확정), 2nd
    # pass에서 borderline 후보를 검사할 때 "두 기사가 이미 같은 그룹인가"를
    # 같이 확인해서, 이미 그룹이 확정된 쌍은 건너뛴다. 오직 "이 쌍의 판정에
    # 따라 최종 그룹핑 결과가 실제로 달라지는" 쌍만 LLM 보조 대상으로 남는다.
    for i, j in combinations(range(n), 2):
        sim = float(sim_matrix[i][j])
        if sim >= threshold:
            uf.union(i, j)

    borderline_pairs = []
    for i, j in combinations(range(n), 2):
        if uf.find(i) == uf.find(j):
            # 이미 같은 그룹으로 확정됨 (직접이든 다른 기사를 거친 간접
            # 연결이든) - 이 쌍의 판정은 최종 결과에 영향이 없으므로 스킵
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
#
# 문서 4번 섹션 (B) 그대로: 2차(임베딩) 유사도가 threshold 근처 애매 구간에
# 걸린 쌍만 LLM에 물어봐서 "같은 사건인지 아닌지" 최종 판단한다.
# (전수 호출 아님 - stage2_group에서 이미 borderline_pairs로 걸러진 소수만
# 대상이라, 2026-07-14 2-pass 버그 수정으로 446개 -> 70개까지 줄어든 규모.)
#
# 판정 기준은 2.1에서 확정한 이슈 그룹핑 정의(2026-07-14)를 그대로 따른다:
# "같은 사건"이란 같은 질병/주제여도 국가·장소·시점이 다르면 별개 이슈다
# (예: 한국 조류독감 발생과 미국 조류독감 발생은 별개). 이 기준을 LLM에게도
# 시스템 프롬프트로 명시한다.
#
# ** 프로바이더 스위치 (2026-07-15 추가) **
# 기본은 Anthropic(Haiku 4.5) - 9.5 섹션 결론대로 이 프로젝트 규모에서
# 유료 API 비용은 무시 가능한 수준이고, 은퇴 공지 의무가 있어 장기 운영에
# 안전하다. 다만 API 키 발급 결재가 아직 안 난 상태에서 로컬 개발/검증을
# 막을 이유는 없으므로, 환경변수 LLM_PROVIDER로 임시 대체 경로(OpenRouter
# 무료 모델)를 켤 수 있게 했다 - 9.5 섹션 "모델 이름은 설정 파일 한 곳에서만
# 관리" 원칙대로, 프로바이더별 설정을 이 블록 하나에 모아둔다.
#
#   LLM_PROVIDER=anthropic (기본값, 아무것도 안 하면 이 경로) - ANTHROPIC_API_KEY 사용
#   LLM_PROVIDER=openrouter                                  - OPENROUTER_API_KEY 사용
#
# ** 중요 - openrouter는 "로컬 검증 전용" 임시 경로다 **
# 9.5 섹션이 무료 모델을 권장하지 않는 이유(예고 없는 정책 변경/모델 제거)가
# 그대로 적용되므로, GitHub Actions 등 실제 운영 환경에는 LLM_PROVIDER를
# 설정하지 말고 기본값(anthropic)을 그대로 둘 것 - 키 발급이 승인되면 이
# 환경변수 자체를 지우기만 하면 원래 경로로 돌아간다.
#
# 무료 모델 하나를 못 박지 않고 OpenRouter의 자체 무료 라우터(openrouter/free)
# 를 기본값으로 쓴 이유: 개별 :free 모델은 공급사가 예고 없이 무료 태그를
# 뗄 수 있어(9.5 섹션과 같은 리스크) 코드가 조용히 깨질 수 있는데,
# openrouter/free는 그 라우팅 자체를 OpenRouter가 대신 처리해준다. 특정
# 모델을 고정하고 싶으면 OPENROUTER_MODEL 환경변수로 덮어쓸 수 있다.
#
# ** 2026-07-15 버그 수정 - os.environ.get(key, default) 대신 or 사용 **
# GitHub Actions에서 리포에 등록 안 된 Variable을 `${{ vars.X }}`로 참조하면
# "아예 안 넘어옴"이 아니라 "빈 문자열로 채워진 환경변수"가 된다(GitHub 공식
# 문서: "설정 안 된 configuration variable을 참조하면 빈 문자열로 평가됨").
# `os.environ.get(key, default)`의 default는 키가 "아예 없을 때"만 적용되고
# 빈 문자열이 있으면 그 빈 문자열을 그대로 돌려주므로, OPENROUTER_MODEL을
# Variables에 등록 안 한 상태로 workflow의 `OPENROUTER_MODEL: ${{ vars.OPENROUTER_MODEL }}`
# 를 그대로 두면 LLM_MODEL_OPENROUTER가 빈 문자열이 되어 OpenRouter API가
# "model" 필드 없음으로 400 Bad Request를 던지는 게 실제로 재현됨(실측
# 로그: "model=, 대상 66쌍" 다음 400 에러 26회). `or` 연산자를 쓰면 빈
# 문자열도 falsy라 기본값으로 자연스럽게 대체된다.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER") or "anthropic"

LLM_MODEL_ANTHROPIC = "claude-haiku-4-5-20251001"
LLM_API_URL_ANTHROPIC = "https://api.anthropic.com/v1/messages"

LLM_MODEL_OPENROUTER = os.environ.get("OPENROUTER_MODEL") or "openrouter/free"
LLM_API_URL_OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

LLM_BATCH_SIZE = 20  # 한 번의 API 호출에 몇 쌍까지 같이 물어볼지 (호출 수 절약,
                      # OpenRouter 무료 티어 분당/일 요청 한도 감안해도 안전한 크기)

_LLM_SYSTEM_PROMPT = (
    "너는 뉴스 이슈 그룹핑을 보조하는 판정기다. 두 기사 제목이 주어지면, "
    "두 기사가 \"완전히 동일한 사건\"을 다루는지 판단하라. "
    "같은 질병/주제를 다뤄도 발생 국가·장소·시점이 다르면 별개 사건으로 "
    "판단한다 (예: 한국의 조류독감 발생 기사와 미국의 조류독감 발생 기사는 "
    "질병명이 같아도 별개 사건). "
    "제목 언어가 서로 다를 수 있다(한국어/영어/기타 언어 혼재) - 언어가 "
    "달라도 같은 사건을 가리키면 같은 사건으로 판단한다. "
    "판단이 확실하지 않으면 반드시 false로 답한다(보수적 기본값 - 잘못 "
    "묶는 것보다 안 묶는 게 안전하다). "
    "다른 설명 없이 JSON 배열만 출력한다. 각 원소는 {\"same_event\": true|false} "
    "형태이며, 입력받은 쌍의 순서와 개수를 정확히 맞춰야 한다."
)


def _build_llm_user_prompt(pairs: list[tuple[dict, dict, float]]) -> str:
    lines = ["다음 기사 제목 쌍들이 각각 완전히 동일한 사건을 다루는지 판단해줘.\n"]
    for idx, (a, b, _sim) in enumerate(pairs, start=1):
        lines.append(f"{idx}. A: \"{a.get('title', '')}\" / B: \"{b.get('title', '')}\"")
    lines.append(
        f"\n총 {len(pairs)}개 쌍이다. 이 개수 그대로 JSON 배열로만 답하라 (예: "
        f"[{{\"same_event\": true}}, {{\"same_event\": false}}, ...])."
    )
    return "\n".join(lines)


def _call_llm(pairs: list[tuple[dict, dict, float]], api_key: str) -> list[bool] | None:
    """
    LLM API를 한 번 호출해서 pairs 각각에 대한 same_event 판정을 받아온다.
    LLM_PROVIDER 값에 따라 Anthropic(claude-haiku-4-5-20251001) 또는
    OpenRouter(무료 라우터/모델)로 분기한다 - 요청 형식은 프로바이더마다
    다르지만(Anthropic: content 블록 리스트, OpenRouter: OpenAI 호환
    choices[0].message.content), 이후 파싱/검증 로직은 공통이다.

    입력/출력 개수 불일치, JSON 파싱 실패, API 에러, 항목 형식 이상 등
    신뢰할 수 없는 응답이면 None을 반환한다 - 9.4 "출력 형식을 코드로 자동
    검증... 어긋나면 fallback" 원칙을 여기서도 그대로 적용 (fallback은 이
    함수를 부르는 stage3_llm_assist 쪽에서 "안 묶음"으로 처리).
    """
    user_prompt = _build_llm_user_prompt(pairs)

    try:
        if LLM_PROVIDER == "openrouter":
            headers = {
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
                # OpenRouter 권장 헤더(선택) - 프로젝트 식별용, 없어도 동작함
                "X-Title": "사료축산뉴스-이슈그룹핑-3차보조",
            }
            body = {
                "model": LLM_MODEL_OPENROUTER,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": _LLM_SYSTEM_PROMPT},
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
                "temperature": 0,  # 9.4 "temperature 낮게" 원칙 그대로 - 판정 일관성 우선
                "system": _LLM_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_prompt}],
            }
            resp = requests.post(LLM_API_URL_ANTHROPIC, headers=headers, json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            text = "".join(
                block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
            ).strip()

        # 코드 펜스(```json ... ```)로 감싸서 올 때가 있어 방어적으로 벗겨낸다
        # (무료 모델은 이런 포맷 이탈이 Haiku보다 잦을 수 있어 특히 중요)
        if text.startswith("```"):
            text = text.split("```")[1]
            text = text[4:] if text.startswith("json") else text
        parsed = json.loads(text.strip())
    except Exception as e:
        print(f"[issue_grouper] 3차 LLM({LLM_PROVIDER}) 호출/파싱 실패 - 이 배치({len(pairs)}쌍)는 "
              f"전부 '안 묶음' fallback: {type(e).__name__} - {e!r}")
        return None

    if not isinstance(parsed, list) or len(parsed) != len(pairs):
        actual = len(parsed) if isinstance(parsed, list) else type(parsed).__name__
        print(f"[issue_grouper] 3차 LLM({LLM_PROVIDER}) 출력 개수/형식 불일치(기대 {len(pairs)}, "
              f"실제 {actual}) - 이 배치는 전부 '안 묶음' fallback")
        return None

    results = []
    for item in parsed:
        if not isinstance(item, dict) or not isinstance(item.get("same_event"), bool):
            print(f"[issue_grouper] 3차 LLM({LLM_PROVIDER}) 출력 항목 형식 이상 - "
                  f"이 배치는 전부 '안 묶음' fallback")
            return None
        results.append(item["same_event"])
    return results


def stage3_llm_assist(borderline_pairs: list[tuple[dict, dict, float]]) -> list[tuple[dict, dict, float]]:
    """
    2차에서 애매 구간에 걸린 쌍들을 LLM에 물어봐서, "같은 사건"으로 확정된
    쌍만 골라 반환한다 (문서 4번 섹션 (B) - 그룹을 지우는 게 아니라 묶을지
    말지만 판단).

    LLM_PROVIDER(기본 anthropic)에 따라 필요한 API 키 환경변수가 다르다:
      anthropic  -> ANTHROPIC_API_KEY
      openrouter -> OPENROUTER_API_KEY (임시 로컬 검증용 - 위 프로바이더
                    스위치 주석 참조, 운영 환경에서는 쓰지 않을 것)

    해당 키가 없거나 모든 배치 호출이 실패하면, 9.4/9.5 원칙대로 "안 묶음"
    보수적 기본값으로 안전하게 fallback한다 - 이 경우 group_issues의 최종
    결과는 3차가 아예 없던 이전 동작과 동일해지므로 전체 파이프라인이
    죽지 않는다.
    """
    if not borderline_pairs:
        return []

    key_env_var = "OPENROUTER_API_KEY" if LLM_PROVIDER == "openrouter" else "ANTHROPIC_API_KEY"
    api_key = os.environ.get(key_env_var)
    if not api_key:
        print(f"[issue_grouper] {key_env_var} 없음(LLM_PROVIDER={LLM_PROVIDER}) - 3차 LLM 보조 생략, "
              f"애매 구간 {len(borderline_pairs)}쌍 전부 '안 묶음' 기본값 유지")
        return []

    model_name = LLM_MODEL_OPENROUTER if LLM_PROVIDER == "openrouter" else LLM_MODEL_ANTHROPIC
    print(f"[issue_grouper] 3차 LLM 보조 시작 - provider={LLM_PROVIDER}, model={model_name}, "
          f"대상 {len(borderline_pairs)}쌍")

    confirmed = []
    for i in range(0, len(borderline_pairs), LLM_BATCH_SIZE):
        batch = borderline_pairs[i:i + LLM_BATCH_SIZE]
        results = _call_llm(batch, api_key)
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
    1차(사전) + 2차(임베딩) + 3차(LLM 보조)를 순서대로 실행해 최종 이슈 그룹
    리스트를 만든다.

    이 함수의 반환값은 scorer.score_and_rank()가 받는 입력과 형태가 동일하다
    (list[list[dict]]) - main.py에서 scorer.to_singleton_groups(articles) 호출을
    이 함수 호출로 그대로 바꿔치기하면 된다 (scorer.py 상단 docstring에 이미
    이렇게 하라고 적혀 있음).

    ** 3차 병합 방식 **
    stage2_group의 결과(stage2_grouped 각 그룹 + still_unmatched 각 기사)를
    "구성요소(component)"로 보고, stage3_llm_assist가 "같은 사건"으로 확정한
    쌍만 이 구성요소들끼리 추가로 union한다 (Union-Find를 구성요소 단위로
    한 번 더 적용 - 그룹 안에 이미 묶인 기사와 아직 단독인 기사가 한 쌍으로
    확정될 수도 있으므로, "기사 단위"가 아니라 "구성요소 단위"로 합쳐야
    한다). article의 url을 구성요소 식별에 쓴다 - 이 시스템의 공통 스키마상
    url은 항상 존재하고 고유하다 (2번 섹션 "완전 동일 기사 제거" 로직도
    같은 전제로 URL을 키로 씀).
    """
    stage1_grouped, stage1_unmatched = stage1_group(articles)

    if model is None:
        # 모델이 안 주어졌으면(예: 아직 설치 전 단계 테스트) 2차 없이
        # 1차 결과 + 나머지를 단독 그룹으로 반환 - to_singleton_groups와
        # 동일한 안전한 fallback (2차가 없으면 3차의 재료인 borderline_pairs
        # 자체가 안 생기므로 3차도 자연히 생략됨)
        print("[issue_grouper] 임베딩 모델이 없어 2차(임베딩) 매칭 생략 - 1차 결과만 사용")
        singleton = [[a] for a in stage1_unmatched]
        return stage1_grouped + singleton

    stage2_grouped, still_unmatched, borderline_pairs = stage2_group(stage1_unmatched, model=model)

    confirmed_pairs: list[tuple[dict, dict, float]] = []
    if borderline_pairs:
        print(f"[issue_grouper] 임계값 애매 구간 {len(borderline_pairs)}쌍 발견 - 3차 LLM 보조로 최종 판단")
        for a, b, sim in borderline_pairs:
            print(f"  - ({sim:.3f}) {a['title'][:40]} <-> {b['title'][:40]}")
        confirmed_pairs = stage3_llm_assist(borderline_pairs)

    components = stage2_grouped + [[a] for a in still_unmatched]

    if confirmed_pairs:
        url_to_component: dict[str, int] = {}
        for idx, comp in enumerate(components):
            for article in comp:
                url_to_component[article.get("url")] = idx

        comp_uf = UnionFind(len(components))
        for a, b, _sim in confirmed_pairs:
            idx_a = url_to_component.get(a.get("url"))
            idx_b = url_to_component.get(b.get("url"))
            if idx_a is not None and idx_b is not None:
                comp_uf.union(idx_a, idx_b)

        merged_components = []
        for indices in comp_uf.groups():
            merged_group = []
            for idx in indices:
                merged_group.extend(components[idx])
            merged_components.append(merged_group)
        components = merged_components

    return stage1_grouped + components


class _FakeEmbeddingModel:
    """
    ** 테스트 전용 - 실제 프로젝트 코드에서는 안 씀 **

    진짜 BGE-M3(sentence-transformers)를 설치하지 않고도 stage2_group의
    "유사도 계산 -> threshold 판단 -> 그룹 병합" 로직 자체가 맞는지 확인하기
    위한 가짜 모델. 미리 정해둔 제목들에는 의도적으로 비슷한 벡터를,
    나머지는 서로 확실히 먼 벡터를 부여한다.

    ** 2026-07-15 수정 - 무관한 텍스트끼리 "우연히" 비슷해지는 문제 제거 **
    기존엔 매칭 안 되는 텍스트에 그냥 rng.normal(size=4) (4차원 랜덤 벡터)를
    부여했는데, 차원이 낮으면 랜덤 벡터끼리도 코사인 유사도가 threshold(0.75)
    를 우연히 넘는 경우가 실제로 발생했다 - 실측: seed=42 기준 "조류독감"
    기사와 "구제역" 기사에 우연히 0.967이 나와서 실제로는 안 묶여야 할 두
    기사(질병명 자체가 다름)가 잘못 묶이는 게 확인됨. 이 자체 테스트에는 그걸
    잡아낼 assert도 없었어서(merged_ok만 확인, 잘못된 병합은 검사 안 함)
    조용히 통과해버리는 문제가 있었음.

    수정 방식: 매칭 안 되는 텍스트마다 서로 직교(orthogonal)하는 전용 축을
    하나씩 배정한다(원-핫 벡터 + 아주 작은 노이즈). 직교 벡터는 코사인
    유사도가 정확히 0에 가깝게 나오도록 수학적으로 보장되므로, 랜덤 시드가
    뭐가 됐든 "무관한 텍스트끼리 우연히 유사해지는" 일 자체가 구조적으로
    발생할 수 없다. 곡물/사료 그룹은 기존처럼 0번 축에 다 같이 모아서
    "의미가 비슷한 문장은 가까운 벡터" 라는 원래 취지는 그대로 유지.

    실제 모델(model.encode(texts, normalize_embeddings=True))과 같은
    인터페이스(encode 메서드, 텍스트 리스트 -> 벡터 리스트)만 흉내낸다.
    """

    def encode(self, texts: list[str], normalize_embeddings: bool = True):
        import numpy as np

        rng = np.random.default_rng(seed=42)
        # 차원 수: 0번 축(곡물/사료 그룹 전용) + 매칭 안 되는 텍스트 개수만큼
        # 여유 있게 잡는다 - 텍스트 수보다 넉넉하면 축이 남아도 무해함.
        dim = len(texts) + 1

        vectors = []
        next_free_axis = 1  # 0번은 곡물/사료 그룹 전용으로 예약
        for text in texts:
            vec = np.zeros(dim)
            if "옥수수" in text or "grain" in text.lower() or "feed price" in text.lower():
                vec[0] = 1.0
            else:
                vec[next_free_axis] = 1.0
                next_free_axis += 1
            vec += rng.normal(scale=0.02, size=dim)  # 완전 동일/완전 0인 벡터는 피하려는 소량 노이즈
            vectors.append(vec)
        return np.array(vectors)


if __name__ == "__main__":
    # === 1차만 단독 확인 (무거운 설치 없이) ===
    sample_articles = [
        {"title": "전북서 고병원성 조류독감 추가 발생", "url": "https://a.com/1"},
        {"title": "USDA reports new avian flu outbreak in Iowa", "url": "https://b.com/1"},
        {"title": "구제역 확산에 한우 수출 잠정 중단", "url": "https://a.com/2"},
        {"title": "Feed prices expected to rise amid grain shortage", "url": "https://b.com/2"},  # 매칭 안 되어야 함
        {"title": "옥수수 국제가격 상승, 배합사료 원가 부담 커져", "url": "https://a.com/3"},       # 위와 같은 이슈지만
                                                                                                    # 1차 사전엔 없음 -> 2차(임베딩)가 잡아야 할 케이스
    ]

    grouped, unmatched = stage1_group(sample_articles)
    print(f"=== 1차 매칭으로 묶인 그룹: {len(grouped)}개 ===")
    for g in grouped:
        print(f"  - {len(g)}건: {[a['title'] for a in g]}")
    print(f"\n=== 1차에서 못 잡은 기사(2차로 넘길 대상): {len(unmatched)}건 ===")
    for a in unmatched:
        print(f"  - {a['title']}")

    # === 2차까지 포함한 전체 파이프라인 확인 (가짜 모델 사용) ===
    print("\n\n=== group_issues() 전체 실행 (가짜 임베딩 모델) ===")
    fake_model = _FakeEmbeddingModel()
    final_groups = group_issues(sample_articles, model=fake_model)

    print(f"\n최종 그룹 수: {len(final_groups)}개")
    for g in final_groups:
        titles = [a["title"] for a in g]
        tag = "그룹" if len(g) >= 2 else "단독"
        print(f"  [{tag}, {len(g)}건] {titles}")

    # 기대 결과: "옥수수 국제가격..."과 "Feed prices..."가 2차에서 같은
    # 그룹으로 묶여야 함 (가짜 모델이 두 문장에 비슷한 벡터를 부여했으므로)
    merged_ok = any(
        len(g) == 2 and
        {"옥수수 국제가격 상승, 배합사료 원가 부담 커져", "Feed prices expected to rise amid grain shortage"}
        == {a["title"] for a in g}
        for g in final_groups
    )
    print(f"\n[검증] 2차 임베딩으로 사료가격 이슈 그룹핑 성공: {merged_ok}")
    assert merged_ok, "2차 임베딩 그룹핑 로직에 문제가 있음"

    # ** 2026-07-15 추가 - 음성(negative) 검증 **
    # "무관한 기사는 절대 안 묶여야 한다"를 명시적으로 확인. 이게 없으면
    # _FakeEmbeddingModel이 우연히 이상한 벡터를 내놔도(과거 실제로 발생함 -
    # 위 클래스 docstring 참고) 테스트가 조용히 통과해버린다.
    def _same_group(title_a: str, title_b: str) -> bool:
        return any({title_a, title_b} <= {a["title"] for a in g} for g in final_groups)

    unrelated_pairs = [
        ("전북서 고병원성 조류독감 추가 발생", "구제역 확산에 한우 수출 잠정 중단"),  # 질병명 자체가 다름
        ("USDA reports new avian flu outbreak in Iowa", "구제역 확산에 한우 수출 잠정 중단"),
        ("USDA reports new avian flu outbreak in Iowa", "옥수수 국제가격 상승, 배합사료 원가 부담 커져"),
    ]
    for title_a, title_b in unrelated_pairs:
        wrongly_merged = _same_group(title_a, title_b)
        print(f"[검증] '{title_a[:20]}...' <-> '{title_b[:20]}...' 안 묶임: {not wrongly_merged}")
        assert not wrongly_merged, f"무관한 기사가 잘못 묶임: {title_a} <-> {title_b}"
    print("\n[검증] 무관한 기사 오탐 없음 - 전부 통과")

    # === 3차 LLM 보조가 실제로 배선(wiring)돼 있는지 mock으로 확인 ===
    # (실제 API 키 없이도, borderline_pairs가 있으면 stage3_llm_assist가
    # 정말 호출되고 응답을 파싱해 병합까지 이어지는지 구조만 검증한다.
    # 앞선 세션에서 "지금 LLM이 작동 안 한 거 아니냐"는 질문이 나온 이유가
    # 바로 이 실행에서는 borderline_pairs 자체가 한 번도 안 생겨서 stage3가
    # 호출조차 안 됐기 때문 - 이 스모크 테스트는 stage3 배선 자체는 정상임을
    # borderline_pairs를 강제로 만들어서 확인한다.)
    print("\n\n=== 3차 LLM 보조 배선 확인 (mock API, 실제 네트워크 호출 없음) ===")
    import os as _os
    import requests as _requests

    _mock_calls = []

    def _mock_post(url, headers=None, json=None, timeout=None):
        _mock_calls.append(url)
        pairs_count = json["messages"][0]["content"].count('A: "')
        results = [{"same_event": True}] * pairs_count  # 이 스모크 테스트는 전부 True로 응답
        text = __import__("json").dumps(results)

        class _MockResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"content": [{"type": "text", "text": text}]}
        return _MockResp()

    _original_post = _requests.post
    _requests.post = _mock_post
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
        assert _mock_calls, "requests.post가 한 번도 호출 안 됨 - stage3가 실제로 API를 안 부름"
        print(f"[검증] stage3_llm_assist 배선 정상 - mock 호출 {len(_mock_calls)}회, "
              f"confirmed {len(confirmed)}쌍")
    finally:
        _requests.post = _original_post
        del _os.environ["ANTHROPIC_API_KEY"]

    print("\n[issue_grouper] 자체 점검 전체 통과 (1차/2차 그룹핑 + 음성 검증 + 3차 배선 확인)")
