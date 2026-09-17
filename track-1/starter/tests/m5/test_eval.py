"""Synthetic audit fixtures: no real development labels or paid model calls."""
from copy import deepcopy
import csv
from dataclasses import asdict
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from eval.audit_run import audit_run, compare_runs
from eval.run_comparison import build_plan, execute_plan
from eval.audit_evidence import audit_evidence_ledger, decode_record, _same
from agents.rca.contracts import CaseContext, UTC8
from agents.rca.data_access import CSVTelemetryStore
from agents.rca.metrics import triage_metrics
from run import format_prediction


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-m5-")
        self.root = Path(self.tmp.name)
        self.out = self.root / "run"
        (self.out / "evidence").mkdir(parents=True)
        (self.out / "diagnostics").mkdir()
        self.labels = self.root / "dev.csv"
        self.instruction = "The system experienced one failure within the time range of March 20, 2022, from 09:00 to 09:30. You are tasked with identifying the root cause component."
        self.write_csv(self.labels, ("row_id", "task_index", "instruction", "scoring_points"), [
            {"row_id": rid, "task_index": "task_3", "instruction": self.instruction,
             "scoring_points": f"The only predicted root cause component is pod-{rid}"} for rid in (8, 42, 99)])
        for rid in (8, 42, 99):
            (self.out / "evidence" / f"{rid}.md").write_text("## Answer\nGuess\n## Confidence\nLow. Validation: valid\n## Evidence\nSynthetic\n## Ruled out\nNone\n")
        self.manifest = {"planned_row_ids_in_order": [8, 42, 99], "code_revision": "synthetic-revision", "source_hashes": {"a": "1"},
            "query_hash": "synthetic-query", "dataset_identity": "synthetic", "price_table_revision": "test",
            "price_table": {"zai-org/GLM-5.2": [1.4, 4.4]}, "cache_condition": "fresh", "hardware_limits": "synthetic",
            "budget_policy": {"seconds": 45}, "config_id": "single", "config": {"mode": "routed", "pinned_model": "zai-org/GLM-5.2"}}
        self.write_predictions([(42, format_prediction([{"component": "pod-42"}]))])
        usage = {"row_id": 42, "wall_s": 2, "models": {"zai-org/GLM-5.2": {"calls": 1, "prompt_tokens": 100, "completion_tokens": 10}}}
        self.write_jsonl(self.out / "usage.jsonl", [usage])
        self.route = {"event": "request", "invocation_index": 2, "case_key": "synthetic", "stage": "flash", "status": "valid", "fallback": False,
                      "actual_model": "zai-org/GLM-5.2", "prompt_tokens": 100, "completion_tokens": 10, "estimated_cost_usd": .000184}
        self.write_jsonl(self.out / "diagnostics" / "routes.jsonl", [self.route])

    def tearDown(self):
        self.tmp.cleanup()

    def write_csv(self, path, fields, rows):
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def write_predictions(self, pairs):
        self.write_csv(self.out / "predictions.csv", ("row_id", "prediction"), [{"row_id": rid, "prediction": prediction} for rid, prediction in pairs])

    def write_jsonl(self, path, records):
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    def audit(self):
        return audit_run(self.manifest, self.out, self.labels)

    def test_missing_cases_remain_in_denominator(self):
        report = self.audit()
        self.assertEqual(report["planned"], 3)
        self.assertEqual(report["fully_solved"], 1)
        self.assertAlmostEqual(report["mean_partial"], 1 / 3)
        self.assertEqual(report["missing_row_ids"], [8, 99])
        self.assertEqual(report["integrity_status"], "invalid")

    def test_duplicates_are_not_last_wins_and_unexpected_flagged(self):
        self.write_predictions([(42, format_prediction([{"component": "pod-42"}])), (42, format_prediction([{"component": "pod-42"}])),
                                (77, format_prediction([{"component": "pod-77"}]))])
        report = self.audit()
        self.assertEqual(report["mean_partial"], 0)
        self.assertEqual(report["duplicate_row_ids"], [42])
        self.assertEqual(report["unexpected_row_ids"], [77])

    def test_every_usage_attempt_counts_and_route_maps_noncontinuous_ids(self):
        usage = {"row_id": 42, "wall_s": 2, "models": {"zai-org/GLM-5.2": {"calls": 1, "prompt_tokens": 100, "completion_tokens": 10}}}
        self.write_jsonl(self.out / "usage.jsonl", [usage, usage])
        self.write_jsonl(self.out / "diagnostics" / "routes.jsonl", [self.route, self.route])
        report = self.audit()
        self.assertEqual(report["per_model_usage"]["zai-org/GLM-5.2"]["calls"], 2)
        self.assertEqual(report["cases"][1]["usage_attempts"], 2)
        self.assertEqual(report["cases"][1]["request_attempts"], 2)
        self.assertAlmostEqual(report["known_cost_usd"], .000368)
        self.assertEqual(report["sum_case_wall_s"], 4)

    def test_unknown_provider_usage_is_not_zero_cost(self):
        self.write_jsonl(self.out / "usage.jsonl", [{"row_id": 42, "models": {"zai-org/GLM-5.2": {
            "calls": 1, "prompt_tokens": 0, "completion_tokens": 0, "unknown_usage_calls": 1}}}])
        route = dict(self.route, prompt_tokens=None, completion_tokens=None, estimated_cost_usd=None)
        self.write_jsonl(self.out / "diagnostics" / "routes.jsonl", [route])
        report = self.audit()
        self.assertFalse(report["pricing_complete"])
        self.assertIsNone(report["total_cost_usd"])
        self.assertIsNone(report["cases"][1]["cost_usd"])

    def test_missing_case_usage_and_omitted_token_fields_are_unknown(self):
        report = self.audit()
        self.assertFalse(report["pricing_complete"])
        self.assertIsNone(report["total_cost_usd"])
        self.manifest["planned_row_ids_in_order"] = [42]
        self.manifest["invocation_rows"] = {"2": 42}
        self.write_jsonl(self.out / "usage.jsonl", [{"row_id": 42, "models": {"zai-org/GLM-5.2": {"calls": 1}}}])
        report = self.audit()
        self.assertFalse(report["pricing_complete"])
        self.assertIsNone(report["total_cost_usd"])

    def test_resume_attempt_mapping_and_conflicts(self):
        attempts = self.out / "diagnostics" / "attempts"
        attempts.mkdir()
        (attempts / "one.json").write_text(json.dumps({"invocation_rows": {"12": 42}}))
        self.write_jsonl(self.out / "diagnostics" / "routes.jsonl", [dict(self.route, invocation_index=12)])
        report = self.audit()
        self.assertEqual(report["cases"][1]["request_attempts"], 1)
        self.assertEqual(report["invocation_mapping_source"], "attempt manifests")
        (attempts / "two.json").write_text(json.dumps({"invocation_rows": {"12": 99}}))
        self.assertTrue(any("Conflicting attempt" in error for error in self.audit()["integrity_errors"]))

    def test_unpriced_model_and_malformed_usage_are_incomplete(self):
        self.write_jsonl(self.out / "usage.jsonl", [{"row_id": 42, "models": {"unpriced-provider-id": {"calls": 1, "prompt_tokens": 5, "completion_tokens": 2}, "broken": "bad"}}])
        report = self.audit()
        self.assertFalse(report["pricing_complete"])
        self.assertTrue(report["mixed_model_run"])

    def test_shape_validation_does_not_replace_official_accuracy(self):
        # Extra fields are officially ignored but violate our exact requested-field contract.
        self.write_predictions([(42, format_prediction([{"component": "pod-42", "reason": "container CPU load"}]))])
        report = self.audit()
        self.assertEqual(report["cases"][1]["partial"], 1)
        self.assertIn("format", report["cases"][1]["error_categories"])

    def test_comparison_rejects_changed_conditions_and_no_calls(self):
        first = self.audit()
        second = deepcopy(first)
        second["manifest"]["config_id"] = "routed"
        second["manifest"]["config"]["pinned_model"] = None
        second["manifest"]["query_hash"] = "changed"
        second["request_attempts"] = 0
        comparison = compare_runs([first, second])
        self.assertFalse(comparison["matched_conditions"])
        self.assertFalse(comparison["routing_comparison_measured"])
        self.assertIsNone(comparison["configurations"]["routed"]["partial_variance"])

    def test_deterministic_pinned_config_is_not_a_measured_single_model(self):
        first = self.audit()
        first.update(integrity_status="valid", pricing_complete=True, request_attempts=0)
        first["manifest"]["config"]["mode"] = "deterministic"
        second = deepcopy(first)
        second["manifest"]["config"] = {"mode": "routed", "pinned_model": None}
        second["manifest"]["config_id"] = "routed"
        second["request_attempts"] = 1
        self.assertFalse(compare_runs([first, second])["routing_comparison_measured"])

    def test_build_plan_strips_labels_preserves_ids_and_has_no_writes(self):
        target = self.root / "experiment"
        plan = build_plan(self.root, self.labels, target, row_ids=[99, 8], repetitions=2)
        self.assertEqual(len(plan), 4)
        self.assertFalse(target.exists())
        self.assertEqual(plan[0]["manifest"]["planned_row_ids_in_order"], [99, 8])
        self.assertTrue(all("scoring_points" not in row for item in plan for row in item["rows"]))
        self.assertEqual(len({item["manifest"]["output_dir"] for item in plan}), 4)
        self.assertEqual(plan[0]["manifest"]["budget_policy"], plan[-1]["manifest"]["budget_policy"])

    def test_execute_refuses_missing_key_and_reused_output_before_running(self):
        plan = build_plan(self.root, self.labels, self.root / "new")
        with patch.dict(os.environ, {}, clear=True), patch("eval.run_comparison.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "API_KEY"):
                execute_plan(plan)
            run.assert_not_called()
        Path(plan[0]["manifest"]["output_dir"]).mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "fresh"):
            execute_plan(plan)

    def test_missing_original_row_id_is_not_renumbered(self):
        self.write_csv(self.labels, ("instruction",), [{"instruction": self.instruction}])
        with self.assertRaisesRegex(ValueError, "original row_id"):
            build_plan(self.root, self.labels, self.root / "new")


