"""
keyword_source.py
구글 시트("웹에 게시 -> CSV")에서 키워드 목록을 읽어오는 공용 모듈.

시트 컬럼: keyword / lang(ko,en) / active(TRUE,FALSE) / note(자유 메모).
KEYWORD_SHEET_CSV_URL 미설정, 네트워크 실패, 형식 이상, 활성 키워드 0건 등
어떤 이유로든 못 읽으면 호출부가 넘긴 fallback으로 안전하게 대체.
"""

import csv
import io
import os

import requests

# 프로세스 내 캐시(성공/실패 둘 다) - 같은 실행 안에서 ko/en 두 번 호출돼도 재요청 안 함.
_cache: dict[str, list[dict] | None] = {}


def _fetch_csv_rows(csv_url: str) -> list[dict] | None:
    """CSV URL을 가져와 dict 리스트로 파싱. 실패 시 None(호출부 fallback). 프로세스 내 캐시."""
    if csv_url in _cache:
        cached = _cache[csv_url]
        print(f"[keyword_source] 캐시된 CSV 재사용 (이번 실행에서 이미 가져온 적 있음)")
        return cached

    try:
        resp = requests.get(csv_url, timeout=30)
        resp.raise_for_status()
        text = resp.content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            print("[keyword_source] 🔴 조치필요 [KS-01] - 시트가 비어있음(CSV 대신 엉뚱한 내용이 왔을 가능성) - fallback 사용")
            _cache[csv_url] = None
            return None
        _cache[csv_url] = rows
        return rows
    except Exception as e:
        print(f"[keyword_source] 🔴 조치필요 [KS-02] - 시트 읽기 실패: {type(e).__name__} - {e!r} - fallback 사용")
        _cache[csv_url] = None
        return None


def _is_active(row: dict) -> bool:
    return str(row.get("active", "")).strip().upper() == "TRUE"


def _detect_keyword_lang(keyword: str) -> str:
    """키워드 실제 글자 구성(한글 비율)으로 언어 판별. 시트 lang 컬럼 오입력 보정용."""
    if not keyword:
        return "en"
    hangul_count = sum(1 for ch in keyword if "\uac00" <= ch <= "\ud7a3")
    return "ko" if (hangul_count / len(keyword)) >= 0.2 else "en"


def _is_valid_keyword(keyword: str) -> bool:
    """문자/숫자를 하나라도 포함하는지 확인(특수문자·공백뿐인 키워드 제외)."""
    return any(ch.isalnum() for ch in keyword)


def get_keywords(lang: str, fallback: list[str]) -> list[str]:
    """
    lang("ko"/"en")의 활성 키워드를 시트에서 읽음. 시트 lang 컬럼 대신
    _detect_keyword_lang으로 재판별해 사용(불일치 시 로그). 못 읽으면
    fallback 반환 - 예외를 던지지 않음.
    """
    csv_url = os.environ.get("KEYWORD_SHEET_CSV_URL")
    if not csv_url:
        print(f"[keyword_source] 🔴 조치필요 [KS-03] - KEYWORD_SHEET_CSV_URL 없음 - {lang} 기본(하드코딩) 키워드 리스트 사용: {fallback}")
        return fallback

    rows = _fetch_csv_rows(csv_url)
    if rows is None:
        return fallback

    keywords = []
    mismatches = []
    invalid_keywords = []
    for row in rows:
        keyword = row.get("keyword", "").strip()
        declared_lang = row.get("lang", "").strip().lower()
        if not keyword or declared_lang not in ("ko", "en") or not _is_active(row):
            continue
        if not _is_valid_keyword(keyword):
            invalid_keywords.append(keyword)
            continue
        actual_lang = _detect_keyword_lang(keyword)
        if actual_lang != declared_lang:
            mismatches.append((keyword, declared_lang, actual_lang))
        if actual_lang == lang:
            keywords.append(keyword)

    if invalid_keywords:
        print(f"[keyword_source] 🟡 주의 [KS-04] - 시트에 문자/숫자가 하나도 없는(특수문자·공백뿐인) "
              f"키워드 {len(invalid_keywords)}건 제외: {invalid_keywords!r}")

    if mismatches:
        detail = ", ".join(f"'{kw}'(시트={declared} -> 실제={actual})" for kw, declared, actual in mismatches)
        print(f"[keyword_source] 🟡 주의 [KS-05] - 시트 lang 컬럼과 실제 키워드 언어가 다른 항목 {len(mismatches)}건 "
              f"발견 - 실제 언어 기준으로 자동 보정해서 사용(시트도 고쳐두는 걸 권장): {detail}")

    # 중복 제거(공백/대소문자 정규화 후 비교, 첫 등장 표기 유지)
    seen_normalized = set()
    deduped_keywords = []
    duplicates = []
    for kw in keywords:
        normalized = " ".join(kw.split()).lower()
        if normalized in seen_normalized:
            duplicates.append(kw)
            continue
        seen_normalized.add(normalized)
        deduped_keywords.append(kw)
    keywords = deduped_keywords

    if duplicates:
        print(f"[keyword_source] 🟡 주의 [KS-06] - 시트에 중복 등록된 {lang} 키워드 {len(duplicates)}건 제외(첫 등장만 유지): "
              f"{duplicates}")

    if not keywords:
        print(f"[keyword_source] 🔴 조치필요 [KS-07] - 구글 시트에 lang={lang} 활성 키워드가 하나도 없음"
              f"(CSV 대신 엉뚱한 내용이 왔거나, 시트에서 실수로 전부 비활성화했을 수 있음) - fallback 사용")
        return fallback

    print(f"[keyword_source] 구글 시트에서 {lang} 키워드 {len(keywords)}개 로드: {keywords}")
    return keywords


