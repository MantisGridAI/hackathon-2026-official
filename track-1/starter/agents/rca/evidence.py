"""Deterministic evidence prose. Measurements only come from EvidenceRecord."""
from __future__ import annotations

from datetime import datetime
import json

from .contracts import UTC8


def _safe(value):
    return str(value).replace("\r", " ").replace("\n", " ").replace("`", "'")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)


def _compact(value, *, depth=0, max_items=6):
    """Keep exact displayed values; explicitly count undisplayed nested entries."""
    if isinstance(value, dict):
        keys = list(value)
        preferred = ("baseline_n", "baseline_median", "baseline_mad", "window_n", "window_median", "window_max", "strength", "episodes", "comparisons", "count", "matched_count", "duration_p50", "duration_p95")
        keys.sort(key=lambda key: (preferred.index(key) if key in preferred else len(preferred), list(value).index(key)))
        chosen = keys[:max_items]
        result = {key: _compact(value[key], depth=depth + 1, max_items=3 if depth else max_items) for key in chosen}
        if len(keys) > len(chosen):
            result["_omitted_fields"] = len(keys) - len(chosen)
        return result
    if isinstance(value, (list, tuple)):
        result = [_compact(item, depth=depth + 1, max_items=3) for item in value[:2]]
        if len(value) > 2:
            result.append({"_omitted_items": len(value) - 2})
        return result
    if isinstance(value, str) and len(value) > 140:
        return value[:140] + f"… [{len(value) - 140} characters omitted]"
    return value


def _when(value):
    return value.astimezone(UTC8).isoformat() if isinstance(value, datetime) and value.tzinfo is not None else _safe(value)


