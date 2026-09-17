"""Directed literal log searches. Matches are observations, never diagnoses."""
from __future__ import annotations

from collections import Counter, OrderedDict
from copy import deepcopy
import weakref

from .contracts import (AnalysisBundle, Candidate, CaseContext, EvidenceRecord,
                        QuerySpec, TelemetryStore, stable_id)
from .network import number, text
from .traces import _evidence, _read_queries


LOG_COLUMNS = ("log_id", "timestamp", "cmdb_id", "log_name", "value")
MAX_LOG_ROWS = 50_000
MAX_MATCH_SAMPLES = 8
MAX_TEXT_LENGTH = 2000
# Bounded tool-result cache, keyed by the actual Store instance and all inputs.
# It holds only compact evidence, never a whole-day or unbounded log DataFrame.
_RESULT_CACHE = weakref.WeakKeyDictionary()


def _log_values(frame, patterns, component=None):
    selected = frame if component is None else frame[frame["_component_id"].map(text) == component]
    rows = selected.to_dict("records")
    matched = []
    pattern_counts = Counter()
    sample_rows = []
    for index, row in enumerate(rows):
        raw = text(row.get("value"))
        found = [pattern for pattern in patterns if pattern in raw.casefold()]
        if not found:
            continue
        matched.append(index)
        pattern_counts.update(found)
        if len(sample_rows) < MAX_MATCH_SAMPLES:
            # Context stays in the same component stream and retains record IDs.
            nearby = []
            for other_index in (index - 1, index + 1):
                if 0 <= other_index < len(rows):
                    other = rows[other_index]
                    if text(other.get("_component_id")) == text(row.get("_component_id")):
                        nearby.append(_positioned_text(other))
            sample = _positioned_text(row)
            sample["matched_patterns"] = found
            sample["context"] = nearby
            sample_rows.append(sample)
    return {
        "searched_rows": len(rows), "matched_rows": len(matched),
        "pattern_match_counts": {pattern: pattern_counts[pattern] for pattern in patterns},
        "match_samples": sample_rows,
        "sample_limit": MAX_MATCH_SAMPLES,
        "text_character_limit": MAX_TEXT_LENGTH,
    }


def _positioned_text(row):
    value = text(row.get("value"))
    record_index = number(row.get("_record_index"))
    return {"source_file": text(row.get("_source_file")),
            "record_index": int(record_index) if record_index is not None else None,
            "log_id": text(row.get("log_id")) or None,
            "timestamp_s": number(row.get("_timestamp_s")),
            "component": text(row.get("_component_id")) or None,
            "log_name": text(row.get("log_name")), "text": value[:MAX_TEXT_LENGTH],
            "text_truncated": len(value) > MAX_TEXT_LENGTH}


def search_fault_evidence(case: CaseContext, component_ids: tuple[str, ...],
                          patterns: tuple[str, ...], sources: tuple[str, ...],
                          store: TelemetryStore, *, deadline: float) -> AnalysisBundle:
    if any(source not in ("log_service", "log_proxy") for source in sources):
        raise ValueError("Log searches support only log_service and log_proxy")
    if len(patterns) > 32 or any(not isinstance(pattern, str) or not pattern or len(pattern) > 256 for pattern in patterns):
        raise ValueError("Use at most 32 nonempty literal patterns, each at most 256 characters")
    if not component_ids or not patterns or not sources:
        return AnalysisBundle(module="m3", warnings=["No components, patterns or sources requested; logs not queried."])
    normalized = tuple(dict.fromkeys(pattern.casefold() for pattern in patterns))
    components = tuple(sorted(set(component_ids)))
    ordered_sources = tuple(source for source in ("log_service", "log_proxy") if source in sources)
    signature = (case.case_key, case.start, case.end, components, normalized, ordered_sources)
    cache = _RESULT_CACHE.setdefault(store, OrderedDict())
    if signature in cache:
        cache.move_to_end(signature)
        return deepcopy(cache[signature])
    bundle = AnalysisBundle(module="m3")
    for source in ordered_sources:
        query = QuerySpec(source, case.start, case.end, LOG_COLUMNS, component_ids=components)
        frame, coverage, warnings, retained = _read_queries([query], store, deadline, [MAX_LOG_ROWS])
        bundle.coverage.extend(coverage)
        bundle.warnings.extend(warnings)
        observed_components = sorted(set(frame["_component_id"].map(text)) - {""})
        # A summary explicitly represents an empty search or missing source.
        for component in observed_components or [None]:
            params = {"patterns": list(normalized), "component": component,
                      "retained_rows_per_query": retained, "row_cap_per_query": MAX_LOG_ROWS,
                      "matching": "casefold-literal-substring.v1", "max_match_samples": MAX_MATCH_SAMPLES,
                      "max_text_characters": MAX_TEXT_LENGTH, "context_records_each_side": 1}
            values = _log_values(frame, normalized, component)
            rows = frame.to_dict("records") if component is None else frame[frame["_component_id"].map(text) == component].to_dict("records")
            # Location samples prioritize actual matches, then the scanned prefix.
            matches = [row for row in rows if any(pattern in text(row.get("value")).casefold() for pattern in normalized)]
            record = _evidence(case, "logs.literal_search.v1", params, values,
                               [component] if component else list(components), [query], coverage,
                               matches[:MAX_MATCH_SAMPLES] or rows[:MAX_MATCH_SAMPLES],
                               {"searched_rows": "count", "matched_rows": "count", "timestamp_s": "epoch seconds"},
                               warnings + [
                                   "Literal keywords can be propagated symptoms or benign text; a match does not establish root cause.",
                                   "No matching keyword is not evidence of health; only searched rows and patterns are characterized.",
                                   "Context follows source record order, which need not be chronological; text may be explicitly truncated.",
                               ], kind="log")
            record.interval = (case.start, case.end)
            record.source_files = sorted({text(row.get("_source_file")) for row in rows} - {""})
            bundle.evidence.append(record)
            if component and values["matched_rows"]:
                bundle.candidates.append(Candidate(
                    candidate_id=stable_id(case.case_key, "m3.candidate", {"component": component, "evidence": record.evidence_id}),
                    component=component, supporting_ids=[record.evidence_id],
                    features={"logs.matched_count": values["matched_rows"], "logs.anomaly_score": 0.5},
                    unresolved=["Keyword match requires semantic review; component attribution, mechanism and subtype remain unresolved."]))
    # A partial/deadline result is not reused: a later authorized investigation may
    # have time for new data. Complete/missing/empty signatures avoid repeat scans.
    if all(cov.status in ("complete", "empty", "missing") for cov in bundle.coverage):
        cache[signature] = deepcopy(bundle)
        while len(cache) > 8:
            cache.popitem(last=False)
    return bundle


def _replay_log_evidence(record: EvidenceRecord, store: TelemetryStore, *, deadline: float) -> dict:
    if record.transform != "logs.literal_search.v1":
        raise ValueError(f"Unsupported log transform: {record.transform}")
    params = record.transform_params
    frame, _, _, retained = _read_queries(record.queries, store, deadline, params["retained_rows_per_query"])
    if retained != params["retained_rows_per_query"]:
        raise TimeoutError("Log replay did not recover recorded input prefix")
    return _log_values(frame, tuple(params["patterns"]), params["component"])
