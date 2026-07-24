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
    # 2026-07-25 영구 제외 확정(2026-07-22엔 "Cloudflare 캐시 고정"으로
    # 오진단했었음 - 정정): 담당자가 실제 Playwright로 재현 테스트한 결과,
    # 원인은 캐시가 아니라 **Cloudflare의 봇 차단(사람 확인) 챌린지 화면**
    # 이었음을 스크린샷으로 확인함("Performing security verification" +
    # "Verify you are human" 체크박스, Ray ID 포함). 자동화 접근을
    # 사이트가 명시적으로 막고 있는 것이라, 이걸 우회하는 시도는 하지
    # 않기로 함(9.x 원칙과 별개로, 이런 보안장치 우회는 이 프로젝트가
    # 하지 않을 일). 그동안 실행마다 WATTAgNet 건수가 0건/10건/360건으로
    # 들쭉날쭉했던 것도 봇 차단이 걸릴 때/안 걸릴 때가 있었던 것으로
    # 설명됨. RSS 피드 등 대안도 검색해봤으나 공식적으로 유지되는 피드를
    # 못 찾음. 같은 회사(WATT Global Media)의 Feed Strategy는 이 문제가
    # 없어 그대로 유지.
    # "WATTAgNet": "https://www.wattagnet.com",
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
MAX_PAGES = 30  # 이 이상이면 문제가 있음... (정상 사이트에 적용되는 기본 상한)

# 2026-07-22 추가, 2026-07-25 원인 정정: 사이트별로 페이지네이션 가능
# 여부를 다르게 둔다. WATTAgNet은 애초에 Cloudflare 봇 차단으로 접근
# 자체가 막혀 있어서(위 SITES 주석 참고, 영구 제외 확정) 페이지네이션
# 여부 자체가 무의미해졌지만, 혹시 나중에 실수로 SITES에 다시 넣더라도
# 최소한의 안전장치가 유지되도록 이 목록은 그대로 둔다(1페이지만
# 시도하다 봇 차단으로 실패하면 그 실행만 0건으로 끝나고, 여러 페이지에
# 걸쳐 반복 시도하지는 않게). Feed Strategy는 이 문제가 없는 것으로
# 확인됐으므로(cf-cache-status는 HIT이지만 age가 정상 갱신되고 페이지마다
# 다른 콘텐츠 반환 - 다만 실제로 2페이지까지 간 사례는 아직 없어 완전
# 검증은 아님) 기본값(MAX_PAGES까지 정상 페이지네이션)을 그대로 적용한다.
SINGLE_PAGE_ONLY_SITES = {"WATTAgNet"}

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
# 2026-07-22: Cache-Control/Pragma: no-cache 헤더를 캐시 우회 목적으로
# 추가했었으나, (1) Cloudflare가 이 헤더를 무시하는 것으로 실측 확인됐고
# (cf-cache-status: HIT가 헤더 추가 후에도 그대로 나옴), (2) 전략 자체를
# "캐시된 1페이지를 그대로 받아들이기"로 바꿔서(위 _collect_site docstring
# 참고) 더 이상 캐시를 우회할 필요도 없어짐 - 목적을 잃은 죽은 코드라 제거.

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
        # 2026-07-23 안정성 보완: 기존엔 browser.close()가 for 루프 뒤에
        # 순서대로만 있어서, browser.new_page()나 루프 안에서 (try/except로
        # 못 잡는) 예상 밖 예외가 나면 close()가 스킵될 수 있었음(발생
        # 가능성은 낮지만, 브라우저 프로세스가 안 정리된 채 남을 위험).
        # try/finally로 감싸서 무슨 일이 있어도 close()가 실행되도록 함.
        try:
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
        finally:
            browser.close()

    return all_results


def _collect_site(page, source_name: str, base_url: str) -> list[dict]:
    """
    2026-07-22: 사이트별로 페이지네이션 가능 여부가 다름을 확인 - WATTAgNet은
    (2026-07-25 정정: 원인은 캐시가 아니라 Cloudflare 봇 차단, `SINGLE_PAGE_ONLY_
    SITES` 주석 참고) 1페이지만 수집하고, 나머지 사이트(Feed Strategy 등)는
    정상적으로 여러 페이지를 순회한다.
    """
    if source_name in SINGLE_PAGE_ONLY_SITES:
        return _collect_single_page(page, source_name, base_url)
    return _collect_paginated(page, source_name, base_url)


def _collect_single_page(page, source_name: str, base_url: str) -> list[dict]:
    """
    페이지네이션이 무의미한 사이트(`SINGLE_PAGE_ONLY_SITES`)용 - 1페이지
    (캐시된 최신 스냅샷, 최대 12개)만 수집한다.

    담당자 결정(2026-07-22): 캐시가 주기적으로 갱신되므로(실측 `age` 기준
    대략 40분 전후) 1페이지 자체는 그럭저럭 최신이고, 안 뚫리는 2페이지
    이상을 억지로 시도하다 가짜로 부풀려진 데이터(360건 사고)를 만드는
    것보다 안전하다는 판단. 7일 안에 12건보다 많은 기사가 나온 날엔 일부
    누락될 수 있다는 트레이드오프는 감수하기로 함.
    """
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
            # 상세 페이지 파싱 실패 - 이 기사만 건너뜀 (전체 중단 아님)
            continue

        if not _is_recent(detail["published_at"], DAYS_BACK):
            # 1페이지(캐시된 최신 12개) 안에서도 7일 지난 기사는 제외한다.
            # 페이지네이션이 없으니 "여기서 종료"할 다음 페이지 자체가 없어,
            # 그냥 이 기사만 걸러내고 나머지 항목은 계속 확인한다(예전처럼
            # break해서 전체를 끝내지 않음 - 1페이지 안에서 순서가 뒤섞여
            # 있을 가능성에도 안전하도록).
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


