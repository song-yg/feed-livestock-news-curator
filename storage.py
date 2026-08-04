"""
storage.py - 5단계 저장 레이어.
data/YYYY-WW/ 아래 raw.json/scored.json/summary.md 저장(main.py run()에서 save_week() 호출).
repo 커밋 방식 사용(지난주 대비 증감 비교용 데이터가 다음 실행에도 checkout 상태로
남아있어야 해서 - Actions 아티팩트는 보존기간 후 사라져 부적합). git commit/push는
run-pipline.yml 책임, 이 모듈은 파일 생성까지만.

raw.json은 body 필드 제외(WATT 본문 저작권), scored.json은 articles 필드 제외
(raw.json과 중복 방지).
"""

import json
import os
from datetime import datetime, timedelta, timezone


def previous_week_dir(base_dir: str = "data", reference: datetime | None = None) -> str:
    """지난주 'data/YYYY-WW' 경로 계산만(디렉토리 생성 안 함, 지난주 대비 증감용)."""
    now = reference or datetime.now(timezone.utc)
    last_week = now - timedelta(weeks=1)
    iso = last_week.isocalendar()
    return os.path.join(base_dir, f"{iso.year}-{iso.week:02d}")


def week_dir(base_dir: str = "data", reference: datetime | None = None) -> str | None:
    """ISO 주차 기준 'data/YYYY-WW' 경로 생성(없으면 만듦). 생성 실패 시 로그 후 None."""
    now = reference or datetime.now(timezone.utc)
    iso = now.isocalendar()
    path = os.path.join(base_dir, f"{iso.year}-{iso.week:02d}")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        print(f"[storage] 🔴 조치필요 [ST-01] - 저장 디렉토리 생성 실패: {path} - {type(e).__name__} - {e!r}")
        return None
    return path


def _strip_body(article: dict) -> dict:
    """body, _cross_axis_partner(내부 임시 필드) 제외."""
    return {k: v for k, v in article.items() if k not in ("body", "_cross_axis_partner")}


def _strip_scored_item(item: dict) -> dict:
    return {k: v for k, v in item.items() if k != "articles"}


