"""
test_timelinevol_only.py
`timelinevolraw`를 빼고 `timelinevol` 하나만 썼을 때, 키워드 5개 규모에서
실제로 얼마나 걸리는지 재는 테스트 스크립트 (2026-07-21).

배경: test_timeline_timing.py로 timelinevol vs timelinevolraw 4회 비교한
결과, timelinevol이 평균 더 빠르고("먼저 부른 경우"만 놓고 봐도 재시도
없이 바로 성공하는 비율이 더 높음) - 다만 노이즈가 커서 100% 확정은
아니었음. "그럼 raw는 빼고 vol만 쓰면 실제로 쓸 만한지" 실사용 규모(5개
키워드)에서 확인해보기 위한 스크립트.

키워드 5개는 v4 키워드 시트의 실제 활성 en 키워드 일부를 가져다 씀 -
실전에서 쓸 법한 조합으로 테스트해야 의미가 있어서(무작위 키워드보다
현실적인 참고치가 됨).

메인 파이프라인(main.py)과 무관하게 독립 실행된다. gdelt_collector.py의
_call_with_retry/Filters/GdeltDoc/TIMESPAN/MAX_RECORDS/REQUEST_INTERVAL을
그대로 재사용해서 User-Agent 주입, 백오프 등 기존 안전장치를 그대로
적용받는다.
"""

import time

import gdelt_collector as gc

# v4 키워드 시트의 실제 활성 en 키워드 중 5개 (실전 규모 테스트를 위해
# 무작위가 아니라 실제 쓰는 키워드로 구성)
TEST_KEYWORDS = [
    "avian influenza",
    "foot-and-mouth disease",
    "feed price",
    "biosecurity",
    "swine industry",
]


def _format_seconds(seconds: float) -> str:
    return f"{seconds:.1f}초 ({seconds / 60:.1f}분)"


def _collect_timelinevol_only(gd: "gc.GdeltDoc", keyword: str) -> tuple[float, int, bool]:
    """
    한 키워드에 대해 timelinevol만 호출하고 (걸린 시간, 행 수, 성공 여부)를
    반환한다. gdelt_collector._collect_timeline_for_keyword와 같은 패턴이되
    timelinevolraw 호출 부분만 뺐다.
    """
    f = gc.Filters(keyword=keyword, timespan=gc.TIMELINE_TIMESPAN, num_records=gc.MAX_RECORDS)
    t0 = time.time()
    try:
        df = gc._call_with_retry(gd.timeline_search, "timelinevol", f,
                                  label=f"{keyword} / timelinevol")
        elapsed = time.time() - t0
        rows = len(df) if df is not None else 0
        return elapsed, rows, True
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  실패: {type(e).__name__} - {e!r}")
        return elapsed, 0, False


def main() -> None:
    print(f"테스트 키워드 ({len(TEST_KEYWORDS)}개): {TEST_KEYWORDS}")
    print("모드: timelinevol만 (timelinevolraw는 제외 - 실측 결과 기반 결정)\n")

    gd = gc.GdeltDoc()
    results = []  # (keyword, elapsed, rows, success)

    run_start = time.time()
    for i, keyword in enumerate(TEST_KEYWORDS, start=1):
        print("=" * 60)
        print(f"[{i}/{len(TEST_KEYWORDS)}] '{keyword}'")
        print("=" * 60)
        elapsed, rows, success = _collect_timelinevol_only(gd, keyword)
        status = "성공" if success else "실패"
        print(f"{status} - {_format_seconds(elapsed)}, {rows}행 반환\n")
        results.append((keyword, elapsed, rows, success))
        if i < len(TEST_KEYWORDS):
            time.sleep(gc.REQUEST_INTERVAL)
    total_elapsed = time.time() - run_start

    print("=" * 60)
    print("=== 결과 요약 ===")
    print("=" * 60)
    for keyword, elapsed, rows, success in results:
        status = "성공" if success else "실패"
        print(f"{keyword:30s} {status:4s} {elapsed:8.1f}초  {rows}행")

    success_count = sum(1 for *_, success in results if success)
    avg_elapsed = sum(e for _, e, _, _ in results) / len(results) if results else 0
    print(f"\n키워드 {len(TEST_KEYWORDS)}개 중 {success_count}개 성공")
    print(f"키워드당 평균 소요 시간: {_format_seconds(avg_elapsed)}")
    print(f"전체 소요 시간(요청 간격 포함): {_format_seconds(total_elapsed)}")
    print(f"\n참고: 이전 테스트(test_timeline_timing.py)에서 timelinevol+timelinevolraw "
          f"둘 다 썼을 때는 키워드 2개만으로도 평균 몇백 초씩 걸리는 경우가 잦았음 - "
          f"이번 결과와 직접 비교해서 timelinevolraw를 빼는 게 실제로 이득인지 판단할 것.")


if __name__ == "__main__":
    main()