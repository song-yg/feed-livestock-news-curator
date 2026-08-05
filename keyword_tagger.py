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
        "kr": ['조류독감', 'AI', '구제역', '아프리카돼지열병', '돼지열병', '럼피스킨병', '브루셀라병', '우결핵', '돼지유행성설사병', '돼지생식기호흡기증후군', '뉴캣슬병', '광우병', '살처분'],
        "en": ['avian influenza', 'avian flu', 'bird flu', 'HPAI', 'LPAI', 'foot-and-mouth disease', 'FMD', 'FMD outbreak', 'African swine fever', 'ASF', 'ASF outbreak', 'classical swine fever', 'hog cholera', 'CSF', 'lumpy skin disease', 'LSD', 'brucellosis', 'bovine tuberculosis', 'bTB', 'porcine epidemic diarrhea', 'PED', 'PEDv', 'porcine reproductive and respiratory syndrome', 'PRRS', 'PRRS virus', 'Newcastle disease', 'ND', 'bovine spongiform encephalopathy', 'BSE', 'mad cow disease', 'culling', 'stamping out', 'depopulation', 'mass depopulation'],
    },
    "시장/가격 용어": {
        "kr": ['사료가격', '사료값', '배합사료', '곡물가격', '국제곡물가', '옥수수', '대두박', '소맥', '사료 원료', '축산물 수급', '산지가격', '도매가격', '소비자가격', '사료자급률', '곡물자급률', '축산물 수출입'],
        "en": ['feed price', 'feed cost', 'feed costs', 'compound feed', 'mixed feed', 'formula feed', 'grain price', 'global grain market', 'international grain market', 'corn', 'maize', 'corn futures', 'CBOT corn', 'soybean meal', 'SBM', 'soybean futures', 'wheat', 'wheat futures', 'feed ingredients', 'livestock supply and demand', 'farm-gate price', 'wholesale price', 'retail price', 'consumer price', 'feed self-sufficiency rate', 'grain self-sufficiency rate', 'livestock exports', 'livestock imports', 'livestock trade'],
    },
    "정부·제도 용어": {
        "kr": ['농림축산식품부', '농림축산검역본부', '가축전염병예방법', '방역', '방역대', '이동제한', '축산물 이력제', '무항생제 축산물', '동물복지 인증', '가축분뇨', '예찰', '감시', '발생조사', '긴급대응'],
        "en": ['Ministry of Agriculture, Food and Rural Affairs', 'MAFRA', 'Animal and Plant Quarantine Agency', 'APQA', 'Act on the Prevention of Contagious Animal Diseases', 'biosecurity', 'quarantine', 'disease control', 'Protection Zone', 'Surveillance Zone', 'movement restriction', 'standstill order', 'movement ban', 'livestock traceability system', 'antibiotic-free livestock products', 'animal welfare certification', 'livestock manure', 'surveillance', 'disease surveillance', 'outbreak investigation', 'emergency response'],
    },
    "축종별 용어": {
        "kr": ['한우', '육우', '젖소', '낙농', '양돈', '산란계', '육계', '오리', '계란 수급', '가금 전반', '축산업 전반'],
        "en": ['Korean native cattle', 'Hanwoo', 'beef cattle', 'dairy cattle', 'dairy farming', 'hog farming', 'pig farming', 'swine industry', 'swine sector', 'laying hens', 'layers', 'broiler', 'broiler chicken', 'broiler industry', 'duck', 'egg supply', 'poultry sector', 'poultry industry', 'livestock sector', 'livestock industry'],
    },
    "사료업계 특화 용어": {
        "kr": ['배합사료업체', '사료공장', '조사료', 'TMR', '곡물엘리베이터', '사료', '프리믹스', '영양첨가업체'],
        "en": ['feed manufacturer', 'feed mill', 'feed producer', 'roughage', 'forage', 'Total Mixed Ration', 'TMR', 'grain elevator', 'animal feed', 'livestock feed', 'premix', 'premix manufacturer', 'nutrition company'],
    },
    "사료첨가제/항생제 규제": {
        "kr": ['사료첨가제', '항생제 성장촉진제', '항생제 내성', '동물용의약품', '항생제 사용 저감', '무항생제 사료', '사료관리법', '사료효소', '프로바이오틱스', '아미노산', '사료보충제', '피타아제'],
        "en": ['feed additive', 'antibiotic growth promoter', 'AGP', 'antimicrobial resistance', 'AMR', 'veterinary medicinal products', 'veterinary drugs', 'reduction of antibiotic use', 'antibiotic-free feed', 'Control of Livestock and Fish Feed Act', 'feed enzyme', 'probiotics', 'amino acid', 'lysine', 'methionine', 'feed supplement', 'phytase'],
    },
    "무역/관세 이슈": {
        "kr": ['수입관세', '할당관세', '저율관세할당', '자유무역협정', '세이프가드', '원산지 표시', '검역협상', '무역분쟁', '수출금지', '수입금지', '시장접근', '교역제한'],
        "en": ['import tariff', 'tariff-rate quota', 'TRQ', 'Free Trade Agreement', 'FTA', 'safeguard measures', 'country-of-origin labeling', 'quarantine negotiations', 'trade dispute', 'export ban', 'import ban', 'market access', 'trade restriction'],
    },
    "가금 계열화/수직계열화": {
        "kr": ['계열화', '계열화사업법', '계열주체', '계약사육', '계열화 기업', '생산비 보장'],
        "en": ['vertical integration', 'Act on Livestock Farm Alliance Systems', 'vertical integrator', 'integrator', 'contract farmer', 'farmer raising livestock under contract', 'contract grower', 'integrated poultry company', 'guarantee of production cost', 'production cost compensation'],
    },
    "스마트팜/축산 기술": {
        "kr": ['정밀축산', '스마트축사', 'ICT 축산', '자동급이시스템', '축산 빅데이터', '축산 자동화', '센서기술'],
        "en": ['precision livestock farming', 'PLF', 'precision farming', 'precision feeding', 'smart livestock barn', 'smart farming', 'ICT-based livestock farming', 'digital agriculture', 'agtech', 'automatic feeding system', 'automated feeding system', 'livestock big data', 'data analytics in livestock', 'livestock automation', 'sensor technology', 'livestock monitoring', 'digital livestock'],
    },
}

# 매칭 제외 토큰. "AI"는 조류독감 약어지만 인공지능 의미로 더 흔히 쓰여 오매칭 위험,
# "ND"도 흔한 2글자 조합이라 오매칭 위험 - 둘 다 런타임에서만 제외(사전 자체는 유지).
EXCLUDED_TERMS = {"AI", "ND"}


def _build_flat_index():
    """{카테고리: {kr,en}} -> {카테고리: [(소문자 토큰, 원본 토큰), ...]} 평탄화(모듈 로드 시 1회)."""
    index = {}
    for category, terms in CATEGORY_KEYWORDS.items():
        flat = []
        for term in terms["kr"] + terms["en"]:
            if term in EXCLUDED_TERMS:
                continue
            flat.append((term.lower(), term))
        index[category] = flat
    return index


_FLAT_INDEX = _build_flat_index()


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