"""
watt_collector.py
WATT Global Media 계열 2사이트(wattagnet.com / feedstrategy.com )
뉴스를 수집하는 모듈. 2사이트가 동일한 CMS/구조를 쓰는 것을 확인했으므로
공통 로직 하나로 처리한다 (알고리즘 문서 "2사이트 공통 로직 시도" 조건 충족).

(알고리즘 문서 "1. 수집 레이어" - watt_collector 스펙 참조)

중요: body(본문 전문)는 LLM 요약 생성까지만 메모리에서 쓰고, repo에 저장하는
raw.json 등에는 절대 포함하지 않는다 (저장 레이어의 save_raw_json 쪽에서 제외 처리).
이 모듈은 body를 "만들어서 반환"까지만 담당하고, 그걸 어디에 얼마나 남길지는
저장 레이어의 책임이다.
"""
# ------------------------------------------------------------------

import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ------------------------------------------------------------------

# 2사이트 다 같은 구조라고 확인됨 -> 도메인만 다르게 순회
SITES = {
    # 일시 비활성화: WATTAgNet
    # 재활성화하려면 아래 주석만 풀면 됨(다른 코드 변경 불필요).
    # "WATTAgNet": "https://www.wattagnet.com",
    "Feed Strategy": "https://www.feedstrategy.com",
}
LIST_PATH = "/latest-news"

# ZoneInfo를 쓰면 서머타임(CDT/CST) 전환도 자동으로 반영됨.
WATT_SOURCE_TIMEZONE = ZoneInfo("America/Chicago")

DAYS_BACK = 7
MAX_PAGES = 30 

# 사이트별로 페이지네이션 가능 여부를 다르게 둔다.
# WATTAgNet을 나중에 SITES에 다시 넣더라도, 이 목록에 남아있는 한 자동으로 1페이지만 수집하도록 안전장치가 유지된다.
SINGLE_PAGE_ONLY_SITES = {"WATTAgNet"}

# 변경하지 말 것. (403 위험)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# TODO: contact-email-here 실제 연락처로 교체 필요
EXTRA_HEADERS = {
    "X-Crawler-Contact": "mailto:contact-email-here",
}

REQUEST_INTERVAL = 1.5

# "Jul 1st, 2026" / "Jul 2nd, 2026" 같은 서수 표기 제거용
ORDINAL_PATTERN = re.compile(r"(\d+)(st|nd|rd|th)")

# 광고 제외. (상세페이지에서 타임아웃 남)
EXCLUDED_PATH_PATTERNS = ("/brand-insights/",)

# ------------------------------------------------------------------

def _parse_published_time(raw: str) -> datetime:
    """
    article:published_time 메타 태그 값을 datetime으로 변환한다.

    실제 값은 "Jul 13th, 2026"(WATTAgNet) / "Jul 10th, 2026"(Feed Strategy)처럼 시:분:초가 아예 없는 서수 표기 형식.

    ISO 8601 분기(아래 1번)는 WATTAgNet/Feed Strategy 둘 다 실측에서 한 번도
    안 탔지만, 혹시 모를 형식 차이(예: 특정 기사 유형만 다른 포맷을 쓰는
    경우)에 대비해 안전망으로 계속 남겨둔다 - 실제로 안 쓰이면 그냥 죽은
    코드일 뿐 부작용은 없음.
    """
    raw = raw.strip()

    # 1) ISO 8601 형식 시도 (예: "2026-07-01T12:00:00Z" 또는 "+00:00")
    #    안 쓰였지만 혹시 모를 형식 차이 대비 안전망으로만 유지
    try:
        iso = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(iso)
    except ValueError:
        pass

    # 2) "Jul 1st, 2026" 같은 서수 표기 형식 - 실제로 확인된 형식
    cleaned = ORDINAL_PATTERN.sub(r"\1", raw)  # "1st" -> "1"
    try:
        dt = datetime.strptime(cleaned, "%b %d, %Y")
        # UTC로 통일
        # "그날의 미국 중부시간 자정"을 대표값으로 잡고 UTC로 환산하는 근사치.
        # scorer.py가 일 단위 정수로만 경과일을 계산하므로 이 정도 근사로도 충분.
        dt_chicago = dt.replace(tzinfo=WATT_SOURCE_TIMEZONE)
        return dt_chicago.astimezone(timezone.utc)
    except ValueError:
        pass

    #둘 모두 실패하면
    raise ValueError(f"published_time 파싱 실패, 형식 확인 필요: {raw!r}")

