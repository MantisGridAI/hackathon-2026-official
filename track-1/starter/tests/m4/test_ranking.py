from dataclasses import replace
from datetime import timedelta
import json
import unittest

from tests.m4.helpers import bundle, candidate, case, evidence, Store
from agents.rca.contracts import AnalysisBundle, DependencyEdge
from agents.rca.prompts import build_messages, validate_selection
from agents.rca.ranking import (EvidenceCollision, choose_candidates, deterministic_gate,
    make_decision, merge_bundles, prepare_candidates, rank_candidates)


class RankingTests(unittest.TestCase):
    def test_conflicting_stable_id_is_error(self):
        first = bundle()
        second = AnalysisBundle("m3", evidence=[replace(first.evidence[0], values={"value": 99})])
        with self.assertRaises(EvidenceCollision):
            merge_bundles([first, second])

    def test_identical_evidence_is_deduplicated(self):
        self.assertEqual(len(merge_bundles([bundle(), bundle()])[1]), 1)

    def test_unknown_reference_is_error(self):
        with self.assertRaises(EvidenceCollision):
            prepare_candidates(case(), [candidate(eid="missing")], Store().catalog, [evidence()])

    def test_correlated_metrics_merge_without_multiplying_score(self):
        first = candidate()
        duplicate = replace(first, candidate_id="c2")
        prepared = prepare_candidates(case(), [first, duplicate], Store().catalog, [evidence()])
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0].features["metrics.strength"], 12)
        self.assertEqual(len(prepared[0].features["m4.provenance"]), 2)

    def test_unknown_network_expands_only_weak_hypotheses(self):
        source = candidate(reason=None)
        source.features = {"traces.network_family": True}
        expanded = prepare_candidates(case(), [source], Store().catalog, [evidence()])
        self.assertEqual(len(expanded), 4)
        self.assertTrue(all(c.features["m4.weak_reason"] and c.unresolved for c in expanded))
        self.assertTrue(all(c.supporting_ids == ["e1"] for c in expanded))

    def test_same_component_different_reasons_not_two_faults(self):
        inputs = [candidate(), candidate("c2", reason="container memory load")]
        ranked = rank_candidates(case(2), inputs, [evidence()])
        self.assertEqual(len(choose_candidates(ranked, 2)), 1)

    def test_fused_weak_reason_hypotheses_have_distinct_ids(self):
        first = candidate(key="trace-op-a", reason=None)
        first.features = {"traces.network_family": True}
        second = replace(first, candidate_id="trace-op-b")
        prepared = prepare_candidates(case(), [first, second], Store().catalog, [evidence()])
        self.assertEqual(len(prepared), 4)
        self.assertEqual(len({c.candidate_id for c in prepared}), 4)
        for hypothesis in prepared:
            payload = dict(selected_candidate_ids=[hypothesis.candidate_id], confidence="low",
                           supporting_evidence_ids=["e1"], unresolved=[], next_query=None)
            reply = validate_selection(json.dumps(payload), case(), prepared, [evidence()])
            self.assertEqual(reply["selected_candidate_ids"], [hypothesis.candidate_id])

    def test_disjoint_episodes_can_be_two_faults_same_component(self):
        other = candidate("c2")
        other.onset_interval = (case().start + timedelta(minutes=5), case().start + timedelta(minutes=6))
        self.assertEqual(len(choose_candidates(rank_candidates(case(2), [candidate(), other], [evidence()]), 2)), 2)

    def test_gate_does_not_treat_anomaly_as_mechanism(self):
        ranked = rank_candidates(case(), [candidate()], [evidence()])
        self.assertFalse(deterministic_gate(case(), ranked, [candidate()], [evidence()]))

    def test_shared_network_edge_is_not_two_independent_faults(self):
        caller = candidate("caller", "synthetic-caller", eid="edge-observation")
        caller.features = {"network.anomaly_score": 5., "traces.network_family": True}
        caller.onset_interval = None
        callee = replace(caller, candidate_id="callee", component="synthetic-callee")
        independent = candidate("independent", "synthetic-independent", eid="independent-observation")
        independent.features = {"metrics.strength": 1.}
        records = [evidence("edge-observation", kind="trace"), evidence("independent-observation")]
        ranked = rank_candidates(case(2), [caller, callee, independent], records)
        edges = [DependencyEdge("edge", caller.component, callee.component, supporting_ids=["edge-observation"])]
        selected = choose_candidates(ranked, 2, edges)
        self.assertIn(independent, selected)
        self.assertEqual(len(selected), 2)
        bad = dict(selected_candidate_ids=["caller", "callee"], confidence="low",
                   supporting_evidence_ids=["edge-observation"], unresolved=[], next_query=None)
        with self.assertRaises(ValueError):
            validate_selection(json.dumps(bad), case(2), [caller, callee, independent], records)

    def test_exact_count_with_empty_catalog(self):
        catalog = Store().catalog
        catalog.components.clear()
        decision = make_decision(case(3), [], [], catalog)
        self.assertEqual(len(decision.answers), 3)
        self.assertTrue(any("placeholder" in text for text in decision.limitations))

    def test_model_cannot_invent_ids_or_queries(self):
        c, e = candidate(), evidence()
        base = dict(selected_candidate_ids=["c1"], confidence="low", supporting_evidence_ids=["e1"], unresolved=[], next_query=None)
        self.assertEqual(validate_selection(json.dumps(base), case(), [c], [e]), base)
        for key, value in (("selected_candidate_ids", ["invented"]),
                           ("supporting_evidence_ids", ["invented"]),
                           ("next_query", {"python": "unsafe"})):
            with self.assertRaises(ValueError):
                validate_selection(json.dumps({**base, key: value}), case(), [c], [e])

    def test_prompt_contains_no_raw_instruction_or_scoring_fields(self):
        c = case()
        c.instruction = "scoring_points SECRET"
        messages, _, _ = build_messages(c, rank_candidates(c, [candidate()], [evidence()]), [evidence()], stage="strong")
        self.assertNotIn("SECRET", str(messages))
        self.assertIn("synthetic.fixture.v1", str(messages))


if __name__ == "__main__":
    unittest.main()
