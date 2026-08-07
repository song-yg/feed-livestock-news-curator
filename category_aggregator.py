"""
category_aggregator.py
카테고리 전체 집계 모듈. keyword_tagger.CATEGORY_KEYWORDS 기준 category 필드로
"이 카테고리가 이번 주 몇 건 다뤄졌는지" 집계하는 거친 보조 지표 (이슈 그룹핑과 별개).
집계는 단순 건수(recency_weight 미적용), 국내/해외 축 분리, 지난주 대비 증감 지원.
"""

import json
import os
from collections import Counter
from datetime import datetime, timezone

from keyword_tagger import CATEGORY_KEYWORDS
import scorer
import storage


_CATEGORY_ORDER = list(CATEGORY_KEYWORDS.keys()) + ["기타"]


def count_by_category(articles: list[dict]) -> Counter:
    """카테고리별 건수 집계. articles는 category 필드가 이미 채워진 상태여야 함."""
    return Counter(a.get("category", "기타") for a in articles)


def aggregate(articles: list[dict]) -> dict[str, Counter]:
    """국내/해외 축으로 나눠 카테고리별 건수 집계. 반환: {"국내": Counter, "해외": Counter}."""
    domestic, international = scorer.split_domestic_international(articles)
    return {
        "국내": count_by_category(domestic),
        "해외": count_by_category(international),
    }


def print_aggregate(aggregated: dict[str, Counter]) -> None:
    """국내/해외 카테고리 집계 콘솔 출력."""
    for axis in ("국내", "해외"):
        counter = aggregated.get(axis, Counter())
        total = sum(counter.values())
        print(f"\n=== 카테고리 집계 - {axis} (전체 {total}건) ===")
        if total == 0:
            print("  (기사 없음)")
            continue
        for category in _CATEGORY_ORDER:
            count = counter.get(category, 0)
            if count == 0:
                continue
            pct = count / total * 100
            print(f"  {category:15s} {count:4d}건 ({pct:.1f}%)")


# ---------------------------------------------------------------------------
# 지난주 대비 증감
# ---------------------------------------------------------------------------

def compare_with_last_week(current: dict[str, Counter], base_dir: str = "data",
                            reference=None) -> dict[str, dict[str, dict]] | None:
    """
    이번 주 집계를 지난주 scored.json의 category_distribution과 비교.
    지난주 파일 없으면 None 반환(첫 실행 등).

    반환(있는 경우): {"국내": {카테고리: {"this_week","last_week","delta"}, ...}, "해외": {...}}
    양쪽 중 한쪽에만 등장한 카테고리도 포함(없는 쪽은 0).
    """
    path = os.path.join(storage.previous_week_dir(base_dir, reference), "scored.json")
    try:
        with open(path, encoding="utf-8") as f:
            last_week_payload = json.load(f)

        last_week_distribution = last_week_payload.get("category_distribution")
        if not isinstance(last_week_distribution, dict):
            raise ValueError(f"category_distribution이 dict가 아님(타입: {type(last_week_distribution).__name__})")

        comparison = {}
        for axis in ("국내", "해외"):
            this_counter = current.get(axis, Counter())
            last_counter = last_week_distribution.get(axis)
            if not isinstance(last_counter, dict):
                last_counter = {}
            categories = set(this_counter) | set(last_counter)
            axis_result = {}
            for category in categories:
                this_count = this_counter.get(category, 0)
                last_count = last_counter.get(category, 0)
                axis_result[category] = {
                    "this_week": this_count,
                    "last_week": last_count,
                    "delta": this_count - last_count,
                }
            comparison[axis] = axis_result

        return comparison

    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        print(f"[category_aggregator] 🟡 주의 [CA-01] - 지난주 데이터 없음/읽기 실패({path}) - "
              f"증감 비교 생략: {type(e).__name__} - {e!r}")
        return None
    except (ValueError, AttributeError, TypeError, KeyError) as e:
        print(f"[category_aggregator] 🟡 주의 [CA-02] - 지난주 데이터 구조 이상({path}) - "
              f"증감 비교 생략: {type(e).__name__} - {e!r}")
        return None


def load_weekly_trend(current_distribution: dict[str, Counter], weeks: int = 4,
                       base_dir: str = "data", reference: datetime | None = None,
                       max_lookback: int | None = None) -> list[dict]:
    """
    이번 주(current_distribution) + 지난 주들(scored.json)을 합쳐 최근 weeks주치
    카테고리 집계를 오래된 순으로 반환. 특정 주 데이터가 없으면 max_lookback
    (기본 weeks*2)주 전까지 더 찾아서 목표 개수를 최대한 채움.
    """
    if max_lookback is None:
        max_lookback = weeks * 2

    collected = []  # 최신 -> 과거 순으로 모음(나중에 뒤집어서 오래된 순으로 만듦)
    n = 1
    while len(collected) < weeks - 1 and n <= max_lookback:
        week_path = storage.week_dir_n_back(n, base_dir, reference)
        week_label = os.path.basename(week_path)
        scored_path = os.path.join(week_path, "scored.json")
        try:
            with open(scored_path, encoding="utf-8") as f:
                payload = json.load(f)
            distribution = payload.get("category_distribution")
            if not isinstance(distribution, dict):
                raise ValueError(f"category_distribution이 dict가 아님(타입: {type(distribution).__name__})")
            collected.append({
                "week_label": week_label,
                "국내": dict(distribution.get("국내") or {}),
                "해외": dict(distribution.get("해외") or {}),
            })
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, AttributeError, TypeError, KeyError) as e:
            print(f"[category_aggregator] 🟡 주의 [CA-03] - {n}주 전({week_label}) 데이터 없음/읽기 실패 - "
                  f"건너뛰고 더 과거를 찾아 목표 개수({weeks}주)를 채움: {type(e).__name__} - {e!r}")
        n += 1

    entries = list(reversed(collected))

    now = reference or datetime.now(timezone.utc)
    iso = now.isocalendar()
    entries.append({
        "week_label": f"{iso.year}-{iso.week:02d}",
        "국내": dict(current_distribution.get("국내") or {}),
        "해외": dict(current_distribution.get("해외") or {}),
    })
    return entries


def print_aggregate_with_comparison(aggregated: dict[str, Counter],
                                     comparison: dict[str, dict[str, dict]] | None) -> None:
    """print_aggregate()와 동일하되 comparison 있으면 줄마다 지난주 대비 증감 추가."""
    for axis in ("국내", "해외"):
        counter = aggregated.get(axis, Counter())
        total = sum(counter.values())
        suffix = "" if comparison is None else ", 지난주 대비"
        print(f"\n=== 카테고리 집계 - {axis} (전체 {total}건{suffix}) ===")
        if total == 0:
            print("  (기사 없음)")
            continue
        axis_comparison = None if comparison is None else comparison.get(axis, {})
        for category in _CATEGORY_ORDER:
            count = counter.get(category, 0)
            if count == 0:
                continue
            pct = count / total * 100
            line = f"  {category:15s} {count:4d}건 ({pct:.1f}%)"
            if axis_comparison is not None and category in axis_comparison:
                delta = axis_comparison[category]["delta"]
                last_count = axis_comparison[category]["last_week"]
                sign = "+" if delta >= 0 else ""
                line += f" [지난주 {last_count}건, {sign}{delta}]"
            print(line)