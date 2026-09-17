"""Bounded metric investigation and replay through the shared telemetry store.

Resource labels are mechanism hypotheses, never final diagnoses. Unknown metric
semantics/units remain unknown; only explicitly configured counters are differenced.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import math
import re
import time

import pandas as pd

from .contracts import (AnalysisBundle, Candidate, Coverage, EvidenceRecord, QuerySpec,
                        SourceRecord, UTC8, stable_id)
from .onset import DEFAULTS, summarize_series


FAMILIES = frozenset({"cpu", "memory", "read_io", "write_io", "process", "network_hints"})
MAX_WINDOW_ROWS = 250_000
MAX_COMPARISON_COMPONENTS = 24
# No gauge/counter semantics are guaranteed by the official telemetry schema.
# Verified exact KPI names may be added here with a transform-version change.
METRIC_SEMANTICS: dict[str, str] = {}
BASE_TRANSFORM = "metrics.baseline_compare.v2"
SERVICE_TRANSFORM = "metrics.service_summary.v2"
COMPARE_TRANSFORM = "metrics.replica_node_compare.v2"
BASE_TRANSFORMS = {BASE_TRANSFORM, "metrics.baseline_compare.v1"}
SERVICE_TRANSFORMS = {SERVICE_TRANSFORM, "metrics.service_summary.v1"}
COMPARE_TRANSFORMS = {COMPARE_TRANSFORM, "metrics.replica_node_compare.v1"}


def _time(value):
    return datetime.fromtimestamp(float(value), tz=UTC8) if value is not None else None


def _family(kpi):
    key = re.sub(r"[^a-z0-9]+", "_", kpi.lower())
    if any(x in key for x in ("network", "net_", "tcp", "rx_", "tx_", "receive", "transmit", "retrans", "packet", "rtt")):
        return "network_hints"
    if "read" in key and any(x in key for x in ("io", "disk", "fs", "byte", "operation")):
        return "read_io"
    if "write" in key and any(x in key for x in ("io", "disk", "fs", "byte", "operation")):
        return "write_io"
    if any(x in key for x in ("restart", "process", "task_count", "oom", "terminated")):
        return "process"
    if any(x in key for x in ("memory", "mem_", "pgfault", "rss")):
        return "memory"
    if "cpu" in key:
        return "cpu"
    return "unknown"


def _hypothesis(kind, kpi, family, episode):
    """Directional resource hints; ambiguous network subtype remains unresolved."""
    key = kpi.lower()
    increasing = episode["direction"] == "increase"
    # Scheduling-period / throttling counters are symptoms, not CPU utilization.
    # Likewise page faults and cache/mapping growth may be caused by I/O; merely
    # containing "cpu" or "memory" does not identify a resource-load mechanism.
    cpu_auxiliary = any(x in key for x in ("cfs_", "throttl", "period", "limit", "quota", "iowait", "steal"))
    if family == "cpu" and not cpu_auxiliary and (increasing != ("idle" in key)):
        return "node CPU spike" if kind == "node" and episode["spike"] else f"{kind} CPU load"
    memory_auxiliary = any(x in key for x in ("fault", "fail", "cache", "mapped", "limit", "swap"))
    if family == "memory" and not memory_auxiliary:
        if increasing != any(x in key for x in ("free", "available")):
            return "node memory consumption" if kind == "node" else "container memory load"
    if family in {"read_io", "write_io"} and increasing:
        direction = "read" if family == "read_io" else "write"
        return f"node disk {direction} I/O consumption" if kind == "node" else f"container {direction} I/O load"
    if kind == "container" and family == "process":
        if (increasing and any(x in key for x in ("restart", "oom", "terminated"))) or (not increasing and "process" in key):
            return "container process termination"
    if kind == "node" and any(x in key for x in ("disk_space", "disk_usage", "filesystem", "fs_usage")):
        if increasing != ("free" in key or "available" in key):
            return "node disk space consumption"
    return None


def _query(case, source, components=None):
    columns = ("service", "timestamp", "rr", "sr", "mrt", "count") if source == "metric_service" else ("timestamp", "cmdb_id", "kpi_name", "value")
    return QuerySpec(source=source, start=case.reference_start, end=case.end,
                     columns=columns, component_ids=components)


def _read(store, query, deadline, *, retained_rows=None):
    """Consume a bounded window and always close its iterator (including deadlines)."""
    chunks, count, warnings = [], 0, []
    row_limit = MAX_WINDOW_ROWS if retained_rows is None else retained_rows
    if row_limit == 0:
        return pd.DataFrame(columns=[*query.columns, "_source_file", "_record_index", "_timestamp_s", "_component_id"]), deepcopy(store.coverage(query)), []
    iterator = store.iter_window(query, deadline=deadline)
    try:
        for chunk in iterator:
            if time.monotonic() >= deadline:
                warnings.append("metric scan deadline reached; retained samples have partial coverage")
                break
            remaining = row_limit - count
            if len(chunk) > remaining:
                chunks.append(chunk.iloc[:remaining].copy())
                count += remaining
                warnings.append(f"metric window row cap {row_limit} reached")
                break
            chunks.append(chunk.copy())
            count += len(chunk)
            if count >= row_limit:
                warnings.append(f"metric window row cap {row_limit} reached")
                break
    finally:
        close = getattr(iterator, "close", None)
        if close:
            close()
    coverage = deepcopy(store.coverage(query))
    if warnings:
        coverage.status = "partial"
        coverage.warnings.extend(warnings)
    return pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame(), coverage, warnings


def _sources(frame):
    if frame.empty:
        return [], []
    files = sorted(str(x) for x in frame["_source_file"].dropna().unique())
    # Locator examples include earliest/latest plus an extreme value; full input
    # is always recovered by queries, never by treating these as the aggregate.
    examples = frame.sort_values("_timestamp_s", kind="stable")
    indices = [0, len(examples) // 2, len(examples) - 1]
    locators, seen = [], set()
    for index in indices:
        row = examples.iloc[index]
        record_index = int(row["_record_index"]) if pd.notna(row["_record_index"]) else None
        key = str(row["_source_file"]), record_index
        if key not in seen:
            locators.append(SourceRecord(source_file=key[0], record_index=key[1]))
            seen.add(key)
    return files, locators


def _units(values):
    result = {}
    for key, value in values.items():
        if key.endswith("_n") or key.endswith("_samples") or key.endswith("_count") or key in {"counter_resets", "invalid_samples", "duplicate_samples"}:
            result[key] = "count"
        elif key.endswith("_timestamp_s") or key in {"first_change_s", "last_change_s", "onset_estimate_s"}:
            result[key] = "epoch seconds"
        elif key.endswith("_s"):
            result[key] = "seconds"
        elif key in {"strength", "effect_ratio", "reference_sensitive", "anomalous_samples"}:
            result[key] = "dimensionless" if key != "anomalous_samples" else "count"
        elif isinstance(value, (float, int)) or value is None:
            result[key] = "native/unknown"
        elif key == "episodes":
            result[key] = "mixed; timestamps=epoch seconds, persistence_s=seconds, samples=count, strength=dimensionless, values=native/unknown"
        else:
            result[key] = "categorical"
    return result


def _record(case, transform, params, values, queries, coverages, frame, components, limitations):
    files, locators = _sources(frame)
    payload = {"transform": transform, "params": params, "values": values,
               "queries": queries, "coverage": coverages, "components": components}
    return EvidenceRecord(evidence_id=stable_id(case.case_key, "m2.evidence", payload),
        kind="metric", component_ids=list(components), interval=(case.reference_start, case.end),
        source_files=files, queries=list(queries), transform=transform, transform_params=params,
        values=values, units=_units(values), source_records=locators,
        coverage=deepcopy(coverages), limitations=list(limitations))


def _summary(frame, params):
    samples = zip(frame["_timestamp_s"].tolist(), frame["value"].tolist())
    return summarize_series(samples, params["start_s"], params["end_s"],
                            semantics=params["semantics"], params=params["thresholds"])


def _limitations(values, coverage):
    items = ["Reference window may already be abnormal; resource cochange does not prove causality.",
             "KPI units and gauge/counter semantics are unconfirmed; raw values are preserved.",
             "Related KPIs are correlated observations, not independent confirmations."]
    if values["baseline_n"] < DEFAULTS["minimum_reference_samples"]:
        items.append("Reference has fewer than three samples; anomaly strength is weak or unavailable.")
    if values["window_n"] < 2:
        items.append("Incident has fewer than two samples; persistence is unknown.")
    if values["reference_sensitive"]:
        items.append("Earlier and recent reference medians differ; result is reference-sensitive.")
    if values["gap_count"]:
        items.append("Sampling gaps widen onset uncertainty and split sustained episodes.")
    if values["invalid_samples"]:
        items.append("Non-numeric or non-finite samples were excluded.")
    if values["duplicate_samples"]:
        items.append("Duplicate timestamps were combined by median, not counted as independent samples.")
    if coverage.status != "complete":
        items.append(f"Query coverage is {coverage.status}; absence of a signal cannot establish health.")
    return items


def _resource_records(case, frame, query, coverage, families, deadline):
    output, warnings = [], []
    if frame.empty:
        return output, warnings
    unknown = int(frame["_component_id"].isna().sum())
    if unknown:
        warnings.append(f"{query.source}: {unknown} records lack canonical component mapping")
    for (component, raw_id, kpi), group in frame.dropna(subset=["_component_id", "kpi_name"]).groupby(["_component_id", "cmdb_id", "kpi_name"], sort=True):
        if time.monotonic() >= deadline:
            warnings.append("metric analysis deadline reached; not all observed series were analysed")
            break
        family = _family(str(kpi))
        if families is not None and family not in families:
            continue
        params = {"start_s": case.start.timestamp(), "end_s": case.end.timestamp(),
                  "component": str(component), "raw_cmdb_id": str(raw_id), "kpi_name": str(kpi),
                  "family": family, "semantics": METRIC_SEMANTICS.get(str(kpi), "unknown"),
                  "thresholds": dict(DEFAULTS), "duplicates": "median per timestamp",
                  "max_window_rows": MAX_WINDOW_ROWS, "retained_rows_per_query": [len(frame)]}
        values = _summary(group, params)
        record = _record(case, BASE_TRANSFORM, params, values, [query], [coverage], group,
                         [str(component)], _limitations(values, coverage))
        output.append((record, family))
    # All scanned series have already been analysed; retaining the results avoids
    # silently removing component/family hypotheses before M4's explicit shortlist.
    output.sort(key=lambda item: (-(item[0].values["strength"] or 0), item[0].evidence_id))
    return output, warnings


def _candidates(case, record, family, kind):
    values, params = record.values, record.transform_params
    component = record.component_ids[0]
    candidates = []
    for episode in values["episodes"]:
        interval = episode["onset_interval_s"]
        interval = tuple(_time(t) for t in interval) if interval else None
        estimate = _time(episode["onset_estimate_s"])
        baseline = values["baseline_median"]
        ratio = episode["peak_value"] / baseline if baseline not in (None, 0) else None
        # Robust z-scores have a different ceiling when MAD is zero. They are
        # useful detectors, but must not be compared directly across KPIs. Use
        # the same symmetric, noise-floored relative effect for every sequence;
        # it is invariant to the KPI's native scale and bounded in [0, 10].
        peak = episode["peak_value"]
        noise = 1.4826 * (values["baseline_mad"] or 0.0) * DEFAULTS["robust_threshold"]
        reference_edge = values.get("baseline_p90" if episode["direction"] == "increase" else "baseline_p10")
        ordinary_change = max(noise, abs(reference_edge - baseline)) if reference_edge is not None and baseline is not None else noise
        scale = max(abs(baseline or 0.0), abs(peak), ordinary_change)
        novel_change = max(0.0, abs(peak - baseline) - ordinary_change) if baseline is not None else 0.0
        calibrated = min(10.0, 10.0 * novel_change / scale) if scale else 0.0
        features = {
            "metrics.strength": episode["strength"], "metrics.family": family,
            "metrics.calibrated_strength": calibrated,
            "metrics.strength_method": "reference_envelope_relative.v2",
            "metrics.reference_variation": ordinary_change,
            "metrics.direction": episode["direction"], "metrics.persistence_samples": episode["persistence_samples"],
            "metrics.persistence_s": episode["persistence_s"], "metrics.sampling_interval_s": values["sampling_interval_s"],
            "metrics.baseline_n": values["baseline_n"], "metrics.window_n": values["window_n"],
            "metrics.effect_ratio": ratio if ratio is None or math.isfinite(ratio) else None,
            "metrics.spike": episode["spike"], "metrics.kpi_name": params["kpi_name"],
            "metrics.raw_cmdb_id": params["raw_cmdb_id"], "metrics.correlation_group": f"{component}:{family}",
            "metrics.reference_sensitive": values["reference_sensitive"],
            "metrics.first_change_s": episode["first_change_s"],
        }
        unresolved = ["Resource mechanism is a hypothesis; metrics alone do not establish root cause.",
                      "Sampled ordering does not prove causal precedence.", *record.limitations]
        if episode["gap_before"]:
            unresolved.append("A gap before the episode prevents precise onset placement.")
        reason = _hypothesis(kind, params["kpi_name"], family, episode)
        features["metrics.mechanism_supported"] = reason is not None
        if reason is None:
            unresolved.append("This metric does not identify a legal fault mechanism or network subtype.")
        candidates.append(Candidate(
            candidate_id=stable_id(case.case_key, "m2.candidate", {"evidence_id": record.evidence_id, "episode": episode, "reason": reason}),
            component=component, reason=reason, onset_interval=interval, onset_estimate=estimate,
            features=features, supporting_ids=[record.evidence_id], unresolved=unresolved,
            episode_id=stable_id(case.case_key, "m2.episode", {"component": component, "first": episode["first_change_s"], "last": episode["last_change_s"]})))
    return candidates


def _service_records(case, frame, query, coverage, deadline):
    records = []
    if frame.empty:
        return records
    for service, group in frame.groupby("service", sort=True):
        if time.monotonic() >= deadline:
            break
        for field in ("rr", "sr", "mrt", "count"):
            values_frame = group.rename(columns={field: "value"})
            params = {"service": str(service), "field": field, "start_s": case.start.timestamp(),
                      "end_s": case.end.timestamp(), "semantics": "unknown", "thresholds": dict(DEFAULTS),
                      "max_window_rows": MAX_WINDOW_ROWS, "retained_rows_per_query": [len(frame)]}
            values = _summary(values_frame, params)
            records.append(_record(case, SERVICE_TRANSFORM, params, values, [query], [coverage], group, [],
                ["Service aggregates only prioritize inspection; they cannot exonerate an individual pod.",
                 "rr/sr/mrt/count units and aggregation semantics are not assumed.", *_limitations(values, coverage)]))
    return records


def _analyse(case, store, sources, components, families, deadline):
    bundle = AnalysisBundle(module="m2")
    service_hints = {}
    for index, source in enumerate(sources):
        query = _query(case, source, components)
        # Work-conserving budget: a cold first read can borrow unused time, while
        # later sources and in-memory analysis retain a small bounded reserve.
        # Equal slices formerly cut the tiny service source before its first
        # chunk during cold filesystem stalls despite ample module time left.
        remaining_sources = len(sources) - index
        remaining = max(0.0, deadline - time.monotonic())
        reserve = sum(2. if next_source == 'metric_container' else .5 for next_source in sources[index + 1:])
        source_deadline = deadline - min(remaining * .25, reserve)
        source_remaining = max(0., source_deadline - time.monotonic())
        read_deadline = source_deadline - min(1., source_remaining * .15)
        frame, coverage, warnings = _read(store, query, read_deadline)
        bundle.coverage.append(coverage)
        bundle.warnings.extend(warnings)
        if coverage.status != "complete":
            bundle.warnings.append(f"{source}: {coverage.status}; missing/partial coverage is not health")
        if source == "metric_service":
            records = _service_records(case, frame, query, coverage, source_deadline)
            bundle.evidence.extend(records)
            for record in records:
                service = record.transform_params["service"]
                if service not in service_hints or (record.values["strength"] or 0) > service_hints[service][0]:
                    service_hints[service] = (record.values["strength"] or 0, record.evidence_id)
            continue
        records, warnings = _resource_records(case, frame, query, coverage, families, source_deadline)
        bundle.warnings.extend(warnings)
        catalog = store.component_catalog()
        for record, family in records:
            bundle.evidence.append(record)
            component = catalog.components.get(record.component_ids[0])
            kind = component.kind if component else ("node" if source == "metric_node" else "container")
            candidates = _candidates(case, record, family, kind)
            if component and component.service in service_hints:
                score, evidence_id = service_hints[component.service]
                for candidate in candidates:
                    candidate.features["metrics.service_strength"] = score
                    candidate.features["metrics.service_evidence_id"] = evidence_id
            bundle.candidates.extend(candidates)
    bundle.candidates.sort(key=lambda candidate: (-candidate.features["metrics.strength"], candidate.candidate_id))
    return bundle


def triage_metrics(case, store, *, deadline):
    """Service clues plus all observed container/node resources; no service filter."""
    return _analyse(case, store, ("metric_service", "metric_container", "metric_node"), None, None, deadline)


def inspect_metrics(case, component_ids, metric_families, store, *, deadline):
    unknown = set(metric_families) - FAMILIES
    if unknown:
        raise ValueError(f"Unknown metric families: {sorted(unknown)}")
    sources = ["metric_container", "metric_node"]
    if "process" in metric_families:
        sources.append("metric_runtime")
    return _analyse(case, store, tuple(sources), tuple(component_ids), set(metric_families), deadline)


def _comparison_values(records, target, relationships):
    """Same KPI contrasts only. Native values from different KPIs are never summed."""
    series = [{"component": r.component_ids[0], "kpi_name": r.transform_params["kpi_name"],
               "family": r.transform_params["family"], "values": r.values} for r in records]
    comparisons = []
    for left in series:
        if left["component"] != target:
            continue
        for right in series:
            if right["component"] == target or right["kpi_name"] != left["kpi_name"]:
                continue
            a, b = left["values"], right["values"]
            av, bv = a["window_median"], b["window_median"]
            ratio = av / bv if av is not None and bv not in (None, 0) else None
            a_interval = a["episodes"][0]["onset_interval_s"] if a["episodes"] else None
            b_interval = b["episodes"][0]["onset_interval_s"] if b["episodes"] else None
            uncertain = not a_interval or not b_interval or not (a_interval[1] < b_interval[0] or b_interval[1] < a_interval[0])
            comparisons.append({"target": target, "peer": right["component"], "kpi_name": left["kpi_name"],
                "relation": relationships.get(right["component"], "observed peer"),
                "target_window_median": av, "peer_window_median": bv,
                "median_difference": av - bv if av is not None and bv is not None else None,
                "median_ratio": ratio if ratio is None or math.isfinite(ratio) else None,
                "both_changed": bool(a["episodes"] and b["episodes"]) if min(a["baseline_n"], b["baseline_n"], a["window_n"], b["window_n"]) >= 2 else None,
                "ordering_uncertain": bool(uncertain),
                "target_onset_interval_s": a_interval, "peer_onset_interval_s": b_interval})
    node_cochange = []
    target_families = {s["family"] for s in series if s["component"] == target and s["values"]["episodes"]}
    for other in series:
        if relationships.get(other["component"]) == "node" and other["family"] in target_families:
            node_cochange.append({"component": other["component"], "family": other["family"],
                                  "kpi_name": other["kpi_name"],
                                  "changed": bool(other["values"]["episodes"]) if min(other["values"]["baseline_n"], other["values"]["window_n"]) >= 2 else None,
                                  "ordering_uncertain": True})
    return {"comparisons": comparisons, "node_cochange": node_cochange,
            "observed_components": sorted({s["component"] for s in series}),
            "analysed_series_count": len(series)}


def compare_replicas_and_node(case, component, store, *, deadline):
    catalog = store.component_catalog()
    target = catalog.components.get(component)
    if target is None:
        return AnalysisBundle(module="m2", warnings=[f"Unknown catalog component {component}; no topology was guessed."])
    relations = {}
    for key, other in sorted(catalog.components.items()):
        if key == component:
            continue
        if target.node_id and key == target.node_id:
            relations[key] = "node"
        elif target.service and other.service == target.service and other.kind == "container":
            relations[key] = "same service replica"
        elif target.node_id and other.node_id == target.node_id and other.kind == "container":
            relations[key] = "same node different service"
        elif target.kind == "node" and other.node_id == component:
            relations[key] = "resident pod"
    ordered = sorted(relations, key=lambda key: (relations[key] != "node", relations[key] != "same service replica", key))
    selected = (component, *ordered[:MAX_COMPARISON_COMPONENTS - 1])
    bundle = _analyse(case, store, ("metric_container", "metric_node"), selected, None, deadline)
    records = [record for record in bundle.evidence if record.transform == BASE_TRANSFORM]
    values = _comparison_values(records, component, relations)
    queries = [_query(case, source, selected) for source in ("metric_container", "metric_node")]
    params = {"target": component, "relationships": {key: relations[key] for key in selected if key in relations},
              "series": [{"params": record.transform_params, "query_index": queries.index(record.queries[0])} for record in records],
              "retained_rows_per_query": [max((record.transform_params["retained_rows_per_query"][0] for record in records if record.queries[0] == query), default=0) for query in queries],
              "max_window_rows": MAX_WINDOW_ROWS}
    limitations = ["Observed topology can be incomplete; unobserved replicas are not exonerated.",
                  "Same-node cochange supports a node hypothesis but does not establish causality.",
                  "Overlapping sampled onset intervals cannot establish causal ordering."]
    if len(ordered) >= MAX_COMPARISON_COMPONENTS:
        limitations.append(f"Comparison capped at {MAX_COMPARISON_COMPONENTS} observed components.")
    record = _record(case, COMPARE_TRANSFORM, params, values, queries, bundle.coverage,
                     pd.DataFrame(), selected, limitations)
    record.source_files = sorted({path for child in records for path in child.source_files})
    record.source_records = [source for child in records[:4] for source in child.source_records[:1]]
    record.units = {"comparisons": "medians/differences=native/unknown, ratios=dimensionless, onset=epoch seconds",
                    "node_cochange": "categorical", "observed_components": "categorical", "analysed_series_count": "count"}
    bundle.evidence.append(record)
    for candidate in bundle.candidates:
        if candidate.component != component:
            continue
        contrasts = [item["median_ratio"] for item in values["comparisons"] if item["relation"] == "same service replica" and item["kpi_name"] == candidate.features["metrics.kpi_name"] and item["median_ratio"] is not None]
        candidate.features["metrics.replica_contrast"] = max(contrasts) if contrasts else None
        node_statuses = [item["changed"] for item in values["node_cochange"] if item["family"] == candidate.features["metrics.family"] and item["changed"] is not None]
        candidate.features["metrics.node_cochange"] = any(node_statuses) if node_statuses else None
        candidate.features["metrics.ordering_uncertain"] = True
        candidate.supporting_ids.append(record.evidence_id)
        candidate.unresolved.extend(limitations)
        candidate.candidate_id = stable_id(case.case_key, "m2.candidate", {"base_candidate": candidate.candidate_id, "comparison": record.evidence_id})
    return bundle


def replay_evidence(record, store, *, deadline):
    """Recompute the recorded values from saved queries and transformation settings."""
    if record.transform not in BASE_TRANSFORMS | SERVICE_TRANSFORMS | COMPARE_TRANSFORMS:
        raise ValueError(f"Unsupported metric transform: {record.transform}")
    frames = []
    retained = record.transform_params.get("retained_rows_per_query")
    if retained is None or len(retained) != len(record.queries) or any(not isinstance(count, int) or count < 0 for count in retained):
        raise ValueError("Metric replay requires the recorded input prefix length for every query")
    for query, count in zip(record.queries, retained):
        frame, coverage, _ = _read(store, query, deadline, retained_rows=count)
        frames.append(frame)
        if len(frame) != count:
            raise ValueError(f"Metric replay retained {len(frame)} rows but requires the original {count}-row prefix ({coverage.status})")
    if record.transform in COMPARE_TRANSFORMS:
        reconstructed = []
        for specification in record.transform_params["series"]:
            params = specification["params"]
            frame = _select(frames[specification["query_index"]], params)
            reconstructed.append(EvidenceRecord(evidence_id="replay", kind="metric", component_ids=[params["component"]],
                interval=record.interval, transform_params=params, values=_summary(frame, params)))
        return _comparison_values(reconstructed, record.transform_params["target"], record.transform_params["relationships"])
    frame = frames[0]
    params = record.transform_params
    if record.transform in SERVICE_TRANSFORMS:
        frame = frame[frame["service"].astype(str) == params["service"]].rename(columns={params["field"]: "value"})
    else:
        frame = _select(frame, params)
    return _summary(frame, params)


def _select(frame, params):
    if frame.empty:
        return pd.DataFrame(columns=["_timestamp_s", "value"])
    return frame[(frame["_component_id"].astype(str) == params["component"]) &
                 (frame["cmdb_id"].astype(str) == params["raw_cmdb_id"]) &
                 (frame["kpi_name"].astype(str) == params["kpi_name"])]
