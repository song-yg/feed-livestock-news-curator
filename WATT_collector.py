"""
watt_collector.py
WATT Global Media 계열 사이트(feedstrategy.com) 뉴스 수집 모듈.
requests+BeautifulSoup은 403으로 막혀 Playwright 사용. WATTAgNet은 Cloudflare
봇 차단으로 영구 제외(SITES 참고).
body(본문 전문)는 메모리에서만 쓰고 repo 저장(raw.json)에는 제외(저장 레이어 책임).
"""

import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

SITES = {
    # WATTAgNet: Cloudflare 봇 차단 챌린지 확인됨(Playwright 재현 테스트) - 영구 제외.
    # "WATTAgNet": "https://www.wattagnet.com",
    "Feed Strategy": "https://www.feedstrategy.com",
}
LIST_PATH = "/latest-news"

WATT_SOURCE_TIMEZONE = ZoneInfo("America/Chicago")  # WATT Global Media 본사(Rockford, IL) 기준

DAYS_BACK = 7
MAX_PAGES = 30

# WATTAgNet류 봇 차단 사이트를 위한 안전장치(현재 SITES에 해당 사이트 없어 실질 미사용).
SINGLE_PAGE_ONLY_SITES = {"WATTAgNet"}

# UA만 바꿔도 403 지속(TLS 핑거프린팅 추정)이라 Playwright 사용.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
EXTRA_HEADERS = {
    "X-Crawler-Contact": "mailto:contact-email-here",
}

REQUEST_INTERVAL = 1.5  # 같은 호스트 연속 요청 간격(초)

ORDINAL_PATTERN = re.compile(r"(\d+)(st|nd|rd|th)")  # "Jul 1st" -> "Jul 1"


def _parse_published_time(raw: str) -> datetime:
    """
    article:published_time 메타 태그 값을 datetime으로 변환.
    실제 형식은 "Jul 13th, 2026"(시:분:초 없음, 날짜만) - scorer.py가 일 단위
    경과일만 쓰므로 정밀도 문제 없음. ISO 8601 분기는 안전망(실측에서 미확인).
    """
    raw = raw.strip()

    try:
        iso = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(iso)
    except ValueError:
        pass

    cleaned = ORDINAL_PATTERN.sub(r"\1", raw)
    try:
        dt = datetime.strptime(cleaned, "%b %d, %Y")
        # WATT 본사 시간대(America/Chicago) 자정으로 해석 후 UTC 환산.
        # 국내(네이버)는 KST 그대로, 해외(GDELT/WATT)는 UTC로 통일하는 원칙.
        dt_chicago = dt.replace(tzinfo=WATT_SOURCE_TIMEZONE)
        return dt_chicago.astimezone(timezone.utc)
    except ValueError:
        pass

    raise ValueError(f"published_time 파싱 실패, 형식 확인 필요: {raw!r}")


def _is_recent(dt: datetime, days: int) -> bool:
    cutoff = datetime.now(dt.tzinfo) - timedelta(days=days)
    return dt >= cutoff


def collect() -> list[dict]:
    """SITES를 순회하며 최근 기사 수집."""
    all_results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT, extra_http_headers=EXTRA_HEADERS)

            for source_name, base_url in SITES.items():
                try:
                    site_results = _collect_site(page, source_name, base_url)
                    all_results.extend(site_results)
                    print(f"[watt] '{source_name}' -> 최근 {DAYS_BACK}일 이내 {len(site_results)}건")
                except Exception as e:
                    print(f"[watt] 🔴 조치필요 [WT-01] - '{source_name}' 수집 실패: {type(e).__name__} - {e!r}")
                    continue
        finally:
            browser.close()

    return all_results


def _collect_site(page, source_name: str, base_url: str) -> list[dict]:
    """SINGLE_PAGE_ONLY_SITES면 1페이지만, 아니면 페이지네이션 수집."""
    if source_name in SINGLE_PAGE_ONLY_SITES:
        return _collect_single_page(page, source_name, base_url)
    return _collect_paginated(page, source_name, base_url)


def _collect_single_page(page, source_name: str, base_url: str) -> list[dict]:
    """페이지네이션 불가 사이트용 - 1페이지(최대 12개)만 수집. 현재 SITES에 해당 없어 미호출."""
    list_url = f"{base_url}{LIST_PATH}"
    items = _fetch_listing_page(page, list_url)

    if not items:
        print(f"[watt] {source_name}: 항목 없음, 종료")
        return []

    print(f"[watt] {source_name} - 첫 항목: \"{items[0]['title']}\" "
          f"/ 마지막 항목: \"{items[-1]['title']}\"")

    results = []
    for item in items:
        time.sleep(REQUEST_INTERVAL)
        detail = _fetch_detail(page, item["url"])
        if detail is None:
            continue

        if not _is_recent(detail["published_at"], DAYS_BACK):
            cutoff = datetime.now(detail["published_at"].tzinfo) - timedelta(days=DAYS_BACK)
            print(f"[watt] {source_name} - 기간 밖 기사 제외 (기사: \"{item['title']}\" / "
                  f"판정된 발행일: {detail['published_at'].isoformat()} / "
                  f"기준선(오늘-{DAYS_BACK}일): {cutoff.isoformat()})")
            continue

        results.append({
            "source": source_name,
            "title": item["title"],
            "url": item["url"],
            "published_at": detail["published_at"].isoformat(),
            "category": item["category"],
            "body": detail["body"],
        })

    return results


