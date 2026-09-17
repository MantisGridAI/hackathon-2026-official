"""All generated telemetry in this file is tiny, synthetic test input."""
import csv
from dataclasses import replace
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agents.rca.contracts import CaseContext, DependencyEdge, UTC8
from agents.rca.data_access import CSVTelemetryStore
from agents.rca.logs import LOG_COLUMNS, search_fault_evidence
from agents.rca.runtime import parse_case
from agents.rca.traces import (TRACE_COLUMNS, _legacy_queries, _read_queries,
                              inspect_dependencies, replay_evidence, triage_traces)


class SyntheticStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-m3-")
        self.root = Path(self.temp.name)
        self.start = datetime(2022, 3, 20, 10, 0, tzinfo=UTC8)
        self.case = CaseContext("synthetic-m3-case", "Synthetic test; not real evidence",
                                self.start, self.start + timedelta(minutes=20),
                                self.start - timedelta(minutes=10), 1, ("component", "reason"))
        self.store = CSVTelemetryStore(self.root, chunk_size=3)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, kind, source, columns, rows):
        path = self.root / "telemetry" / "2022_03_20" / kind / f"{source}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def span(self, seconds, trace, span_id, parent, component="pod-a", duration=10, status="0", kind="rpc"):
        return dict(timestamp=(self.start.timestamp() + seconds) * 1000, cmdb_id=component,
                    span_id=span_id, trace_id=trace, parent_span=parent, duration=duration,
                    status_code=status, type=kind, operation_name="SyntheticOperation")

    def make_traces(self):
        rows = []
        for i, second in enumerate((-500, -400, -300, -200, 100, 200, 300, 400)):
            incident = second > 0
            rows.append(self.span(second, f"trace-{i}", "p", "", "pod-a", 10))
            rows.append(self.span(second + (10 if incident else 1), f"trace-{i}", "c", "p", "pod-b",
                                  100 if incident else 10, "Ok", "db"))
        # Orphan and duplicate parent are explicitly synthetic pathology records.
        rows += [self.span(500, "orphan", "c", "missing", "pod-c", status="7"),
                 self.span(600, "ambiguous", "p", "", "pod-a"),
                 self.span(600, "ambiguous", "p", "", "pod-c"),
                 self.span(601, "ambiguous", "c", "p", "pod-b")]
        return self.write("trace", "trace_span", TRACE_COLUMNS, rows)

    def test_independent_trace_candidates_native_units_and_replay(self):
        path = self.make_traces()
        bundle = triage_traces(self.case, self.store, deadline=time.monotonic() + 15)
        self.assertIn("pod-b", {c.component for c in bundle.candidates})
        self.assertTrue(all(c.reason is None for c in bundle.candidates))
        self.assertTrue(bundle.edges)
        group = next(e for e in bundle.evidence if e.transform == "traces.group_compare.v2"
                     and e.transform_params["component"] == "pod-b" and e.transform_params["span_type"] == "db")
        self.assertEqual(group.values["duration_ratio"], 10)
        self.assertEqual(group.values["baseline"]["recognized_success_count"], 4)
        self.assertEqual(group.values["incident"]["recognized_error_count"], 0)
        self.assertEqual(group.values["frequency_ratio"], .5)
        self.assertEqual(group.units["duration_median"], "native/unknown")
        self.assertTrue(any("Low sample" in limitation for limitation in group.limitations))
        with path.open(newline="", encoding="utf-8") as handle:
            source_rows = list(csv.DictReader(handle))
        for record in bundle.evidence:
            json.dumps(record.values, allow_nan=False)
            self.assertEqual(record.values, replay_evidence(record, self.store, deadline=time.monotonic() + 15))
            for location in record.source_records:
                raw = source_rows[location.record_index - 1]
                self.assertEqual(location.span_id, raw["span_id"])
                self.assertEqual(location.trace_id, raw["trace_id"])
        health = next(e for e in bundle.evidence if e.transform == "traces.pairing_quality.v2")
        self.assertEqual(health.values["duplicate_key_count"], 1)
        self.assertEqual(health.values["missing_parent_rows"], 1)
        self.assertEqual(health.values["ambiguous_parent_rows"], 1)

    def test_unknown_status_is_not_error_and_absent_baseline_no_ratio(self):
        self.write("trace", "trace_span", TRACE_COLUMNS, [self.span(5, "t", "s", "", status="7")])
        bundle = triage_traces(self.case, self.store, deadline=time.monotonic() + 10)
        group = next(e for e in bundle.evidence if e.transform == "traces.group_compare.v2")
        self.assertEqual(group.values["incident"]["unknown_status_count"], 1)
        self.assertEqual(group.values["incident"]["recognized_error_count"], 0)
        self.assertIsNone(group.values["duration_ratio"])
        self.assertFalse(bundle.candidates)

    def test_padding_provides_parent_but_no_context_only_candidate(self):
        rows = [self.span(-605, "border", "p", "", "context-pod"),
                self.span(-598, "border", "c", "p", "pod-b")]
        self.write("trace", "trace_span", TRACE_COLUMNS, rows)
        bundle = triage_traces(self.case, self.store, deadline=time.monotonic() + 10)
        self.assertTrue(bundle.edges)
        self.assertNotIn("context-pod", [e.transform_params.get("component") for e in bundle.evidence])
        health = next(e for e in bundle.evidence if e.transform == "traces.pairing_quality.v2")
        self.assertEqual(health.values["padding_context_rows"], 1)

    def test_requested_dependency_inspection_and_bad_edges(self):
        self.make_traces()
        first = triage_traces(self.case, self.store, deadline=time.monotonic() + 10)
        deep = inspect_dependencies(self.case, ("pod-b",), tuple(first.edges), self.store, deadline=time.monotonic() + 10)
        self.assertTrue(deep.edges)
        with self.assertRaises(ValueError):
            inspect_dependencies(self.case, (), (DependencyEdge("invented", "a", "b"),), self.store, deadline=time.monotonic() + 10)

    def test_missing_and_deadline_are_not_health(self):
        missing = triage_traces(self.case, self.store, deadline=time.monotonic() + 10)
        self.assertEqual({c.status for c in missing.coverage}, {"missing"})
        self.assertFalse(missing.candidates)
        expired = triage_traces(self.case, self.store, deadline=time.monotonic() - 1)
        self.assertEqual({c.status for c in expired.coverage}, {"not_queried"})
        self.assertTrue(expired.warnings)

    def test_memory_limit_is_partial_and_replay_reproduces_retained_prefix(self):
        self.make_traces()
        with patch("agents.rca.traces.TRACE_MEMORY_BYTES", 4000):
            bundle = triage_traces(self.case, self.store, deadline=time.monotonic() + 10)
            self.assertTrue(all(c.status == "partial" for c in bundle.coverage))
            self.assertTrue(bundle.evidence)
            for record in bundle.evidence:
                self.assertEqual(record.values, replay_evidence(record, self.store, deadline=time.monotonic() + 10))

    def test_one_covering_query_ignores_old_row_cap_and_keeps_late_data(self):
        self.make_traces()
        with patch("agents.rca.traces.MAX_ROWS_PER_QUERY", 5), patch.object(
                self.store, "iter_window", wraps=self.store.iter_window) as scanner:
            bundle = triage_traces(self.case, self.store, deadline=time.monotonic() + 10)
            self.assertEqual(scanner.call_count, 1)
        self.assertEqual([c.status for c in bundle.coverage], ["complete"])
        health = next(r for r in bundle.evidence if "pairing_quality" in r.transform)
        self.assertEqual(health.values["span_rows"], 20)
        self.assertEqual(health.values["missing_parent_rows"], 1)

    def test_repeated_metadata_compression_preserves_full_window_under_memory_guard(self):
        rows = [self.span(100 + index, f"trace-{index}", "p", "") for index in range(100)]
        for row in rows:
            row["operation_name"] = "long-synthetic-operation-" * 200
        self.write("trace", "trace_span", TRACE_COLUMNS, rows)
        self.store = CSVTelemetryStore(self.root, chunk_size=200)
        with patch("agents.rca.traces.TRACE_MEMORY_BYTES", 48000):
            bundle = triage_traces(self.case, self.store, deadline=time.monotonic() + 10)
        self.assertEqual([c.status for c in bundle.coverage], ["complete"])
        health = next(r for r in bundle.evidence if "pairing_quality" in r.transform)
        self.assertEqual(health.values["span_rows"], 100)
        self.assertEqual(health.values, replay_evidence(health, self.store, deadline=time.monotonic() + 10))

    def test_old_two_query_ledger_transforms_still_replay(self):
        self.make_traces()
        bundle = triage_traces(self.case, self.store, deadline=time.monotonic() + 10)
        queries = _legacy_queries(self.case)
        _, _, _, retained = _read_queries(queries, self.store, time.monotonic() + 10)
        for record in bundle.evidence:
            legacy = replace(record, transform=record.transform.replace(".v2", ".v1"), queries=queries,
                             transform_params=dict(record.transform_params, retained_rows_per_query=retained,
                                                   row_cap_per_query=150000))
            self.assertEqual(record.values, replay_evidence(legacy, self.store, deadline=time.monotonic() + 10))

    def test_all_observed_edges_are_analyzed_beyond_old_256_edge_cap(self):
        rows = []
        for index in range(260):
            rows += [self.span(100, str(index), "p", "", "caller"),
                     self.span(101, str(index), "c", "p", f"callee-{index}")]
        self.write("trace", "trace_span", TRACE_COLUMNS, rows)
        bundle = triage_traces(self.case, self.store, deadline=time.monotonic() + 20)
        self.assertEqual(len(bundle.edges), 260)
        self.assertFalse(any("stopped" in warning for warning in bundle.warnings))

    def test_logs_complete_scan_ignores_old_prefix_cap(self):
        self.write("log", "log_service", LOG_COLUMNS, self.logs())
        with patch("agents.rca.logs.MAX_LOG_ROWS", 1):
            bundle = search_fault_evidence(self.case, ("pod-b",), ("timeout",), ("log_service",), self.store,
                                           deadline=time.monotonic() + 10)
        record = bundle.evidence[0]
        self.assertEqual(record.values["searched_rows"], 3)
        self.assertEqual(record.values["matched_rows"], 1)
        self.assertEqual(bundle.coverage[0].status, "complete")
        legacy = replace(record, transform="logs.literal_search.v1")
        self.assertEqual(record.values, replay_evidence(legacy, self.store, deadline=time.monotonic() + 10))

    def logs(self):
        return [dict(log_id=f"synthetic-log-{i}", timestamp=self.start.timestamp() + i,
                     cmdb_id="pod-b", log_name="service", value=value)
                for i, value in enumerate(("context before", "ERROR: literal .* and Timeout, retry\nnext line", "context after"))]

    def test_logs_literal_matching_csv_multiline_context_cache_and_replay(self):
        path = self.write("log", "log_service", LOG_COLUMNS, self.logs())
        with patch.object(self.store, "iter_window", wraps=self.store.iter_window) as scanner:
            bundle = search_fault_evidence(self.case, ("pod-b",), ("TIMEOUT", ".*"), ("log_service",), self.store,
                                           deadline=time.monotonic() + 10)
            repeated = search_fault_evidence(self.case, ("pod-b",), ("TIMEOUT", ".*"), ("log_service",), self.store,
                                             deadline=time.monotonic() + 10)
            self.assertEqual(scanner.call_count, 1)
            self.assertEqual(bundle.evidence[0].values, repeated.evidence[0].values)
        record = bundle.evidence[0]
        self.assertEqual(record.values["searched_rows"], 3)
        self.assertEqual(record.values["matched_rows"], 1)
        sample = record.values["match_samples"][0]
        self.assertIn("\n", sample["text"])
        self.assertEqual(sample["record_index"], 2)
        self.assertEqual(len(sample["context"]), 2)
        self.assertEqual(record.values, replay_evidence(record, self.store, deadline=time.monotonic() + 10))
        self.assertTrue(path.exists())

    def test_logs_no_match_missing_source_and_invalid_sources(self):
        self.write("log", "log_service", LOG_COLUMNS, self.logs())
        bundle = search_fault_evidence(self.case, ("pod-b",), ("(error|timeout)",), ("log_service", "log_proxy"),
                                       self.store, deadline=time.monotonic() + 10)
        self.assertEqual(bundle.evidence[0].values["matched_rows"], 0)
        self.assertEqual(bundle.evidence[0].values["searched_rows"], 3)
        self.assertEqual(bundle.coverage[1].status, "missing")
        self.assertFalse(bundle.candidates)
        with self.assertRaises(ValueError):
            search_fault_evidence(self.case, ("pod-b",), ("error",), ("dev_answers",), self.store, deadline=time.monotonic() + 10)


@unittest.skipUnless(os.environ.get("RCA_TEST_DATA"), "RCA_TEST_DATA not provided; real telemetry check not run")
class RealTelemetrySmoke(unittest.TestCase):
    def test_real_first_label_free_query(self):
        dataset = Path(os.environ["RCA_TEST_DATA"])
        with (dataset / "query.csv").open(encoding="utf-8", newline="") as handle:
            instruction = next(csv.DictReader(handle))["instruction"]
        case = parse_case(instruction)
        store = CSVTelemetryStore(dataset)
        budget = float(os.environ.get("RCA_TEST_SECONDS", "30"))
        begun = time.monotonic()
        bundle = triage_traces(case, store, deadline=begun + budget)
        self.assertTrue(bundle.coverage)
        self.assertTrue(bundle.evidence, "No real observations processed within deadline")
        json.dumps([record.values for record in bundle.evidence], allow_nan=False)
        print(json.dumps({"budget_s": budget, "elapsed_s": round(time.monotonic() - begun, 3),
                          "real_trace_evidence": len(bundle.evidence), "candidates": len(bundle.candidates),
                          "edges": len(bundle.edges), "coverage": [c.status for c in bundle.coverage]}))


if __name__ == "__main__":
    unittest.main()
