"""Synthetic boundary tests; none of these records is real RCA evidence."""
import csv
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

import pandas as pd

from agents.rca.contracts import *
from agents.rca.data_access import CSVTelemetryStore, SOURCES
from agents.rca.runtime import parse_case, get_run_state

BASE = datetime(2022, 3, 20, 9, tzinfo=UTC8)


class ParserTests(unittest.TestCase):
    def test_seven_field_sets(self):
        for wanted in (("datetime",), ("reason",), ("component",), ("datetime", "reason"),
                       ("datetime", "component"), ("component", "reason"), ("datetime", "component", "reason")):
            text = "Two failures on March 20, 2022, from 09:00 to 09:30. You are tasked with identifying " + " and ".join("root cause " + x for x in wanted)
            case = parse_case(text)
            self.assertEqual(case.requested_fields, wanted)
            self.assertEqual(case.failure_count, 2)
            self.assertEqual(case.start.astimezone(timezone.utc).hour, 1)

    def test_midnight_explicit_and_implicit(self):
        for end in ("00:15", "March 21, 2022, at 00:15"):
            case = parse_case("One failure March 20, 2022, from 23:45 to " + end + ". Identify root cause reason")
            self.assertEqual(case.end - case.start, timedelta(minutes=30))
            self.assertEqual(case.reference_start, case.start - timedelta(minutes=10))

    def test_invalid_and_degraded(self):
        for text in ("no dates", "March 20, 2022 29:00 to 30:00", "March 20, 2022 12:00 to March 19, 2022 12:30"):
            with self.assertRaises(CaseParseError):
                parse_case(text)
        self.assertEqual(parse_case("March 20, 2022 09:00 to 09:30").parse_status, "degraded")

    def test_paraphrased_request_clause(self):
        case = parse_case("A failure March 20, 2022 from 09:00 to 09:30. You need to identify and determine the root cause occurrence time and the reason behind the failure.")
        self.assertEqual(case.requested_fields, ("datetime", "reason"))

    def test_labels_match_official_docs(self):
        official = (Path(__file__).resolve().parents[3] / "docs/data.md").read_text(encoding="utf-8")
        self.assertEqual(len(LEGAL_REASONS), 15)
        for reason in LEGAL_REASONS:
            self.assertIn("`" + reason + "`", official)

    def test_stable_ids_and_no_shared_defaults(self):
        a = stable_id("case", "m2.evidence", {"when": BASE, "x": 2})
        self.assertEqual(a, stable_id("case", "m2.evidence", {"x": 2, "when": BASE}))
        self.assertNotEqual(a, stable_id("case", "m2.evidence", {"x": 3, "when": BASE}))
        with self.assertRaises(ValueError):
            stable_id("c", "n", {"bad": float("nan")})
        first, second = AnalysisBundle("m2"), AnalysisBundle("m2")
        first.warnings.append("x")
        self.assertFalse(second.warnings)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = CSVTelemetryStore(self.root, chunk_size=2)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, source, rows, day="2022_03_20"):
        folder, columns = SOURCES[source]
        path = self.root / "telemetry" / day / folder / (source + ".csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            writer.writerows(rows)
        return path

    def query(self, source="metric_container", **kwargs):
        return QuerySpec(source, BASE, BASE + timedelta(minutes=30), SOURCES[source][1], **kwargs)

    def read(self, query):
        return list(self.store.iter_window(query, deadline=time.monotonic() + 10))

    def test_unsorted_half_open_original_record_numbers(self):
        t = BASE.timestamp()
        self.write("metric_container", [[t + 1900, "node-x.pod-1", "cpu", 99],
            [t + 10, "node-x.pod-1", "cpu", 2], [t - 1, "node-x.pod-1", "cpu", 1],
            [t, "node-x.pod-1", "cpu", 3], [t + 1800, "node-x.pod-1", "cpu", 10]])
        q = self.query()
        result = pd.concat(self.read(q))
        self.assertEqual(result._record_index.tolist(), [2, 4])
        self.assertEqual(result._component_id.tolist(), ["pod-1", "pod-1"])
        self.assertEqual(self.store.coverage(q).status, "complete")
        self.assertEqual(self.store.coverage(q).rows_scanned, 5)
        self.assertFalse(Path(result._source_file.iloc[0]).is_absolute())
        self.assertEqual(self.store.component_catalog().components["pod-1"].node_id, "node-x")

    def test_trace_milliseconds_and_raw_duration(self):
        self.write("trace_span", [[BASE.timestamp()*1000, "api-1", "0001", "0007", 900, "rpc", "0", "Get", ""]])
        frame = self.read(self.query("trace_span"))[0]
        self.assertEqual(frame.timestamp.iloc[0], BASE.timestamp()*1000)
        self.assertEqual(frame._timestamp_s.iloc[0], BASE.timestamp())
        self.assertEqual(frame.duration.iloc[0], 900)
        self.assertEqual(frame.span_id.iloc[0], "0001")

    def test_pod_name_beginning_with_node_is_not_a_node(self):
        self.write("trace_span", [[BASE.timestamp()*1000, "node-service-0", "s", "t", 5, "rpc", "0", "Get", ""]])
        self.read(self.query("trace_span"))
        self.assertEqual(self.store.component_catalog().components["node-service-0"].kind, "container")

    def test_csv_commas_multiline_and_empty_filters(self):
        self.write("log_service", [["1", BASE.timestamp(), "api-1", "log", "hello, world\nsecond line"],
            ["2", BASE.timestamp() + 1, "api-2", "log", "OK"]])
        q = self.query("log_service")
        frame = self.read(q)[0]
        self.assertEqual(frame.value.iloc[0], "hello, world\nsecond line")
        self.assertEqual(frame._record_index.tolist(), [1, 2])
        empty = replace(q, component_ids=())
        self.assertEqual(self.read(empty), [])
        self.assertEqual(self.store.coverage(empty).status, "empty")

    def test_coverage_lifecycle_deadline_and_early_close(self):
        self.write("metric_container", [[BASE.timestamp()+i, "n.p-1", "cpu", i] for i in range(5)])
        q = self.query()
        self.assertEqual(self.store.coverage(q).status, "not_queried")
        it = self.store.iter_window(q, deadline=time.monotonic()+10)
        next(it)
        it.close()
        self.assertEqual(self.store.coverage(q).status, "partial")
        self.assertEqual(list(self.store.iter_window(q, deadline=time.monotonic()-1)), [])
        self.assertEqual(self.store.coverage(q).status, "partial")
        self.read(q)
        cached = self.read(q)
        self.assertEqual(sum(len(f) for f in cached), 5)
        self.assertEqual(self.store.coverage(q).status, "complete")

    def test_missing_partial_failed(self):
        q = self.query()
        self.assertEqual(self.read(q), [])
        self.assertEqual(self.store.coverage(q).status, "missing")
        self.assertIsNone(self.store.coverage(q).missing_value_count)
        path = self.write("metric_container", [[BASE.timestamp(), "n.p-1", "cpu", 1]])
        cross = replace(q, end=BASE + timedelta(days=1))
        self.read(cross)
        self.assertEqual(self.store.coverage(cross).status, "partial")
        path.write_text("wrong,columns\n1,2\n", encoding="utf-8")
        self.read(q)
        self.assertEqual(self.store.coverage(q).status, "failed")

    def test_validation_and_path_traversal(self):
        for q in (replace(self.query(), source="../../dev/query_dev"), replace(self.query(), columns=("scoring_points",)),
                  replace(self.query(), operation_names=("Get",)), replace(self.query(), start=BASE.replace(tzinfo=None))):
            with self.assertRaises(QueryValidationError):
                self.store.iter_window(q, deadline=time.monotonic()+1)

    def test_service_filter_unknown_is_partial_then_observed(self):
        self.write("metric_service", [["api", BASE.timestamp(), 1, 1, 1, 1]])
        q = self.query("metric_service", component_ids=("api-1",))
        self.assertFalse(self.read(q))
        self.assertEqual(self.store.coverage(q).status, "partial")
        self.write("metric_container", [[BASE.timestamp(), "node-1.api-1", "cpu", 1]])
        self.read(self.query())
        result = self.read(q)[0]
        self.assertIsNone(result._component_id.iloc[0])
        self.assertIn("cannot isolate", " ".join(self.store.coverage(q).warnings))

    def test_state_identity_invocation_and_startup_budget(self):
        ctx = {"out_dir": self.root / "out", "started_monotonic": time.monotonic()-5}
        state = get_run_state(self.root, ctx, RunConfig())
        self.assertEqual(state.invocation_index, 1)
        self.assertIs(state, get_run_state(self.root, ctx, RunConfig()))
        self.assertEqual(state.invocation_index, 2)
        self.assertLess(state.deadline, time.monotonic()+state.config.run_soft_seconds-4)
        with self.assertRaises(ValueError):
            get_run_state(self.root / "other", ctx, RunConfig())


if __name__ == "__main__":
    unittest.main()
