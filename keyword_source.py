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


def _fetch_csv_rows(csv_url: str) -> list[dict] | None:
    """
    구글 시트 게시 CSV URL을 가져와서 dict 리스트로 파싱한다.
    실패하면(네트워크 오류, 형식 이상 등) None을 반환 - 호출부가 fallback.
    """
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
            return None
        return rows
    except Exception as e:
        print(f"[keyword_source] 시트 읽기 실패: {type(e).__name__} - {e!r} - fallback 사용")
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