def _collect_paginated(page, source_name: str, base_url: str) -> list[dict]:
    """페이지네이션 사이트용. 최신순 정렬 전제, 7일 지난 기사 만나면 즉시 종료."""
    results = []

    for page_num in range(1, MAX_PAGES + 1):
        list_url = f"{base_url}{LIST_PATH}" if page_num == 1 else f"{base_url}{LIST_PATH}?page={page_num}"
        items = _fetch_listing_page(page, list_url)

        if not items:
            print(f"[watt] {source_name} {page_num}페이지: 항목 없음, 종료")
            break

        print(f"[watt] {source_name} {page_num}페이지 - 첫 항목: \"{items[0]['title']}\" "
              f"/ 마지막 항목: \"{items[-1]['title']}\"")

        hit_cutoff = False
        for item in items:
            time.sleep(REQUEST_INTERVAL)
            detail = _fetch_detail(page, item["url"])
            if detail is None:
                continue

            if not _is_recent(detail["published_at"], DAYS_BACK):
                cutoff = datetime.now(detail["published_at"].tzinfo) - timedelta(days=DAYS_BACK)
                print(f"[watt] {source_name} {page_num}페이지에서 기간 이탈, 종료 "
                      f"(기사: \"{item['title']}\" / 판정된 발행일: "
                      f"{detail['published_at'].isoformat()} / 기준선(오늘-{DAYS_BACK}일): "
                      f"{cutoff.isoformat()})")
                hit_cutoff = True
                break

            results.append({
                "source": source_name,
                "title": item["title"],
                "url": item["url"],
                "published_at": detail["published_at"].isoformat(),
                "category": item["category"],
                "body": detail["body"],
            })

        if hit_cutoff:
            break

        time.sleep(REQUEST_INTERVAL)

    return results


EXCLUDED_PATH_PATTERNS = ("/brand-insights/",)  # 스폰서 콘텐츠 제외


def _domain_only(url: str) -> str:
    """로그용 도메인만 추출."""
    domain = urlparse(url).netloc
    return domain[4:] if domain.startswith("www.") else domain


def _fetch_listing_page(page, url: str) -> list[dict]:
    """목록 페이지 수집. 이동/로딩 실패 시 빈 리스트(이 사이트만 0건으로 안전하게 종료)."""
    try:
        response = page.goto(url, timeout=30000, wait_until="networkidle")
    except Exception as e:
        print(f"[watt] 🟡 주의 [WT-02] - 목록 페이지 이동 실패: {_domain_only(url)} - {type(e).__name__} - {e!r}")
        return []

    if response is not None:
        headers = response.headers
        interesting_keys = ("cf-cache-status", "x-cache", "age", "cache-control", "server", "cf-ray", "vary")
        found = {k: headers[k] for k in interesting_keys if k in headers}
        print(f"[watt] {_domain_only(url)} - 응답 헤더(캐시/서버 관련): "
              f"{found if found else '(해당 헤더 없음)'}")

    try:
        page.wait_for_selector("h5 a, h4 a", timeout=15000)
    except Exception:
        print(f"[watt] 🟡 주의 [WT-03] - 목록 로딩 실패 또는 타임아웃: {_domain_only(url)}")
        return []

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")

    results = []
    for heading in soup.select("h5 a[href], h4 a[href]"):
        title = heading.get_text(strip=True)
        link = urljoin(url, heading["href"])

        category = None
        prev_link = heading.find_previous("a")
        if prev_link and prev_link.get_text(strip=True):
            category = prev_link.get_text(strip=True)

        if title and link and not any(pat in link for pat in EXCLUDED_PATH_PATTERNS):
            results.append({"title": title, "url": link, "category": category})

    print(f"[watt] {_domain_only(url)} - 목록에서 {len(results)}개 항목 읽음")
    return results


def _fetch_detail(page, url: str) -> dict | None:
    """상세 페이지에서 발행일+본문 추출. 실패 시 None(그 기사만 건너뜀)."""
    try:
        page.goto(url, timeout=30000)
    except Exception as e:
        print(f"[watt] 🟡 주의 [WT-04] - 상세 페이지 이동 실패: {url} - {type(e).__name__} - {e!r}")
        return None

    try:
        page.wait_for_selector("meta[property='article:published_time']", state="attached", timeout=15000)
    except Exception:
        print(f"[watt] 🟡 주의 [WT-05] - 상세 페이지 로딩 실패 또는 타임아웃: {url}")
        return None

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")

    meta_tag = soup.find("meta", attrs={"property": "article:published_time"})
    if not meta_tag or not meta_tag.get("content"):
        print(f"[watt] 🟡 주의 [WT-06] - published_time 메타 태그 없음: {url}")
        return None

    try:
        published_at = _parse_published_time(meta_tag["content"])
    except ValueError as e:
        print(f"[watt] 🟡 주의 [WT-07] - {type(e).__name__} - {e!r}")
        return None

    # 본문 컨테이너 class 미상 - 추천/관련기사 위젯 전 <p> 태그 전부 수집.
    paragraphs = []
    for p_tag in soup.find_all("p"):
        text = p_tag.get_text(strip=True)
        if not text:
            continue
        if text.lower() in ("recommended", "related stories"):
            break
        paragraphs.append(text)
    body = "\n\n".join(paragraphs) if paragraphs else None

    return {"published_at": published_at, "body": body}


if __name__ == "__main__":
    results = collect()
    print(f"\n총 {len(results)}건 수집 완료")
    for r in results[:3]:
        preview = (r["body"] or "")[:100]
        print({**r, "body": preview + "..." if r["body"] else None})