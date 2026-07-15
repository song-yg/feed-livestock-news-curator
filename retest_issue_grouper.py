"""
retest_issue_grouper.py
issue_grouper.py의 stage1/stage2 버그 수정(2026-07-14)이 실제로 맞는지
"빠르게" 재검증하는 스크립트.

** 재수집을 안 하는 이유 **
지난 재검증(calibrate_issue_grouper.py)이 GDELT 429 rate limit 때문에
3시간 18분 걸렸다 - 근데 그때 수집한 진짜 데이터(calibration_raw_2026-W29.json,
580건)는 이미 아티팩트로 받아뒀다. 이번에 확인하려는 건 "오늘 데이터가
다른가"가 아니라 "코드 수정이 제대로 됐는가"라서, 같은 데이터로 다시
돌리는 게 더 정확한 비교도 되고(변수가 데이터가 아니라 코드 하나로 고정됨)
GDELT를 다시 안 때리니 몇 분 안에 끝난다.

** 이 스크립트가 확인하는 것 **
  1. ISSUE_SYNONYM_GROUPS를 비운 뒤 1차 매칭이 실제로 빈 그룹을 내는지
     (수정 전: 2개 그룹, 24건+101건 - 국가 다른 조류독감/구제역이 섞여있었음
      수정 후 기대값: 0개 그룹, 455건 전량 2차로)
  2. stage2_group의 borderline 개수가 실제로 크게 줄었는지
     (수정 전: 446쌍, 그중 342쌍이 이미 같은 그룹인 "농협" 내부 중복 쌍이었음)
  3. 국내-해외 교차(cross-source) 매칭이 이번엔 눈에 보이는지 - 1차가 더는
     먼저 채가지 않으니 실제로 존재했다면 2차 임베딩 결과에 나타나야 함
     ([4], [5] 출력에 "[교차]" 표시로 국내/해외 소스가 다른 쌍을 표시함)

실행 전제: calibration_raw_2026-W29.json이 이 스크립트와 같은 위치에 있어야
함 (지난 재검증 아티팩트에서 받은 파일을 리포에 커밋해둘 것 - 워크플로우도
이 전제로 짜뒀음, retest-issue-grouper.yml 참고).
"""

import json
import sys

from sentence_transformers import SentenceTransformer

import issue_grouper

DEFAULT_RAW_PATH = "calibration_raw_2026-W29.json"


def run(raw_path: str = DEFAULT_RAW_PATH) -> None:
    with open(raw_path, encoding="utf-8") as f:
        articles = json.load(f)
    print(f"=== 원본 데이터 로드: {raw_path} ({len(articles)}건, 재수집 없음) ===")

    print("\n=== [2] 1차 키워드 사전 매칭 (ISSUE_SYNONYM_GROUPS 비운 뒤) ===")
    stage1_grouped, stage1_unmatched = issue_grouper.stage1_group(articles)
    print(f"1차로 묶인 그룹 {len(stage1_grouped)}개 (수정 전엔 2개 - 이번엔 0개여야 정상)")
    print(f"2차로 넘어갈 기사 {len(stage1_unmatched)}건 (수정 전엔 455건 - 전량 넘어가야 정상)")

    if len(stage1_unmatched) < 2:
        print("2차로 넘길 기사가 2건 미만 - 유사도 비교 불가. 종료.")
        return

    print("\n=== [3] BGE-M3 모델 로딩 ===")
    model = SentenceTransformer("BAAI/bge-m3")
    print("로딩 완료")

    print("\n=== [4] 전체 N x N 유사도 - THRESHOLD 무관, 전 구간 순위 ===")
    texts = [issue_grouper._embedding_text(a) for a in stage1_unmatched]
    vectors = model.encode(texts, normalize_embeddings=True)
    sim_matrix = issue_grouper._cosine_similarity_matrix(vectors)

    n = len(stage1_unmatched)
    all_pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            all_pairs.append((float(sim_matrix[i][j]), i, j))
    all_pairs.sort(reverse=True)

    top_k = min(40, len(all_pairs))
    print(f"상위 {top_k}쌍 ([교차] = 국내/해외 소스가 서로 다른 쌍):")
    for sim, i, j in all_pairs[:top_k]:
        ai, aj = stage1_unmatched[i], stage1_unmatched[j]
        si, sj = ai.get("source", "?"), aj.get("source", "?")
        cross = " [교차]" if si != sj else ""
        print(f"  {sim:.3f} ({si}/{sj}){cross}  {ai['title'][:42]:42s} <-> {aj['title'][:42]}")

    print("\n=== [5] 현재 코드값으로 실제 그룹핑한 결과 (버그 수정 후) ===")
    print(f"현재 값: THRESHOLD={issue_grouper.THRESHOLD}, BORDERLINE_MARGIN={issue_grouper.BORDERLINE_MARGIN}")
    stage2_grouped, still_unmatched, borderline_pairs = issue_grouper.stage2_group(stage1_unmatched, model=model)

    print(f"\n2차에서 새로 묶인 그룹: {len(stage2_grouped)}개 (수정 전 35개와 비교)")
    print(f"애매 구간(borderline): {len(borderline_pairs)}쌍 (수정 전 446쌍과 비교 - 크게 줄어야 정상)")
    for a, b, sim in sorted(borderline_pairs, key=lambda x: -x[2])[:60]:
        si, sj = a.get("source", "?"), b.get("source", "?")
        cross = " [교차]" if si != sj else ""
        print(f"  ({sim:.3f}){cross} {a['title'][:42]} <-> {b['title'][:42]}")

    cross_source_groups = [g for g in stage2_grouped if len({a.get("source") for a in g}) > 1]
    print(f"\n국내-해외 교차로 묶인 그룹: {len(cross_source_groups)}개")
    for g in cross_source_groups:
        print(f"  {[(a.get('source'), a['title'][:40]) for a in g]}")

    print(f"\n끝까지 혼자 남은 기사(단독): {len(still_unmatched)}건")

    print("\n=== 요약 (수정 전 대비) ===")
    print(f"1차 그룹: 2개 -> {len(stage1_grouped)}개")
    print(f"2차 그룹: 35개 -> {len(stage2_grouped)}개")
    print(f"borderline: 446쌍 -> {len(borderline_pairs)}쌍")
    print(f"국내-해외 교차 그룹: 0개(확인 불가) -> {len(cross_source_groups)}개")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RAW_PATH
    run(path)
