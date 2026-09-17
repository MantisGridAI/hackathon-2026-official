"""Bounded trace relationships; dependency direction is never a causal claim."""
from __future__ import annotations

from collections import Counter, defaultdict
import math
import time
from typing import Any

import numpy as np
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
    if isinstance(pairs, pd.DataFrame):
        return _compact_edge_values(pairs, baseline_start, incident_start, incident_end)
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


def pair_spans_compact(frame: pd.DataFrame, *, deadline: float | None = None):
    """Same pairing semantics as v1, without materializing every span as a dict.

    Integer row positions reference the unchanged input frame. This keeps exact
    source locators and cross-chunk joins while avoiding copies of raw span text
    in the key index, pair records, evidence samples and each edge's statistics.
    """
    def check():
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Dependency pairing deadline reached")

    check()
    trace = frame["trace_id"].astype("string").fillna("")
    span = frame["span_id"].astype("string").fillna("")
    parent = frame["parent_span"].astype("string").fillna("")
    keys = pd.MultiIndex.from_arrays([trace, span])
    valid = trace.ne("").to_numpy() & span.ne("").to_numpy()
    duplicate = keys.duplicated(keep=False) & valid
    roots = parent.str.lower().isin(ROOT_PARENTS).to_numpy()
    expected = ~roots
    children = expected & valid & ~duplicate
    check()
    parent_keys = pd.MultiIndex.from_arrays([trace, parent])
    unique = valid & ~duplicate
    lookup = pd.Series(np.flatnonzero(unique), index=keys[unique], dtype="int64")
    parent_index = lookup.reindex(parent_keys).fillna(-1).to_numpy(dtype="int64")
    duplicate_parent = parent_keys.isin(keys[duplicate].drop_duplicates())
    ambiguous = children & (duplicate_parent | parent.eq(span).to_numpy())
    missing = children & (parent_index < 0) & ~duplicate_parent
    # Self-parent is an ambiguous topology even though its unique key exists.
    paired = children & ~ambiguous & (parent_index >= 0)
    child_index = np.flatnonzero(paired)
    matched_parent = parent_index[child_index]
    components = frame["_component_id"].astype("string").fillna("").to_numpy()
    mapped = (components[child_index] != "") & (components[matched_parent] != "")
    unmapped = int((~mapped).sum())
    child_index, matched_parent = child_index[mapped], matched_parent[mapped]
    starts = pd.to_numeric(frame["_timestamp_s"], errors="coerce").to_numpy(dtype=float)
    finite = np.isfinite(starts[child_index]) & np.isfinite(starts[matched_parent])
    child_index, matched_parent = child_index[finite], matched_parent[finite]
    gaps = starts[child_index] - starts[matched_parent]
    check()
    span_types = frame["type"].astype("string").fillna("").to_numpy()
    operations = frame["operation_name"].astype("string").fillna("").to_numpy()
    pairs = pd.DataFrame({
        "caller": components[matched_parent], "callee": components[child_index],
        "operation": operations[child_index], "_child_row": child_index,
        "_parent_row": matched_parent, "child_timestamp_s": starts[child_index],
        "start_gap_s": gaps,
        "type_pair": np.char.add(np.char.add(span_types[matched_parent].astype(str), "->"),
                                 span_types[child_index].astype(str)),
    })
    counts = {"span_rows": len(frame),
              "duplicate_key_count": int((keys.duplicated(keep="first") & valid).sum()),
              "invalid_key_rows": int((~valid).sum()), "root_rows": int(roots.sum()),
              "parent_expected_rows": int(expected.sum()), "paired_rows": len(pairs),
              "missing_parent_rows": int(missing.sum()),
              "ambiguous_parent_rows": int(ambiguous.sum()),
              "ambiguous_child_rows": int((expected & valid & duplicate).sum()),
              "unmapped_component_rows": unmapped,
              "negative_start_gap_rows": int((gaps < 0).sum())}
    counts["pairing_fraction"] = len(pairs) / int(expected.sum()) if expected.any() else None
    groups = {key: group for key, group in pairs.groupby(["caller", "callee", "operation"], sort=True)}
    check()
    return groups, counts


def _compact_edge_values(pairs, baseline_start, incident_start, incident_end):
    result = {}
    for label, start, end in (("baseline", baseline_start, incident_start),
                              ("incident", incident_start, incident_end)):
        selected = pairs[pairs["child_timestamp_s"].ge(start) & pairs["child_timestamp_s"].lt(end)]
        gaps = selected["start_gap_s"]
        result[label] = {
            "paired_count": len(selected), "observed_pairs_per_minute": len(selected) * 60 / (end - start),
            "start_gap_median_s": quantile(gaps.tolist(), .5),
            "start_gap_p95_s": quantile(gaps.tolist(), .95),
            "negative_gap_count": int(gaps.lt(0).sum()),
            "type_pair_counts": dict(sorted((str(k), int(v)) for k, v in selected["type_pair"].value_counts().items())),
        }
    before, after = (result[label]["start_gap_median_s"] for label in ("baseline", "incident"))
    result["start_gap_ratio"] = after / before if before is not None and before > 0 and after is not None else None
    result["causal_direction"] = None
    return result
