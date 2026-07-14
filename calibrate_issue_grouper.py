"""
calibrate_issue_grouper.py
issue_grouper.py의 THRESHOLD/BORDERLINE_MARGIN을 "진짜 수집 데이터"로
재검증하기 위한 1회성 진단 스크립트.

main.py(본 파이프라인)와는 별개 - main.py는 아직 issue_grouper를 연결 안
한 상태(scorer.to_singleton_groups 그대로) 그대로 둔다. 재검증 끝나고 값
조정까지 확정된 뒤에 main.py 연결(우선순위 4번 작업)로 넘어갈 것.

** 실행 전 확인 필요 - 발견한 불일치 (2026-07-14) **
issue_grouper.py 코드에는 현재 THRESHOLD=0.75, BORDERLINE_MARGIN=0.05로
돼 있다. 지난 세션 기록상으로는 test_bge_m3_calibration.py 결과(같은 사건
쌍 0.528~0.696 / 다른 사건 쌍 0.287~0.451)를 바탕으로 THRESHOLD=0.50,
BORDERLINE_MARGIN=0.15로 "반영 완료"했다고 돼 있었는데, 실제 파일은 그
값으로 안 바뀌어 있다 - 반영이 누락된 것으로 보인다. 이번 재검증에서
0.75(현재 코드) / 0.50(지난 세션 기록) 중 실측 데이터가 어느 쪽에 더
가까운지, 혹은 둘 다 아닌 제3의 값이 맞는지 같이 확인한다.

** 재검증이 실제로 하는 일 - 기존 calibration 테스트와 다른 점 **
기존 test_bge_m3_calibration.py는 사람이 미리 지어낸 예문 8쌍만 봤다 -
표본이 작고, 실제 수집되는 기사 제목의 문체·길이·중복 패턴과 다를 수
있다. 이번엔:
  1. 실제 수집 파이프라인(main.py의 run_collectors + normalize)을 그대로
     돌려서 진짜 기사 제목을 모은다
  2. 1차(사전) 매칭 결과를 먼저 걷어내고, 남은 기사 전체에 대해 "필터링
     없는" 전체 N x N 코사인 유사도를 계산해 높은 순으로 전부 나열한다 -
     지금 THRESHOLD 값에 얽매이지 않고 실제 분포 전체를 보기 위함 (지금
     값이 애초에 잘못 잡혀 있으면 borderline_margin 안쪽만 봐서는 그
     사실 자체를 알 수 없기 때문)
  3. 동시에 issue_grouper.stage2_group()을 "현재 코드에 있는 값 그대로"
     실행해서 실제로 어떤 그룹이 만들어지는지도 같이 보여준다 - 2번의
     "전체 순위 리스트"와 3번의 "지금 값으로 실제 나온 결과"를 나란히
     두고 사람이 눈으로 비교
  4. 사람이 두 출력을 보고 "이 근처에서 같은 이슈/다른 이슈가 갈리는구나"를
     판단해서 THRESHOLD/BORDERLINE_MARGIN을 조정한다 - 정답 라벨이 없어서
     이 판단은 자동화 불가, 사람이 직접 봐야 하는 지점

실행 후 다음 액션: 이 스크립트 출력(특히 [4] 전체 순위 리스트)을 보고 값을
조정하기로 했으면, issue_grouper.py 상단 THRESHOLD/BORDERLINE_MARGIN 숫자만
바꾸면 됨 (다른 코드는 안 건드려도 됨).
"""

import json
from datetime import datetime, timezone

from sentence_transformers import SentenceTransformer

import issue_grouper
import main as pipeline  # run_collectors, normalize만 재사용 - main.py 자체는 안 건드림


def _dump_raw_for_reference(articles: list[dict]) -> str:
    """
    재검증에 쓴 실제 원본 기사를 참고용으로 파일에 남겨둔다. 저장 레이어
    본 구현(5번 섹션)은 아직 아니지만, 오늘 실행에 어떤 데이터가 쓰였는지
    나중에 다시 확인할 수 있어야 재검증 판단 근거를 추적할 수 있어서
    최소한만 남긴다 (raw.json과 같은 취지, 파일명만 구분).
    """
    now = datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()
    filename = f"calibration_raw_{year}-W{week:02d}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2, default=str)
    return filename


def run():
    print("=== [1] 실제 수집 (main.py의 run_collectors 재사용) ===")
    articles, _gdelt_timeline, failed = pipeline.run_collectors()
    articles = pipeline.normalize(articles)
    print(f"정규화 후 {len(articles)}건 (실패 소스: {failed or '없음'})")

    raw_path = _dump_raw_for_reference(articles)
    print(f"[calibrate] 참고용 원본 저장: {raw_path}")

    print("\n=== [2] 1차 키워드 사전 매칭 ===")
    stage1_grouped, stage1_unmatched = issue_grouper.stage1_group(articles)
    print(f"1차로 묶인 그룹 {len(stage1_grouped)}개, 2차로 넘어갈 기사 {len(stage1_unmatched)}건")
    for g in stage1_grouped:
        print(f"  [1차, {len(g)}건] {[a['title'] for a in g]}")

    if len(stage1_unmatched) < 2:
        print("2차로 넘길 기사가 2건 미만이라 유사도 비교 자체가 불가능. 종료.")
        return

    print("\n=== [3] BGE-M3 모델 로딩 ===")
    model = SentenceTransformer("BAAI/bge-m3")
    print("로딩 완료")

    print("\n=== [4] 전체 N x N 유사도 - THRESHOLD 무관, 전 구간 순위 (핵심 확인 자료) ===")
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
    print(f"상위 {top_k}쌍 (유사도 내림차순 - 사람이 직접 '같은 이슈'인지 눈으로 판단):")
    for sim, i, j in all_pairs[:top_k]:
        title_i = stage1_unmatched[i]["title"][:45]
        title_j = stage1_unmatched[j]["title"][:45]
        print(f"  {sim:.3f}  {title_i:45s} <-> {title_j}")

    print("\n=== [5] 현재 코드값(THRESHOLD/BORDERLINE_MARGIN)으로 실제 그룹핑한 결과 ===")
    print(f"현재 값: THRESHOLD={issue_grouper.THRESHOLD}, BORDERLINE_MARGIN={issue_grouper.BORDERLINE_MARGIN}")
    stage2_grouped, still_unmatched, borderline_pairs = issue_grouper.stage2_group(stage1_unmatched, model=model)

    print(f"\n2차에서 새로 묶인 그룹: {len(stage2_grouped)}개")
    for g in stage2_grouped:
        print(f"  [2차, {len(g)}건] {[a['title'] for a in g]}")

    print(f"\n애매 구간(borderline) {len(borderline_pairs)}쌍:")
    for a, b, sim in sorted(borderline_pairs, key=lambda x: -x[2]):
        print(f"  ({sim:.3f}) {a['title'][:45]} <-> {b['title'][:45]}")

    print(f"\n끝까지 혼자 남은 기사(단독): {len(still_unmatched)}건")

    print("\n=== 다음 액션 ===")
    print("[4]의 전체 순위 리스트를 위에서부터 눈으로 훑어서 '이 밑으로는 확실히")
    print("다른 이슈다' 싶은 지점을 찾아 THRESHOLD 후보로 잡고, 그 위아래 폭을")
    print("BORDERLINE_MARGIN으로 잡으면 됨. [5]는 지금 코드값(0.75/0.05)이 그")
    print("지점과 얼마나 맞는지/틀린지 비교하는 참고 자료.")


if __name__ == "__main__":
    run()
