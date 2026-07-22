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

*** 검증 상태 (2026-07-14 최초 작성 / 2026-07-15 Feed Strategy 확인 후 갱신) ***
알고리즘 문서에는 이 수집기가 "실행 검증 완료"라고 적혀 있는데, 항목별로
검증 시점과 방법이 다르므로 아래에 정리한다:
  - 검증 완료(WATTAgNet, 2026-07-14, 실제 Playwright 실행 11건 기준): 403/
    Cloudflare 차단 여부(Feed & Grain만 걸리고 이 2사이트는 안 걸림), 본문
    (body) 추출 정확도(아래 _fetch_detail 내 "확인 완료 (2026-07-13)" 주석
    참고), 목록 페이지 아이템 셀렉터·카테고리 추출 로직·기간이탈(cutoff)
    로직, 발행일 포맷(아래 _parse_published_time 주석 참고)
  - 검증 완료(Feed Strategy, 2026-07-15, 실제 사이트 라이브 페이지 대조
    기준 - 아래 주의사항 참고): 목록 페이지 구조("헤드라인 바로 앞 카테고리
    링크" 패턴 포함), 발행일 포맷(서수 표기, 시:분:초 없음 - WATTAgNet과
    동일), 페이지네이션 URL 패턴(`?page=2`), 본문 종료 마커("Recommended"/
    "Related Stories") - 실제 목록 페이지 1개 + 상세 페이지 1개를 열어
    대조한 결과이며, WATTAgNet 때처럼 이 collector 코드를 Playwright로
    직접 실행해서 확인한 것은 아니다 (이 세션 환경 네트워크 정책상
    feedstrategy.com에 직접 접근 불가 - 별도 웹 조회 도구로 대조함).
    구조 자체가 같다는 신뢰도는 높아졌으나, 실제 코드 실행 기준의 완전한
    검증(예: 전체 목록 페이지네이션, 예외적인 기사 포맷 등)은 아직 아니므로
    최초 실제 운영 시 결과를 한 번 더 확인하는 걸 권장.

