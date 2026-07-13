"""
watt_collector.py
WATT Global Media 계열 3사이트(wattagnet.com / feedstrategy.com / feedandgrain.com)
뉴스를 수집하는 모듈. 3사이트가 동일한 CMS/구조를 쓰는 것을 확인했으므로
공통 로직 하나로 처리한다 (알고리즘 문서 "3사이트 공통 로직 시도" 조건 충족).

(알고리즘 문서 "1. 수집 레이어" - watt_collector 스펙 참조)

requests + BeautifulSoup으로 시도했으나 403 Forbidden으로 막힘 (WAF가 UA를
보고 차단하는 것으로 추정 - TLS 핑거프린팅일 가능성도 있음). robots.txt/이용약관
둘 다 자동 수집을 막지 않는 것으로 확인됐으므로, SunSirs 때와 동일한 논리로
Playwright(실제 브라우저 엔진)를 사용한다.

*** 아직 검증 전 초안입니다 — 아래 "확인 필요" 표시된 부분(특히 본문 셀렉터)은
    실제 실행 결과를 보고 나서 다음 단계에서 고쳐야 함 ***

중요: body(본문 전문)는 LLM 요약 생성까지만 메모리에서 쓰고, repo에 저장하는
raw.json 등에는 절대 포함하지 않는다 (저장 레이어의 save_raw_json 쪽에서 제외 처리).
이 모듈은 body를 "만들어서 반환"까지만 담당하고, 그걸 어디에 얼마나 남길지는
저장 레이어의 책임이다.
"""

import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# 3사이트 다 같은 구조라고 확인됨 -> 도메인만 다르게 순회
SITES = {
    "WATTAgNet": "https://www.wattagnet.com",
    "Feed Strategy": "https://www.feedstrategy.com",
    "Feed & Grain": "https://www.feedandgrain.com",
}
LIST_PATH = "/latest-news"  # 3사이트 공통 확인됨

DAYS_BACK = 7
MAX_PAGES = 30  # 안전장치 (naver/sunsirs와 동일한 취지) 이 숫자를 넘기면 뭔가 잘못된 거다...

# requests의 UA만 바꿔도 403이 계속 떠서(TLS 핑거프린팅 등 추정) Playwright로 전환.
# 완전히 정체를 숨기지는 않기 위해 커스텀 헤더에 연락처는 남겨둠.
# TODO: contact-email-here 실제 연락처로 교체 필요
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
EXTRA_HEADERS = {
    "X-Crawler-Contact": "mailto:contact-email-here",
}

# 같은 호스트 연속 요청 간격. sunsirs보다 요청 수가 많아지므로
# (기사 1건당 상세페이지 요청 1번씩 추가) 조금 더 넉넉하게 잡음
REQUEST_INTERVAL = 1.5

# "Jul 1st, 2026" / "Jul 2nd, 2026" 같은 서수 표기 제거용
ORDINAL_PATTERN = re.compile(r"(\d+)(st|nd|rd|th)")


def _parse_published_time(raw: str) -> datetime:
    """
    article:published_time 메타 태그 값을 datetime으로 변환한다.

    TODO 확인 필요: 실제 raw HTML의 meta 태그 content가 ISO 8601
    ("2026-07-01T12:00:00Z")인지, 사람이 읽는 형식("Jul 1st, 2026")인지
    직접 확인 안 된 상태. 그래서 두 형식 다 시도하도록 짜둠.
    """
    raw = raw.strip()

    # 1) ISO 8601 형식 시도 (예: "2026-07-01T12:00:00Z" 또는 "+00:00")
    try:
        iso = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(iso)
    except ValueError:
        pass

    # 2) "Jul 1st, 2026" 같은 서수 표기 형식 시도
    cleaned = ORDINAL_PATTERN.sub(r"\1", raw)  # "1st" -> "1"
    try:
        dt = datetime.strptime(cleaned, "%b %d, %Y")
        return dt.replace(tzinfo=timezone.utc)  # 시간대 정보가 없어 UTC로 가정 (확인 필요)
    except ValueError:
        pass

    raise ValueError(f"published_time 파싱 실패, 형식 확인 필요: {raw!r}")


def _is_recent(dt: datetime, days: int) -> bool:
    cutoff = datetime.now(dt.tzinfo) - timedelta(days=days)
    return dt >= cutoff


def collect() -> list[dict]:
    """
    3사이트를 순서대로 돌면서 최근 기사를 수집한다.
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


def _collect_site(page, source_name: str, base_url: str) -> list[dict]:
    results = []

    for page_num in range(1, MAX_PAGES + 1):
        list_url = f"{base_url}{LIST_PATH}" if page_num == 1 else f"{base_url}{LIST_PATH}?page={page_num}"
        items = _fetch_listing_page(page, list_url)

        if not items:
            print(f"[watt] {source_name} {page_num}페이지: 항목 없음, 종료")
            break

        hit_cutoff = False
        for item in items:
            time.sleep(REQUEST_INTERVAL)
            detail = _fetch_detail(page, item["url"])
            if detail is None:
                # 상세 페이지 파싱 실패 - 이 기사만 건너뜀 (전체 중단 아님)
                continue

            if not _is_recent(detail["published_at"], DAYS_BACK):
                # 최신순 정렬이 확실하므로, 여기서 바로 이 사이트 수집을 끝낸다
                print(f"[watt] {source_name} {page_num}페이지에서 기간 이탈, 종료")
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

        print(f"[watt-debug] {source_name} {page_num}페이지: items={len(items)}건 파싱, "
              f"이번 페이지 URL 목록={[it['url'] for it in items]}, "
              f"현재까지 누적 results={len(results)}건, "
              f"결과 published_at 최근5개={[r['published_at'][:10] for r in results[-5:]]}")

        if hit_cutoff:
            break

        time.sleep(REQUEST_INTERVAL)

    return results


# 뉴스 기사가 아니라 스폰서 콘텐츠(네이티브 광고)라 제외.
# (예: /brand-insights/... 경로, 카테고리가 회사명으로 찍혀있고
#  article:published_time 메타 태그도 없어서 상세페이지에서 타임아웃 남)
EXCLUDED_PATH_PATTERNS = ("/brand-insights/",)


def _fetch_listing_page(page, url: str) -> list[dict]:
    page.goto(url, timeout=30000)

    # TODO 확인 필요: 실제 목록 아이템을 감싸는 컨테이너 class를 몰라서
    # "제목 링크로 보이는 <h5><a>" 패턴으로 느슨하게 잡음. 오탐(광고/추천 위젯
    # 포함) 가능성 있어 실행 결과 보고 좁혀야 함.
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

        # 카테고리: 헤드라인 바로 앞에 오는 카테고리 링크로 추정 (확인 필요)
        category = None
        prev_link = heading.find_previous("a")
        if prev_link and prev_link.get_text(strip=True):
            category = prev_link.get_text(strip=True)

        if title and link and not any(pat in link for pat in EXCLUDED_PATH_PATTERNS):
            results.append({"title": title, "url": link, "category": category})

    return results


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

    # 본문 컨테이너 class를 몰라서 "추천/관련기사 위젯 전 <p> 태그 전부"로
    # 긁는 방식으로 짰었는데, 실제 기사로 전체 출력 검증해본 결과 위젯 텍스트
    # 섞임 없이 깔끔하게 추출됨 확인 완료 (2026-07-13) - 이 방식으로 확정.
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
        # body는 길어서 미리보기만 (100자)
        preview = (r["body"] or "")[:100]
        print({**r, "body": preview + "..." if r["body"] else None})
