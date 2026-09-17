"""Stateless rca-v1 output validation; only M4 may choose a fallback decision."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import PurePosixPath

from run import format_prediction
from .contracts import (Answer, CONTAINER_REASONS, Decision, LEGAL_REASONS,
                        NODE_REASONS, RenderedResult, UTC8)
from .evidence import render_evidence


FIELDS = ("datetime", "component", "reason")


def _source_path(value):
    path = PurePosixPath(str(value).replace("\\", "/"))
    parts = path.parts
    return (not path.is_absolute() and ".." not in parts and len(parts) == 4 and
            parts[0] == "telemetry" and parts[2] in {"metric", "trace", "log"} and
            parts[3] in {"metric_container.csv", "metric_node.csv", "metric_service.csv", "metric_runtime.csv", "metric_mesh.csv", "trace_span.csv", "log_service.csv", "log_proxy.csv"})


def validate_and_render(case, decision, evidence, catalog):
    errors, warnings = [], []
    if not isinstance(decision, Decision):
        decision = Decision(answers=[], limitations=["Invalid Decision shape received"])
        errors.append("Decision must be the shared Decision type")
    requested = case.requested_fields
    if not requested or any(field not in FIELDS for field in requested) or tuple(field for field in FIELDS if field in requested) != requested:
        errors.append("Requested fields are not a nonempty ordered subset of datetime/component/reason")
    if not isinstance(decision.answers, list):
        errors.append("Decision.answers must be a list")
        answers = []
    else:
        answers = decision.answers
    if len(answers) != case.failure_count:
        errors.append(f"Expected exactly {case.failure_count} failures, received {len(answers)}")
    if decision.confidence not in {"low", "medium", "high"}:
        errors.append("Confidence must be low, medium, or high")
    canonical = catalog.components if catalog else {}
    raw_aliases = {raw: targets for (_, raw), targets in catalog.raw_to_components.items()} if catalog else {}
    services = {component.service for component in canonical.values() if component.service}
    formatted = []
    for index, answer in enumerate(answers, 1):
        if not isinstance(answer, Answer):
            errors.append(f"Answer {index} must be the shared Answer type")
            continue
        row = {}
        for field in requested:
            value = getattr(answer, field, None)
            if value is None or value == "":
                errors.append(f"Answer {index} lacks required {field}")
            if field == "datetime":
                if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                    errors.append(f"Answer {index} datetime must be aware")
                else:
                    if not case.start <= value < case.end:
                        errors.append(f"Answer {index} datetime is outside the incident interval")
                    row[field] = value.astimezone(UTC8).strftime("%Y-%m-%d %H:%M:%S")
            elif value is not None:
                if not isinstance(value, str) or not value.strip() or any(char in value for char in ("\n", "\r")):
                    errors.append(f"Answer {index} {field} must be a nonempty single-line string")
                else:
                    row[field] = value
        if answer.reason is not None and (not isinstance(answer.reason, str) or answer.reason not in LEGAL_REASONS):
            errors.append(f"Answer {index} has an illegal reason")
        if "component" in requested and isinstance(answer.component, str) and answer.component:
            if answer.component not in canonical:
                if answer.component in raw_aliases and answer.component not in raw_aliases[answer.component]:
                    errors.append(f"Answer {index} component is a raw alias rather than an observed canonical ID")
                elif answer.component in services:
                    errors.append(f"Answer {index} component is an aggregate service, not an observed node/pod ID")
                else:
                    warnings.append(f"Answer {index} component {answer.component} is not verified by the observed catalog; low-confidence best guess")
        component = canonical.get(answer.component) if isinstance(answer.component, str) else None
        if component and answer.reason is not None:
            allowed = NODE_REASONS if component.kind == "node" else CONTAINER_REASONS
            if answer.reason not in allowed:
                errors.append(f"Answer {index} reason conflicts with known {component.kind} component")
        formatted.append(row)
    # Sort only emitted fields; do not invent hidden onset times or mutate answers.
    if "datetime" in requested:
        formatted.sort(key=lambda item: (item.get("datetime", ""), item.get("component", ""), item.get("reason", "")))
    elif case.failure_count > 1:
        formatted.sort(key=lambda item: (item.get("component", ""), item.get("reason", "")))
        warnings.append("Occurrence time is not requested; fault order is deterministic and does not assert causal chronology")
    ledger = {}
    for record in evidence:
        eid = getattr(record, "evidence_id", None)
        if not isinstance(eid, str) or not eid:
            errors.append("Evidence record has no valid evidence_id")
            continue
        if eid in ledger:
            if asdict(ledger[eid]) != asdict(record):
                errors.append(f"Conflicting contents for evidence ID {eid}")
            continue
        ledger[eid] = record
        try:
            json.dumps(record.values, allow_nan=False)
            json.dumps(record.transform_params, allow_nan=False)
        except (ValueError, TypeError):
            errors.append(f"Evidence {eid} has non-finite or non-JSON values")
        if any(not _source_path(path) for path in record.source_files):
            errors.append(f"Evidence {eid} has a non-telemetry or unsafe source path")
        for locator in record.source_records:
            if not _source_path(locator.source_file) or locator.source_file not in record.source_files:
                errors.append(f"Evidence {eid} has an unsupported source locator")
            if locator.record_index is not None and (not isinstance(locator.record_index, int) or locator.record_index < 1):
                errors.append(f"Evidence {eid} has an invalid original record index")
    referenced = list(decision.supporting_ids)
    for alternative in decision.alternatives:
        if alternative.status not in {"weakened", "unresolved", "excluded"}:
            errors.append(f"Alternative {alternative.candidate_id} has an invalid status")
        if alternative.status == "excluded" and not alternative.evidence_ids:
            errors.append(f"Excluded alternative {alternative.candidate_id} has no evidence")
        referenced.extend(alternative.evidence_ids)
    for eid in sorted(set(referenced)):
        if eid not in ledger:
            errors.append(f"Dangling evidence reference {eid}")
    if not decision.supporting_ids:
        warnings.append("No structured support was selected; answer remains a best guess")
    for eid in decision.supporting_ids:
        if eid not in ledger:
            continue
        record = ledger[eid]
        if record.kind != "coverage" and (not record.source_files or not record.queries or not record.transform):
            warnings.append(f"Evidence {eid} lacks reproducible source/query/transform provenance")
        if not record.coverage:
            warnings.append(f"Evidence {eid} has no measured coverage")
        for coverage in record.coverage:
            if coverage.status != "complete":
                warnings.append(f"Evidence {eid} coverage is {coverage.status}; no health conclusion follows")
    errors, warnings = list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))
    status = "invalid" if errors else "degraded" if warnings else "valid"
    prediction = format_prediction(formatted)
    rendered = render_evidence(case, decision, list(ledger.values()), prediction, status, warnings, errors)
    return RenderedResult(prediction, rendered, status, warnings, errors)