def render_evidence(case, decision, records, prediction, status, warnings, errors):
    """Render exactly four sections without reading data or editing a decision.

    All referenced records are shown, along with unselected observations involving
    the answer's components. Remaining ledger records are summarized by count;
    their absence from the display is never presented as exclusion.
    """
    lines = ["## Answer", "", prediction, "", "## Confidence", ""]
    reported = getattr(decision, "confidence", "low")
    confidence = "low" if status != "valid" else reported
    lines.append(f"{str(confidence).capitalize()}. Validation: {status}. Validation checks the output contract, not causal correctness.")
    lines.append(f"Stop reason: `{_safe(getattr(decision, 'stop_reason', 'unknown'))}`.")
    lines.append("Occurrence times are estimates from sampled telemetry or explicitly disclosed best guesses; ranking scores are not probabilities.")
    distinct_limitations = list(dict.fromkeys([*warnings, *getattr(decision, "limitations", [])]))
    for warning in distinct_limitations:
        lines.append(f"- Limitation / inference: {_safe(warning)}")
    for error in errors:
        lines.append(f"- Validation error: {_safe(error)}")
    lines.extend(["", "## Evidence", ""])
    referenced = set(getattr(decision, "supporting_ids", []))
    for alternative in getattr(decision, "alternatives", []):
        referenced.update(getattr(alternative, "evidence_ids", []))
    answers = decision.answers if isinstance(decision.answers, list) else []
    components = {answer.component for answer in answers if isinstance(getattr(answer, "component", None), str)}
    mandatory = [record for record in records if record.evidence_id in referenced]
    additional = sorted([record for record in records if record.evidence_id not in referenced and components.intersection(record.component_ids)], key=lambda record: record.evidence_id)
    representatives, rest, groups = [], [], set()
    for record in additional:
        group = (record.kind, record.transform_params.get("family"), tuple(record.component_ids))
        (rest if group in groups else representatives).append(record)
        groups.add(group)
    shown = sorted(mandatory, key=lambda record: record.evidence_id) + (representatives + rest)[:24]
    lines.append(f"Structured ledger: {len(records)} unique records; {len(shown)} relevant records shown once each, including every referenced record and at most 24 additional observations. Omitted records are not exclusions. Shared references are not independent confirmations.")
    if not shown:
        lines.append("No structured supporting observations are available. The answer is an unverified best guess.")
    for record in shown:
        lines.append(f"- Evidence `{_safe(record.evidence_id)}` ({_safe(record.kind)}); components: {_safe(', '.join(record.component_ids) or 'aggregate / unknown')}.")
        lines.append(f"  Interval: `{_when(record.interval[0])}` to `{_when(record.interval[1])}` (end exclusive); transform `{_safe(record.transform)}`.")
        lines.append("  Sources: " + (", ".join(f"`{_safe(path)}`" for path in record.source_files) or "none recorded") + ".")
        for locator in record.source_records[:2]:
            details = [f"record_index={locator.record_index}"] if locator.record_index is not None else []
            details.extend(f"{key}={_safe(getattr(locator, key))}" for key in ("trace_id", "span_id", "log_id") if getattr(locator, key) is not None)
            lines.append(f"  Locator: `{_safe(locator.source_file)}`; {', '.join(details) or 'no record identifier'}.")
        if len(record.source_records) > 2:
            lines.append(f"  {len(record.source_records) - 2} additional locators omitted; full structured ledger retains them.")
        try:
            compact = _compact(record.values)
            lines.append("  Observed / computed values: `" + _safe(_json(compact)) + "`.")
            displayed_units = {key: record.units.get(key, "native/unknown") for key in compact if not key.startswith("_omitted")}
            lines.append("  Units: `" + _safe(_json(displayed_units)) + "`.")
            lines.append("  Method parameters: `" + _safe(_json(_compact(record.transform_params, max_items=3))) + "`.")
        except (TypeError, ValueError):
            lines.append("  Invalid structured values could not be rendered as finite JSON.")
        for query in record.queries[:2]:
            lines.append(f"  Query `{_safe(query.source)}` [{_when(query.start)}, {_when(query.end)}); filters=`{_safe(_json(_compact({'components':query.component_ids,'operations':query.operation_names,'KPIs':query.kpi_names})))}`.")
        if len(record.queries) > 2:
            lines.append(f"  {len(record.queries) - 2} additional queries retained in the full ledger.")
        for coverage in record.coverage:
            lines.append(f"  Coverage `{_safe(coverage.query_id)}`: {coverage.status}; scanned={coverage.rows_scanned}; matched={coverage.rows_matched}; first={_when(coverage.first_time)}; last={_when(coverage.last_time)}.")
            for warning in coverage.warnings:
                lines.append(f"  Coverage limitation: {_safe(warning)}")
        if not record.coverage:
            lines.append("  Coverage unknown: no query coverage was recorded.")
        for limitation in record.limitations[:4]:
            lines.append(f"  Limitation: {_safe(limitation)}")
        if len(record.limitations) > 4:
            lines.append(f"  {len(record.limitations) - 4} additional limitations retained in the full ledger; no exclusion is inferred.")
    lines.append("Missing, empty, partial, failed, and unqueried coverage do not establish health. Native/unknown units are not converted to rates. Cochange or dependency direction alone does not establish causality.")
    lines.append("The complete structured case, decision, queries, units, locators, measurements and limitations are retained in diagnostics/evidence/<invocation_index>.json; abbreviated displays are not complete aggregate inputs.")
    lines.extend(["", "## Ruled out", ""])
    alternatives = getattr(decision, "alternatives", [])
    if not alternatives:
        lines.append("No alternative has been strictly ruled out by the available structured evidence.")
    for alternative in alternatives:
        label = {"excluded": "Reported excluded hypothesis", "weakened": "Weakened hypothesis", "unresolved": "Still unresolved"}.get(getattr(alternative, "status", ""), "Unverified alternative")
        lines.append(f"- {label} `{_safe(getattr(alternative, 'candidate_id', 'unknown'))}`; evidence: {_safe(', '.join(getattr(alternative, 'evidence_ids', [])) or 'none')}. Inference, not a measurement: {_safe(getattr(alternative, 'explanation', ''))}")
    lines.append("Absence of a log match, a normal service aggregate, or incomplete node coverage is not an exclusion.")
    return "\n".join(lines) + "\n"
