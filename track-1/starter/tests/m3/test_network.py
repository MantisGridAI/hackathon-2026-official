"""Synthetic records only: these fixtures are never runtime evidence."""
import unittest
import time

import pandas as pd

from agents.rca.network import edge_values, pair_spans, pair_spans_compact


def span(trace, span_id, parent, component, timestamp, kind="rpc"):
    return dict(trace_id=trace, span_id=span_id, parent_span=parent,
                _component_id=component, _timestamp_s=timestamp,
                operation_name="synthetic-op", type=kind)


class PairingTests(unittest.TestCase):
    def test_vectorized_pairing_exactly_matches_legacy_pathologies(self):
        frame = pd.DataFrame([
            span("t1", "p", "", "caller-a", 10),
            span("t2", "p", "", "caller-b", 20),
            span("t2", "c", "p", "callee", 19, "db"),
            span("t3", "c", "p", "orphan", 22),
            span("dup", "p", "", "first", 10),
            span("dup", "p", "", "second", 10),
            span("dup", "c", "p", "callee", 11),
            span("dc", "p", "", "parent", 10),
            span("dc", "c", "p", "child", 11),
            span("dc", "c", "p", "child", 11),
            span("self", "s", "s", "loop", 10),
            span("", "missing-trace", "p", "invalid", 10),
            span("no-component", "p", "", "", 10),
            span("no-component", "c", "p", "child", 11),
            span("null-component", "p", "", None, 10),
            span("null-component", "c", "p", "child", 11),
            span("async", "p", "", "caller", 100, "http"),
            span("async", "c", "p", "callee", 105, "telemetry"),
        ])
        legacy, expected = pair_spans(frame)
        frame["_component_id"] = frame["_component_id"].astype("category")
        frame["type"] = frame["type"].astype("category")
        compact, observed = pair_spans_compact(frame, deadline=time.monotonic() + 5)
        self.assertEqual(expected, observed)
        self.assertEqual(set(legacy), set(compact))
        for key in legacy:
            self.assertEqual(edge_values(legacy[key], 0, 20, 120), edge_values(compact[key], 0, 20, 120))
        with self.assertRaises(TimeoutError):
            pair_spans_compact(frame, deadline=time.monotonic() - 1)

    def test_same_span_id_across_traces_does_not_cross_join(self):
        frame = pd.DataFrame([
            span("t1", "p", "", "caller-a", 10),
            span("t2", "p", "", "caller-b", 20),
            span("t2", "c", "p", "callee", 21),
            span("t3", "c", "p", "orphan", 22),
        ])
        groups, counts = pair_spans(frame)
        self.assertEqual(list(groups), [("caller-b", "callee", "synthetic-op")])
        self.assertEqual(counts["missing_parent_rows"], 1)
        self.assertEqual(counts["paired_rows"], 1)

    def test_duplicate_parent_never_produces_trusted_edge(self):
        frame = pd.DataFrame([
            span("t", "p", "", "first", 10),
            span("t", "p", "", "second", 10),
            span("t", "c", "p", "callee", 11),
        ])
        groups, counts = pair_spans(frame)
        self.assertEqual(groups, {})
        self.assertEqual(counts["duplicate_key_count"], 1)
        self.assertEqual(counts["ambiguous_parent_rows"], 1)

    def test_negative_gap_preserved_and_duration_not_subtracted(self):
        frame = pd.DataFrame([
            span("t", "p", "", "caller", 20, "http"),
            span("t", "c", "p", "callee", 19, "db"),
        ])
        groups, counts = pair_spans(frame)
        values = edge_values(next(iter(groups.values())), 0, 10, 30)
        self.assertEqual(counts["negative_start_gap_rows"], 1)
        self.assertEqual(values["incident"]["start_gap_median_s"], -1)
        self.assertEqual(values["incident"]["type_pair_counts"], {"http->db": 1})
        self.assertIsNone(values["causal_direction"])

    def test_frequency_uses_each_period_duration(self):
        frame = pd.DataFrame([
            span("a", "p", "", "caller", 1), span("a", "c", "p", "callee", 2),
            span("b", "p", "", "caller", 11), span("b", "c", "p", "callee", 12),
        ])
        groups, _ = pair_spans(frame)
        values = edge_values(next(iter(groups.values())), 0, 10, 30)
        self.assertEqual(values["baseline"]["observed_pairs_per_minute"], 6)
        self.assertEqual(values["incident"]["observed_pairs_per_minute"], 3)


if __name__ == "__main__":
    unittest.main()
