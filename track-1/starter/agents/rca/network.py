"""Bounded trace relationships; dependency direction is never a causal claim."""
from __future__ import annotations

from collections import Counter, defaultdict
import math
from typing import Any

import pandas as pd


ROOT_PARENTS = frozenset(("", "0", "-1", "none", "null", "nan"))


def text(value: Any) -> str:
    """Preserve raw identifiers while making absent scalar values explicit."""
    return "" if value is None or pd.isna(value) else str(value)


def number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lo = int(position)
    hi = min(lo + 1, len(ordered) - 1)
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo))


def pair_spans(frame: pd.DataFrame) -> tuple[dict[tuple[str, str, str], list[dict]], dict]:
    """Join only unique (trace_id, span_id) keys, including across chunks.

    Duplicate keys are ambiguous even if their contents happen to match. Neither
    duplicate children nor duplicate parents create a trusted dependency edge.
    The caller bounds frame size before this in-memory join.
    """
    rows = frame.to_dict("records")
    keyed: dict[tuple[str, str], list[dict]] = defaultdict(list)
    invalid_key_rows = 0
    for row in rows:
        key = (text(row.get("trace_id")), text(row.get("span_id")))
        if not all(key):
            invalid_key_rows += 1
        else:
            keyed[key].append(row)
    counts = {
        "span_rows": len(rows),
        "duplicate_key_count": sum(len(items) - 1 for items in keyed.values()),
        "invalid_key_rows": invalid_key_rows,
        "root_rows": 0,
        "parent_expected_rows": 0,
        "paired_rows": 0,
        "missing_parent_rows": 0,
        "ambiguous_parent_rows": 0,
        "ambiguous_child_rows": 0,
        "unmapped_component_rows": 0,
        "negative_start_gap_rows": 0,
    }
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        parent_id = text(row.get("parent_span"))
        if parent_id.lower() in ROOT_PARENTS:
            counts["root_rows"] += 1
            continue
        counts["parent_expected_rows"] += 1
        trace_id, span_id = text(row.get("trace_id")), text(row.get("span_id"))
        if not trace_id or not span_id:
            continue
        if len(keyed[(trace_id, span_id)]) != 1:
            counts["ambiguous_child_rows"] += 1
            continue
        parents = keyed.get((trace_id, parent_id), [])
        if not parents:
            counts["missing_parent_rows"] += 1
            continue
        if len(parents) != 1:
            counts["ambiguous_parent_rows"] += 1
            continue
        parent = parents[0]
        # Self-parent and repeated ID are invalid topology, not self-causation.
        if parent is row or parent_id == span_id:
            counts["ambiguous_parent_rows"] += 1
            continue
        caller = text(parent.get("_component_id"))
        callee = text(row.get("_component_id"))
        if not caller or not callee:
            counts["unmapped_component_rows"] += 1
            continue
        start = number(row.get("_timestamp_s"))
        parent_start = number(parent.get("_timestamp_s"))
        if start is None or parent_start is None:
            continue
        gap = start - parent_start
        counts["paired_rows"] += 1
        counts["negative_start_gap_rows"] += int(gap < 0)
        groups[(caller, callee, text(row.get("operation_name")))].append({
            "child": row,
            "parent": parent,
            "child_timestamp_s": start,
            "start_gap_s": gap,
        })
    expected = counts["parent_expected_rows"]
    counts["pairing_fraction"] = counts["paired_rows"] / expected if expected else None
    return dict(groups), counts


def edge_values(pairs: list[dict], baseline_start: float, incident_start: float,
                incident_end: float) -> dict:
    """Use child start time for period membership; never subtract durations."""
    result: dict[str, Any] = {}
    for label, start, end in (
        ("baseline", baseline_start, incident_start),
        ("incident", incident_start, incident_end),
    ):
        selected = [p for p in pairs if start <= p["child_timestamp_s"] < end]
        gaps = [p["start_gap_s"] for p in selected]
        types = Counter(
            f"{text(p['parent'].get('type'))}->{text(p['child'].get('type'))}"
            for p in selected
        )
        result[label] = {
            "paired_count": len(selected),
            "observed_pairs_per_minute": len(selected) * 60 / (end - start),
            "start_gap_median_s": quantile(gaps, .5),
            "start_gap_p95_s": quantile(gaps, .95),
            "negative_gap_count": sum(gap < 0 for gap in gaps),
            "type_pair_counts": dict(sorted(types.items())),
        }
    before = result["baseline"]["start_gap_median_s"]
    after = result["incident"]["start_gap_median_s"]
    result["start_gap_ratio"] = after / before if before is not None and before > 0 and after is not None else None
    result["causal_direction"] = None
    return result
