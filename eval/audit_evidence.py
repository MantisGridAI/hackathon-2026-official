#!/usr/bin/env python3
"""Offline structured-evidence audit using M1 reads and M2/M3 replay only."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "track-1" / "starter"))
from agents.rca.contracts import Coverage, EvidenceRecord, QuerySpec, SourceRecord
from agents.rca.data_access import CSVTelemetryStore
from agents.rca.validation import _source_path


def _datetime(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def decode_record(value):
    """Deserialize the shared contract, without adding a second runtime schema."""
    fields = dict(value)
    fields["interval"] = tuple(_datetime(item) for item in fields["interval"])
    queries = []
    for raw in fields.get("queries", []):
        query = dict(raw)
        query["start"], query["end"] = _datetime(query["start"]), _datetime(query["end"])
        for key in ("columns", "component_ids", "operation_names", "kpi_names"):
            if query.get(key) is not None:
                query[key] = tuple(query[key])
        queries.append(QuerySpec(**query))
    fields["queries"] = queries
    coverages = []
    for raw in fields.get("coverage", []):
        coverage = dict(raw)
        for key in ("first_time", "last_time"):
            coverage[key] = _datetime(coverage.get(key))
        coverages.append(Coverage(**coverage))
    fields["coverage"] = coverages
    fields["source_records"] = [SourceRecord(**raw) for raw in fields.get("source_records", [])]
    return EvidenceRecord(**fields)


def _same(left, right, *, relative_tolerance=1e-10):
    if isinstance(left, (int, float)) and not isinstance(left, bool) and isinstance(right, (int, float)) and not isinstance(right, bool):
        import math
        return math.isfinite(left) and math.isfinite(right) and math.isclose(left, right, rel_tol=relative_tolerance, abs_tol=1e-12)
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _locators(record, store, deadline):
    pending = {(locator.source_file, locator.record_index, locator.trace_id, locator.span_id, locator.log_id) for locator in record.source_records}
    if not pending:
        return {"status": "unverified", "required": 0, "found": 0, "reason": "No source locators recorded"}
    found = set()
    for query in record.queries:
        iterator = store.iter_window(query, deadline=deadline)
        try:
            for chunk in iterator:
                for locator in pending - found:
                    source, index, trace, span, log = locator
                    mask = chunk["_source_file"] == source
                    if index is not None:
                        mask &= chunk["_record_index"] == index
                    for column, value in (("trace_id", trace), ("span_id", span), ("log_id", log)):
                        if value is not None:
                            mask &= (chunk[column].astype(str) == str(value)) if column in chunk else False
                    if bool(mask.any()):
                        found.add(locator)
                if found == pending or time.monotonic() >= deadline:
                    break
        finally:
            close = getattr(iterator, "close", None)
            if close:
                close()
        if found == pending or time.monotonic() >= deadline:
            break
    return {"status": "verified" if found == pending else "unverified", "required": len(pending), "found": len(found)}


def audit_evidence_ledger(ledger_path, dataset, *, samples_per_transform=1, deadline=None):
    if samples_per_transform < 1:
        raise ValueError("samples_per_transform must be positive")
    deadline = time.monotonic() + 120 if deadline is None else deadline
    ledger = json.loads(Path(ledger_path).read_text(encoding="utf-8"))
    raw_records = ledger.get("evidence", [])
    records, errors = [], []
    seen = {}
    for raw in raw_records:
        record = decode_record(raw)
        if record.evidence_id in seen:
            if seen[record.evidence_id] != raw:
                errors.append(f"Conflicting evidence ID {record.evidence_id}")
            continue
        seen[record.evidence_id] = raw
        records.append(record)
    decision = ledger.get("decision", {})
    references = list(decision.get("supporting_ids", []))
    for alternative in decision.get("alternatives", []):
        references.extend(alternative.get("evidence_ids", []))
    dangling = sorted(set(references) - seen.keys())
    if dangling:
        errors.append(f"Dangling evidence references: {dangling}")
    root = Path(dataset).resolve()
    checked_files = {}
    for record in records:
        for source in record.source_files:
            if source in checked_files:
                continue
            safe = _source_path(source)
            resolved = (root / source).resolve()
            exists = safe and resolved.is_relative_to(root) and resolved.is_file()
            checked_files[source] = bool(exists)
            if not exists:
                errors.append(f"Missing or unsafe source file {source}")
    groups = defaultdict(list)
    for record in sorted(records, key=lambda item: item.evidence_id):
        groups[record.transform].append(record)
    selected = [record for transform in sorted(groups) for record in groups[transform][:samples_per_transform]]
    store = CSVTelemetryStore(root)
    audits = []
    for record in selected:
        if time.monotonic() >= deadline:
            audits.append({"evidence_id": record.evidence_id, "transform": record.transform, "status": "unverified", "reason": "Audit deadline exhausted"})
            continue
        try:
            if record.transform.startswith("metrics."):
                from agents.rca.metrics import replay_evidence
            elif record.transform.startswith(("traces.", "network.", "logs.")):
                from agents.rca.traces import replay_evidence
            else:
                raise ValueError("Unsupported replay transform")
            values = replay_evidence(record, store, deadline=deadline)
            matches = _same(record.values, values)
            locators = _locators(record, store, deadline)
            status = "verified" if matches and locators["status"] == "verified" else "failed" if not matches else "unverified"
            audits.append({"evidence_id": record.evidence_id, "transform": record.transform, "status": status,
                           "values_match": matches, "source_locators": locators,
                           "units_status": "Declared units preserved; physical unit semantics require separate review"})
        except Exception as exc:
            audits.append({"evidence_id": record.evidence_id, "transform": record.transform,
                           "status": "unverified", "reason": type(exc).__name__ + ": " + str(exc)})
    return {"schema_version": "evidence-audit.v1", "ledger": str(ledger_path), "case_key": ledger.get("case_key"),
            "invocation_index": ledger.get("invocation_index"), "records": len(records),
            "sampling_method": "First N evidence IDs in lexicographic order within each transform, transforms sorted",
            "samples_per_transform": samples_per_transform, "selected_count": len(selected),
            "source_files_checked": checked_files, "dangling_evidence_ids": dangling, "integrity_errors": errors,
            "sampled_records": audits, "values_tolerance": {"relative": 1e-10, "absolute": 1e-12},
            "all_sampled_verified": not errors and bool(audits) and all(item["status"] == "verified" for item in audits),
            "causal_inference_review": "Not performed. Record/replay verification does not establish causal support."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--samples-per-transform", type=int, default=1)
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = audit_evidence_ledger(args.ledger, args.dataset, samples_per_transform=args.samples_per_transform,
                                   deadline=time.monotonic() + args.seconds)
    encoded = json.dumps(result, indent=2, allow_nan=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
