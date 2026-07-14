"""
watt_collector.py
WATT Global Media 계열 2사이트(wattagnet.com / feedstrategy.com )
뉴스를 수집하는 모듈. 2사이트가 동일한 CMS/구조를 쓰는 것을 확인했으므로
공통 로직 하나로 처리한다 (알고리즘 문서 "2사이트 공통 로직 시도" 조건 충족).

(알고리즘 문서 "1. 수집 레이어" - watt_collector 스펙 참조)

requests + BeautifulSoup으로 시도했으나 403 Forbidden으로 막힘 (WAF가 UA를
보고 차단하는 것으로 추정 - TLS 핑거프린팅일 가능성도 있음). robots.txt/이용약관
둘 다 자동 수집을 막지 않는 것으로 확인됐으므로, SunSirs 때와 동일한 논리로
Playwright(실제 브라우저 엔진)를 사용한다.

*** 검증 상태 (2026-07-14, 알고리즘 문서 "1. 수집 레이어" 표와 통일) ***
알고리즘 문서에는 이 수집기가 "2026-07-14 실행 검증 완료"라고만 적혀 있는데,
그건 아래 항목까지만을 뜻한다 - 전체가 다 검증된 게 아니라 항목별로 상태가 다름:
  - 검증 완료: 2사이트(WATTAgNet/Feed Strategy) 동일 구조 여부, 403/Cloudflare
    차단 여부(Feed & Grain만 걸리고 이 2사이트는 안 걸림), 본문(body) 추출
    정확도(아래 _fetch_detail 내 "확인 완료 (2026-07-13)" 주석 참고), 목록
    페이지 아이템 셀렉터·카테고리 추출 로직·기간이탈(cutoff) 로직(2026-07-14,
    WATTAgNet 실행 11건으로 확인), 발행일 포맷(2026-07-14, WATTAgNet만 확인 -
    아래 _parse_published_time 주석 참고)
  - 아직 미검증: 위 항목들 중 Feed Strategy 쪽은 별도로 직접 확인한 적 없음
    (2사이트 동일 CMS/구조라는 전제로 같은 로직을 공유하는 것 - 구조 자체가
    같다는 것과, 발행일 표기 형식까지 100% 같다는 것은 별개 확인이 필요할 수
    있어 다음 단계에서 Feed Strategy도 한 번은 직접 찍어보는 걸 권장)

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

# 2사이트 다 같은 구조라고 확인됨 -> 도메인만 다르게 순회
SITES = {
    "WATTAgNet": "https://www.wattagnet.com",
    "Feed Strategy": "https://www.feedstrategy.com",
}
LIST_PATH = "/latest-news"  # 2사이트 공통 확인됨

DAYS_BACK = 7
MAX_PAGES = 30 #이 이상이면 문제가 있음...

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

    확인 완료 (2026-07-14, WATTAgNet 실제 기사로 검증):
    실제 값은 "Jul 13th, 2026"처럼 시:분:초가 아예 없는 서수 표기 형식이었다
    (check_date.py로 직접 확인 - 이전엔 ISO 8601일 가능성도 열어두고 두 형식
    다 시도하도록 짜뒀었는데, 실측 결과 서수 표기 브랜치 쪽으로 정상 파싱됨을
    확인). 사이트가 애초에 "일" 단위 정보만 주고 시각(hour/minute)은 아예
    제공하지 않는다는 뜻이다.

    이게 문제가 안 되는 이유: scorer.py의 recency_weight/일수계산은 전부
    "경과일수(정수)" 단위로만 동작하고 시:분:초는 어디서도 쓰이지 않는다
    (3번 섹션 계단형 가중치 표 자체가 일 단위). 따라서 시간 정보가 없는 채로
    00:00:00에 고정되는 건 버그가 아니라 사이트 데이터 자체의 특성이고,
    이 프로젝트 스코어링 정확도에 영향이 없다.

    ISO 8601 분기(아래 1번)는 WATTAgNet 실측에서는 한 번도 안 탔지만,
    Feed Strategy는 아직 개별 확인 전이라(위 모듈 docstring 참고) 혹시
    모를 형식 차이에 대비해 안전망으로 남겨둔다 - 실제로 안 쓰이면 그냥
    죽은 코드일 뿐 부작용은 없음.
    """
    raw = raw.strip()

    # 1) ISO 8601 형식 시도 (예: "2026-07-01T12:00:00Z" 또는 "+00:00")
    #    WATTAgNet 실측에서는 안 쓰였음 (2026-07-14 확인) - Feed Strategy
    #    대비 안전망으로만 유지
    try:
        iso = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(iso)
    except ValueError:
        pass

    # 2) "Jul 1st, 2026" 같은 서수 표기 형식 - 실제로 확인된 형식 (2026-07-14)
    cleaned = ORDINAL_PATTERN.sub(r"\1", raw)  # "1st" -> "1"
    try:
        dt = datetime.strptime(cleaned, "%b %d, %Y")
        # 시각 정보 자체가 없는 형식이라 항상 00:00:00 - tzinfo는 UTC로 가정.
        # 날짜 단위 계산만 쓰는 scorer.py 특성상 타임존 오차가 결과에 미치는
        # 영향은 무시 가능한 수준 (경계 케이스라 해봐야 최대 하루 오차).
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    raise ValueError(f"published_time 파싱 실패, 형식 확인 필요: {raw!r}")


def _is_recent(dt: datetime, days: int) -> bool:
    cutoff = datetime.now(dt.tzinfo) - timedelta(days=days)
    return dt >= cutoff


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

        if hit_cutoff:
            break

        time.sleep(REQUEST_INTERVAL)

    return results


# 뉴스 기사가 아니라 스폰서 콘텐츠(네이티브 광고)라 제외.
# (예: /brand-insights/... 경로, 카테고리가 회사명으로 찍혀있고
#  article:published_time 메타 태그도 없어서 상세페이지에서 타임아웃 남)
EXCLUDED_PATH_PATTERNS = ("/brand-insights/",)


def _fetch_listing_page(page, url: str) -> list[dict]:
    page.goto(url, timeout=30000, wait_until="networkidle")

    # 확인 완료 (2026-07-14, WATTAgNet 11건 정상 수집으로 검증):
    # "제목 링크로 보이는 <h5><a>" 패턴이 실제 목록 아이템과 일치함을 확인.
    # 오탐(광고/추천 위젯 포함) 없이 정상 동작.
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

        # 확인 완료 (2026-07-14): 헤드라인 바로 앞 카테고리 링크 방식으로
        # 정상 추출됨 (WATTAgNet 11건 기준)
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
