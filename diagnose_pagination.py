from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()

    resp1 = page.goto("https://www.feedandgrain.com/latest-news?page=1", wait_until="domcontentloaded", timeout=30000)
    html1 = page.content()

    resp2 = page.goto("https://www.feedandgrain.com/latest-news?page=2", wait_until="domcontentloaded", timeout=30000)
    html2 = page.content()

    print("page=1 응답 상태코드:", resp1.status)
    print("page=2 응답 상태코드:", resp2.status)
    print("page=1 응답 헤더:", dict(resp1.headers))
    print("page=2 응답 헤더:", dict(resp2.headers))
    print("HTML 길이 동일?", len(html1) == len(html2))
    print("HTML 완전 동일?", html1 == html2)

    browser.close()
