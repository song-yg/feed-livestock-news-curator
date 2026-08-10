"""
keyword_tagger.py
키워드 태깅 모듈(정규화 단계). 기사 제목을 CATEGORY_KEYWORDS 사전과 매칭해
카테고리 라벨링(질병명/시장가격/정부제도 등). 이슈 그룹핑(issue_grouper.py)과
별개 기능 - 임베딩/LLM 없이 순수 문자열 매칭만 사용.
"""

import re
from collections import Counter

CATEGORY_KEYWORDS = {
    "질병명": {
        "kr": ['광우병', '구제역', '뉴캣슬병', '돼지생식기호흡기증후군', '돼지열병', '돼지유행성설사병', '럼피스킨병', '브루셀라병', '아프리카돼지열병', '우결핵', '조류독감'],
        "en": ['African swine fever', 'ASF', 'avian influenza', 'bird flu', 'bovine spongiform encephalopathy', 'bovine tuberculosis', 'brucellosis', 'BSE', 'classical swine fever', 'FMD', 'foot-and-mouth disease', 'lumpy skin disease', 'Newcastle disease', 'PED', 'porcine epidemic diarrhea', 'porcine reproductive and respiratory syndrome'],
    },
    "시장/가격 용어": {
        "kr": ['곡물가격', '곡물자급률', '국제곡물가', '대두박', '배합사료', '사료 원료', '사료가격', '사료값', '사료자급률', '산지가격', '축산물 수출입'],
        "en": ['compound feed', 'corn futures', 'farm-gate price', 'feed cost', 'feed ingredients', 'feed price', 'feed self-sufficiency rate', 'formula feed', 'global grain market', 'grain price', 'grain self-sufficiency rate', 'livestock exports', 'livestock imports', 'livestock supply and demand', 'mixed feed', 'SBM', 'soybean futures', 'soybean meal', 'wheat futures'],
    },
    "정부·제도 용어": {
        "kr": ['가축 이동제한', '가축분뇨', '가축전염병예방법', '농림축산검역본부', '동물복지 인증', '무항생제 축산물', '방역대', '축산물 이력제'],
        "en": ['Animal and Plant Quarantine Agency', 'animal welfare certification', 'antibiotic-free livestock products', 'biosecurity', 'disease control', 'disease surveillance', 'livestock manure', 'livestock movement restriction', 'livestock traceability system', 'Ministry of Agriculture, Food and Rural Affairs', 'Protection Zone', 'quarantine', 'standstill order', 'Surveillance Zone'],
    },
    "축종별 용어": {
        "kr": ['가금 전반', '계란 수급', '낙농', '산란계', '양돈', '육계', '육우', '젖소', '축산업 전반'],
        "en": ['beef cattle', 'broiler', 'dairy cattle', 'dairy farming', 'egg supply', 'Korean native cattle', 'laying hens', 'livestock industry', 'pig farming', 'poultry industry', 'swine industry'],
    },
    "사료업계 특화 용어": {
        "kr": ['곡물엘리베이터', '배합사료업체', '사료공장', '영양첨가업체', '조사료', '프리믹스'],
        "en": ['animal feed', 'feed manufacturer', 'feed mill', 'feed producer', 'grain elevator', 'livestock feed', 'nutrition company', 'premix', 'roughage', 'Total Mixed Ration'],
    },
    "사료첨가제/항생제 규제": {
        "kr": ['동물용의약품', '무항생제 사료', '사료관리법', '사료보충제', '사료첨가제', '사료효소', '피타아제', '항생제 사용 저감', '항생제 성장촉진제'],
        "en": ['AMR', 'antibiotic growth promoter', 'antibiotic-free feed', 'antimicrobial resistance', 'Control of Livestock and Fish Feed Act', 'feed additive', 'feed enzyme', 'feed supplement', 'methionine', 'phytase', 'reduction of antibiotic use', 'veterinary drugs'],
    },
    "무역/관세 이슈": {
        "kr": ['축산물 할당관세'],
        "en": ['livestock import tariff'],
    },
    "가금 계열화/수직계열화": {
        "kr": ['계약사육', '계열주체', '계열화 기업', '계열화사업법', '생산비 보장', '축산업 계열화'],
        "en": ['contract farmer', 'contract grower', 'guarantee of production cost', 'poultry vertical integration', 'vertical integrator'],
    },
    "스마트팜/축산 기술": {
        "kr": ['스마트축사', '자동급여시스템', '정밀축산', '축산 빅데이터', '축산 자동화'],
        "en": ['livestock automation', 'livestock big data', 'precision livestock farming', 'smart farming'],
    },
}