중요: body(본문 전문)는 LLM 요약 생성까지만 메모리에서 쓰고, repo에 저장하는
raw.json 등에는 절대 포함하지 않는다 (저장 레이어의 save_raw_json 쪽에서 제외 처리).
이 모듈은 body를 "만들어서 반환"까지만 담당하고, 그걸 어디에 얼마나 남길지는
저장 레이어의 책임이다.
"""

import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# 2사이트 다 같은 구조라고 확인됨 -> 도메인만 다르게 순회
SITES = {
    "WATTAgNet": "https://www.wattagnet.com",
    "Feed Strategy": "https://www.feedstrategy.com",
}
LIST_PATH = "/latest-news"  # 2사이트 공통 확인됨

# 2026-07-21 추가: WATT Global Media(WATTAgNet/Feed Strategy 둘 다 이 회사
# 발행) 본사가 일리노이주 록포드(Rockford, IL)라는 걸 검색으로 확인 -
# 미국 중부시간대. 발행일에 시:분:초가 없어(_parse_published_time 참고)
# 정확한 발행 시각까지는 알 수 없지만, 최소한 "그 날짜"를 어느 시간대
# 기준으로 해석해야 하는지는 이걸로 정할 수 있다. ZoneInfo를 쓰면 서머타임
# (CDT/CST) 전환도 자동으로 반영됨.
WATT_SOURCE_TIMEZONE = ZoneInfo("America/Chicago")

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
    # 2026-07-22 추가: WATTAgNet 목록 페이지가 특정 순간에 ~2.5개월 전
    # 기사를 최상단에 보여준 사례가 실측 확인됨(같은 URL을 사람이 직접
    # 브라우저로 열었을 때는 최신 콘텐츠였음) - CDN/캐시 계층이 가끔 오래된
    # 스냅샷을 돌려주는 것으로 추정, 캐시를 우회하도록 요청 헤더 추가.
    # 이 헤더만으로 100% 해결된다는 보장은 없음(서버가 무시할 수도 있음) -
    # 아래 리스트 URL의 캐시 버스팅 쿼리 파라미터와 함께 이중으로 시도.
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# 같은 호스트 연속 요청 간격. sunsirs보다 요청 수가 많아지므로
# (기사 1건당 상세페이지 요청 1번씩 추가) 조금 더 넉넉하게 잡음
REQUEST_INTERVAL = 1.5

# "Jul 1st, 2026" / "Jul 2nd, 2026" 같은 서수 표기 제거용
ORDINAL_PATTERN = re.compile(r"(\d+)(st|nd|rd|th)")


def _parse_published_time(raw: str) -> datetime:
    """
    article:published_time 메타 태그 값을 datetime으로 변환한다.

    확인 완료 (2026-07-14 WATTAgNet 실제 기사 / 2026-07-15 Feed Strategy
    라이브 페이지 대조):
    실제 값은 "Jul 13th, 2026"(WATTAgNet) / "Jul 10th, 2026"(Feed Strategy)
    처럼 시:분:초가 아예 없는 서수 표기 형식이었다 (WATTAgNet은
    check_date.py로 직접 확인, Feed Strategy는 2026-07-15 라이브 페이지
    조회로 대조 - 이전엔 ISO 8601일 가능성도 열어두고 두 형식 다 시도하도록
    짜뒀었는데, 실측 결과 두 사이트 다 서수 표기 브랜치 쪽으로 정상 파싱됨을
    확인). 사이트가 애초에 "일" 단위 정보만 주고 시각(hour/minute)은 아예
    제공하지 않는다는 뜻이다.

    이게 문제가 안 되는 이유: scorer.py의 recency_weight/일수계산은 전부
    "경과일수(정수)" 단위로만 동작하고 시:분:초는 어디서도 쓰이지 않는다
    (3번 섹션 계단형 가중치 표 자체가 일 단위). 따라서 시간 정보가 없는 채로
    00:00:00에 고정되는 건 버그가 아니라 사이트 데이터 자체의 특성이고,
    이 프로젝트 스코어링 정확도에 영향이 없다.

    ISO 8601 분기(아래 1번)는 WATTAgNet/Feed Strategy 둘 다 실측에서 한 번도
    안 탔지만, 혹시 모를 형식 차이(예: 특정 기사 유형만 다른 포맷을 쓰는
    경우)에 대비해 안전망으로 계속 남겨둔다 - 실제로 안 쓰이면 그냥 죽은
    코드일 뿐 부작용은 없음.
    """
    raw = raw.strip()

    # 1) ISO 8601 형식 시도 (예: "2026-07-01T12:00:00Z" 또는 "+00:00")
    #    WATTAgNet/Feed Strategy 실측 둘 다에서 안 쓰였음 (2026-07-14/07-15
    #    확인) - 혹시 모를 형식 차이 대비 안전망으로만 유지
    try:
        iso = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(iso)
    except ValueError:
        pass

    # 2) "Jul 1st, 2026" 같은 서수 표기 형식 - 실제로 확인된 형식 (2026-07-14)
    cleaned = ORDINAL_PATTERN.sub(r"\1", raw)  # "1st" -> "1"
    try:
        dt = datetime.strptime(cleaned, "%b %d, %Y")
        # 2026-07-21 수정: 예전엔 시각 정보가 없다는 이유로 그냥 UTC 자정으로
        # 박아버렸는데, 실제로는 WATT Global Media 본사(WATTAgNet/Feed
        # Strategy 둘 다 이 회사 발행)가 일리노이주 록포드(Rockford, IL) -
        # 미국 중부시간대(America/Chicago, 서머타임 자동 적용)에 있다는 걸
        # 확인함(검색으로 본사 주소 확인). 그러니 "Jul 13th"는 UTC 자정이
        # 아니라 미국 중부시간 자정에 더 가깝다고 보는 게 맞음.
        #
        # 이 프로젝트의 시간대 원칙: 네이버(국내)는 API가 이미 정확한
        # +09:00(KST)를 주므로 그대로 두고, GDELT/WATT(해외)는 UTC로
        # 통일한다 - "국내는 KST, 해외는 UTC" 일관된 축으로 맞추기 위함
        # (담당자 결정, 2026-07-21).
        #
        # 여전히 시:분:초 단위 정밀도는 없다(사이트가 날짜만 줌) - "그날의
        # 미국 중부시간 자정"을 대표값으로 잡고 UTC로 환산하는 근사치다.
        # scorer.py가 일 단위 정수로만 경과일을 계산하므로 이 정도 근사로도
        # 충분하지만, 예전(그냥 UTC 자정으로 착각)보다는 최대 하루 가까이
        # 나던 오차가 훨씬 줄어든다.
        dt_chicago = dt.replace(tzinfo=WATT_SOURCE_TIMEZONE)
        return dt_chicago.astimezone(timezone.utc)
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
        # 2026-07-22 추가: 캐시 우회용 타임스탬프 쿼리 파라미터. 이 URL은
        # item["url"](상세 페이지 링크, 기사 dict에 저장돼 이후 dedup 등에
        # 쓰임)과는 별개로 "목록 페이지 탐색용"으로만 쓰이므로, 파라미터를
        # 붙여도 저장되는 기사 데이터에는 영향 없음.
        cache_bust = f"_cb={int(time.time())}"
        if page_num == 1:
            list_url = f"{base_url}{LIST_PATH}?{cache_bust}"
        else:
            list_url = f"{base_url}{LIST_PATH}?page={page_num}&{cache_bust}"
        items = _fetch_listing_page(page, list_url)

        if not items:
            print(f"[watt] {source_name} {page_num}페이지: 항목 없음, 종료")
            break

        # 2026-07-22 추가: 캐시 버스팅 파라미터 도입 후 23페이지(276건)를
        # 넘어도 "기간 이탈"이 안 뜨는 이상 현상 발생(담당자 실측) - 페이지네이션
        # 자체가 깨져서 같은 콘텐츠를 반복해서 주는 건 아닌지 바로 확인할 수
        # 있게, 각 페이지의 첫/마지막 항목 제목을 그대로 로그에 남긴다.
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
                # 2026-07-22 추가: "왜" 기간 이탈로 판정됐는지 실제 날짜 값을
                # 남긴다 - 담당자가 같은 사이트에서 실행마다 0건/10건/360건
                # 처럼 크게 다른 결과를 실측 확인했는데, 기존 로그로는 원인
                # 파악이 안 돼서(파싱된 날짜가 진짜로 오래된 건지, 아니면
                # 날짜 파싱 자체가 잘못돼서 최신 기사를 오래된 걸로 오판한
                # 건지 구분 불가) 추가함.
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


# 뉴스 기사가 아니라 스폰서 콘텐츠(네이티브 광고)라 제외.
# (예: /brand-insights/... 경로, 카테고리가 회사명으로 찍혀있고
#  article:published_time 메타 태그도 없어서 상세페이지에서 타임아웃 남)
EXCLUDED_PATH_PATTERNS = ("/brand-insights/",)


def _fetch_listing_page(page, url: str) -> list[dict]:
    page.goto(url, timeout=30000, wait_until="networkidle")

    # 확인 완료 (2026-07-14 WATTAgNet 11건 Playwright 실행 / 2026-07-15
    # Feed Strategy 라이브 페이지 대조):
    # "제목 링크로 보이는 <h5><a>" 패턴이 실제 목록 아이템과 일치함을 확인.
    # 오탐(광고/추천 위젯 포함) 없이 정상 동작. (Feed Strategy는 실제 코드
    # 실행이 아니라 라이브 페이지 조회로 구조만 대조한 것 - 위 모듈 docstring
    # "검증 상태" 참고)
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

        # 확인 완료: 헤드라인 바로 앞 카테고리 링크 방식으로 정상 추출됨
        # (WATTAgNet 11건 실행 기준 2026-07-14 / Feed Strategy는 2026-07-15
        # 라이브 페이지 대조로 같은 패턴 확인 - 위 모듈 docstring 참고)
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