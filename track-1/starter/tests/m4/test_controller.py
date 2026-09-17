from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.m4.helpers import bundle, candidate, case, evidence, response, state, Transport
from agents.rca.contracts import AnalysisBundle, InvestigationResult, RenderedResult
from agents.rca.controller import investigate
from agents.rca.ranking import make_decision
from agents.rca.routing import CHEAP


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = state(self.tmp.name, mode="deterministic")
        self.metrics = SimpleNamespace(triage_metrics=Mock(return_value=bundle()),
            compare_replicas_and_node=Mock(return_value=AnalysisBundle("m2")))
        self.traces = SimpleNamespace(triage_traces=Mock(return_value=AnalysisBundle("m3")),
            inspect_dependencies=Mock(return_value=AnalysisBundle("m3")))
        self.modules = patch.dict("sys.modules", {"agents.rca.metrics": self.metrics,
                                                 "agents.rca.traces": self.traces})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def run_case(self):
        return investigate(case(), self.state, deadline=time.monotonic() + 30)

    def test_trace_runs_even_when_metric_triage_fails(self):
        self.metrics.triage_metrics.side_effect = ValueError("synthetic failure")
        result = self.run_case()
        self.traces.triage_traces.assert_called_once()
        self.assertTrue(any("tool failed" in s for s in result.decision.limitations))
        self.assertEqual(len(result.decision.answers), 1)

    def test_at_most_one_followup(self):
        self.run_case()
        self.assertEqual(self.metrics.compare_replicas_and_node.call_count +
                         self.traces.inspect_dependencies.call_count, 1)

    def test_all_models_fail_keeps_best_guess_and_real_usage(self):
        self.state.config.mode = "routed"
        self.state.client = Transport([lambda m: response(m, None, None, None, "provider_error_body")] * 4)
        result = self.run_case()
        self.assertEqual(result.decision.answers[0].component, "synthetic-pod")
        self.assertEqual(len(self.state.client.calls), 4)
        self.assertEqual(sum(v["calls"] for v in result.fallback.usage_delta.values()), 4)
        self.assertEqual(len([e for e in result.decision.route_events if e["event"] == "request"]), 4)

    def test_model_choice_has_no_authority_to_create_facts(self):
        self.state.config.mode = "routed"
        reply = dict(selected_candidate_ids=["c1"], confidence="high",
            supporting_evidence_ids=["e1"], unresolved=[], next_query=None)
        self.state.client = Transport([lambda m: response(m, json.dumps(reply))])
        result = self.run_case()
        self.assertEqual(result.decision.confidence, "medium")
        self.assertEqual(result.evidence[0].values, {"value": 2.})
        self.assertEqual(result.decision.stop_reason, "flash_selection")

    def test_partial_coverage_caps_confidence(self):
        self.metrics.triage_metrics.return_value.evidence[0].coverage[0].status = "partial"
        self.state.config.mode = "routed"
        self.state.config.max_model_stages = 1
        reply = dict(selected_candidate_ids=["c1"], confidence="high",
                     supporting_evidence_ids=["e1"], unresolved=[], next_query=None)
        self.state.client = Transport([lambda m: response(m, json.dumps(reply))])
        self.assertEqual(self.run_case().decision.confidence, "low")

    def test_invalid_renderer_retries_only_once_and_settles_usage(self):
        from agents import routed
        preferred = make_decision(case(), [candidate()], [], self.state.store.catalog)
        result = InvestigationResult(preferred, deepcopy(preferred), [evidence()])
        def investigated(*args, **kwargs):
            self.state.usage_ledger[CHEAP[0]] = dict(calls=1, prompt_tokens=9, completion_tokens=3)
            return result
        renderer = Mock(return_value=RenderedResult("", "", "invalid", errors=["synthetic invalid"] ))
        with patch.object(routed, "get_run_state", return_value=self.state), \
             patch.object(routed, "parse_case", return_value=case()), \
             patch("agents.rca.controller.investigate", side_effect=investigated), \
             patch.dict("sys.modules", {"agents.rca.validation": SimpleNamespace(validate_and_render=renderer)}):
            solution = routed.solve("synthetic", self.state.out_dir, {})
        self.assertEqual(renderer.call_count, 2)
        self.assertEqual(solution.usage[CHEAP[0]]["prompt_tokens"], 9)
        self.assertIn("synthetic-pod", solution.prediction)
        self.assertEqual(solution.evidence.count("## "), 4)
        saved = json.loads((Path(self.tmp.name) / "diagnostics/evidence/1.json").read_text())
        self.assertEqual(saved["case_key"], case().case_key)
        self.assertEqual(saved["validation_status"], "invalid")
        self.assertEqual(saved["decision"]["usage_delta"][CHEAP[0]]["calls"], 1)
        self.assertEqual(saved["evidence"][0]["evidence_id"], "e1")
        self.assertTrue(saved["case"]["start"].endswith("+08:00"))

    def test_renderer_exception_retains_fallback_guess_and_usage(self):
        from agents import routed
        fallback = make_decision(case(), [candidate()], [], self.state.store.catalog)
        result = InvestigationResult(fallback, deepcopy(fallback), [evidence()])
        def investigated(*args, **kwargs):
            self.state.usage_ledger[CHEAP[0]] = dict(calls=1, prompt_tokens=6, completion_tokens=2)
            return result
        with patch.object(routed, "get_run_state", return_value=self.state), \
             patch.object(routed, "parse_case", return_value=case()), \
             patch("agents.rca.controller.investigate", side_effect=investigated), \
             patch.dict("sys.modules", {"agents.rca.validation": SimpleNamespace(validate_and_render=Mock(side_effect=ValueError))}):
            solution = routed.solve("synthetic", self.state.out_dir, {})
        self.assertIn("synthetic-pod", solution.prediction)
        self.assertEqual(solution.usage[CHEAP[0]]["calls"], 1)

    def test_parse_failure_still_emits_exact_count(self):
        from agents import routed
        with patch.object(routed, "get_run_state", return_value=self.state):
            solution = routed.solve("two failures. You are tasked with root cause component and reason", self.state.out_dir, {})
        parsed = json.loads(solution.prediction.removeprefix("```json\n").removesuffix("\n```"))
        self.assertEqual(len(parsed), 2)
        self.assertEqual(list(parsed["1"]), ["root cause component", "root cause reason"])
        self.assertIn("could not be reliably parsed", solution.evidence)


if __name__ == "__main__":
    unittest.main()