if __name__ == "__main__":
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
    assert result_ko == ["구제역"]
    assert result_en == ["feed price"]

    fake_csv_swapped = (
        "keyword,lang,active,note\n"
        "smart farming,ko,TRUE,\n"
        "사료공장,en,TRUE,\n"
    )
    _FakeResp.content = fake_csv_swapped.encode("utf-8-sig")
    _cache.clear()
    result_ko_swapped = get_keywords("ko", ["fallback"])
    result_en_swapped = get_keywords("en", ["fallback"])
    print("[검증3] lang 뒤바뀜 자동 보정 - ko:", result_ko_swapped, "/ en:", result_en_swapped)
    assert result_ko_swapped == ["사료공장"], "한글 키워드는 시트의 en 표기와 무관하게 ko로 가야 함"
    assert result_en_swapped == ["smart farming"], "영어 키워드는 시트의 ko 표기와 무관하게 en으로 가야 함"

    fake_csv_dup = (
        "keyword,lang,active,note\n"
        "구제역,ko,TRUE,\n"
        "구제역,ko,TRUE,\n"
        "조류  독감,ko,TRUE,\n"
        "조류 독감,ko,TRUE,\n"
        "feed price,en,TRUE,\n"
        "Feed  Price,en,TRUE,\n"
    )
    _FakeResp.content = fake_csv_dup.encode("utf-8-sig")
    _cache.clear()
    result_ko_dup = get_keywords("ko", ["fallback"])
    result_en_dup = get_keywords("en", ["fallback"])
    print("[검증4] 중복 제거 - ko:", result_ko_dup, "/ en:", result_en_dup)
    assert result_ko_dup == ["구제역", "조류  독감"], "중복 제거 후 첫 등장 표기만 남아야 함"
    assert result_en_dup == ["feed price"], "대소문자/공백만 다른 중복도 제거돼야 함"

    fake_csv_invalid = (
        "keyword,lang,active,note\n"
        ";,ko,TRUE,\n"
        "   ,en,TRUE,\n"
        "---,en,TRUE,\n"
        "smart farming,en,TRUE,\n"
    )
    _FakeResp.content = fake_csv_invalid.encode("utf-8-sig")
    _cache.clear()
    result_ko_invalid = get_keywords("ko", ["fallback"])
    result_en_invalid = get_keywords("en", ["fallback"])
    print("[검증5] 특수문자/공백뿐인 키워드 제외 - ko:", result_ko_invalid, "/ en:", result_en_invalid)
    assert result_ko_invalid == ["fallback"], "유효한 ko 키워드가 하나도 없으면 fallback을 써야 함"
    assert result_en_invalid == ["smart farming"], "특수문자/공백뿐인 항목은 빠지고 정상 키워드만 남아야 함"

    print("\n[keyword_source] 자체 점검 통과 (fallback 경로 + 정상 파싱 경로 + lang 뒤바뀜 자동 보정 "
          "+ 중복 제거 + 특수문자/공백뿐인 키워드 제외)")