"""
test_timeline_timing.py
`timeline_search`(timelinevol/timelinevolraw) 유무에 따른 실제 소요 시간
차이를 재보기 위한 1회성 테스트 스크립트 (2026-07-20).

배경: "시계열 켰을 때 몇 시간씩 걸렸다"는 게 실제 체감이었는데, 정확히
몇 배/몇 분 차이인지 숫자로 확인된 적은 없었음. 이 스크립트가 그걸 재서
"아예 뺄지/절충안(timelinevolraw만 쓰기)으로 갈지/그냥 둘지" 판단에 쓸
실측 근거를 만든다.

메인 파이프라인(main.py)과 무관하게 독립 실행된다 - WATT/네이버 수집,
BGE-M3 임베딩, LLM 요약 등은 전혀 안 건드리고 gdelt_collector.collect()
만 두 가지 조건으로 각각 호출해서 시간을 잰다.

키워드는 일부러 적게(2개) 잡았다 - 이건 "시계열이 있고 없고의 배율/추가
시간" 자체를 보려는 목적이라, 키워드 개수를 늘려서 생기는 다른 변수
(크라우딩, 배치 재시도 등)가 섞이지 않게 하기 위함. 키워드 개수에 따라
어떻게 스케일되는지는 이 결과를 보고 나서 별도로 따져볼 것.
"""

import time

import gdelt_collector

# 일부러 적게 - 시계열 유무 자체의 시간 차이만 보려는 목적 (위 docstring 참고)
TEST_KEYWORDS = ["avian influenza", "feed price"]

BUFFER_SECONDS = 15  # 두 실행 사이 안전 대기 (측정 시간에는 안 넣음)


def _format_seconds(seconds: float) -> str:
    return f"{seconds:.1f}초 ({seconds / 60:.1f}분)"


def main() -> None:
    print(f"테스트 키워드: {TEST_KEYWORDS}\n")

    print("=" * 60)
    print("[1/2] skip_timeline=True (시계열 없이 기사만 수집)")
    print("=" * 60)
    t0 = time.time()
    articles_without, timeline_without = gdelt_collector.collect(
        keywords=TEST_KEYWORDS, skip_timeline=True
    )
    elapsed_without = time.time() - t0
    print(f"\n[결과] 소요 시간: {_format_seconds(elapsed_without)}")
    print(f"[결과] 수집된 기사 수: {len(articles_without)}")

    print(f"\n(다음 측정 전 안전 대기 {BUFFER_SECONDS}초 - 이 시간은 측정에 안 들어감)")
    time.sleep(BUFFER_SECONDS)

    print("\n" + "=" * 60)
    print("[2/2] skip_timeline=False (기사 + 시계열 둘 다 수집)")
    print("=" * 60)
    t1 = time.time()
    articles_with, timeline_with = gdelt_collector.collect(
        keywords=TEST_KEYWORDS, skip_timeline=False
    )
    elapsed_with = time.time() - t1
    print(f"\n[결과] 소요 시간: {_format_seconds(elapsed_with)}")
    print(f"[결과] 수집된 기사 수: {len(articles_with)}")
    print(f"[결과] 시계열 수집된 키워드 수: {len(timeline_with)}/{len(TEST_KEYWORDS)}")

    print("\n" + "=" * 60)
    print("=== 비교 결과 ===")
    print("=" * 60)
    diff = elapsed_with - elapsed_without
    print(f"시계열 없이: {_format_seconds(elapsed_without)}")
    print(f"시계열 포함: {_format_seconds(elapsed_with)}")
    print(f"차이(시계열 때문에 추가로 걸린 시간): {_format_seconds(diff)}")
    if elapsed_without > 0:
        print(f"배율: {elapsed_with / elapsed_without:.2f}배")
    print(f"\n참고: 이번 테스트는 키워드 {len(TEST_KEYWORDS)}개 기준입니다. "
          f"시계열은 키워드마다 timelinevol+timelinevolraw 2번씩 개별 호출하므로, "
          f"키워드 수가 늘면 이 차이도 대략 그에 비례해서 커질 가능성이 높습니다"
          f"(429/백오프 변동성 때문에 정확히 비례하진 않을 수 있음).")


if __name__ == "__main__":
    main()