class EvidenceReplayTests(unittest.TestCase):
    def test_boolean_is_not_a_measured_count(self):
        self.assertFalse(_same({"count": 1}, {"count": True}))
        self.assertFalse(_same({"count": 0}, {"count": False}))
        self.assertTrue(_same({"count": 1}, {"count": 1.0}))

    def test_actual_store_replay_and_locator_audit(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-m5-replay-") as directory:
            root = Path(directory)
            metric_dir = root / "telemetry" / "2022_04_01" / "metric"
            metric_dir.mkdir(parents=True)
            start = datetime(2022, 4, 1, 9, 0, tzinfo=UTC8)
            with (metric_dir / "metric_container.csv").open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(("timestamp", "cmdb_id", "kpi_name", "value"))
                for minute in range(-3, 5):
                    writer.writerow(((start + timedelta(minutes=minute)).timestamp(), "node-x.pod-4", "cpu_usage", 0 if minute < 2 else 20))
            case = CaseContext("synthetic", "synthetic", start, start + timedelta(minutes=5), start - timedelta(minutes=3), 1, ("component",))
            bundle = triage_metrics(case, CSVTelemetryStore(root), deadline=time.monotonic() + 10)
            ledger = {"case_key": case.case_key, "invocation_index": 1, "evidence": [asdict(record) for record in bundle.evidence],
                      "decision": {"supporting_ids": [bundle.evidence[0].evidence_id]}}
            path = root / "ledger.json"
            path.write_text(json.dumps(ledger, default=lambda value: value.isoformat()), encoding="utf-8")
            report = audit_evidence_ledger(path, root, deadline=time.monotonic() + 10)
            self.assertTrue(report["all_sampled_verified"], report)
            self.assertEqual(report["selected_count"], 1)
            self.assertIn("does not establish", report["causal_inference_review"])


if __name__ == "__main__":
    unittest.main()