def _collect_paginated(page, source_name: str, base_url: str) -> list[dict]:
    """
    정상적으로 페이지네이션이 되는 사이트용 - 원래 로직(2026-07-14 최초 구현)
    그대로. 최신순 정렬이 보장된다는 전제 하에, 7일 지난 기사를 만나는 즉시
    그 사이트 수집을 끝낸다(`hit_cutoff`/`break`) - 이래야 이미 다 지나간
    과거 페이지까지 불필요하게 순회하지 않는다.
    """
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


# 뉴스 기사가 아니라 스폰서 콘텐츠(네이티브 광고)라 제외.
# (예: /brand-insights/... 경로, 카테고리가 회사명으로 찍혀있고
#  article:published_time 메타 태그도 없어서 상세페이지에서 타임아웃 남)
EXCLUDED_PATH_PATTERNS = ("/brand-insights/",)


def _fetch_listing_page(page, url: str) -> list[dict]:
    # 2026-07-23 안정성 보완: 기존엔 page.goto()가 try/except 밖에 있어서,
    # 네비게이션 자체가 실패하면(타임아웃, DNS 오류, 연결 끊김 등) 예외가
    # 그대로 위(collect())까지 전파돼 그 사이트에서 이미 모은 결과까지
    # 통째로 버려지는 구조였음(naver_collector.py의 같은 유형 문제와 동일 -
    # 담당자 지적으로 발견). wait_for_selector 실패와 똑같은 패턴(빈 리스트
    # 반환 + 로그)으로 통일해서, 목록 페이지 자체를 못 읽어도 그 사이트
    # 수집이 "이번엔 0건"으로 안전하게 끝나도록 함.
    try:
        response = page.goto(url, timeout=30000, wait_until="networkidle")
    except Exception as e:
        print(f"[watt] 목록 페이지 이동 실패: {url} - {type(e).__name__}: {e}")
        return []

    # 2026-07-22 추가: 같은 사이트에서 실행마다 콘텐츠가 요동치는 원인이
    # (a) CDN/캐시 계층이 오래된 스냅샷을 주는 것인지, (b) 안티봇 시스템이
    # 자동화 트래픽을 감지해 의도적으로 다른(오래된/제한된) 콘텐츠를 주는
    # 것인지 구분이 안 돼서(담당자 지적), 응답 헤더 중 캐시/서버 식별에
    # 쓰이는 것들을 그대로 로그에 남긴다.
    # - cf-cache-status/x-cache/age가 있고 HIT면 -> 캐시 문제 쪽에 무게
    # - 이런 헤더가 없거나 MISS인데도 내용이 반복되면 -> 캐시가 아니라
    #   서버가 매 요청마다 새로 렌더링하면서 의도적으로 다른 콘텐츠를 주는
    #   것(안티봇 대응 등)일 가능성이 커짐
    if response is not None:
        headers = response.headers
        interesting_keys = ("cf-cache-status", "x-cache", "age", "cache-control", "server", "cf-ray", "vary")
        found = {k: headers[k] for k in interesting_keys if k in headers}
        print(f"[watt] {url} - 응답 헤더(캐시/서버 관련): "
              f"{found if found else '(해당 헤더 없음)'}")

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

    # 2026-07-22 추가, 2026-07-25 원인 규명 완료: 실행마다 WATTAgNet 건수가
    # 크게 요동쳤던 것(0건/10건/360건 실측 확인 - 담당자 지적)은 Cloudflare
    # 봇 차단이 걸릴 때/안 걸릴 때가 있었기 때문으로 확인됨(SITES 주석 참고,
    # WATTAgNet 영구 제외 확정). 이 로그 자체는 다른 사이트 진단에도 계속
    # 유용해서 그대로 남겨둔다.
    print(f"[watt] {url} - 목록에서 {len(results)}개 항목 읽음")
    return results


def _fetch_detail(page, url: str) -> dict | None:
    # 2026-07-23 안정성 보완: page.goto()가 기존엔 try/except 밖에 있어서,
    # 이 기사 하나의 네비게이션이 실패해도(타임아웃 등) 예외가 그대로
    # 호출부(_collect_single_page/_collect_paginated의 for 루프)까지
    # 전파돼 그때까지 모은 다른 기사 결과까지 통째로 버려지는 구조였음.
    # 아래 wait_for_selector 실패와 같은 패턴(None 반환 - "이 기사만
    # 건너뜀")으로 통일.
    try:
        page.goto(url, timeout=30000)
    except Exception as e:
        print(f"[watt] 상세 페이지 이동 실패: {url} - {type(e).__name__}: {e}")
        return None

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