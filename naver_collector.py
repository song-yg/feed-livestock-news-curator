"""
naver_collector.py
네이버 뉴스 검색 API를 호출해서 뉴스를 수집하는 모듈.
(알고리즘 문서 "1. 수집 레이어" - naver_collector 스펙 참조)
"""

import re
import html
import os
import time
import requests
from urllib.parse import urlparse
from datetime import datetime, timedelta
from dotenv import load_dotenv

import keyword_source

# 실행 시점에 .env 파일을 찾아서 그 안의 값들을 환경변수로 등록해준다.
# .env 파일이 없어도 에러 없이 그냥 넘어감 (예: GitHub Actions에서는
# .env 없이 Secrets가 이미 환경변수로 주입돼 있으므로 이 줄은 그냥 무시됨)
load_dotenv()

NAVER_API_URL = "https://openapi.naver.com/v1/search/news.json"

# 이 프로젝트는 주 1회 실행이므로, 최근 7일 이내 기사만 남긴다.
DAYS_BACK = 7

# 예시 키워드(fallback). 2026-07-17부터 구글 시트(KEYWORD_SHEET_CSV_URL)가
# 설정돼 있으면 그쪽을 우선 쓰고, 없거나 읽기 실패하면 이 리스트로 대체된다
# (keyword_source.py 참고 - 담당자 없이도 시트 편집만으로 키워드 추가 가능).
KEYWORDS = ["조류독감", "구제역", "사료 가격", "축산물 수급"]


def _is_recent(published_at: str, days: int) -> bool:
    """
    published_at(ISO 8601 문자열)이 오늘 기준 최근 N일 이내인지 확인한다.

    예: DAYS_BACK=7이면, 오늘이 7/13일 때 7/6일 이전 기사는 False가 됨.
    """
    pub_dt = datetime.fromisoformat(published_at)
    # pub_dt에 이미 +09:00 같은 시간대 정보가 들어있으므로,
    # 비교 기준(now)도 같은 시간대로 맞춰줘야 에러 없이 비교 가능하다.
    cutoff = datetime.now(pub_dt.tzinfo) - timedelta(days=days)
    return pub_dt >= cutoff


# 네이버 API가 허용하는 start의 최댓값 (그 이상은 요청해도 에러남)
MAX_START = 1000


