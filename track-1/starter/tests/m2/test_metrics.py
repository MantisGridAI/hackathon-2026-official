"""Synthetic, tiny CSV tests exercise the real M1 store, not real incident facts."""
import csv
from dataclasses import asdict
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agents.rca.contracts import CaseContext, LEGAL_REASONS, UTC8
from agents.rca.data_access import CSVTelemetryStore
from agents.rca.metrics import (COMPARE_TRANSFORM, BASE_TRANSFORM, compare_replicas_and_node,
    inspect_metrics, replay_evidence, triage_metrics)


class MetricTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-m2-")
        self.path = Path(self.tmp.name)
        start = datetime(2022, 4, 15, 9, 0, tzinfo=UTC8)
        self.case = CaseContext("synthetic-m2", "synthetic test instruction", start, start + timedelta(minutes=8),
                                start - timedelta(minutes=3), 2, ("datetime", "component", "reason"))
        self.metric_path = self.path / "telemetry" / "2022_04_15" / "metric"
        self.metric_path.mkdir(parents=True)
        self.rows = []
        for minute in range(-3, 8):
            timestamp = (start + timedelta(minutes=minute)).timestamp()
            changed = 2 <= minute <= 3 or 6 <= minute <= 7
            for raw, scale in (("host-new.mystery-7", 1), ("host-new.mystery-8", 0), ("host-new.other-3", 0.5)):
                self.rows.extend([
                    [timestamp, raw, "container_fs_reads_bytes", (80 if changed else 0) * scale],
                    [timestamp, raw, "container_cpu_usage", 2 + ((8 if changed else 0) * scale)],
                    [timestamp, raw, "container_memory_usage", 10 + ((15 if changed else 0) * scale)],
                ])
        self.write("metric_container", ("timestamp", "cmdb_id", "kpi_name", "value"), self.rows)
        self.write("metric_node", ("timestamp", "cmdb_id", "kpi_name", "value"), [
            [(start + timedelta(minutes=m)).timestamp(), "host-new", "cpu_usage", 2 + (15 if 2 <= m <= 3 else 0)] for m in range(-3, 8)])
        self.write("metric_service", ("service", "timestamp", "rr", "sr", "mrt", "count"), [
            ["mystery", (start + timedelta(minutes=m)).timestamp(), 1, 1, 10, 100] for m in range(-3, 8)])
        self.store = CSVTelemetryStore(self.path, chunk_size=13)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, source, header, rows):
        with (self.metric_path / f"{source}.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)

    def analyse(self):
        return triage_metrics(self.case, self.store, deadline=time.monotonic() + 20)

    def test_service_stability_does_not_filter_pod_resource_changes(self):
        bundle = self.analyse()
        primary = [c for c in bundle.candidates if c.component == "mystery-7"]
        self.assertTrue(primary)
        families = {c.features["metrics.family"] for c in primary}
        self.assertEqual(families, {"read_io", "cpu", "memory"})
        self.assertTrue(all(c.reason in LEGAL_REASONS for c in primary))
        self.assertTrue(any(c.component == "host-new" for c in bundle.candidates))
        self.assertTrue(all(c.features["metrics.service_strength"] == 0 for c in primary))
        self.assertEqual(len([c for c in primary if c.features["metrics.family"] == "read_io"]), 2)
        self.assertTrue(all(c.onset_estimate.utcoffset() == timedelta(hours=8) for c in primary))
        self.assertEqual({cov.status for cov in bundle.coverage}, {"complete"})

    def test_all_evidence_replays_exactly_from_saved_queries(self):
        bundle = self.analyse()
        for record in bundle.evidence:
            with self.subTest(evidence=record.evidence_id):
                self.assertEqual(record.values, replay_evidence(record, self.store, deadline=time.monotonic() + 20))
                self.assertTrue(record.source_records)
                self.assertTrue(all(not Path(path).is_absolute() for path in record.source_files))
                self.assertEqual(set(record.units), set(record.values))
                json.dumps(record.values, allow_nan=False)

    def test_inspect_respects_components_and_families(self):
        bundle = inspect_metrics(self.case, ("mystery-7",), ("read_io",), self.store, deadline=time.monotonic() + 20)
        self.assertTrue(bundle.candidates)
        self.assertEqual({c.component for c in bundle.candidates}, {"mystery-7"})
        self.assertEqual({c.features["metrics.family"] for c in bundle.candidates}, {"read_io"})
        with self.assertRaises(ValueError):
            inspect_metrics(self.case, (), ("invalid",), self.store, deadline=time.monotonic() + 20)

    def test_comparison_uses_same_metric_and_preserves_ordering_uncertainty(self):
        original = self.analyse()
        bundle = compare_replicas_and_node(self.case, "mystery-7", self.store, deadline=time.monotonic() + 20)
        record = next(r for r in bundle.evidence if r.transform == COMPARE_TRANSFORM)
        self.assertEqual(record.values, replay_evidence(record, self.store, deadline=time.monotonic() + 20))
        self.assertTrue(any(x["relation"] == "same service replica" for x in record.values["comparisons"]))
        self.assertTrue(any(x["relation"] == "same node different service" for x in record.values["comparisons"]))
        self.assertTrue(any(x["changed"] for x in record.values["node_cochange"]))
        self.assertTrue(all(x["ordering_uncertain"] for x in record.values["node_cochange"]))
        original_ids = {c.candidate_id for c in original.candidates}
        self.assertFalse(original_ids & {c.candidate_id for c in bundle.candidates})
        self.assertTrue(all(c.features["metrics.ordering_uncertain"] for c in bundle.candidates if c.component == "mystery-7"))

    def test_unknown_component_has_no_guessed_topology(self):
        bundle = compare_replicas_and_node(self.case, "never-observed", self.store, deadline=time.monotonic() + 20)
        self.assertFalse(bundle.candidates)
        self.assertIn("no topology was guessed", bundle.warnings[0])

    def test_missing_node_data_is_unknown_in_comparison(self):
        self.analyse()
        (self.metric_path / "metric_node.csv").unlink()
        fresh = CSVTelemetryStore(self.path)
        triage_metrics(self.case, fresh, deadline=time.monotonic() + 20)
        bundle = compare_replicas_and_node(self.case, "mystery-7", fresh, deadline=time.monotonic() + 20)
        self.assertTrue(all(c.features["metrics.node_cochange"] is None for c in bundle.candidates if c.component == "mystery-7"))
        comparison = next(r for r in bundle.evidence if r.transform == COMPARE_TRANSFORM)
        self.assertEqual(len(comparison.queries), len(comparison.coverage))

    def test_missing_files_and_empty_window_are_not_healthy(self):
        absent = CSVTelemetryStore(self.path / "absent")
        bundle = triage_metrics(self.case, absent, deadline=time.monotonic() + 20)
        self.assertFalse(bundle.candidates)
        self.assertEqual({c.status for c in bundle.coverage}, {"missing"})
        self.assertTrue(bundle.warnings)
        case = CaseContext("empty", "synthetic", self.case.start + timedelta(hours=1), self.case.end + timedelta(hours=1),
                           self.case.reference_start + timedelta(hours=1), 1, ("component",))
        empty = triage_metrics(case, self.store, deadline=time.monotonic() + 20)
        self.assertFalse(empty.candidates)
        self.assertEqual({c.status for c in empty.coverage}, {"empty"})

    def test_expired_deadline_returns_coverage(self):
        bundle = triage_metrics(self.case, self.store, deadline=time.monotonic() - 1)
        self.assertFalse(bundle.candidates)
        self.assertEqual(len(bundle.coverage), 3)
        self.assertTrue(all(c.status in {"partial", "not_queried"} for c in bundle.coverage))

    def test_stable_ids_are_reproducible(self):
        first = self.analyse()
        second = self.analyse()
        self.assertEqual([c.candidate_id for c in first.candidates], [c.candidate_id for c in second.candidates])
        self.assertEqual([r.evidence_id for r in first.evidence], [r.evidence_id for r in second.evidence])

    def test_cold_source_can_borrow_unused_module_budget(self):
        from agents.rca import metrics
        with patch('agents.rca.metrics.time.monotonic', return_value=100.), \
             patch('agents.rca.metrics._read', wraps=metrics._read) as read:
            result = triage_metrics(self.case, self.store, deadline=115.)
        self.assertEqual(len(read.call_args_list), 3)
        self.assertGreater(read.call_args_list[0].args[2], 110.)
        self.assertTrue(all(call.args[2] < 115. for call in read.call_args_list))
        self.assertTrue(all(c.status == 'complete' for c in result.coverage))

    def test_row_cap_closes_iterator_and_marks_partial(self):
        with patch("agents.rca.metrics.MAX_WINDOW_ROWS", 20):
            bundle = self.analyse()
        container = next(c for c in bundle.coverage if c.source == "metric_container")
        self.assertEqual(container.status, "partial")
        self.assertTrue(any("row cap" in warning for warning in bundle.warnings))
        # Replay must use the original prefix even after the row cap is restored.
        for record in bundle.evidence:
            self.assertEqual(record.values, replay_evidence(record, self.store, deadline=time.monotonic() + 20))

    def test_replay_rejects_insufficient_prefix_and_missing_prefix_metadata(self):
        from copy import deepcopy
        record = next(r for r in self.analyse().evidence if r.transform == BASE_TRANSFORM)
        with self.assertRaises(ValueError):
            replay_evidence(record, self.store, deadline=time.monotonic() - 1)
        missing = deepcopy(record)
        del missing.transform_params["retained_rows_per_query"]
        with self.assertRaisesRegex(ValueError, "prefix length"):
            replay_evidence(missing, self.store, deadline=time.monotonic() + 20)

    def test_node_single_sample_spike_is_distinct_from_sustained_load(self):
        start = self.case.start
        self.write("metric_node", ("timestamp", "cmdb_id", "kpi_name", "value"), [
            [(start + timedelta(minutes=m)).timestamp(), "unseen-machine", "cpu_usage", 90 if m == 2 else 0] for m in range(-3, 8)])
        bundle = self.analyse()
        spikes = [candidate for candidate in bundle.candidates if candidate.component == "unseen-machine"]
        self.assertEqual(len(spikes), 1)
        self.assertEqual(spikes[0].reason, "node CPU spike")
        self.assertTrue(spikes[0].features["metrics.spike"])

    def test_calibrated_effect_has_same_scale_for_zero_and_nonzero_mad(self):
        from agents.rca.metrics import _candidates
        from agents.rca.onset import summarize_series
        from agents.rca.contracts import EvidenceRecord
        from agents.rca.ranking import rank_candidates
        def observation(name, baseline, changed, multiplier=1.):
            start = self.case.start.timestamp()
            samples = [(start - (len(baseline) - i) * 60, v * multiplier) for i, v in enumerate(baseline)]
            samples += [(start + i * 60, changed * multiplier) for i in range(6)]
            values = summarize_series(samples, start, self.case.end.timestamp())
            record = EvidenceRecord(name, "metric", [name], (self.case.reference_start, self.case.end),
                                    transform_params={"kpi_name": "synthetic_cpu_usage", "raw_cmdb_id": name},
                                    values=values)
            return _candidates(self.case, record, "cpu", "container")[0], record
        activation, first = observation("synthetic-activation", [0.] * 6, 8.)
        moderate, second = observation("synthetic-moderate", [.0049, .005, .0051, .0049, .005, .0051], .015)
        rescaled, _ = observation("synthetic-rescaled", [.0049, .005, .0051, .0049, .005, .0051], .015, 1e6)
        # The detector's zero-MAD ceiling used to invert these two effects.
        self.assertLess(activation.features["metrics.strength"], moderate.features["metrics.strength"])
        self.assertGreater(activation.features["metrics.calibrated_strength"], moderate.features["metrics.calibrated_strength"])
        self.assertAlmostEqual(moderate.features["metrics.calibrated_strength"], rescaled.features["metrics.calibrated_strength"])
        self.assertEqual(rank_candidates(self.case, [moderate, activation], [first, second])[0].candidate, activation)
        repeated, _ = observation("synthetic-periodic", [0., .015, 0., 0., .015, 0.], .015)
        self.assertEqual(repeated.features["metrics.calibrated_strength"], 0.)

    def test_cpu_decrease_retains_unknown_mechanism(self):
        from agents.rca.metrics import _hypothesis
        self.assertIsNone(_hypothesis("container", "cpu_usage", "cpu", {"direction": "decrease", "spike": False}))
        self.assertEqual(_hypothesis("node", "cpu_idle", "cpu", {"direction": "decrease", "spike": False}), "node CPU load")

    def test_auxiliary_counters_do_not_establish_resource_load(self):
        from agents.rca.metrics import _hypothesis
        episode = {"direction": "increase", "spike": False}
        for family, kpi in (("cpu", "container_cpu_cfs_periods"),
                            ("cpu", "container_cpu_throttled_seconds"),
                            ("cpu", "system.cpu.iowait"),
                            ("memory", "container_memory_failures.pgfault"),
                            ("memory", "container_memory_cache"),
                            ("memory", "container_memory_mapped_file")):
            with self.subTest(kpi=kpi):
                self.assertIsNone(_hypothesis("container", kpi, family, episode))
        self.assertEqual(_hypothesis("node", "memory_available", "memory", {"direction": "decrease", "spike": False}), "node memory consumption")


class RealTelemetrySmoke(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("RCA_TEST_DATA"), "RCA_TEST_DATA unset: real telemetry check not executed")
    def test_official_real_window(self):
        """Only label-free query.csv and whitelisted telemetry are consumed."""
        from agents.rca.runtime import parse_case
        dataset = Path(os.environ["RCA_TEST_DATA"])
        with (dataset / "query.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        requested_indices = [int(value) for value in os.environ.get("RCA_TEST_ROWS", "0").split(",")]
        store = CSVTelemetryStore(dataset)
        for index in requested_indices:
            case = parse_case(rows[index]["instruction"])
            bundle = triage_metrics(case, store, deadline=time.monotonic() + 90)
            self.assertTrue(bundle.evidence, f"No evidence for real query index {index}")
            self.assertTrue(any(c.status in {"complete", "partial"} and c.rows_matched for c in bundle.coverage))
            resource = next((r for r in bundle.evidence if r.transform == BASE_TRANSFORM and r.values["episodes"]), None)
            if resource is not None and all(c.status == "complete" for c in resource.coverage):
                self.assertEqual(resource.values, replay_evidence(resource, store, deadline=time.monotonic() + 90))
            print(json.dumps({"real_query_index": index, "candidates": len(bundle.candidates),
                              "evidence": len(bundle.evidence), "coverage": [c.status for c in bundle.coverage],
                              "top_component": bundle.candidates[0].component if bundle.candidates else None}))


if __name__ == "__main__":
    unittest.main()
