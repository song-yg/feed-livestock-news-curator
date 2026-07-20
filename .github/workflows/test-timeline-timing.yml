"""
test_timeline_timing.py
`timeline_search`의 두 모드 - `timelinevol`(비율)과 `timelinevolraw`(실제
건수) - 사이의 소요 시간 차이를 재기 위한 1회성 테스트 스크립트 (2026-07-20).

** 2026-07-20 수정 **: 처음엔 "시계열 있음/없음" 전체 비교로 만들었는데,
담당자가 원한 건 "시계열 안의 두 API 호출(timelinevol vs timelinevolraw)
자체의 시간 차이"였음 - 둘 다 매번 같이 부르고 있는데, 혹시 하나가 특별히
느리다면 그것만 빼는 것도 절충안이 될 수 있어서 확인하는 것. `timelinevolraw`
는 `Article Count`/`All Articles` 필드가 있어 비율을 직접 계산할 수 있으므로
(timelinevol = Article Count / All Articles), 이론적으로는 timelinevolraw
하나만 있으면 둘 다 커버 가능 - 근데 만약 timelinevol 쪽이 훨씬 빠르다면
반대로 그쪽만 남기는 게 나을 수도 있어 실측이 필요함.

메인 파이프라인(main.py)과 무관하게 독립 실행된다. gdelt_collector.py의
_call_with_retry/Filters/GdeltDoc/TIMESPAN/MAX_RECORDS를 그대로 재사용해서
User-Agent 주입, 백오프 등 기존 안전장치를 그대로 적용받는다.

키워드는 일부러 적게(2개) 잡았다 - 모드 간 시간 차이 자체를 보려는 목적이라
키워드 수를 늘려서 생기는 변동성(429 등)을 최소화하기 위함.
"""

import time

import gdelt_collector as gc

# 일부러 적게 - 두 모드 간 시간 차이 자체만 보려는 목적 (위 docstring 참고)
TEST_KEYWORDS = ["avian influenza", "feed price"]

MODES = ["timelinevol", "timelinevolraw"]


def _format_seconds(seconds: float) -> str:
    return f"{seconds:.1f}초"


def _time_one_call(gd: "gc.GdeltDoc", mode: str, keyword: str) -> tuple[float, int, bool]:
    """
    한 키워드에 대해 timeline_search를 한 모드로 한 번 호출하고,
    (걸린 시간, 반환된 행 수, 성공 여부)를 반환한다.
    """
    f = gc.Filters(keyword=keyword, timespan=gc.TIMESPAN, num_records=gc.MAX_RECORDS)
    t0 = time.time()
    try:
        df = gc._call_with_retry(gd.timeline_search, mode, f, label=f"{keyword} / {mode}")
        elapsed = time.time() - t0
        rows = len(df) if df is not None else 0
        return elapsed, rows, True
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  실패: {type(e).__name__} - {e!r}")
        return elapsed, 0, False


def main() -> None:
    print(f"테스트 키워드: {TEST_KEYWORDS}")
    print(f"비교할 모드: {MODES}\n")

    gd = gc.GdeltDoc()
    results: dict[str, list[float]] = {mode: [] for mode in MODES}

    for keyword in TEST_KEYWORDS:
        print("=" * 60)
        print(f"키워드: '{keyword}'")
        print("=" * 60)
        for mode in MODES:
            print(f"\n[{mode}] 호출 중...")
            elapsed, rows, success = _time_one_call(gd, mode, keyword)
            status = "성공" if success else "실패"
            print(f"[{mode}] {status} - {_format_seconds(elapsed)}, {rows}행 반환")
            results[mode].append(elapsed)
            time.sleep(gc.REQUEST_INTERVAL)
        print()

    print("=" * 60)
    print("=== 비교 결과 (키워드별) ===")
    print("=" * 60)
    header = f"{'키워드':30s}" + "".join(f"{mode:>18s}" for mode in MODES)
    print(header)
    for i, keyword in enumerate(TEST_KEYWORDS):
        row = f"{keyword:30s}" + "".join(f"{results[mode][i]:>15.1f}초" for mode in MODES)
        print(row)

    print("\n=== 모드별 평균 ===")
    averages = {}
    for mode in MODES:
        avg = sum(results[mode]) / len(results[mode]) if results[mode] else 0
        averages[mode] = avg
        print(f"{mode:20s} 평균 {_format_seconds(avg)} (호출 {len(results[mode])}회)")

    if len(MODES) == 2 and all(averages[m] > 0 for m in MODES):
        a, b = MODES
        faster, slower = (a, b) if averages[a] <= averages[b] else (b, a)
        ratio = averages[slower] / averages[faster] if averages[faster] > 0 else float("inf")
        diff = averages[slower] - averages[faster]
        print(f"\n'{faster}'가 '{slower}'보다 평균 {_format_seconds(diff)} 더 빠름 "
              f"({ratio:.2f}배)")


if __name__ == "__main__":
    main()