def collect() -> list[dict]:
    """
    KEYWORDS 리스트를 순서대로 돌면서 네이버 뉴스를 전부 수집한다.
    이 함수가 naver_collector의 '진입점'(다른 모듈에서 이걸 호출해서 씀).
    """
    # API 키는 코드에 절대 직접 안 적는다. 환경변수로 읽어온다.
    # 이유: 이 코드는 GitHub 리포에 올라가는데, 키를 코드에 박으면
    #       리포를 보는 모든 사람(공개 리포면 전 세계)이 키를 볼 수 있게 됨.
    #       GitHub Actions에서 돌릴 때는 "Secrets"라는 별도 저장소에
    #       키를 등록해두고, 실행 시점에만 환경변수로 주입받는다.
    client_id = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]

    # 2026-07-17 추가: 구글 시트에 등록된 활성 키워드를 우선 사용하고,
    # 시트 미설정/읽기 실패 시 위 KEYWORDS(하드코딩)로 안전하게 대체
    # (keyword_source.py 참고 - 이 함수는 예외를 던지지 않음)
    target_keywords = keyword_source.get_keywords("ko", KEYWORDS)

    all_results = []
    # 2026-07-23 추가: 세션 재사용. 기존엔 search_naver_news 호출마다
    # requests.get()이 매번 새 연결(TCP+TLS 핸드셰이크)을 맺었는데, 한 번
    # 실행에 키워드마다 여러 페이지를 반복 호출하는 구조라(최대 10페이지 x
    # 키워드 수) 세션을 하나만 만들어 재사용하면 커넥션이 유지돼 반복
    # 호출의 오버헤드가 줄어든다. keyword_source.get_keywords()는 세션과
    # 무관한 별도의 단발성 호출이라 그대로 둠.
    with requests.Session() as session:
        for keyword in target_keywords:
            # 2026-07-23 수정: 기존엔 all_results.extend(keyword_results)가
            # try 블록 "안", while 루프보다 뒤에 있어서, 페이지네이션 도중
            # (예: 3페이지째에서 타임아웃) 예외가 나면 그 줄 자체가 실행이
            # 안 돼 이미 모은 1~2페이지 결과까지 통째로 버려지는 문제가
            # 있었음(담당자 지적으로 발견). try 범위를 "네트워크 호출이
            # 실제로 일어나는 부분"으로 좁히고, extend는 항상(성공이든
            # 부분 실패든) 실행되도록 밖으로 뺐다.
            keyword_results = []
            start = 1
            try:
                while start <= MAX_START:
                    page = search_naver_news(keyword, client_id, client_secret, start=start, session=session)

                    if not page:
                        # 더 가져올 결과 자체가 없음 (검색어에 대한 기사가 소진됨)
                        break

                    recent_in_page = [r for r in page if _is_recent(r["published_at"], DAYS_BACK)]
                    keyword_results.extend(recent_in_page)

                    # 이 페이지의 마지막 항목 = 이 페이지 안에서 가장 오래된 기사
                    # (sort=date로 최신순 정렬돼 있으므로 항상 마지막이 제일 오래됨)
                    oldest_in_page = page[-1]
                    if not _is_recent(oldest_in_page["published_at"], DAYS_BACK):
                        # 이 페이지 끝에서 이미 기간을 벗어났다 -> 다음 페이지는
                        # 이보다 더 오래된 기사만 있을 게 뻔하므로 더 안 가져와도 됨
                        break

                    if len(page) < 100:
                        # 네이버가 100건 미만을 줬다는 건 더 이상 결과가 없다는 뜻
                        break

                    start += 100
                    time.sleep(0.2)  # 페이지 사이에도 간격을 둠
            except requests.exceptions.RequestException as e:
                # 이 키워드의 나머지 페이지는 못 가져왔지만, 지금까지 모은
                # keyword_results는 아래에서 그대로 살려서 반영한다 - 페이지
                # 하나 실패했다고 이미 확보한 데이터까지 버릴 이유는 없음.
                print(f"[naver] '{keyword}' 수집 중 오류 발생(지금까지 모은 "
                      f"{len(keyword_results)}건은 보존하고 다음 키워드로 진행): {e}")

            all_results.extend(keyword_results)
            print(f"[naver] '{keyword}' -> 최근 {DAYS_BACK}일 이내 {len(keyword_results)}건 "
                  f"({start if start <= MAX_START else MAX_START}건째까지 확인)")

            time.sleep(0.2)  # 키워드 사이에도 간격을 둬서 짧은 시간에 몰아치지 않도록 함

    return all_results


def search_naver_news(keyword: str, client_id: str, client_secret: str, start: int = 1,
                       session: requests.Session | None = None) -> list[dict]:
    """
    네이버 뉴스 검색 API를 호출해서, 결과를 우리 공통 스키마 형태로 정리해 돌려준다.

    keyword       : 검색어 (예: "조류독감")
    client_id     : 네이버 개발자센터에서 발급받은 애플리케이션 ID
    client_secret : 위와 함께 발급받은 비밀키
    start         : 몇 번째 결과부터 가져올지 (1, 101, 201 ... 페이지네이션용)
    session       : 재사용할 requests.Session (2026-07-23 추가). 안 넘기면
                    (예: 이 함수를 단독으로 테스트할 때) requests 모듈
                    자체를 그대로 써서 매번 새 연결을 맺는 예전 방식으로
                    안전하게 동작함 - 세션 재사용은 순전히 성능 최적화라
                    없어도 기능은 동일함.
    """
    # 1) 헤더: "나 누구인지" 증명하는 정보. API 키가 여기 들어간다.
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }

    # 2) 파라미터: "무엇을 달라"는 요청 내용
    params = {
        "query": keyword,
        "display": 100,   # 한 번에 최대 100건까지 요청 가능 (API 하드 리밋)
        "start": start,
        "sort": "date",   # 최신순 정렬 (기본값 sim=정확도순 대신 date 사용)
    }

    # 3) 실제 GET 요청. timeout은 응답이 안 올 때 무한정 기다리지 않도록 하는 안전장치
    requester = session if session is not None else requests
    response = requester.get(NAVER_API_URL, headers=headers, params=params, timeout=10)
    response.raise_for_status()  # 200번대가 아니면(401, 429, 500 등) 여기서 에러를 던짐

    data = response.json()  # 응답 body(JSON 문자열)를 파이썬 딕셔너리로 변환

    results = []
    for item in data.get("items", []):
        results.append({
            "source": "네이버",
            "title": _strip_html_tags(item["title"]),
            "url": item["originallink"] or item["link"],
            "published_at": _parse_pub_date(item["pubDate"]),
            "category": None,   # 카테고리는 정제(normalizer) 단계에서 채움
            "body": None,       # 네이버는 본문을 안 줌
            "description": _strip_html_tags(item["description"]),
            "press": _extract_press(item["originallink"]),
        })
    return results


