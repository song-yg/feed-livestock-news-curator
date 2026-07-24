"""
test_wattagnet_cache.py - WATTAgNet Cloudflare 캐시 문제 재확인용 독립 스크립트.

실제 파이프라인 코드(WATT_collector.py)의 _fetch_listing_page()를 그대로
가져와서 쓴다 - 담당자 직접 실행 요청(2026-07-25): 이 세션의 web_fetch
도구로 확인한 결과는 실제 GitHub Actions 러너/Playwright 환경과 IP·헤더·
CDN 엣지 위치가 달라서 다르게 나올 수 있으므로, 실제 프로덕션 코드 경로와
동일한 방식(Playwright)으로 직접 재현해서 확인하기 위함.

** 실행 전 준비 **
  pip install playwright beautifulsoup4 --break-system-packages
  playwright install chromium

** 실행 방법 **
  이 파일을 WATT_collector.py와 같은 디렉토리(프로젝트 루트)에 놓고:
  python test_wattagnet_cache.py

** 확인 포인트 **
  - 각 페이지(?page=1, 2, 3)의 응답 헤더(cf-cache-status, age, cf-ray) -
    cf-cache-status가 HIT이고 age가 크면(예: 수만 초 이상) 캐시가 갱신 안
    되고 있다는 뜻
  - 각 페이지의 "첫 항목"/"마지막 항목" 제목 - 페이지 번호가 다른데도
    똑같은 제목이 나오면(혹은 요청한 페이지 번호와 실제 내용이 안 맞으면)
    캐시 문제가 여전하다는 뜻(2026-07-22 최초 발견 당시와 동일 증상)
"""

import sys

from playwright.sync_api import sync_playwright

# WATT_collector.py를 그대로 import해서 실제 파이프라인과 동일한 파싱
# 로직(_fetch_listing_page)을 쓴다 - 이 스크립트가 직접 파싱 로직을
# 따로 구현하면, "테스트 코드는 정상인데 실제 코드는 문제 있음" 같은
# 괴리가 생길 수 있어서 피함.
try:
    from WATT_collector import _fetch_listing_page
except ImportError:
    print("[오류] WATT_collector.py를 찾을 수 없습니다 - 이 스크립트를 "
          "WATT_collector.py와 같은 디렉토리(프로젝트 루트)에서 실행해주세요.")
    sys.exit(1)

BASE_URL = "https://www.wattagnet.com"
LIST_PATH = "/latest-news"
PAGES_TO_CHECK = [1, 2, 3]


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        results = {}
        try:
            for page_num in PAGES_TO_CHECK:
                url = f"{BASE_URL}{LIST_PATH}" if page_num == 1 else f"{BASE_URL}{LIST_PATH}?page={page_num}"
                print(f"\n{'=' * 70}")
                print(f"요청: {url}")
                print(f"{'=' * 70}")

                items = _fetch_listing_page(page, url)
                # _fetch_listing_page가 이미 응답 헤더(cf-cache-status 등)를
                # 자체적으로 print하므로 여기서 따로 안 찍음 - 위 출력 참고.

                if not items:
                    print("  -> 항목을 하나도 못 읽어옴 (페이지 구조가 바뀌었거나 접근 실패)")
                    results[page_num] = None
                    continue

                print(f"  -> {len(items)}개 항목 읽음")
                print(f"  -> 첫 항목: \"{items[0]['title']}\"")
                print(f"  -> 마지막 항목: \"{items[-1]['title']}\"")
                results[page_num] = items[0]["title"]

        finally:
            browser.close()

        print(f"\n{'=' * 70}")
        print("=== 종합 판정 ===")
        print(f"{'=' * 70}")
        titles = [t for t in results.values() if t is not None]
        if len(titles) < 2:
            print("비교할 페이지가 충분치 않습니다(2개 이상 성공해야 비교 가능).")
            return

        if len(set(titles)) == 1:
            print("❌ 캐시 문제 여전함: 서로 다른 page 번호를 요청했는데 "
                  "\"첫 항목\" 제목이 전부 동일합니다.")
            print("   -> WATTAgNet은 계속 SITES에서 제외 상태로 두는 게 안전합니다.")
        else:
            print("✅ 페이지마다 다른 콘텐츠가 나옵니다 - 캐시 문제가 해소됐을 "
                  "가능성이 있습니다.")
            print("   -> 그래도 며칠 더 지켜본 뒤에 SITES에 다시 넣는 걸 권장합니다"
                  "(하루짜리 우연일 수 있음).")
        print()
        for page_num, title in results.items():
            print(f"  page={page_num}: {title!r}")


if __name__ == "__main__":
    main()