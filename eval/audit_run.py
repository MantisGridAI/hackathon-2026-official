#!/usr/bin/env python3
"""Audit a frozen full denominator using the unchanged official evaluator."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
STARTER = ROOT / "track-1" / "starter"
if str(STARTER) not in sys.path:
    sys.path.insert(0, str(STARTER))
from cost import PRICES
from score import DIFFICULTY, evaluate
from agents.rca.runtime import parse_case


def _load(value):
    return json.loads(Path(value).read_text(encoding="utf-8")) if isinstance(value, (str, Path)) else value


def _csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def _jsonl(path, errors):
    if not path.exists():
        return []
    records = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("record is not an object")
            records.append(value)
        except (ValueError, TypeError):
            errors.append(f"Malformed JSONL at {path.name}:{index}")
    return records


def _stats(values):
    values = sorted(value for value in values if isinstance(value, (int, float)) and math.isfinite(value))
    if not values:
        return {"count": 0, "mean": None, "median": None, "p95": None, "max": None}
    return {"count": len(values), "mean": statistics.mean(values), "median": statistics.median(values),
            "p95": values[max(0, math.ceil(.95 * len(values)) - 1)], "max": max(values)}


def _id(value):
    if isinstance(value, bool):
        raise ValueError("Boolean row ID")
    return int(str(value))


def _usage(records, prices):
    models = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "unknown_usage_calls": 0})
    incomplete = False
    for record in records:
        if not isinstance(record.get("models", {}), dict):
            incomplete = True
            continue
        for model, counts in record.get("models", {}).items():
            if not isinstance(counts, dict):
                incomplete = True
                continue
            calls = counts.get("calls")
            if not isinstance(calls, (int, float)) or isinstance(calls, bool) or not math.isfinite(calls) or calls < 0:
                incomplete = True
            if isinstance(calls, (int, float)) and calls > 0 and any(key not in counts for key in ("prompt_tokens", "completion_tokens")):
                incomplete = True
            for key in models[model]:
                value = counts.get(key, 0)
                if value is None or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    incomplete = True
                else:
                    models[model][key] += value
    known_cost = 0.0
    per_model_cost = {}
    for model, counts in models.items():
        if model not in prices:
            incomplete = True
            per_model_cost[model] = None
            continue
        pin, pout = prices[model]
        observed = (counts["prompt_tokens"] * pin + counts["completion_tokens"] * pout) / 1e6
        known_cost += observed
        per_model_cost[model] = None if counts["unknown_usage_calls"] else observed
        incomplete |= counts["unknown_usage_calls"] > 0
    return dict(models), known_cost, per_model_cost, incomplete


def _prediction_shape(prediction, instruction=None):
    try:
        raw = prediction.strip()
        if raw.startswith("```json") and raw.endswith("```"):
            raw = raw[7:-3].strip()
        value = json.loads(raw)
        if not isinstance(value, dict) or not value or list(value) != [str(index) for index in range(1, len(value) + 1)]:
            return False
        fields = ("root cause occurrence datetime", "root cause component", "root cause reason")
        shape = all(isinstance(answer, dict) and answer and list(answer) == [field for field in fields if field in answer] and
                   all(isinstance(item, str) and item and "\n" not in item and "\r" not in item for item in answer.values())
                   for answer in value.values())
        if instruction:
            case = parse_case(instruction)
            names = dict(zip(("datetime", "component", "reason"), fields))
            expected = [names[field] for field in case.requested_fields]
            shape = shape and len(value) == case.failure_count and all(list(answer) == expected for answer in value.values())
        return shape
    except (ValueError, TypeError):
        return False


def audit_run(manifest, output_dir, dev_queries):
    """No inner join: every planned ID contributes once, missing/duplicate = zero."""
    manifest = _load(manifest)
    planned = [_id(value) for value in manifest["planned_row_ids_in_order"]]
    if not planned or len(planned) != len(set(planned)):
        raise ValueError("Manifest must contain a nonempty, unique planned row sequence")
    output_dir = Path(output_dir)
    errors, warnings = [], []
    predictions = defaultdict(list)
    for row in _csv(output_dir / "predictions.csv"):
        try:
            predictions[_id(row["row_id"])].append(row)
        except (KeyError, ValueError, TypeError):
            errors.append("Prediction has an invalid or missing row_id")
    labels = {}
    for row in _csv(Path(dev_queries)):
        try:
            rid = _id(row["row_id"])
            if rid in labels:
                errors.append(f"Duplicate development label ID {rid}")
            labels[rid] = row
        except (KeyError, ValueError, TypeError):
            errors.append("Development query has an invalid row_id")
    missing = [rid for rid in planned if rid not in predictions]
    duplicates = sorted(rid for rid, values in predictions.items() if len(values) > 1)
    unexpected = sorted(set(predictions) - set(planned))
    for name, values in (("missing", missing), ("duplicate", duplicates), ("unexpected", unexpected)):
        if values:
            errors.append(f"{name} prediction IDs: {values}")
    usage_records = _jsonl(output_dir / "usage.jsonl", errors)
    usage_by_id = defaultdict(list)
    for record in usage_records:
        try:
            usage_by_id[_id(record["row_id"])].append(record)
        except (ValueError, KeyError, TypeError):
            errors.append("Usage record has invalid row_id; its model costs still contribute to total")
    routes = _jsonl(output_dir / "diagnostics" / "routes.jsonl", errors)
    routes_by_id = defaultdict(list)
    invocation_rows = {}
    attempt_paths = sorted((output_dir / "diagnostics" / "attempts").glob("*.json"))
    if attempt_paths:
        for path in attempt_paths:
            try:
                attempt = _load(path)
                for key, value in attempt.get("invocation_rows", {}).items():
                    index, rid = _id(key), _id(value)
                    if index in invocation_rows and invocation_rows[index] != rid:
                        errors.append(f"Conflicting attempt mapping for invocation {index}")
                    else:
                        invocation_rows[index] = rid
            except (ValueError, TypeError, AttributeError):
                errors.append(f"Malformed attempt manifest {path.name}")
    else:
        invocation_rows = {_id(key): _id(value) for key, value in manifest.get("invocation_rows", {}).items()}
        if not invocation_rows:
            invocation_rows = {index: rid for index, rid in enumerate(planned, 1)}
    expected_keys = manifest.get("case_keys_by_row_id", {})
    for route in routes:
        rid = invocation_rows.get(route.get("invocation_index"))
        if rid is None:
            errors.append("Route has an unknown invocation_index; attempts still counted")
            continue
        if str(rid) in expected_keys and route.get("case_key") != expected_keys[str(rid)]:
            errors.append(f"Route case_key does not match manifest row {rid}")
        routes_by_id[rid].append(route)
    prices = manifest.get("price_table", PRICES)
    models, known_cost, model_cost, unknown_usage = _usage(usage_records, prices)
    requests = [route for route in routes if route.get("event") == "request"]
    if any(route.get("estimated_cost_usd") is None or route.get("prompt_tokens") is None or route.get("completion_tokens") is None for route in requests):
        unknown_usage = True
    observed_calls = sum(counts["calls"] for counts in models.values())
    if observed_calls != len(requests):
        warnings.append(f"Usage calls ({observed_calls}) differ from route attempts ({len(requests)}); cost completeness unverified")
        unknown_usage = True
    if not (output_dir / "usage.jsonl").exists():
        warnings.append("Usage ledger missing; zero paid usage cannot be established")
        unknown_usage = True
    if not (output_dir / "diagnostics" / "routes.jsonl").exists():
        warnings.append("Routes ledger missing; zero requests cannot be established")
        unknown_usage = True
    cases = []
    for rid in planned:
        entries = predictions.get(rid, [])
        label = labels.get(rid)
        prediction = entries[0].get("prediction", "") if len(entries) == 1 else ""
        tags = []
        status = "missing" if not entries else "duplicate" if len(entries) > 1 else "returned"
        if not prediction.strip():
            tags.append("empty_prediction")
        if prediction and not _prediction_shape(prediction, label.get("instruction") if label else None):
            tags.append("format")
            errors.append(f"Prediction shape/count/requested fields invalid for row {rid}")
        if label is None or not label.get("scoring_points"):
            errors.append(f"Missing development scoring labels for planned row {rid}")
            partial, failed = 0.0, []
            tags.append("missing_labels")
        else:
            _, failed, partial = evaluate(prediction, label["scoring_points"])
            # Accuracy always uses official evaluate, not custom validity filtering.
            if partial != 1:
                text = label["scoring_points"]
                for category, phrase in (("component", "root cause component"), ("reason", "root cause reason"), ("parser/time", "occurrence time")):
                    if phrase in text:
                        tags.append(category + "_review_needed")
        evidence_path = output_dir / "evidence" / f"{rid}.md"
        body = evidence_path.read_text(encoding="utf-8") if evidence_path.exists() else ""
        required = ("## Answer", "## Confidence", "## Evidence", "## Ruled out")
        section_ok = all(body.count(section) == 1 for section in required) and [body.find(section) for section in required] == sorted(body.find(section) for section in required)
        if not section_ok:
            errors.append(f"Missing/malformed four-section evidence for row {rid}")
            tags.append("evidence_format")
        usage = usage_by_id[rid]
        case_models, case_known, _, case_unknown = _usage(usage, prices)
        case_requests = [route for route in routes_by_id[rid] if route.get("event") == "request"]
        case_unknown |= any(route.get("estimated_cost_usd") is None for route in case_requests)
        case_unknown |= not usage
        case_unknown |= sum(counts["calls"] for counts in case_models.values()) != len(case_requests)
        case_unknown |= not (output_dir / "diagnostics" / "routes.jsonl").exists()
        if not usage:
            errors.append(f"Missing per-case usage for planned row {rid}; incurred cost is unverified")
        if not routes_by_id[rid]:
            errors.append(f"Missing route/bypass events for planned row {rid}; request count is unverified")
            case_unknown = True
        unknown_usage |= case_unknown
        timing = [record.get("wall_s") for record in usage if isinstance(record.get("wall_s"), (int, float))]
        if any(route.get("status") not in {"valid", "bypass"} for route in routes_by_id[rid]):
            tags.append("provider_or_model_response")
        task = label.get("task_index", "unknown") if label else "unknown"
        cases.append({"row_id": rid, "task": task, "difficulty": DIFFICULTY.get(task, "unknown"),
            "status": status, "partial": partial, "fully_solved": partial == 1.0,
            "evidence_present": bool(body), "evidence_four_sections": section_ok,
            "validation_status": "invalid" if "Validation: invalid" in body else "degraded" if "Validation: degraded" in body else "valid" if "Validation: valid" in body else "unknown",
            "usage_attempts": len(usage), "wall_s": sum(timing) if timing else None,
            "models": case_models, "known_cost_usd": case_known, "cost_usd": None if case_unknown else case_known,
            "request_attempts": len(case_requests), "fallback_attempts": sum(bool(route.get("fallback")) for route in case_requests),
            "strong_attempts": sum(route.get("stage") == "strong" for route in case_requests),
            "bypass_events": sum(route.get("event") == "bypass" for route in routes_by_id[rid]),
            "stop_reasons": sorted({str(route.get("reason")) for route in routes_by_id[rid] if route.get("event") == "bypass"}),
            "error_categories": tags, "failed_scoring_points": failed})
    by_task, by_difficulty = {}, {}
    for key, groups in (("task", by_task), ("difficulty", by_difficulty)):
        for group in sorted({case[key] for case in cases}):
            subset = [case for case in cases if case[key] == group]
            groups[group] = {"planned": len(subset), "mean_partial": statistics.mean(case["partial"] for case in subset),
                             "fully_solved": sum(case["fully_solved"] for case in subset)}
    external = None
    timing_path = output_dir / "execution.json"
    if timing_path.exists():
        execution = _load(timing_path)
        external = execution.get("external_wall_s")
        if execution.get("returncode") != 0:
            errors.append(f"Runner exited with {execution.get('returncode')}")
    pinned = manifest.get("config", {}).get("pinned_model")
    mixed = bool(pinned and any(model != pinned and counts["calls"] for model, counts in models.items()))
    if mixed:
        errors.append("Pinned single-model run used another model; this is a mixed-model degraded run")
    return {"schema_version": "evaluation.v1", "manifest": manifest,
        "integrity_status": "invalid" if errors else "valid", "integrity_errors": errors, "warnings": warnings,
        "planned": len(planned), "returned_unique_planned": sum(rid in predictions for rid in planned),
        "missing_row_ids": missing, "duplicate_row_ids": duplicates, "unexpected_row_ids": unexpected,
        "mean_partial": statistics.mean(case["partial"] for case in cases),
        "fully_solved": sum(case["fully_solved"] for case in cases),
        "strict_fraction": sum(case["fully_solved"] for case in cases) / len(planned),
        "completion_fraction": sum(len(predictions.get(rid, [])) == 1 for rid in planned) / len(planned),
        "empty_prediction_fraction": sum("empty_prediction" in case["error_categories"] for case in cases) / len(planned),
        "evidence_four_section_fraction": sum(case["evidence_four_sections"] for case in cases) / len(planned),
        "external_wall_s": external, "sum_case_wall_s": sum(case["wall_s"] or 0 for case in cases),
        "time_stats": _stats([case["wall_s"] for case in cases]),
        "cost_stats": _stats([case["cost_usd"] for case in cases]),
        "known_cost_usd": known_cost, "total_cost_usd": None if unknown_usage else known_cost,
        "pricing_complete": not unknown_usage, "per_model_usage": models, "per_model_cost_usd": model_cost,
        "request_attempts": len(requests), "invocation_mapping_source": "attempt manifests" if attempt_paths else "primary manifest",
        "bypass_events": sum(route.get("event") == "bypass" for route in routes),
        "fallback_attempts": sum(bool(route.get("fallback")) for route in requests),
        "escalated_case_fraction": sum(case["strong_attempts"] > 0 for case in cases) / len(planned),
        "mixed_model_run": mixed, "by_task": by_task, "by_difficulty": by_difficulty, "cases": cases,
        "evidence_audit": {"level1": "section/ID manifest integrity only; use audit_evidence.py for ledger/source checks",
                           "level2": "not executed by audit_run", "level3": "causal support requires human review"}}


def compare_runs(reports):
    reports = [_load(report) for report in reports]
    if len(reports) < 2:
        raise ValueError("Comparison requires at least two run reports")
    keys = ("planned_row_ids_in_order", "code_revision", "source_hashes", "query_hash", "dataset_identity",
            "price_table_revision", "price_table", "cache_condition", "hardware_limits", "budget_policy")
    mismatches = []
    first = reports[0]["manifest"]
    for index, report in enumerate(reports):
        manifest = report["manifest"]
        for key in keys:
            if key not in manifest or manifest.get(key) != first.get(key):
                mismatches.append({"run": index, "field": key})
    groups = defaultdict(list)
    for report in reports:
        groups[report["manifest"].get("config_id", "unknown")].append(report)
    summaries = {}
    for config, repetitions in groups.items():
        scores = [report["mean_partial"] for report in repetitions]
        summaries[config] = {"repetitions": len(repetitions), "mean_partial": statistics.mean(scores),
            "partial_variance": statistics.variance(scores) if len(scores) > 1 else None,
            "variance_status": "measured" if len(scores) > 1 else "not measured: one repetition",
            "strict_fraction": statistics.mean(report["strict_fraction"] for report in repetitions),
            "total_known_cost_usd": sum(report["known_cost_usd"] for report in repetitions),
            "pricing_complete": all(report["pricing_complete"] for report in repetitions),
            "request_attempts": sum(report["request_attempts"] for report in repetitions),
            "external_wall_s": _stats([report["external_wall_s"] for report in repetitions])}
    pure_routing_pair = any(report["manifest"].get("config", {}).get("pinned_model") for report in reports) and any(
        report["manifest"].get("config", {}).get("mode") == "routed" and not report["manifest"].get("config", {}).get("pinned_model") for report in reports)
    models_exercised = all(report["request_attempts"] > 0 for report in reports if report["manifest"].get("config", {}).get("mode") == "routed")
    integrity = all(report["integrity_status"] == "valid" for report in reports)
    eligible = not mismatches and integrity and pure_routing_pair and models_exercised and all(report["pricing_complete"] for report in reports)
    return {"schema_version": "comparison.v1", "matched_conditions": not mismatches,
            "condition_mismatches": mismatches, "all_run_integrity_valid": integrity,
            "routing_comparison_measured": eligible, "configurations": summaries,
            "interpretation": "Matched same-agent routing comparison with actual calls; inspect repetitions and uncertainty." if eligible else
                "No routing-savings claim: conditions, run integrity, complete pricing, or actual single/routed calls are missing.",
            "pairwise_partial_deltas_from_first": [report["mean_partial"] - reports[0]["mean_partial"] for report in reports]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path, help="Existing runner output directory")
    parser.add_argument("--dev-queries", required=True, type=Path, help="Offline-only development labels")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="List frozen case IDs and audit paths without scoring or writing")
    args = parser.parse_args()
    if args.dry_run:
        print(json.dumps({"dry_run": True, "planned_row_ids_in_order": _load(args.manifest)["planned_row_ids_in_order"],
                          "output_dir": str(args.out), "offline_dev_labels": str(args.dev_queries), "report": str(args.report) if args.report else None}, indent=2))
        return
    result = audit_run(args.manifest, args.out, args.dev_queries)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
