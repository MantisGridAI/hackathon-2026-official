"""Independent trace triage with replayable, source-backed observations.

All duration statistics retain native units. Start differences use normalized
epoch seconds and are not measurements of network latency or causal direction.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import timedelta
import time

import pandas as pd

from .contracts import (AnalysisBundle, Candidate, CaseContext, Coverage,
                        DependencyEdge, EvidenceRecord, QuerySpec, SourceRecord,
                        TelemetryStore, stable_id)
from .network import edge_values, number, pair_spans, quantile, text


TRACE_COLUMNS = ("timestamp", "cmdb_id", "span_id", "trace_id", "duration",
                 "type", "status_code", "operation_name", "parent_span")
MAX_ROWS_PER_QUERY = 150_000
MAX_GROUPS = 512
MAX_EDGES = 256
CONTEXT_SECONDS = 30
MIN_COMPARE_SAMPLES = 3
COMMON_LIMITATIONS = [
    "Duration units are unverified native units; timestamp milliseconds do not establish duration units.",
    "Observed spans/minute are sampled trace frequency, not total business request rate.",
    "The reference period is a comparison, not a guarantee of health.",
]
EDGE_LIMITATIONS = [
    "Dependency direction is not causal or fault-propagation direction.",
    "Parent-child start differences include scheduling, concurrency, async work and clock skew; they are not measured network latency.",
    "Bounded context and sampled or incomplete traces can omit parents; no complete path is asserted.",
]


def _read_queries(queries, store, deadline, limits=None):
    """Read bounded prefixes and always close iterators, including at a cap."""
    frames, coverages, warnings, retained = [], [], [], []
    for index, query in enumerate(queries):
        limit = MAX_ROWS_PER_QUERY if limits is None else int(limits[index])
        if limit < 0 or limit > MAX_ROWS_PER_QUERY:
            raise ValueError("Invalid trace/log replay row limit")
        pieces, count, truncated = [], 0, False
        if time.monotonic() >= deadline or limit == 0:
            cov = replace(store.coverage(query), status="not_queried",
                          warnings=["Deadline exhausted or zero-row replay prefix; no scan."])
        else:
            # Split the remaining scan allowance across queries. A baseline's
            # full-day file scan must not consume the entire incident allowance.
            query_deadline = time.monotonic() + max(0, deadline - time.monotonic()) / (len(queries) - index)
            iterator = store.iter_window(query, deadline=query_deadline)
            try:
                for chunk in iterator:
                    if time.monotonic() >= query_deadline:
                        truncated = True
                        break
                    remaining = limit - count
                    piece = chunk.iloc[:remaining].copy()
                    if not piece.empty:
                        pieces.append(piece)
                        count += len(piece)
                    if len(chunk) > remaining or count >= limit:
                        truncated = True
                        break
            finally:
                close = getattr(iterator, "close", None)
                if close is not None:
                    close()
            cov = store.coverage(query)
            if truncated:
                cov = replace(cov, status="partial", warnings=list(cov.warnings) + [
                    f"M3 retained a bounded prefix of {count} rows; row/deadline cap reached."])
        coverages.append(cov)
        retained.append(count)
        if cov.status not in ("complete", "empty"):
            warnings.append(f"{query.source} coverage {cov.status}; absence is not health.")
        warnings.extend(cov.warnings)
        frames.extend(pieces)
    columns = list(queries[0].columns) + ["_source_file", "_record_index", "_timestamp_s", "_component_id"] if queries else []
    frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)
    return frame, coverages, list(dict.fromkeys(warnings)), retained


def _source_records(rows, limit=8):
    found = []
    seen = set()
    # Both ends of a period-spanning group provide useful positioning samples.
    sample = rows if len(rows) <= limit else rows[:limit // 2] + rows[-(limit - limit // 2):]
    for row in sample:
        file_name = text(row.get("_source_file"))
        index = number(row.get("_record_index"))
        key = (file_name, int(index) if index is not None else None)
        if not file_name or key in seen:
            continue
        seen.add(key)
        found.append(SourceRecord(source_file=file_name, record_index=key[1],
                                  trace_id=text(row.get("trace_id")) or None,
                                  span_id=text(row.get("span_id")) or None,
                                  log_id=text(row.get("log_id")) or None))
        if len(found) >= limit:
            break
    return found


def _evidence(case, transform, params, values, components, queries, coverage,
              rows, units, limitations, kind="trace"):
    records = _source_records(rows)
    sources = sorted({text(row.get("_source_file")) for row in rows if text(row.get("_source_file"))})
    payload = {"transform": transform, "params": params, "components": components,
               "queries": queries, "values": values,
               "coverage_status": [cov.status for cov in coverage]}
    return EvidenceRecord(
        evidence_id=stable_id(case.case_key, "m3.evidence", payload), kind=kind,
        component_ids=list(components), interval=(case.reference_start, case.end),
        source_files=sources, queries=list(queries), transform=transform,
        transform_params=dict(params), values=values, units=units,
        source_records=records, coverage=list(coverage),
        limitations=list(dict.fromkeys(limitations)))


def _status_class(raw, span_type):
    """Do not use a nonzero rule: RPC codes and HTTP statuses differ."""
    value = text(raw).strip().lower()
    if value in ("0", "0.0", "ok", "success", "successful", "unset"):
        return "success"
    if value in ("error", "failed", "failure", "timeout", "cancelled", "canceled"):
        return "error"
    numeric = number(value)
    if text(span_type).lower() == "http" and numeric is not None:
        if 200 <= numeric < 400:
            return "success"
        if 400 <= numeric < 600:
            return "error"
    return "unknown"


def _group_values(frame, baseline_start, incident_start, incident_end):
    result = {}
    for label, start, end in (("baseline", baseline_start, incident_start),
                              ("incident", incident_start, incident_end)):
        selected = frame[(frame["_timestamp_s"] >= start) & (frame["_timestamp_s"] < end)]
        duration = pd.to_numeric(selected["duration"], errors="coerce")
        valid = [value for value in map(number, duration.tolist()) if value is not None and value >= 0]
        statuses = Counter(text(value) or "<missing>" for value in selected["status_code"])
        classified = Counter(_status_class(row["status_code"], row["type"])
                             for row in selected.to_dict("records"))
        result[label] = {
            "span_count": len(selected), "valid_duration_count": len(valid),
            "invalid_duration_count": len(selected) - len(valid),
            "observed_spans_per_minute": len(selected) * 60 / (end - start),
            "duration_median": quantile(valid, .5), "duration_p95": quantile(valid, .95),
            "status_counts": dict(sorted(statuses.items())),
            "recognized_success_count": classified["success"],
            "recognized_error_count": classified["error"],
            "unknown_status_count": classified["unknown"],
            "recognized_error_fraction": classified["error"] / len(selected) if len(selected) else None,
        }
    before, after = result["baseline"], result["incident"]
    for key, field in (("duration_ratio", "duration_median"), ("p95_ratio", "duration_p95"),
                       ("frequency_ratio", "observed_spans_per_minute")):
        left, right = before[field], after[field]
        result[key] = right / left if left is not None and left > 0 and right is not None else None
    left, right = before["recognized_error_fraction"], after["recognized_error_fraction"]
    result["error_rate_delta"] = right - left if left is not None and right is not None else None
    return result


def _queries(case):
    return [
        QuerySpec("trace_span", case.reference_start - timedelta(seconds=CONTEXT_SECONDS),
                  case.start, TRACE_COLUMNS),
        QuerySpec("trace_span", case.start, case.end + timedelta(seconds=CONTEXT_SECONDS), TRACE_COLUMNS),
    ]


def _params(case, retained):
    return {"baseline_start_s": case.reference_start.timestamp(), "incident_start_s": case.start.timestamp(),
            "incident_end_s": case.end.timestamp(), "retained_rows_per_query": retained,
            "row_cap_per_query": MAX_ROWS_PER_QUERY, "context_seconds": CONTEXT_SECONDS,
            "duration_unit": "native/unknown", "minimum_compare_samples": MIN_COMPARE_SAMPLES,
            "duration_ratio_threshold": 2.0, "p95_ratio_threshold": 2.5,
            "error_fraction_delta_threshold": .2, "status_rule": "explicit-status-and-http.v1"}


def _analyze(case, store, deadline, selected=None, selected_edges=()):
    queries = _queries(case)
    remaining = max(0.0, deadline - time.monotonic())
    # Reserve some of the caller's actual budget for materializing evidence.
    scan_deadline = deadline - min(6.0, remaining * .25)
    frame, coverage, warnings, retained = _read_queries(queries, store, scan_deadline)
    bundle = AnalysisBundle(module="m3", coverage=coverage, warnings=warnings)
    params = _params(case, retained)
    if frame.empty:
        bundle.warnings.append("No trace observations available; trace health and dependency topology are unknown.")
        return bundle
    frame["_timestamp_s"] = pd.to_numeric(frame["_timestamp_s"], errors="coerce")
    frame["_component_id"] = frame["_component_id"].map(text)
    frame["operation_name"] = frame["operation_name"].map(text)
    frame["type"] = frame["type"].map(text)
    baseline, incident, end = (params[key] for key in ("baseline_start_s", "incident_start_s", "incident_end_s"))
    in_period = frame[(frame["_timestamp_s"] >= baseline) & (frame["_timestamp_s"] < end)]
    focus = in_period[in_period["_component_id"].isin(selected)] if selected is not None else in_period
    grouped = focus.groupby(["_component_id", "operation_name", "type"], sort=True, dropna=False)
    for index, (key, group) in enumerate(grouped):
        if index >= MAX_GROUPS or time.monotonic() >= deadline:
            bundle.warnings.append("Trace group analysis stopped at group/deadline limit; unanalysed groups are unknown.")
            break
        component, operation, span_type = key
        if not component:
            continue
        group_params = dict(params, component=component, operation=operation, span_type=span_type)
        values = _group_values(group, baseline, incident, end)
        before, after = values["baseline"], values["incident"]
        limitations = COMMON_LIMITATIONS + list(warnings)
        if before["span_count"] < 20 or after["span_count"] < 20:
            limitations.append("Low sample count: empirical p95 is descriptive and unstable, not a reliable tail estimate.")
        if before["unknown_status_count"] or after["unknown_status_count"]:
            limitations.append("Unrecognized status codes remain unknown; nonzero is not automatically an error.")
        record = _evidence(case, "traces.group_compare.v1", group_params, values, [component], queries,
                           coverage, group.to_dict("records"),
                           {"duration_median": "native/unknown", "duration_p95": "native/unknown",
                            "span_count": "count", "observed_spans_per_minute": "observed spans/minute",
                            "duration_ratio": "ratio", "p95_ratio": "ratio", "frequency_ratio": "ratio",
                            "error_rate_delta": "fraction"}, limitations)
        bundle.evidence.append(record)
        enough = min(before["span_count"], after["span_count"]) >= MIN_COMPARE_SAMPLES
        duration_samples = min(before["valid_duration_count"], after["valid_duration_count"])
        ratios = [values["duration_ratio"] or 0 if duration_samples >= MIN_COMPARE_SAMPLES else 0,
                  (values["p95_ratio"] or 0) * .8 if duration_samples >= 20 else 0]
        error_delta = values["error_rate_delta"]
        anomalous = enough and (max(ratios) >= 2 or (error_delta is not None and error_delta >= .2))
        if anomalous:
            features = {f"traces.{name}": value for name, value in values.items()
                        if name not in ("baseline", "incident") and value is not None}
            features.update({"traces.anomaly_score": min(10.0, max(ratios) - 1 + max(0, error_delta or 0) * 3),
                             "traces.sample_count": after["span_count"], "traces.network_family": True})
            candidate = Candidate(
                candidate_id=stable_id(case.case_key, "m3.candidate", {"component": component, "evidence": record.evidence_id}),
                component=component, features=features, supporting_ids=[record.evidence_id],
                unresolved=["Slow/error spans can reflect waiting on another component; no causal claim.",
                            "Network family is a hypothesis; network subtype and resource alternatives remain unresolved."],
            )
            bundle.candidates.append(candidate)
    if time.monotonic() >= deadline:
        bundle.warnings.append("Deadline exhausted before dependency pairing; topology not queried by transform.")
        return bundle
    groups, pairing_counts = pair_spans(frame)
    pairing_counts["padding_context_rows"] = len(frame) - len(in_period)
    health = _evidence(case, "traces.pairing_quality.v1", params, pairing_counts, [], queries, coverage,
                       frame.head(8).to_dict("records"), {"pairing_fraction": "fraction"},
                       EDGE_LIMITATIONS + warnings)
    # Include all source files even though the positioning sample is intentionally tiny.
    health.source_files = sorted(set(frame["_source_file"].map(text)) - {""})
    bundle.evidence.append(health)
    edge_keys = {(edge.caller, edge.callee, edge.operation or "") for edge in selected_edges}
    edge_number = 0
    for key in sorted(groups):
        caller, callee, operation = key
        if selected is not None and caller not in selected and callee not in selected and key not in edge_keys:
            continue
        if edge_number >= MAX_EDGES or time.monotonic() >= deadline:
            bundle.warnings.append("Dependency analysis stopped at edge/deadline cap; remaining edges unknown.")
            break
        edge_number += 1
        pairs = groups[key]
        values = edge_values(pairs, baseline, incident, end)
        if not values["baseline"]["paired_count"] and not values["incident"]["paired_count"]:
            continue  # Context-only pairs are not in-window evidence.
        edge_params = dict(params, caller=caller, callee=callee, operation=operation)
        rows = [row for pair in pairs for row in (pair["parent"], pair["child"])]
        limitations = EDGE_LIMITATIONS + warnings
        if min(values["baseline"]["paired_count"], values["incident"]["paired_count"]) < 20:
            limitations += ["Low pair sample count: gap quantiles and changes are uncertain."]
        if any(values[label]["negative_gap_count"] for label in ("baseline", "incident")):
            limitations += ["Negative start differences observed; clock skew or asynchronous semantics may apply."]
        if len(set().union(*(values[label]["type_pair_counts"] for label in ("baseline", "incident")))) > 1:
            limitations += ["Mixed span-type relationships: start-gap comparison is semantically heterogeneous."]
        record = _evidence(case, "network.start_gap_compare.v1", edge_params, values, sorted({caller, callee}),
                           queries, coverage, rows, {"start_gap_median_s": "seconds", "start_gap_p95_s": "seconds",
                           "paired_count": "count", "start_gap_ratio": "ratio"}, limitations)
        bundle.evidence.append(record)
        bundle.edges.append(DependencyEdge(
            edge_id=stable_id(case.case_key, "m3.edge", {"caller": caller, "callee": callee, "operation": operation,
                                                      "queries": queries, "transform": record.transform,
                                                      "supporting_ids": [record.evidence_id]}),
            caller=caller, callee=callee, operation=operation or None,
            supporting_ids=[record.evidence_id], limitations=limitations))
        ratio = values["start_gap_ratio"]
        enough = min(values["baseline"]["paired_count"], values["incident"]["paired_count"]) >= MIN_COMPARE_SAMPLES
        if enough and ratio is not None and ratio >= 2:
            for component in sorted({caller, callee}):
                bundle.candidates.append(Candidate(
                    candidate_id=stable_id(case.case_key, "m3.candidate", {"component": component, "evidence": record.evidence_id}),
                    component=component, supporting_ids=[record.evidence_id],
                    features={"network.start_gap_ratio": ratio, "network.anomaly_score": min(5.0, ratio - 1),
                              "traces.network_family": True},
                    unresolved=["Caller/callee attribution and network subtype unresolved; start difference is weak evidence."]))
    return bundle


def triage_traces(case: CaseContext, store: TelemetryStore, *, deadline: float) -> AnalysisBundle:
    return _analyze(case, store, deadline)


def inspect_dependencies(case: CaseContext, component_ids: tuple[str, ...],
                         edges: tuple[DependencyEdge, ...], store: TelemetryStore,
                         *, deadline: float) -> AnalysisBundle:
    for edge in edges:
        if not isinstance(edge, DependencyEdge) or not edge.edge_id.startswith("m3.edge:") or not edge.supporting_ids:
            raise ValueError("inspect_dependencies requires actual M3 DependencyEdge objects")
        expected = stable_id(case.case_key, "m3.edge", {
            "caller": edge.caller, "callee": edge.callee, "operation": edge.operation or "",
            "queries": _queries(case), "transform": "network.start_gap_compare.v1",
            "supporting_ids": edge.supporting_ids})
        if edge.edge_id != expected:
            raise ValueError("DependencyEdge does not match this case's M3 producer signature")
    selected = set(component_ids) | {component for edge in edges for component in (edge.caller, edge.callee)}
    if not selected:
        return AnalysisBundle(module="m3", warnings=["No components or dependency edges requested; not queried."])
    # The bounded context query keeps other components because a component-filtered
    # store read would remove parents. Only requested entities produce deep output.
    return _analyze(case, store, deadline, selected, edges)


def replay_evidence(record: EvidenceRecord, store: TelemetryStore, *, deadline: float) -> dict:
    if record.transform.startswith("logs."):
        from .logs import _replay_log_evidence
        return _replay_log_evidence(record, store, deadline=deadline)
    if record.transform not in ("traces.group_compare.v1", "traces.pairing_quality.v1", "network.start_gap_compare.v1"):
        raise ValueError(f"Unsupported M3 evidence transform: {record.transform}")
    params = record.transform_params
    frame, coverage, _, retained = _read_queries(record.queries, store, deadline, params["retained_rows_per_query"])
    if retained != params["retained_rows_per_query"]:
        raise TimeoutError("Replay did not recover the recorded input prefix; values are not verified")
    baseline, incident, end = (params[key] for key in ("baseline_start_s", "incident_start_s", "incident_end_s"))
    frame["_timestamp_s"] = pd.to_numeric(frame["_timestamp_s"], errors="coerce")
    if record.transform == "traces.group_compare.v1":
        mask = ((frame["_component_id"].map(text) == params["component"]) &
                (frame["operation_name"].map(text) == params["operation"]) &
                (frame["type"].map(text) == params["span_type"]))
        return _group_values(frame[mask], baseline, incident, end)
    groups, counts = pair_spans(frame)
    if record.transform == "traces.pairing_quality.v1":
        counts["padding_context_rows"] = int(((frame["_timestamp_s"] < baseline) | (frame["_timestamp_s"] >= end)).sum())
        return counts
    key = (params["caller"], params["callee"], params["operation"])
    return edge_values(groups.get(key, []), baseline, incident, end)
