"""Synthetic tests for complete-window reuse; no real case answers are used."""
from dataclasses import replace
from unittest.mock import patch
import time
import unittest

import pandas as pd

from tests.m1 import test_runtime_store as fixtures

BASE = fixtures.BASE


class WindowCacheTests(unittest.TestCase):
    setUp = fixtures.StoreTests.setUp
    tearDown = fixtures.StoreTests.tearDown
    write = fixtures.StoreTests.write
    query = fixtures.StoreTests.query
    read = fixtures.StoreTests.read

    def test_covering_window_filters_projection_and_original_records(self):
        t = BASE.timestamp()
        self.write("metric_container", [
            [t + 80, "n.p-1", "cpu", 8], [t + 10, "n.p-2", "cpu", 2],
            [t + 40, "n.p-1", "memory", 4], [t + 30, "n.p-1", "cpu", 3],
            [t + 60, "n.p-1", "cpu", 6]])
        broad = self.query()
        self.read(broad)
        narrow = replace(broad, start=BASE + pd.Timedelta(seconds=20),
                         end=BASE + pd.Timedelta(seconds=60),
                         columns=("timestamp", "value"),
                         component_ids=("p-1",), kpi_names=("cpu",))
        with patch("agents.rca.data_access.pd.read_csv", side_effect=AssertionError("unexpected scan")):
            result = pd.concat(self.read(narrow))
        self.assertEqual(result._record_index.tolist(), [4])
        self.assertEqual(result.value.tolist(), [3])
        cov = self.store.coverage(narrow)
        self.assertEqual(cov.status, "complete")
        self.assertEqual(cov.rows_matched, 1)
        self.assertEqual(cov.first_time, BASE + pd.Timedelta(seconds=30))

    def test_trace_filters_from_covering_window_preserve_units_and_ids(self):
        self.write("trace_span", [
            [BASE.timestamp()*1000, "api-1", "0001", "0007", 900, "rpc", "0", "Get", ""],
            [(BASE.timestamp()+1)*1000, "api-2", "0002", "0007", 100, "rpc", "0", "Put", "0001"]])
        broad = self.query("trace_span")
        self.read(broad)
        q = replace(broad, component_ids=("api-1",), operation_names=("Get",))
        with patch("agents.rca.data_access.pd.read_csv", side_effect=AssertionError("unexpected scan")):
            result = self.read(q)[0]
        self.assertEqual(result.span_id.tolist(), ["0001"])
        self.assertEqual(result.duration.tolist(), [900])
        self.assertEqual(result.timestamp.tolist(), [BASE.timestamp()*1000])

    def test_cached_multiline_content_matches_cold_read(self):
        self.write("log_service", [["0001", BASE.timestamp(), "a-1", "log", "a,b\nnext"],
                                   ["0002", BASE.timestamp()+1, "a-2", "log", "ok"]])
        broad = self.query("log_service")
        self.read(broad)
        q = replace(broad, component_ids=("a-1",))
        warm = pd.concat(self.read(q), ignore_index=True)
        self.store._cache.clear()
        self.store._cache_size = 0
        cold = pd.concat(self.read(q), ignore_index=True)
        pd.testing.assert_frame_equal(warm, cold)

    def test_partial_window_never_proves_complete_coverage(self):
        self.write("metric_container", [[BASE.timestamp()+i, "n.p-1", "cpu", i] for i in range(5)])
        broad = self.query()
        it = self.store.iter_window(broad, deadline=time.monotonic()+10)
        next(it)
        it.close()
        self.assertFalse(self.store._cache)
        with patch("agents.rca.data_access.pd.read_csv", wraps=pd.read_csv) as reader:
            self.read(replace(broad, component_ids=("p-1",)))
        reader.assert_called_once()

    def test_cached_early_close_and_deadline_remain_partial(self):
        self.write("metric_container", [[BASE.timestamp()+i, "n.p-1", "cpu", i] for i in range(5)])
        broad = self.query()
        self.read(broad)
        q = replace(broad, component_ids=("p-1",))
        it = self.store.iter_window(q, deadline=time.monotonic()+10)
        next(it)
        it.close()
        self.assertEqual(self.store.coverage(q).status, "partial")
        self.assertEqual(self.store.coverage(q).rows_matched, 2)
        self.assertEqual(list(self.store.iter_window(q, deadline=time.monotonic()-1)), [])
        self.assertEqual(self.store.coverage(q).status, "partial")

    def test_narrow_or_different_filter_cannot_cover_broader_query(self):
        self.write("metric_container", [[BASE.timestamp(), "n.p-1", "cpu", 1],
                                       [BASE.timestamp(), "n.p-2", "memory", 2]])
        q = self.query(component_ids=("p-1",), kpi_names=("cpu",))
        self.read(q)
        for other in (self.query(), replace(q, component_ids=("p-2",)),
                      replace(q, kpi_names=("memory",))):
            self.store._cache = type(self.store._cache)(list(self.store._cache.items())[:1])
            with patch("agents.rca.data_access.pd.read_csv", wraps=pd.read_csv) as reader:
                self.read(other)
            reader.assert_called_once()

    def test_missing_projection_column_requires_scan(self):
        self.write("metric_container", [[BASE.timestamp(), "n.p-1", "cpu", 1]])
        q = replace(self.query(), columns=("timestamp",))
        self.read(q)
        with patch("agents.rca.data_access.pd.read_csv", wraps=pd.read_csv) as reader:
            self.read(self.query())
        reader.assert_called_once()

    def test_changed_file_invalidates_covering_cache(self):
        self.write("metric_container", [[BASE.timestamp(), "n.p-1", "cpu", 1]])
        self.read(self.query())
        self.write("metric_container", [[BASE.timestamp(), "n.p-1", "cpu", 123456]])
        q = self.query(component_ids=("p-1",))
        with patch("agents.rca.data_access.pd.read_csv", wraps=pd.read_csv) as reader:
            result = self.read(q)[0]
        reader.assert_called_once()
        self.assertEqual(result.value.tolist(), [123456])

    def test_file_changed_during_scan_does_not_enter_cache(self):
        self.write("metric_container", [[BASE.timestamp()+i, "n.p-1", "cpu", i] for i in range(5)])
        q = self.query()
        before = self.store._file_identity(list(self.store._paths(q)))
        after = tuple((*entry[:-1], -1) for entry in before)
        with patch.object(self.store, "_file_identity", side_effect=(before, after)):
            self.read(q)
        self.assertFalse(self.store._cache)
        self.assertEqual(self.store.coverage(q).status, "partial")
        self.assertIn("changed during scan", " ".join(self.store.coverage(q).warnings))

    def test_coverage_query_spanning_missing_day_never_cached(self):
        self.write("metric_container", [[BASE.timestamp(), "n.p-1", "cpu", 1]])
        q = replace(self.query(), end=BASE+pd.Timedelta(days=1))
        self.read(q)
        self.assertFalse(self.store._cache)
        self.assertEqual(self.store.coverage(q).status, "partial")

    def test_cached_path_still_rejects_escaping_symlink(self):
        path = self.write("metric_container", [[BASE.timestamp(), "n.p-1", "cpu", 1]])
        q = self.query()
        self.read(q)
        # Only a synthetic fixture is replaced, never real telemetry.
        path.unlink()
        path.symlink_to(self.root.parent / "outside-synthetic-dataset.csv")
        with self.assertRaises(fixtures.QueryValidationError):
            self.read(q)

    def test_service_cache_recomputes_newly_observed_mapping(self):
        self.write("metric_service", [["api", BASE.timestamp(), 1, 1, 1, 1]])
        self.read(self.query("metric_service"))
        filtered = self.query("metric_service", component_ids=("api-1",))
        self.read(filtered)
        self.assertEqual(self.store.coverage(filtered).status, "partial")
        self.write("metric_container", [[BASE.timestamp(), "n.api-1", "cpu", 1]])
        self.read(self.query())
        with patch("agents.rca.data_access.pd.read_csv", side_effect=AssertionError("unexpected scan")):
            result = self.read(filtered)[0]
        self.assertEqual(len(result), 1)
        self.assertEqual(self.store.coverage(filtered).status, "complete")
        self.assertIn("cannot isolate", " ".join(self.store.coverage(filtered).warnings))

    def test_zero_cache_budget_and_small_budget_are_bounded(self):
        self.write("metric_container", [[BASE.timestamp()+i, "n.p-1", "cpu", i] for i in range(5)])
        self.store.cache_bytes = 0
        self.read(self.query())
        self.assertFalse(self.store._cache)
        self.assertEqual(self.store._cache_size, 0)
        self.store.cache_bytes = 1
        self.read(self.query())
        self.assertFalse(self.store._cache)
        self.assertEqual(self.store._cache_size, 0)

    def test_cached_output_cannot_mutate_later_reads(self):
        self.write("metric_container", [[BASE.timestamp(), "n.p-1", "cpu", 1]])
        broad = self.query()
        cold = self.read(broad)[0]
        cold.loc[0, "value"] = 111
        q = replace(broad, component_ids=("p-1",))
        warm = self.read(q)[0]
        warm.loc[0, "value"] = 222
        self.assertEqual(self.read(q)[0].value.tolist(), [1])

    def test_lru_memory_accounting_and_eviction(self):
        self.write("metric_container", [[BASE.timestamp(), "n.p-1", "cpu", 1],
                                       [BASE.timestamp(), "n.p-2", "cpu", 2]])
        first = self.query(component_ids=("p-1",))
        second = self.query(component_ids=("p-2",))
        self.read(first)
        one_entry = self.store._cache_size
        self.store.cache_bytes = one_entry + 1
        self.read(second)
        self.assertEqual(len(self.store._cache), 1)
        self.assertEqual(self.store._cache_size, one_entry)
        with patch("agents.rca.data_access.pd.read_csv", wraps=pd.read_csv) as reader:
            self.read(first)
        reader.assert_called_once()
        self.assertLessEqual(self.store._cache_size, self.store.cache_bytes)
