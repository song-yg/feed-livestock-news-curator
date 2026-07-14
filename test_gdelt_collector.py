"""
test_gdelt_collector.py
gdelt_collector.py의 language/sourcecountry 분포만 빠르게 확인하기 위한
테스트 전용 스크립트. 정식 파이프라인(main.py)에서는 안 씀.

정식 collect()는 키워드 4개(HPAI 스킵 후) x 3번 호출(article_search +
timelinevol + timelinevolraw) x REQUEST_INTERVAL(15초) + 429 재시도 대기까지
겹치면 실행이 꽤 오래 걸린다. 지금 확인하려는 건 language/sourcecountry
분포뿐이라 시계열(timeline_search)은 필요 없음 - 그래서:

  1. 키워드를 2개로 줄임 (하나는 결과가 많이 나왔던 것, 하나는 적게 나왔던 것 -
     2026-07-14 실행 기준 "foot and mouth disease"=42건, "feed price"=7건)
  2. skip_timeline=True로 timeline_search 호출 자체를 생략 (키워드당 API 호출
     3번 -> 1번)

이 두 가지만으로 호출 수가 4x3=12번에서 2x1=2번으로 줄어서, 429가 없다고
가정하면 몇 분 안에 끝난다. (429가 뜨면 여전히 최대 900초까지는 대기할 수
있음 - 이건 API 쪽 제약이라 테스트 모드로도 완전히 피할 수는 없음)

사용법:
    python test_gdelt_collector.py

    # 다른 키워드로 확인하고 싶으면:
    python test_gdelt_collector.py "avian influenza" "livestock market"
"""

import sys

import gdelt_collector as gc

# 기본 테스트 키워드 - 2026-07-14 실행에서 결과 건수가 대비되는 2개를 선택
# (많이 나온 것 / 적게 나온 것 둘 다 봐야 분포가 편중되는지 확인 가능)
DEFAULT_TEST_KEYWORDS = ["foot and mouth disease", "feed price"]


def main():
    keywords = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_TEST_KEYWORDS

    print(f"[test] 테스트 키워드: {keywords}")
    print(f"[test] skip_timeline=True (article_search만 수행)")
    print(f"[test] 예상 API 호출 수: 키워드 {len(keywords)}개 x 1번 = {len(keywords)}번 "
          f"(429 재시도 제외)\n")

    articles, timeline = gc.collect(keywords=keywords, skip_timeline=True)

    print(f"\n총 {len(articles)}건 기사 수집 완료")
    print(f"시계열 데이터: {timeline} (skip_timeline=True이므로 항상 비어있음)")

    for a in articles[:3]:
        print(a)

    # 이번 테스트의 핵심 질문 - 언어/국가 분포
    gc._print_distribution(articles)


if __name__ == "__main__":
    main()
