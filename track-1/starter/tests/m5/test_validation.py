"""All facts below are synthetic test records, not real incident evidence."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import unittest

from agents.rca.contracts import (Alternative, Answer, CaseContext, Component, ComponentCatalog,
    Coverage, Decision, EvidenceRecord, QuerySpec, SourceRecord, UTC8)
from agents.rca.validation import validate_and_render


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2022, 4, 1, 9, 0, tzinfo=UTC8)
        self.case = CaseContext("synthetic", "synthetic", self.start, self.start + timedelta(minutes=30),
                                self.start - timedelta(minutes=10), 1, ("datetime", "component", "reason"))
        self.query = QuerySpec("metric_container", self.case.reference_start, self.case.end,
                               ("timestamp", "cmdb_id", "kpi_name", "value"))
        coverage = Coverage("synthetic-query", "metric_container", "complete", 4, 4)
        self.record = EvidenceRecord("synthetic:ev", "metric", ["pod-7"], (self.case.reference_start, self.case.end),
            source_files=["telemetry/2022_04_01/metric/metric_container.csv"], queries=[self.query],
            transform="synthetic.test.v1", transform_params={"semantics": "unknown"},
            values={"baseline_median": 0, "window_peak": 12}, units={"baseline_median": "native/unknown", "window_peak": "native/unknown"},
            source_records=[SourceRecord("telemetry/2022_04_01/metric/metric_container.csv", 2)], coverage=[coverage])
        self.catalog = ComponentCatalog({"pod-7": Component("pod-7", "container", ("node-a.pod-7",), "node-a", "pod"),
            "node-a": Component("node-a", "node")}, {("metric_container", "node-a.pod-7"): ("pod-7",)}, [coverage])
        self.decision = Decision([Answer(self.start + timedelta(minutes=3), "pod-7", "container read I/O load")],
                                 supporting_ids=[self.record.evidence_id], confidence="medium", stop_reason="synthetic")

    def render(self):
        return validate_and_render(self.case, self.decision, [self.record], self.catalog)

    def test_valid_four_sections_and_source_values(self):
        result = self.render()
        self.assertEqual(result.validation_status, "valid")
        self.assertEqual([line for line in result.evidence.splitlines() if line.startswith("## ")],
                         ["## Answer", "## Confidence", "## Evidence", "## Ruled out"])
        self.assertIn('"window_peak": 12', result.evidence)
        self.assertIn("native/unknown", result.evidence)
        self.assertIn("record_index=2", result.evidence)
        self.assertLess(result.prediction.index("occurrence datetime"), result.prediction.index("component"))
        self.assertLess(result.prediction.index("component"), result.prediction.index("reason"))

    def test_all_seven_requested_field_combinations(self):
        for mask in range(1, 8):
            fields = tuple(field for i, field in enumerate(("datetime", "component", "reason")) if mask & (1 << i))
            self.case.requested_fields = fields
            answer = deepcopy(self.decision.answers[0])
            for field in ("datetime", "component", "reason"):
                if field not in fields:
                    setattr(answer, field, None)
            self.decision.answers = [answer]
            result = self.render()
            self.assertNotEqual(result.validation_status, "invalid", fields)
            body = json.loads(result.prediction.removeprefix("```json\n").removesuffix("\n```"))["1"]
            self.assertEqual(len(body), len(fields))
            self.decision.answers = [Answer(self.start, "pod-7", "container read I/O load")]

    def test_wrong_count_and_layer_are_invalid(self):
        self.case.failure_count = 2
        self.assertEqual(self.render().validation_status, "invalid")
        self.case.failure_count = 1
        self.decision.answers[0].reason = "node CPU spike"
        self.assertTrue(any("conflicts" in error for error in self.render().errors))

    def test_illegal_reason_time_and_dangling_reference(self):
        self.decision.answers[0].reason = "imaginary"
        self.decision.answers[0].datetime = self.case.end
        self.decision.supporting_ids.append("not-real")
        result = self.render()
        self.assertEqual(result.validation_status, "invalid")
        self.assertTrue(any("illegal reason" in error for error in result.errors))
        self.assertTrue(any("outside" in error for error in result.errors))
        self.assertTrue(any("Dangling" in error for error in result.errors))

    def test_partial_unknown_catalog_is_degraded_not_healthy(self):
        self.decision.answers[0].component = "new-unobserved-pod"
        self.record.coverage[0].status = "partial"
        result = self.render()
        self.assertEqual(result.validation_status, "degraded")
        self.assertIn("Low.", result.evidence)
        self.assertIn("partial", result.evidence)
        self.assertIn("not establish health", result.evidence)

    def test_complete_query_does_not_make_incremental_catalog_exhaustive(self):
        self.decision.answers[0].component = "new-unobserved-pod"
        self.assertEqual(self.render().validation_status, "degraded")

    def test_observed_raw_alias_and_aggregate_are_invalid(self):
        for component in ("node-a.pod-7", "pod"):
            self.decision.answers[0].component = component
            self.assertEqual(self.render().validation_status, "invalid")

    def test_alternative_refs_and_nonfinite_values(self):
        self.decision.alternatives = [Alternative("other", "excluded", [], "unsupported")]
        self.record.values["window_peak"] = float("nan")
        result = self.render()
        self.assertEqual(result.validation_status, "invalid")
        self.assertIn("Invalid structured values", result.evidence)

    def test_deterministic_without_mutating_decision(self):
        before = deepcopy(self.decision)
        first, second = self.render(), self.render()
        self.assertEqual(first, second)
        self.assertEqual(before, self.decision)

    def test_missing_reproducible_provenance_degrades(self):
        self.record.queries = []
        result = self.render()
        self.assertEqual(result.validation_status, "degraded")
        self.assertTrue(any("provenance" in warning for warning in result.warnings))

    def test_unsafe_source_and_duplicate_conflict(self):
        duplicate = deepcopy(self.record)
        duplicate.values["window_peak"] = 99
        result = validate_and_render(self.case, self.decision, [self.record, duplicate], self.catalog)
        self.assertEqual(result.validation_status, "invalid")
        self.record.source_files = ["../answers.csv"]
        self.assertEqual(self.render().validation_status, "invalid")
        for source in ("telemetry/2022_99_01/metric/metric_container.csv", "telemetry/2022_04_01/trace/metric_container.csv"):
            self.record.source_files = [source]
            self.record.source_records = []
            self.assertEqual(self.render().validation_status, "invalid")

    def test_ambiguous_order_disclosed_and_ordered(self):
        self.case.requested_fields = ("component",)
        self.case.failure_count = 2
        self.decision.answers = [Answer(component="pod-7"), Answer(component="node-a")]
        result = self.render()
        self.assertIn("order is deterministic", result.evidence)
        self.assertLess(result.prediction.index("node-a"), result.prediction.index("pod-7"))

    def test_additional_records_bounded_but_all_support_shown(self):
        records = []
        for index in range(50):
            record = deepcopy(self.record)
            record.evidence_id = f"synthetic:{index:02}"
            records.append(record)
        self.decision.supporting_ids = [records[-1].evidence_id]
        result = validate_and_render(self.case, self.decision, records, self.catalog)
        self.assertEqual(result.evidence.count("- Evidence `"), 25)
        self.assertIn("synthetic:49", result.evidence)


if __name__ == "__main__":
    unittest.main()