# 매칭 제외 토큰 - 짧은 약어라 다른 의미로 오매칭될 위험이 있는 키워드용
# 안전장치(현재 목록엔 해당 사례 없어서 비어있음, 필요 시 추가).
EXCLUDED_TERMS = set()


def _build_flat_index(category_keywords: dict[str, dict[str, list[str]]]):
    """{카테고리: {kr,en}} -> {카테고리: [(소문자 토큰, 원본 토큰), ...]} 평탄화."""
    index = {}
    for category, terms in category_keywords.items():
        flat = []
        for term in terms["kr"] + terms["en"]:
            if term in EXCLUDED_TERMS:
                continue
            flat.append((term.lower(), term))
        index[category] = flat
    return index


_active_category_keywords = CATEGORY_KEYWORDS  # 코드 내장 기본값으로 시작
_FLAT_INDEX = _build_flat_index(_active_category_keywords)


def set_category_keywords(category_keywords: dict[str, dict[str, list[str]]]) -> None:
    """카테고리 판정 사전을 교체(구글 시트에서 불러온 것으로). main.py가 [2]
    정규화 시작 전에 1회 호출."""
    global _active_category_keywords, _FLAT_INDEX
    _active_category_keywords = category_keywords
    _FLAT_INDEX = _build_flat_index(_active_category_keywords)


def _dedupe_contained(terms: list[str]) -> list[str]:
    """다른 항목의 부분 문자열인 항목 제거(대소문자 무시). 예: ["corn","corn futures"] -> ["corn futures"]."""
    result = []
    for term in terms:
        term_lower = term.lower()
        if any(term != other and term_lower in other.lower() for other in terms):
            continue
        result.append(term)
    return result


def tag_title(title: str) -> tuple[str, list[str]]:
    """
    제목 하나를 카테고리에 매칭(대소문자 무시 부분 문자열). 카테고리별 매칭
    개수가 가장 많은 쪽 채택, 동점이면 CATEGORY_KEYWORDS 사전 순서상 먼저
    나오는 쪽. _dedupe_contained로 포함 관계 매칭 중복 집계 방지.
    반환: (category, matched_terms). 안 걸리면 ("기타", []).
    """
    if not title:
        return "기타", []

    title_lower = title.lower()
    best_category = None
    best_matches: list[str] = []

    for category, flat_terms in _FLAT_INDEX.items():
        raw_matches = [orig for lower, orig in flat_terms if lower in title_lower]
        matches = _dedupe_contained(raw_matches)
        if len(matches) > len(best_matches):
            best_category = category
            best_matches = matches

    if best_category is None:
        return "기타", []
    return best_category, best_matches


def tag_articles(articles: list[dict]) -> list[dict]:
    """
    기사 리스트 전체에 category 필드를 채움(in-place).
    WATT 소스는 원래 category(사이트 자체 분류)를 site_category로 보존 후 덮어씀.
    """
    other_count = 0
    for article in articles:
        original_category = article.get("category")
        if original_category and article.get("source") not in ("네이버", "GDELT"):
            article["site_category"] = original_category

        category, matched_terms = tag_title(article.get("title", ""))
        article["category"] = category
        article["matched_keywords"] = matched_terms

        if category == "기타":
            other_count += 1

    total = len(articles)
    other_ratio = (other_count / total * 100) if total else 0.0
    print(f"[keyword_tagger] {total}건 중 '기타' {other_count}건 ({other_ratio:.1f}%) "
          f"- 비율이 높으면 사전에 신규 키워드 추가 검토")

    return articles


def print_category_distribution(articles: list[dict]) -> None:
    """카테고리별 건수 분포 콘솔 출력(진단용)."""
    counter = Counter(a.get("category", "기타") for a in articles)
    total = len(articles)
    print(f"\n=== 카테고리 분포 (전체 {total}건) ===")
    for category, count in counter.most_common():
        pct = count / total * 100 if total else 0
        print(f"  {category:15s} {count:4d}건 ({pct:.1f}%)")


def print_uncategorized_sample(articles: list[dict], sample_size: int = 30) -> None:
    """'기타' 분류 기사 제목 샘플 출력(진단용, 정규 실행 경로에서는 호출 안 함)."""
    uncategorized = [a for a in articles if a.get("category", "기타") == "기타"]
    total = len(uncategorized)
    print(f"\n=== '기타' 분류 기사 샘플 (전체 {total}건 중 최대 {sample_size}건) ===")
    if total == 0:
        print("  (해당 없음)")
        return
    for i, article in enumerate(uncategorized[:sample_size], start=1):
        source = article.get("source", "?")
        title = article.get("title", "(제목 없음)")
        print(f"  {i:2d}. [{source}] {title}")
    if total > sample_size:
        print(f"  ... 외 {total - sample_size}건 생략")