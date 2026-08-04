"""
naver_collector.py
네이버 뉴스 검색 API 수집 모듈(수집 레이어).
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

load_dotenv()

NAVER_API_URL = "https://openapi.naver.com/v1/search/news.json"
DAYS_BACK = 7  # 최근 N일 이내 기사만

# fallback 키워드. 구글 시트(KEYWORD_SHEET_CSV_URL) 우선, 실패 시 이 리스트 사용.
# 시트 변경 시 수동 동기화 필요(자동 아님).
KEYWORDS = ["조류독감", "사료가격", "방역대", "가축 이동제한", "양돈",
            "사료공장", "사료첨가제", "축산물 할당관세", "축산업 계열화", "스마트축사"]


def _is_recent(published_at: str, days: int) -> bool:
    """published_at이 오늘 기준 최근 N일 이내인지 확인."""
    pub_dt = datetime.fromisoformat(published_at)
    cutoff = datetime.now(pub_dt.tzinfo) - timedelta(days=days)
    return pub_dt >= cutoff


MAX_START = 1000  # 네이버 API가 허용하는 start 최댓값


def _phrase_present(article: dict, keyword: str) -> bool:
    """
    네이버 API는 공백 구분 검색어를 단어별 AND로 처리해 오탐 가능(예: "축산물 수급"
    -> 무관한 기사에 "축산물"/"수급"이 각각 다른 맥락으로 등장해도 매칭). 여러
    단어 키워드는 문구가 실제로 인접해 등장하는지 재확인. 단어 1개 키워드는 항상 통과.
    """
    if " " not in keyword.strip():
        return True
    combined = f"{article.get('title', '')} {article.get('description', '')}"
    combined_normalized = " ".join(combined.split())
    keyword_normalized = " ".join(keyword.split())
    return keyword_normalized in combined_normalized


def collect() -> list[dict]:
    """KEYWORDS(또는 시트 활성 키워드)를 돌며 네이버 뉴스 전체 수집. 진입점."""
    client_id = os.environ["NAVER_CLIENT_ID"]
    client_secret = os.environ["NAVER_CLIENT_SECRET"]

    target_keywords = keyword_source.get_keywords("ko", KEYWORDS)

    all_results = []
    with requests.Session() as session:
        for keyword in target_keywords:
            keyword_results = []
            start = 1
            try:
                while start <= MAX_START:
                    page = search_naver_news(keyword, client_id, client_secret, start=start, session=session)

                    if not page:
                        break

                    recent_in_page = [r for r in page if _is_recent(r["published_at"], DAYS_BACK)]
                    phrase_ok = [r for r in recent_in_page if _phrase_present(r, keyword)]
                    filtered_out = len(recent_in_page) - len(phrase_ok)
                    if filtered_out:
                        print(f"[naver] '{keyword}' - 문구 인접성 필터로 {filtered_out}건 제외"
                              f"(AND 매칭 오탐 방지)")
                    keyword_results.extend(phrase_ok)

                    oldest_in_page = page[-1]
                    if not _is_recent(oldest_in_page["published_at"], DAYS_BACK):
                        break

                    if len(page) < 100:
                        break

                    start += 100
                    time.sleep(0.2)
            except requests.exceptions.RequestException as e:
                print(f"[naver] 🔴 조치필요 [NV-01] - '{keyword}' 수집 중 오류 발생(지금까지 모은 "
                      f"{len(keyword_results)}건은 보존하고 다음 키워드로 진행): {type(e).__name__} - {e!r}")

            all_results.extend(keyword_results)
            print(f"[naver] '{keyword}' -> 최근 {DAYS_BACK}일 이내 {len(keyword_results)}건 "
                  f"({start if start <= MAX_START else MAX_START}건째까지 확인)")

            time.sleep(0.2)

    return all_results


def search_naver_news(keyword: str, client_id: str, client_secret: str, start: int = 1,
                       session: requests.Session | None = None) -> list[dict]:
    """네이버 뉴스 검색 API 호출 -> 공통 스키마로 변환해 반환."""
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }

    params = {
        "query": keyword,
        "display": 100,
        "start": start,
        "sort": "date",
    }

    requester = session if session is not None else requests
    response = requester.get(NAVER_API_URL, headers=headers, params=params, timeout=10)
    response.raise_for_status()

    data = response.json()

    results = []
    for item in data.get("items", []):
        results.append({
            "source": "네이버",
            "title": _strip_html_tags(item["title"]),
            "url": item["originallink"] or item["link"],
            "published_at": _parse_pub_date(item["pubDate"]),
            "category": None,
            "body": None,
            "description": _strip_html_tags(item["description"]),
            "press": _extract_press(item["originallink"]),
        })
    return results


def _parse_pub_date(pub_date_str: str) -> str:
    """RFC 822 형식("Mon, 13 Jul 2026 09:00:00 +0900") -> ISO 8601 변환."""
    dt = datetime.strptime(pub_date_str, "%a, %d %b %Y %H:%M:%S %z")
    return dt.isoformat()


def _extract_press(originallink: str) -> str:
    """originallink 도메인을 언론사 식별자로 사용. 서브도메인 통합은 안 함."""
    if not originallink:
        return ""
    domain = urlparse(originallink).netloc
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _strip_html_tags(text: str) -> str:
    """title/description의 <b> 강조 태그 제거 + HTML 엔티티 복원."""
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return text


if __name__ == "__main__":
    results = collect()
    print(f"\n총 {len(results)}건 수집 완료")
    for r in results[:3]:
        print(r)
