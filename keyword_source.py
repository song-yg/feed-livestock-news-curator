"""
keyword_source.py
구글 시트에서 키워드 목록을 읽어오는 공용 모듈 (2026-07-17 신규).

배경: 담당자가 없을 때도 다른 사람이 키워드를 편하게 추가/수정할 수 있게
해달라는 요청. GitHub 직접 편집(git push)은 진입장벽이 있어서, 구글 시트를
"웹에 게시(CSV)"해두고 실행할 때마다 그 URL을 읽어오는 방식을 택함.

** 이 방식을 고른 이유 **
- 서비스 계정/OAuth 인증이 전혀 필요 없음 - 시트를 "파일 > 공유 > 웹에 게시"
  로 CSV 형식으로 게시하면 인증 없이 GET으로 읽을 수 있는 공개 URL이 생김
- 키워드 등록에 GitHub 계정조차 필요 없어짐 - 구글 시트 편집 권한만 있으면 됨
- 코드 배포 없이 실행할 때마다 최신 시트 내용을 읽으므로, 키워드 추가에
  git push/PR/배포 절차가 전혀 필요 없음

** 시트 형식 (한 시트, 아래 4개 컬럼 - 첫 행은 헤더) **

  keyword                  | lang | active | note
  --------------------------|------|--------|------------------------
  구제역                     | ko   | TRUE   |
  foot and mouth disease    | en   | TRUE   |
  HPAI                      | en   | FALSE  | GDELT가 너무 짧다고 거부함

  - keyword: 실제 검색어 (naver_collector는 "ko" 행을, gdelt_collector는
    "en" 행을 가져다 씀)
  - lang: "ko" 또는 "en" (대소문자 무관)
  - active: TRUE/FALSE - 사람이 행을 삭제하지 않고 켜고 끌 수 있게 함
    (실수로 지워서 이력이 사라지는 것보다, 꺼두고 note에 이유를 남기는
    쪽이 안전 - 9.4/9.5 섹션의 "보수적 기본값" 철학과 같은 결)
  - note: 자유 메모(왜 껐는지 등) - 코드는 안 읽음, 사람이 보는 용도

** 실패 시 fallback (9.4/9.5 원칙 재사용) **
KEYWORD_SHEET_CSV_URL 환경변수가 없거나, 네트워크 실패, 시트 형식이
깨졌거나, 특정 lang에 활성 키워드가 하나도 없는 경우 등 - 어떤 이유로든
정상적으로 못 읽으면 호출부가 넘겨준 fallback(각 collector에 원래 있던
하드코딩 리스트)으로 안전하게 대체한다. 즉 이 기능을 아예 설정 안 해도,
설정했다가 시트가 일시적으로 안 열려도 파이프라인 자체는 죽지 않는다.
"""

import csv
import io
import os

import requests

# 2026-07-23 추가: 프로세스 내 캐시. get_keywords가 "ko"/"en" 각각을 위해
# 독립적으로 호출되는데(naver_collector가 ko용, gdelt_collector가 en용),
# 기존엔 매번 완전히 같은 CSV를 처음부터 다시 요청+파싱했음(한 실행에 총
# 2번). 이 프로세스는 한 번 실행되고 끝나는 구조(매번 새 GitHub Actions
# 러너)라 "캐시가 오래돼서 stale해지는" 걱정 없이, 같은 실행 안에서만
# 재사용하면 충분함 - 실행 중간에 시트 내용이 바뀌는 경우까지 반영할
# 필요는 이 프로젝트 성격상 없다고 판단(주 1회 실행, 실행 도중 편집
# 반영을 요구하는 요구사항 없음).
_cache: dict[str, list[dict] | None] = {}