def _parse_pub_date(pub_date_str: str) -> str:
    """
    네이버 API가 주는 pubDate는 이런 형식이다:
        "Mon, 13 Jul 2026 09:00:00 +0900"
    (RFC 822 형식, 이메일/RSS에서 흔히 쓰는 날짜 표기법)

    이걸 우리 시스템 공통 스키마에서 쓰기로 한 ISO 8601 형식으로 바꾼다:
        "2026-07-13T09:00:00+09:00"
    """
    dt = datetime.strptime(pub_date_str, "%a, %d %b %Y %H:%M:%S %z")
    return dt.isoformat()


def _extract_press(originallink: str) -> str:
    """
    네이버 API 응답에는 언론사명 필드가 따로 없다.
    originallink(원문 URL)의 도메인을 언론사 식별자로 대신 사용한다.

    예: "https://www.yna.co.kr/view/AKR2026..." -> "yna.co.kr"

    2026-07-15 코드 리뷰 반영:
    - www. 제거를 .replace("www.", "") 대신 startswith 체크 후 슬라이싱으로
      변경함 - .replace()는 문자열 어디에 있든 매칭돼서, 만약 서브도메인
      이름 중간에 우연히 "www."가 들어간 경우(드물지만 가능) 의도치 않게
      지워질 수 있었음. startswith는 맨 앞에 있을 때만 제거하므로 더 안전.
    - 서브도메인 통합(예: biz.yna.co.kr / www.yna.co.kr을 같은 언론사로 인식)은
      검토했으나 보류함 - 정확히 하려면 한국 도메인 특유의 복합 접미사(.co.kr,
      .or.kr, .go.kr 등)를 다뤄야 해서 간단한 규칙으로는 안 되고, tldextract
      같은 라이브러리를 새로 추가해야 함. 실제 수집 데이터에서 이게 scorer.py
      PRESS_DEDUP_CAP(동일 언론사 도배 dedup) 정확도를 왜곡하는 사례가 아직
      확인된 바 없어(이론적 가능성만 있음), 이 프로젝트의 기존 철학(확인된
      패턴만 대응, 일반화 규칙은 미리 안 만듦 - gdelt_collector의
      FALSE_POSITIVE_FILTERS, keyword_tagger의 EXCLUDED_TERMS와 동일 원칙)대로
      실측으로 문제가 확인되면 그때 대응하기로 결정.
    """
    if not originallink:
        return ""
    domain = urlparse(originallink).netloc
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _strip_html_tags(text: str) -> str:
    """
    네이버 API 응답의 title/description에는 검색어 강조를 위해
    <b>태그</b>가 섞여서 온다. 이걸 제거하고 순수 텍스트만 남긴다.

    예: "고병원성 <b>조류독감</b> 국내 첫 발생" -> "고병원성 조류독감 국내 첫 발생"
    """
    # 1) <...> 형태의 태그를 전부 빈 문자열로 치환
    text = re.sub(r"<[^>]+>", "", text)
    # 2) &amp; , &quot; 같은 HTML 엔티티도 원래 문자로 복원 (& , " 등)
    text = html.unescape(text)
    return text


if __name__ == "__main__":
    # 터미널에서 python naver_collector.py 로 직접 실행했을 때만 동작.
    # 다른 파일에서 import naver_collector 로 불러쓸 땐 이 블록은 안 돌아감.
    results = collect()
    print(f"\n총 {len(results)}건 수집 완료")
    for r in results[:3]:
        print(r)