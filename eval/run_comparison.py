#!/usr/bin/env python3
"""Freeze and run same-agent routed/single-model experiments in fresh directories."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if __package__:
    from .audit_run import ROOT, STARTER, PRICES, _csv, audit_run, compare_runs
else:
    from audit_run import ROOT, STARTER, PRICES, _csv, audit_run, compare_runs
from agents.rca.runtime import parse_case


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _code_identity():
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False).stdout.strip() or "unavailable"
    files = [*STARTER.glob("*.py"), *STARTER.glob("agents/**/*.py"), STARTER / "requirements.txt"]
    return revision, {path.relative_to(ROOT).as_posix(): _hash_file(path) for path in sorted(files) if path.is_file()}


def _resolved_config(mode, pinned):
    """Freeze M4's actual resolved policy, restoring the caller's environment."""
    from agents.rca.routing import load_config
    previous = {key: os.environ.get(key) for key in ("RCA_MODE", "RCA_MODEL")}
    try:
        os.environ["RCA_MODE"] = mode
        os.environ.pop("RCA_MODEL", None)
        if pinned:
            os.environ["RCA_MODEL"] = pinned
        return asdict(load_config())
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def build_plan(dataset, queries, out, *, row_ids=None, limit=0, repetitions=1,
               single_model="zai-org/GLM-5.2", include_deterministic=False,
               cache_condition="new process per run; operating-system cache uncontrolled",
               hardware_limits="host execution; no CPU/memory container limits measured"):
    dataset, queries, out = Path(dataset).resolve(), Path(queries).resolve(), Path(out).resolve()
    if single_model not in PRICES:
        raise ValueError("single-model must be a documented GLM model ID")
    if repetitions < 1 or limit < 0:
        raise ValueError("repetitions must be positive and limit nonnegative")
    rows = _csv(queries)
    planned = []
    for row in rows:
        if "row_id" not in row:
            raise ValueError("Queries must preserve explicit original row_id; implicit renumbering is forbidden")
        rid = int(row["row_id"])
        if row_ids is not None and rid not in row_ids:
            continue
        if not row.get("instruction"):
            raise ValueError(f"Missing instruction for row {rid}")
        # Labels and every other column are deliberately excluded from the runner input.
        planned.append({"row_id": rid, "task_index": row.get("task_index", ""), "instruction": row["instruction"]})
    if row_ids is not None:
        found = {row["row_id"] for row in planned}
        if found != set(row_ids):
            raise ValueError(f"Requested row IDs absent from queries: {sorted(set(row_ids) - found)}")
        order = {rid: index for index, rid in enumerate(row_ids)}
        planned.sort(key=lambda row: order[row["row_id"]])
    if limit:
        planned = planned[:limit]
    if not planned or len({row["row_id"] for row in planned}) != len(planned):
        raise ValueError("Selected queries must contain unique nonempty row IDs")
    revision, source_hashes = _code_identity()
    dataset_manifest = dataset / "manifest.json"
    identity = {"bundle_name": dataset.name, "manifest_sha256": _hash_file(dataset_manifest) if dataset_manifest.is_file() else None,
                "label_free_query_sha256": _hash_file(dataset / "query.csv") if (dataset / "query.csv").is_file() else None}
    # Full 12GB telemetry is not hashed during planning; the official bundle
    # manifest identity and this limitation are saved rather than implied.
    identity["telemetry_content_hash_status"] = "not computed; official bundle manifest identity only"
    config_pairs = [("single-model", "routed", single_model), ("routed", "routed", None)]
    if include_deterministic:
        config_pairs.insert(0, ("deterministic", "deterministic", None))
    configs = []
    for config_id, mode, pinned in config_pairs:
        config = _resolved_config(mode, pinned)
        budget = {key: value for key, value in config.items() if key not in {"mode", "pinned_model"}}
        for repetition in range(1, repetitions + 1):
            run_dir = out / config_id / f"rep-{repetition:02}"
            query_path = run_dir / "queries.csv"
            command = [sys.executable, str(STARTER / "run.py"), "--dataset", str(dataset), "--queries", str(query_path),
                       "--out", str(run_dir), "--agent", "agents.routed"]
            case_keys = {}
            for row in planned:
                try:
                    case_keys[str(row["row_id"])] = parse_case(row["instruction"]).case_key
                except ValueError:
                    pass  # Parser failures remain planned cases and will be audited.
            manifest = {"schema_version": "experiment.v1", "experiment_id": out.name,
                "created_utc": datetime.now(timezone.utc).isoformat(), "code_revision": revision,
                "source_hashes": source_hashes, "config_id": config_id, "config": config,
                "config_hash": _hash(config), "budget_policy": budget,
                "planned_row_ids_in_order": [row["row_id"] for row in planned],
                "invocation_rows": {str(index): row["row_id"] for index, row in enumerate(planned, 1)},
                "case_keys_by_row_id": case_keys, "query_hash": _hash(planned),
                "input_query_file_sha256": _hash_file(queries), "repetition": repetition,
                "dataset_identity": identity, "price_table_revision": "official-track1-models-2026-09-17",
                "price_table": PRICES, "cache_condition": cache_condition, "hardware_limits": hardware_limits,
                "tuning_status": "public development data; not hidden-test or independent holdout performance",
                "runner_command": command, "output_dir": str(run_dir),
                "allowed_environment": {"RCA_MODE": mode, "RCA_MODEL": pinned},
                "credential_values": "never recorded"}
            configs.append({"manifest": manifest, "rows": planned, "command": command})
    return configs


def execute_plan(plan, *, dev_queries=None):
    # Fail before any run starts if an output directory already exists.
    for item in plan:
        if Path(item["manifest"]["output_dir"]).exists():
            raise ValueError("Each run requires a fresh output directory: " + item["manifest"]["output_dir"])
    if any(item["manifest"]["config"]["mode"] == "routed" for item in plan) and not os.environ.get("FEATHERLESS_API_KEY"):
        raise ValueError("FEATHERLESS_API_KEY is unavailable; use --dry-run or run the deterministic runner separately")
    reports = []
    import csv
    for item in plan:
        manifest = item["manifest"]
        directory = Path(manifest["output_dir"])
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        with (directory / "queries.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=("row_id", "task_index", "instruction"))
            writer.writeheader()
            writer.writerows(item["rows"])
        env = dict(os.environ)
        env["RCA_MODE"] = manifest["config"]["mode"]
        env.pop("RCA_MODEL", None)
        if manifest["config"]["pinned_model"]:
            env["RCA_MODEL"] = manifest["config"]["pinned_model"]
        started = time.monotonic()
        with (directory / "runner.log").open("w", encoding="utf-8") as log:
            try:
                completed = subprocess.run(item["command"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                           timeout=manifest["config"]["run_soft_seconds"] + 60, check=False)
                returncode, timed_out = completed.returncode, False
            except subprocess.TimeoutExpired:
                returncode, timed_out = -1, True
        execution = {"external_wall_s": time.monotonic() - started, "returncode": returncode,
                     "timed_out": timed_out, "peak_memory_bytes": None,
                     "resource_limit_status": "No container resource measurement; see manifest hardware_limits"}
        (directory / "execution.json").write_text(json.dumps(execution, indent=2) + "\n", encoding="utf-8")
        if dev_queries:
            report = audit_run(manifest, directory, dev_queries)
            (directory / "audit.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            reports.append(report)
    if len(reports) >= 2:
        comparison = compare_runs(reports)
        common = Path(plan[0]["manifest"]["output_dir"]).parents[1]
        (common / "comparison.json").write_text(json.dumps(comparison, indent=2) + "\n", encoding="utf-8")
        return comparison
    return {"runs_executed": len(plan), "scoring": "not requested"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--queries", required=True, type=Path, help="Runner input is rewritten without scoring fields")
    parser.add_argument("--out", required=True, type=Path, help="Fresh experiment directory")
    parser.add_argument("--dev-queries", type=Path, help="Used by offline audit only, never passed to Agent")
    parser.add_argument("--rows", help="Comma-separated original row IDs in desired order")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--single-model", default="zai-org/GLM-5.2", choices=sorted(PRICES))
    parser.add_argument("--include-deterministic", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print commands, frozen cases, budgets, and directories; no files or API calls")
    args = parser.parse_args()
    row_ids = [int(value) for value in args.rows.split(",")] if args.rows else None
    plan = build_plan(args.dataset, args.queries, args.out, row_ids=row_ids, limit=args.limit,
                      repetitions=args.repetitions, single_model=args.single_model,
                      include_deterministic=args.include_deterministic)
    print(json.dumps({"dry_run": args.dry_run, "plans": [item["manifest"] for item in plan]}, indent=2))
    if not args.dry_run:
        print(json.dumps(execute_plan(plan, dev_queries=args.dev_queries), indent=2))


if __name__ == "__main__":
    main()