def _fetch_csv_rows(csv_url: str) -> list[dict] | None:
    """
    구글 시트 게시 CSV URL을 가져와서 dict 리스트로 파싱한다.
    실패하면(네트워크 오류, 형식 이상 등) None을 반환 - 호출부가 fallback.

    같은 csv_url에 대해 이 프로세스 안에서 이미 한 번 가져온 적 있으면,
    네트워크 요청/파싱을 다시 하지 않고 캐시된 결과를 그대로 돌려준다
    (성공이든 실패든 캐시함 - 실패까지 캐시하는 이유는, 실패했다는 걸
    두 번째 호출에서 다시 네트워크로 확인할 필요가 없기 때문. 어차피
    한 실행 안에서 네트워크 상태가 그새 좋아질 걸 기대하고 재시도할
    이유가 없음 - 재시도가 필요한 경우는 이미 gdelt_collector 등에서
    보는 것처럼 응답이 불안정한 API에나 해당하고, 이건 정적 파일 하나
    가져오는 것뿐이라 다름).
    """
    if csv_url in _cache:
        cached = _cache[csv_url]
        print(f"[keyword_source] 캐시된 CSV 재사용 (이번 실행에서 이미 가져온 적 있음)")
        return cached

    try:
        resp = requests.get(csv_url, timeout=15)
        resp.raise_for_status()
        # 구글 시트가 게시하는 CSV는 UTF-8 BOM이 붙어서 오는 경우가 많아
        # utf-8-sig로 디코딩해야 헤더 첫 컬럼명 앞에 BOM이 안 섞여 들어옴
        text = resp.content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            print("[keyword_source] 시트가 비어있음 - fallback 사용")
            _cache[csv_url] = None
            return None
        _cache[csv_url] = rows
        return rows
    except Exception as e:
        print(f"[keyword_source] 시트 읽기 실패: {type(e).__name__} - {e!r} - fallback 사용")
        _cache[csv_url] = None
        return None


def _is_active(row: dict) -> bool:
    return str(row.get("active", "")).strip().upper() == "TRUE"


def get_keywords(lang: str, fallback: list[str]) -> list[str]:
    """
    lang("ko" 또는 "en")에 해당하는 활성(active=TRUE) 키워드 리스트를 구글
    시트에서 읽어온다.

    KEYWORD_SHEET_CSV_URL 환경변수가 없거나 읽기/파싱에 실패하거나 해당
    lang의 활성 키워드가 하나도 없으면, fallback(호출부가 원래 갖고 있던
    하드코딩 리스트)을 그대로 반환한다 - 이 함수가 예외를 던지는 경우는 없음.
    """
    csv_url = os.environ.get("KEYWORD_SHEET_CSV_URL")
    if not csv_url:
        print(f"[keyword_source] KEYWORD_SHEET_CSV_URL 없음 - {lang} 기본(하드코딩) 키워드 리스트 사용: {fallback}")
        return fallback

    rows = _fetch_csv_rows(csv_url)
    if rows is None:
        return fallback

    keywords = [
        row["keyword"].strip()
        for row in rows
        if row.get("lang", "").strip().lower() == lang
        and _is_active(row)
        and row.get("keyword", "").strip()
    ]

    if not keywords:
        print(f"[keyword_source] 구글 시트에 lang={lang} 활성 키워드가 하나도 없음 - fallback 사용")
        return fallback

    print(f"[keyword_source] 구글 시트에서 {lang} 키워드 {len(keywords)}개 로드: {keywords}")
    return keywords


if __name__ == "__main__":
    # 자체 점검용 - 환경변수 없이 fallback 경로, mock CSV로 정상 경로 둘 다 확인
    fallback_ko = ["조류독감", "구제역"]
    result = get_keywords("ko", fallback_ko)
    assert result == fallback_ko
    print("[검증1] KEYWORD_SHEET_CSV_URL 없음 -> fallback 정상 반환")

    os.environ["KEYWORD_SHEET_CSV_URL"] = "https://example.com/fake.csv"
    fake_csv = (
        "keyword,lang,active,note\n"
        "구제역,ko,TRUE,\n"
        "조류독감,ko,FALSE,휴지기라 잠시 끔\n"
        "feed price,en,TRUE,\n"
    )

    class _FakeResp:
        status_code = 200
        content = fake_csv.encode("utf-8-sig")
        def raise_for_status(self):
            pass

    requests.get = lambda *a, **kw: _FakeResp()

    result_ko = get_keywords("ko", ["fallback"])
    result_en = get_keywords("en", ["fallback"])
    print("[검증2] ko:", result_ko, "/ en:", result_en)
    assert result_ko == ["구제역"]  # 조류독감은 active=FALSE라 제외돼야 함
    assert result_en == ["feed price"]

    print("\n[keyword_source] 자체 점검 통과 (fallback 경로 + 정상 파싱 경로)")