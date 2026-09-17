"""Opt-in smoke against official telemetry; never opens development labels."""
import csv
import os
from pathlib import Path
import time
import unittest

from agents.rca.contracts import QuerySpec
from agents.rca.data_access import CSVTelemetryStore
from agents.rca.runtime import parse_case


@unittest.skipUnless(os.environ.get("RCA_TEST_DATA"), "RCA_TEST_DATA is required for real telemetry checks")
class RealStoreTests(unittest.TestCase):
    def test_official_query_window_and_record_replay(self):
        dataset = Path(os.environ["RCA_TEST_DATA"])
        with (dataset / "query.csv").open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        expected_fields = {"task_1": ("datetime",), "task_2": ("reason",), "task_3": ("component",),
                           "task_4": ("datetime", "reason"), "task_5": ("datetime", "component"),
                           "task_6": ("component", "reason"), "task_7": ("datetime", "component", "reason")}
        for row in rows:
            self.assertEqual(parse_case(row["instruction"]).requested_fields, expected_fields[row["task_index"]])
        case = parse_case(rows[0]["instruction"])
        store = CSVTelemetryStore(dataset)
        query = QuerySpec("metric_node", case.start, case.end, ("timestamp", "cmdb_id", "kpi_name", "value"))
        iterator = store.iter_window(query, deadline=time.monotonic()+30)
        frames = list(iterator)
        self.assertTrue(frames)
        self.assertEqual(store.coverage(query).status, "complete")
        sample = frames[0].iloc[0]
        with (dataset / sample._source_file).open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for index, row in enumerate(reader, 1):
                if index == sample._record_index:
                    self.assertEqual(row["cmdb_id"], sample.cmdb_id)
                    self.assertEqual(float(row["value"]), sample.value)
                    break
            else:
                self.fail("Source record locator was not found")


if __name__ == "__main__":
    unittest.main()