# ------------------------------------------------------------------

def _is_recent(dt: datetime, days: int) -> bool:
    cutoff = datetime.now(dt.tzinfo) - timedelta(days=days)
    return dt >= cutoff

# ------------------------------------------------------------------

def collect() -> list[dict]:
    """
    2사이트를 순서대로 돌면서 최근 기사를 수집한다.
    """
    all_results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT, extra_http_headers=EXTRA_HEADERS)

        for source_name, base_url in SITES.items():
            try:
                site_results = _collect_site(page, source_name, base_url)
                all_results.extend(site_results)
                print(f"[watt] '{source_name}' -> 최근 {DAYS_BACK}일 이내 {len(site_results)}건")
            except Exception as e:
                # 사이트 하나 실패해도 전체를 멈추지 않는다 (9.1 소스별 독립 실행 구조)
                print(f"[watt] '{source_name}' 수집 실패: {e}")
                continue

        browser.close()

    return all_results

# ------------------------------------------------------------------

def _collect_site(page, source_name: str, base_url: str) -> list[dict]:
    """
    WATTAgNet은 1페이지만 수집하고, 나머지 사이트(Feed Strategy 등)는 정상적으로 여러 페이지를 순회한다.
    """
    if source_name in SINGLE_PAGE_ONLY_SITES:
        return _collect_single_page(page, source_name, base_url)
    return _collect_paginated(page, source_name, base_url)

# ------------------------------------------------------------------

def _collect_single_page(page, source_name: str, base_url: str) -> list[dict]:
    """
    페이지네이션이 무의미한 사이트(`SINGLE_PAGE_ONLY_SITES`)용
    """
    list_url = f"{base_url}{LIST_PATH}"
    items = _fetch_listing_page(page, list_url)

    if not items:
        print(f"[watt] {source_name}: 항목 없음, 종료")
        return []

    print(f"[watt] {source_name} - 첫 항목: \"{items[0]['title']}\" "
          f"({items[0]['url']}) / 마지막 항목: \"{items[-1]['title']}\"")

    results = []
    for item in items:
        time.sleep(REQUEST_INTERVAL)
        detail = _fetch_detail(page, item["url"])
        if detail is None:
            # 상세 페이지 파싱 실패 - 이 기사만 건너뜀 (전체 중단 아님)
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
            "body": detail["body"],  # 메모리에서만 사용 - repo 저장 시 제외 (저장 레이어 책임)
        })

    return results

# ------------------------------------------------------------------

def _collect_paginated(page, source_name: str, base_url: str) -> list[dict]:
    """
    정상적으로 페이지네이션이 되는 사이트용
    """
    results = []

    for page_num in range(1, MAX_PAGES + 1):
        list_url = f"{base_url}{LIST_PATH}" if page_num == 1 else f"{base_url}{LIST_PATH}?page={page_num}"
        items = _fetch_listing_page(page, list_url)

        if not items:
            print(f"[watt] {source_name} {page_num}페이지: 항목 없음, 종료")
            break

        print(f"[watt] {source_name} {page_num}페이지 - 첫 항목: \"{items[0]['title']}\" "
              f"({items[0]['url']}) / 마지막 항목: \"{items[-1]['title']}\"")

        hit_cutoff = False
        for item in items:
            time.sleep(REQUEST_INTERVAL)
            detail = _fetch_detail(page, item["url"])
            if detail is None:
                # 상세 페이지 파싱 실패 - 이 기사만 건너뜀 (전체 중단 아님)
                continue

            if not _is_recent(detail["published_at"], DAYS_BACK):
                # 최신순 정렬이 확실하므로, 여기서 바로 이 사이트 수집을 끝낸다
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
                "body": detail["body"],  # 메모리에서만 사용 - repo 저장 시 제외 (저장 레이어 책임)
            })

        if hit_cutoff:
            break

        time.sleep(REQUEST_INTERVAL)

    return results

