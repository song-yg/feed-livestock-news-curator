"""
issue_grouper.py
"2.1 이슈 그룹핑" 담당 모듈 (알고리즘 문서 "2.1 이슈 그룹핑" 참조)

문서에 정의된 하이브리드 파이프라인 중 지금 이 파일에서 구현하는 범위:
  1차 - KR<->EN 키워드 사전 매칭         -> 구현
  2차 - BGE-M3 임베딩 코사인 유사도       -> 구현 (아래 두 번째 스텝에서 추가 예정)
  3차 - LLM 그룹핑 보조 (임계값 애매 구간) -> 미구현 (TODO, LLM 연동 단계에서 추가)

이번 스텝(1단계)에서는 "1차 사전 매칭"과 "그룹을 어떻게 합칠지"(Union-Find)
로직만 먼저 만든다. 임베딩(무거운 설치가 필요한 부분)은 이 로직이 맞다는 걸
확인한 다음에 그 위에 얹을 것이다.
"""

from itertools import combinations

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
# 지금은 자주 나올 법한 이슈 몇 개만 예시로 채워뒀다. 문서에 명시된 대로
# "사전에 없는 신규 표현·의역된 제목"은 2차 임베딩이 커버하는 몫이라, 이
# 사전을 처음부터 완벽하게 채울 필요는 없다 - 배포 전 테스트 기간에 점진적으로
# 보강하면 된다 (2.2 키워드 태깅 섹션의 "배포 전 테스트 기간에 사전을 직접
# 보강" 원칙과 같은 방식).
ISSUE_SYNONYM_GROUPS: list[set[str]] = [
    {"조류독감", "고병원성 조류독감", "AI", "avian influenza", "avian flu", "bird flu", "HPAI", "LPAI"},
    {"구제역", "foot-and-mouth disease", "FMD"},
    {"아프리카돼지열병", "ASF", "African swine fever"},
    {"럼피스킨병", "lumpy skin disease", "LSD"},
]
# 자체 테스트(2026-07-14) 중 실제로 재현된 문제: "AI"는 "grain"("gr-AI-n")
# 처럼 전혀 무관한 단어 안에 부분 문자열로 우연히 들어있는 경우가 많아
# 오매칭을 일으킨다 - keyword_tagger.py가 카테고리 태깅에서 이미 같은 이유로
# "AI"를 제외했던 것과 동일한 문제. 사전 자체(위 목록)는 사람이 읽을 때
# 이해하기 쉽도록 "AI"를 그대로 남겨두고, 실제 매칭 시점(_stage1_match_keys)
# 에서만 keyword_tagger.EXCLUDED_TERMS 기준으로 걸러낸다.


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
# 대상. LLM 연동이 아직 안 됐으므로(다음 세션 작업) 지금은 애매 구간을
# "일단 안 묶는" 보수적 기본값으로 처리하고, 어떤 쌍이 애매 구간에 걸렸는지만
# 기록해둔다 - 3차가 실제로 붙으면 이 기록을 그대로 LLM 입력으로 넘기면 된다.
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
    borderline_pairs = []

    for i, j in combinations(range(n), 2):
        sim = float(sim_matrix[i][j])
        if sim >= threshold:
            uf.union(i, j)
        elif sim >= threshold - borderline_margin:
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
# 최종 진입점: 1차 + 2차를 합쳐서 scorer.py에 바로 넘길 수 있는 형태로 반환
# ---------------------------------------------------------------------------

def group_issues(articles: list[dict], model=None) -> list[list[dict]]:
    """
    1차(사전) + 2차(임베딩)를 순서대로 실행해 최종 이슈 그룹 리스트를 만든다.

    이 함수의 반환값은 scorer.score_and_rank()가 받는 입력과 형태가 동일하다
    (list[list[dict]]) - main.py에서 scorer.to_singleton_groups(articles) 호출을
    이 함수 호출로 그대로 바꿔치기하면 된다 (scorer.py 상단 docstring에 이미
    이렇게 하라고 적혀 있음).

    3차(LLM 보조)는 아직 미구현이라, 지금은 borderline_pairs를 출력만 하고
    그룹핑에는 반영하지 않는다 (다음 세션에서 LLM 연동 시 여기 이어붙일 것).
    """
    stage1_grouped, stage1_unmatched = stage1_group(articles)

    if model is None:
        # 모델이 안 주어졌으면(예: 아직 설치 전 단계 테스트) 2차 없이
        # 1차 결과 + 나머지를 단독 그룹으로 반환 - to_singleton_groups와
        # 동일한 안전한 fallback
        print("[issue_grouper] 임베딩 모델이 없어 2차(임베딩) 매칭 생략 - 1차 결과만 사용")
        singleton = [[a] for a in stage1_unmatched]
        return stage1_grouped + singleton

    stage2_grouped, still_unmatched, borderline_pairs = stage2_group(stage1_unmatched, model=model)

    if borderline_pairs:
        print(f"[issue_grouper] 임계값 애매 구간 {len(borderline_pairs)}쌍 발견 - "
              f"3차 LLM 보조 미구현으로 지금은 그룹핑 안 함 (아래 목록 참고, 다음 세션 작업)")
        for a, b, sim in borderline_pairs:
            print(f"  - ({sim:.3f}) {a['title'][:40]} <-> {b['title'][:40]}")

    singleton = [[a] for a in still_unmatched]
    return stage1_grouped + stage2_grouped + singleton


class _FakeEmbeddingModel:
    """
    ** 테스트 전용 - 실제 프로젝트 코드에서는 안 씀 **

    진짜 BGE-M3(sentence-transformers)를 설치하지 않고도 stage2_group의
    "유사도 계산 -> threshold 판단 -> 그룹 병합" 로직 자체가 맞는지 확인하기
    위한 가짜 모델. 미리 정해둔 제목들에는 의도적으로 비슷한 벡터를,
    나머지는 서로 먼 벡터를 부여한다.

    실제 모델(model.encode(texts, normalize_embeddings=True))과 같은
    인터페이스(encode 메서드, 텍스트 리스트 -> 벡터 리스트)만 흉내낸다.
    """

    def encode(self, texts: list[str], normalize_embeddings: bool = True):
        import numpy as np

        # "옥수수/사료" 계열 문장은 벡터를 거의 같게, 나머지는 랜덤하게 떨어뜨림
        # (real BGE-M3라면 의미가 비슷한 문장끼리 자연히 가까운 벡터가 나오는데,
        # 그 결과를 시뮬레이션하는 것)
        vectors = []
        rng = np.random.default_rng(seed=42)
        for text in texts:
            if "옥수수" in text or "grain" in text.lower() or "feed price" in text.lower():
                base = np.array([1.0, 0.0, 0.0, 0.0])
                noise = rng.normal(scale=0.02, size=4)  # 살짝만 흔들어서 완전 동일 벡터는 피함
            else:
                base = rng.normal(size=4)
                noise = 0
            vectors.append(base + noise)
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