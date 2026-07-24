"""
test_wattagnet_cache.py - WATTAgNet Cloudflare 캐시 문제 재확인용 독립 스크립트.

실제 파이프라인 코드(WATT_collector.py)의 파싱 로직(제목/링크/카테고리
추출)은 그대로 가져와서 쓰되, 페이지 진입 방식만 다르게 한다 - 담당자 1차
실행에서 `_fetch_listing_page`가 쓰는 `wait_until="networkidle"`에서 3개
페이지 전부 30초 타임아웃이 난 게 확인됨(2026-07-25). networkidle은
"500ms 동안 네트워크 요청 0개"가 조건인데, 광고/트래커가 많은 뉴스
사이트는 백그라운드 요청이 끊이지 않아 이 조건이 영원히 안 만족될 수
있음(Playwright 공식 문서에서도 실서비스 사이트엔 권장 안 함) - 이 스크립트
에선 `domcontentloaded`로 먼저 진입하고, 실제 콘텐츠 로딩 여부는 원래
코드에도 있던 `wait_for_selector`(제목 링크 태그 대기)로 별도 확인한다.

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
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.wattagnet.com"
LIST_PATH = "/latest-news"
PAGES_TO_CHECK = [1, 2, 3]

# WATT_collector.py의 EXCLUDED_PATH_PATTERNS와 동일 - 스폰서 콘텐츠 제외
EXCLUDED_PATH_PATTERNS = ("/brand-insights/",)


def fetch_listing_page(page, url: str, page_num: int) -> list[dict]:
    """
    WATT_collector._fetch_listing_page()와 파싱 로직은 동일, 페이지 진입
    방식만 다름(위 모듈 docstring 참고 - networkidle 타임아웃 회피).

    2026-07-25(2차) 추가: wait_for_selector가 실패할 때, "진짜 콘텐츠가
    안 왔다"는 것만 알아선 원인(캐시 vs 봇 차단)을 못 가르니 - 그 시점에
    실제로 브라우저에 뭐가 로딩돼 있는지(페이지 제목, 본문 앞부분, 스크린샷)
    를 남긴다. Cloudflare 봇 차단/챌린지 페이지는 보통 제목이 "Just a
    moment..." 등 특징적인 문구라 이것만 봐도 바로 구분 가능.
    """
    try:
        response = page.goto(url, timeout=30000, wait_until="domcontentloaded")
    except Exception as e:
        print(f"[watt-test] 목록 페이지 이동 실패: {url} - {type(e).__name__}: {e}")
        return []

    if response is not None:
        headers = response.headers
        interesting_keys = ("cf-cache-status", "x-cache", "age", "cache-control", "server", "cf-ray", "vary")
        found = {k: headers[k] for k in interesting_keys if k in headers}
        print(f"[watt-test] {url} - 응답 헤더(캐시/서버 관련): "
              f"{found if found else '(해당 헤더 없음)'}")
        print(f"[watt-test] {url} - HTTP 상태 코드: {response.status}")

    try:
        page.wait_for_selector("h5 a, h4 a", timeout=20000)
    except Exception:
        print(f"[watt-test] 목록 로딩 실패 또는 타임아웃(콘텐츠 셀렉터 못 찾음): {url}")
        # 2026-07-25(2차) 추가: 뭐가 로딩됐는지 진단 정보 남기기
        try:
            page_title = page.title()
            body_text = page.inner_text("body")[:300]
            print(f"[watt-test]   -> 실제 로딩된 페이지 제목: {page_title!r}")
            print(f"[watt-test]   -> 본문 앞부분(300자): {body_text!r}")
            screenshot_path = f"wattagnet_debug_page{page_num}.png"
            page.screenshot(path=screenshot_path)
            print(f"[watt-test]   -> 스크린샷 저장: {screenshot_path} (직접 열어서 확인해보세요)")
        except Exception as diag_error:
            print(f"[watt-test]   -> 진단 정보 수집도 실패: {diag_error}")
        return []

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")

    results = []
    for heading in soup.select("h5 a[href], h4 a[href]"):
        title = heading.get_text(strip=True)
        link = urljoin(url, heading["href"])
        if title and link and not any(pat in link for pat in EXCLUDED_PATH_PATTERNS):
            results.append({"title": title, "url": link})
    return results


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

                items = fetch_listing_page(page, url, page_num)

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
            print("(페이지 진입 자체가 계속 실패한다면 캐시 문제 이전 단계의 "
                  "별도 문제일 수 있습니다 - 접속 제한/봇 차단 등)")
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