# ------------------------------------------------------------------

def _fetch_listing_page(page, url: str) -> list[dict]:
    response = page.goto(url, timeout=30000, wait_until="networkidle")

    # 2026-07-22 추가: 같은 사이트에서 실행마다 콘텐츠가 요동치는 원인이
    # (a) CDN/캐시 계층이 오래된 스냅샷을 주는 것인지,
    # (b) 안티봇 시스템이 자동화 트래픽을 감지해 의도적으로 다른(오래된/제한된) 콘텐츠를 주는 것인지 구분이 안 돼서,
    # 응답 헤더 중 캐시/서버 식별에 쓰이는 것들을 그대로 로그에 남긴다.
    # - cf-cache-status/x-cache/age가 있고 HIT면 -> 캐시 문제 쪽에 무게
    # - 이런 헤더가 없거나 MISS인데도 내용이 반복되면 -> 캐시가 아니라
    #   서버가 매 요청마다 새로 렌더링하면서 의도적으로 다른 콘텐츠를 주는 것(안티봇 대응 등)일 가능성이 커짐
    if response is not None:
        headers = response.headers
        interesting_keys = ("cf-cache-status", "x-cache", "age", "cache-control", "server", "cf-ray", "vary")
        found = {k: headers[k] for k in interesting_keys if k in headers}
        print(f"[watt] {url} - 응답 헤더(캐시/서버 관련): "
              f"{found if found else '(해당 헤더 없음)'}")

    try:
        page.wait_for_selector("h5 a, h4 a", timeout=15000)
    except Exception:
        print(f"[watt] 목록 로딩 실패 또는 타임아웃: {url}")
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

    # 2026-07-22 추가: 실행마다 WATTAgNet 건수가 크게 요동치는 원인을 못
    # 찾아서(0건/10건/360건 실측 확인 - 담당자 지적), 우선 "그 순간 목록
    # 페이지에서 실제로 몇 개 항목을 읽어왔는지"부터 남긴다. 이게 매번
    # 비슷하면 문제는 상세페이지/날짜 판정 쪽에 있는 거고, 이것부터 들쭉날쭉
    #하면 목록 페이지 자체(캐싱/봇 차단 등)가 원인일 가능성이 커진다.
    print(f"[watt] {url} - 목록에서 {len(results)}개 항목 읽음")
    return results

# ------------------------------------------------------------------

def _fetch_detail(page, url: str) -> dict | None:
    page.goto(url, timeout=30000)

    try:
        page.wait_for_selector("meta[property='article:published_time']", state="attached", timeout=15000)
    except Exception:
        print(f"[watt] 상세 페이지 로딩 실패 또는 타임아웃: {url}")
        return None

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")

    meta_tag = soup.find("meta", attrs={"property": "article:published_time"})
    if not meta_tag or not meta_tag.get("content"):
        print(f"[watt] published_time 메타 태그 없음: {url}")
        return None

    try:
        published_at = _parse_published_time(meta_tag["content"])
    except ValueError as e:
        print(f"[watt] {e}")
        return None

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

# ------------------------------------------------------------------

if __name__ == "__main__":
    results = collect()
    print(f"\n총 {len(results)}건 수집 완료")
    for r in results[:3]:
        # body는 길어서 미리보기만 (100자)
        preview = (r["body"] or "")[:100]
        print({**r, "body": preview + "..." if r["body"] else None})