def save_raw(directory: str, articles: list[dict]) -> str | None:
    """필터링까지 완료된 최종 기사 데이터셋을 raw.json으로 저장. 실패 시 로그 + None."""
    cleaned = [_strip_body(a) for a in articles]
    path = os.path.join(directory, "raw.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
    except (OSError, TypeError, ValueError) as e:
        print(f"[storage] 🔴 조치필요 [ST-02] - raw.json 저장 실패: {path} - {type(e).__name__} - {e!r}")
        return None
    print(f"[storage] raw.json 저장 완료 ({len(cleaned)}건) -> {path}")
    return path


def save_scored(directory: str, domestic_summarized: list[dict],
                 international_summarized: list[dict],
                 domestic_by_category: dict[str, list[dict]],
                 international_by_category: dict[str, list[dict]],
                 gdelt_timeline: dict, failed_sources: list[str],
                 category_distribution: dict) -> str | None:
    """스코어링+요약 결과, 카테고리 집계, 실패 소스, GDELT 시계열을 scored.json에 저장."""
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "domestic": [_strip_scored_item(item) for item in domestic_summarized],
        "international": [_strip_scored_item(item) for item in international_summarized],
        "domestic_by_category": {
            category: [_strip_scored_item(item) for item in items]
            for category, items in domestic_by_category.items()
        },
        "international_by_category": {
            category: [_strip_scored_item(item) for item in items]
            for category, items in international_by_category.items()
        },
        "category_distribution": category_distribution,
        "gdelt_timeline": gdelt_timeline,
        "failed_sources": failed_sources,
    }
    path = os.path.join(directory, "scored.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    except (OSError, TypeError, ValueError) as e:
        print(f"[storage] 🔴 조치필요 [ST-03] - scored.json 저장 실패: {path} - {type(e).__name__} - {e!r}")
        return None
    print(f"[storage] scored.json 저장 완료 -> {path}")
    return path


def _format_issue_section(item: dict) -> str:
    """summary.md 이슈 하나 분량 마크다운 블록."""
    titles = item.get("titles", [])
    rep_title = titles[0] if titles else "(제목 없음)"
    lines = [
        f"### {rep_title}",
        f"- 점수: {item.get('issue_score', 0):.2f} / 언급 {item.get('mention_count', 0)}건"
        + (f" (그룹 내 추가 {len(titles) - 1}건 생략)" if len(titles) > 1 else ""),
    ]
    if item.get("cross_axis_partner"):
        lines.append(f"- 🔗 반대 축에서도 다뤄짐: {item['cross_axis_partner']}")
    if item.get("summary"):
        lines.append(f"\n{item['summary']}\n")
    else:
        reason = item.get("summary_skipped_reason", "사유 불명")
        lines.append(f"\n(요약 생략 - {reason})\n")

    urls = item.get("urls", [])
    shown = urls[:3]
    more = f" 외 {len(urls) - 3}건" if len(urls) > 3 else ""
    if shown:
        lines.append("원문 링크: " + ", ".join(shown) + more)
    return "\n".join(lines)


def _format_category_sections(by_category: dict[str, list[dict]]) -> list[str]:
    """카테고리별 Top N을 summary.md용 마크다운 줄 리스트로 변환."""
    lines = []
    for category, items in by_category.items():
        lines.append(f"\n#### {category}")
        for item in items:
            lines.append("")
            lines.append(_format_issue_section(item))
    return lines


def _format_category_comparison_section(category_comparison: dict[str, dict[str, dict]] | None) -> list[str]:
    """카테고리별 지난주 대비 증감을 summary.md용 마크다운 줄 리스트로 변환. None이면 빈 리스트."""
    if not category_comparison:
        return []
    lines = ["\n## 카테고리별 지난주 대비 증감"]
    for axis in ("국내", "해외"):
        axis_data = category_comparison.get(axis, {})
        if not axis_data:
            continue
        lines.append(f"\n### {axis}")
        for category, values in axis_data.items():
            delta = values["delta"]
            sign = "+" if delta >= 0 else ""
            lines.append(f"- {category}: {values['this_week']}건 (지난주 {values['last_week']}건, {sign}{delta})")
    return lines


def save_summary_md(directory: str, week_label: str, domestic_summarized: list[dict],
                     international_summarized: list[dict],
                     domestic_by_category: dict[str, list[dict]],
                     international_by_category: dict[str, list[dict]],
                     failed_sources: list[str],
                     category_comparison: dict[str, dict[str, dict]] | None = None) -> str | None:
    """사람이 읽을 배포용 요약본(마크다운). 요약 유무와 무관하게 원문 링크는 항상 포함."""
    lines = [f"# 사료·축산업 뉴스 큐레이션 - {week_label}", ""]
    lines.append(f"생성 시각(UTC): {datetime.now(timezone.utc).isoformat()}")
    lines.append("")
    lines.extend(_format_category_comparison_section(category_comparison))

    lines.append("## 국내")
    if domestic_summarized:
        for item in domestic_summarized:
            lines.append("")
            lines.append(_format_issue_section(item))
    else:
        lines.append("\n(이번 주 국내 이슈 없음)")
    if domestic_by_category:
        lines.append("\n### 국내 - 카테고리별 Top N")
        lines.extend(_format_category_sections(domestic_by_category))

    lines.append("\n## 해외")
    if international_summarized:
        for item in international_summarized:
            lines.append("")
            lines.append(_format_issue_section(item))
    else:
        lines.append("\n(이번 주 해외 이슈 없음)")
    if international_by_category:
        lines.append("\n### 해외 - 카테고리별 Top N")
        lines.extend(_format_category_sections(international_by_category))

    if failed_sources:
        lines.append("\n## 참고 - 이번 실행에서 실패한 소스")
        lines.append(", ".join(failed_sources))

    content = "\n".join(lines) + "\n"
    path = os.path.join(directory, "summary.md")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        print(f"[storage] 🔴 조치필요 [ST-04] - summary.md 저장 실패: {path} - {type(e).__name__} - {e!r}")
        return None
    print(f"[storage] summary.md 저장 완료 -> {path}")
    return path


def save_week(articles: list[dict], domestic_summarized: list[dict],
              international_summarized: list[dict],
              domestic_by_category: dict[str, list[dict]],
              international_by_category: dict[str, list[dict]],
              gdelt_timeline: dict, failed_sources: list[str], category_distribution: dict,
              category_comparison: dict[str, dict[str, dict]] | None = None,
              base_dir: str = "data") -> str | None:
    """main.py 호출 진입점. raw.json/scored.json/summary.md 저장 후 디렉토리 경로 반환.
    디렉토리 생성 실패 시 전체 포기(None). 개별 파일 실패는 나머지 계속 진행 후 요약 로그."""
    directory = week_dir(base_dir)
    if directory is None:
        print("[storage] 🔴 조치필요 [ST-05] - 저장 디렉토리를 만들지 못해 이번 주 저장을 건너뜀 "
              "(raw.json/scored.json/summary.md 전부 저장 안 됨)")
        return None

    week_label = os.path.basename(directory)

    saved = {
        "raw.json": save_raw(directory, articles),
        "scored.json": save_scored(directory, domestic_summarized, international_summarized,
                                    domestic_by_category, international_by_category,
                                    gdelt_timeline, failed_sources, category_distribution),
        "summary.md": save_summary_md(directory, week_label, domestic_summarized,
                                       international_summarized,
                                       domestic_by_category, international_by_category,
                                       failed_sources, category_comparison),
    }

    succeeded = [name for name, path in saved.items() if path is not None]
    failed = [name for name, path in saved.items() if path is None]

    if failed:
        print(f"[storage] 🔴 조치필요 [ST-06] - 이번 주 저장 일부 실패 - 성공: {succeeded or '없음'} / "
              f"실패: {failed} (실패 원인은 위 개별 로그 참고) -> {directory}/")
    else:
        print(f"[storage] 이번 주 저장 완료 -> {directory}/ (raw.json, scored.json, summary.md)")

    return directory


if __name__ == "__main__":
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp()
    try:
        sample_articles = [
            {"title": "구제역 확산", "url": "https://a.com/1", "source": "네이버",
             "press": "yna.co.kr", "published_at": "2026-07-22T00:00:00+00:00"},
            {"title": "Spain Newcastle disease", "url": "https://b.com/2", "source": "WATT",
             "press": "wattagnet.com", "published_at": "2026-07-21T00:00:00+00:00",
             "body": "본문 - raw.json에는 남되 이 필드는 제외돼야 함"},
        ]
        sample_scored = [{
            "issue_score": 3.5, "mention_count": 2, "raw_mention_count": 2,
            "titles": ["구제역 확산", "구제역 추가 발생"],
            "urls": ["https://a.com/1", "https://a.com/2"],
            "press_list": ["yna.co.kr"],
            "articles": sample_articles,
            "summary": "테스트 요약입니다.",
        }]
        sample_by_category = {
            "질병명": [{
                "issue_score": 3.5, "mention_count": 2, "raw_mention_count": 2,
                "titles": ["구제역 확산", "구제역 추가 발생"],
                "urls": ["https://a.com/1", "https://a.com/2"],
                "press_list": ["yna.co.kr"],
                "articles": sample_articles,
                "summary": "카테고리별 테스트 요약입니다.",
                "category": "질병명",
            }]
        }
        d = save_week(sample_articles, sample_scored, [], sample_by_category, {},
                      {}, [], {"국내": {}, "해외": {}}, base_dir=tmp)
        with open(os.path.join(d, "raw.json"), encoding="utf-8") as f:
            raw = json.load(f)
        assert "body" not in raw[1], "raw.json에 body가 남아있으면 안 됨"
        with open(os.path.join(d, "scored.json"), encoding="utf-8") as f:
            scored = json.load(f)
        assert "articles" not in scored["domestic"][0], "scored.json에 articles가 남아있으면 안 됨"
        assert "질병명" in scored["domestic_by_category"], "카테고리별 결과가 scored.json에 없음"
        assert "articles" not in scored["domestic_by_category"]["질병명"][0], \
            "카테고리별 결과에도 articles가 빠져야 함"
        assert os.path.exists(os.path.join(d, "summary.md"))
        print("[storage] 자체 점검 통과")
    finally:
        shutil.rmtree(tmp)
