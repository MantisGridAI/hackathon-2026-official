from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.m4.helpers import bundle, candidate, case, evidence, response, state, Transport
from agents.rca.contracts import AnalysisBundle, InvestigationResult, RenderedResult
from agents.rca.controller import _bypass_limitations, _followup_plan, _escalation_reason, investigate
from agents.rca.ranking import Ranked, make_decision
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
        self.logs = SimpleNamespace(search_fault_evidence=Mock(return_value=AnalysisBundle("m3")))
        self.modules = patch.dict("sys.modules", {"agents.rca.metrics": self.metrics,
                                                 "agents.rca.traces": self.traces,
                                                 "agents.rca.logs": self.logs})
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
                         self.traces.inspect_dependencies.call_count + self.logs.search_fault_evidence.call_count, 1)

    def test_followup_plan_routes_question_to_relevant_tool(self):
        item = candidate()
        rank = [Ranked(item, 4., {})]
        self.assertEqual(_followup_plan(rank, [])['tool'], 'compare_replicas_and_node')
        item.features['metrics.family'] = 'process'
        self.assertEqual(_followup_plan(rank, [])['tool'], 'search_fault_evidence')
        item.features = {'traces.anomaly_score': 3.}
        self.assertEqual(_followup_plan(rank, [object()])['tool'], 'inspect_dependencies')
        self.assertEqual(_followup_plan(rank, [])['tool'], 'search_fault_evidence')

    def test_process_hypothesis_executes_one_log_followup(self):
        self.metrics.triage_metrics.return_value.candidates[0].features['metrics.family'] = 'process'
        result = self.run_case()
        self.logs.search_fault_evidence.assert_called_once()
        self.metrics.compare_replicas_and_node.assert_not_called()
        plans = [e for e in result.decision.route_events if e['event'] == 'followup_plan']
        self.assertEqual(plans[0]['tool'], 'search_fault_evidence')
        self.assertIn('OOM', plans[0]['question'])

    def test_strong_escalation_requires_measured_unresolved_competition(self):
        a, b = candidate(), candidate('c2', 'synthetic-peer', eid='e2')
        ranked = [Ranked(a, 6., {}), Ranked(b, 5.5, {})]
        records = [evidence(), evidence('e2', 'synthetic-peer')]
        reply = dict(confidence='low', unresolved=['Two measured resource mechanisms remain plausible'])
        self.assertEqual(_escalation_reason(reply, [a], ranked, records), 'unresolved_competing_observations')
        records[1].coverage[0].status = 'partial'
        self.assertIsNone(_escalation_reason(reply, [a], ranked, records))
        records[1].coverage[0].status = 'complete'
        b.features['m4.weak_reason'] = True
        self.assertIsNone(_escalation_reason(reply, [a], ranked, records))

    def test_workflow_status_distinguishes_valid_output_from_truncated_tools(self):
        from agents.routed import _workflow_status
        result = self.run_case()
        self.assertTrue(_workflow_status(result.decision, 'valid')['complete'])
        result.decision.route_events[0]['status'] = 'incomplete'
        summary = _workflow_status(result.decision, 'valid')
        self.assertFalse(summary['complete'])
        self.assertIn('tool:triage_metrics', summary['interruptions'])

    def test_historical_selection_is_not_reported_as_final_fallback_adoption(self):
        from agents.routed import _workflow_status
        result = self.run_case()
        result.fallback.route_events.append(dict(event='selection', selection_applied=True, stage='flash'))
        result.fallback.route_events.append(dict(event='render_fallback', status='applied'))
        summary = _workflow_status(result.fallback, 'valid')
        self.assertFalse(summary['complete'])
        self.assertFalse(summary['model_selection_applied'])
        self.assertIn('preferred_decision_rejected', summary['interruptions'])

    def test_all_models_fail_keeps_best_guess_and_real_usage(self):
        self.state.config.mode = "routed"
        self.state.client = Transport([lambda m: response(m, None, None, None, "provider_error_body")] * 4)
        result = self.run_case()
        self.assertEqual(result.decision.answers[0].component, "synthetic-pod")
        self.assertEqual(len(self.state.client.calls), 4)
        self.assertEqual(sum(v["calls"] for v in result.fallback.usage_delta.values()), 4)
        self.assertEqual(len([e for e in result.decision.route_events if e["event"] == "request"]), 4)

    def test_missing_key_bypass_is_explained_in_both_decisions(self):
        self.state.config.mode = "routed"
        with patch.dict(os.environ, {"FEATHERLESS_API_KEY": ""}):
            result = self.run_case()
        for decision in (result.decision, result.fallback):
            notes = [note for note in decision.limitations if "model client initialization was unavailable" in note]
            self.assertEqual(len(notes), 1, "Repeated stage bypasses should have one concise explanation")
            self.assertIn("no request was sent", notes[0])
            self.assertEqual(decision.usage_delta, {})
        self.assertIsNone(self.state.client)

    def test_operational_bypass_notes_are_bounded_and_exclude_deliberate_routes(self):
        reasons = ["deterministic_mode", "deterministic_gate", "no_new_mechanism_evidence",
                   "deadline_exhausted", "http_attempt_limit", "cost_reservation_limit",
                   "model_stage_limit", "circuit_open:zai-org/GLM-5.2"]
        notes = _bypass_limitations([dict(event="bypass", reason=reason) for reason in reasons])
        self.assertEqual(len(notes), 5)
        self.assertTrue(any("circuit breaker" in note for note in notes))
        self.assertTrue(any("cost budget" in note for note in notes))
        self.assertTrue(any("HTTP attempts" in note for note in notes))
        self.assertTrue(any("deadline" in note for note in notes))
        many = [dict(event="bypass", reason=f"circuit_open:synthetic-{i}") for i in range(30)]
        self.assertEqual(len(_bypass_limitations(many)), 8)

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
        reply = dict(selected_candidate_ids=["c1"], confidence="high",
                     supporting_evidence_ids=["e1"], unresolved=[], next_query=None)
        self.state.client = Transport([lambda m: response(m, json.dumps(reply))])
        result = self.run_case()
        self.assertEqual(result.decision.confidence, "low")
        self.assertEqual(result.decision.stop_reason, "flash_selection")
        self.assertEqual(len(self.state.client.calls), 1)
        applied = [e for e in result.decision.route_events if e.get("selection_applied")]
        self.assertEqual(applied[0]["selected_candidate_ids"], ["c1"])

    def test_telemetry_and_followup_leave_model_reservation(self):
        self.state.config.mode = "routed"
        self.state.config.pinned_model = CHEAP[0]
        self.state.config.max_model_stages = 1
        self.state.client = Transport([lambda m: response(m, error="empty_content")])
        with patch("agents.rca.controller.time.monotonic", return_value=100.):
            investigate(case(), self.state, deadline=145.)
        metric_deadline = self.metrics.triage_metrics.call_args.kwargs["deadline"]
        trace_deadline = self.traces.triage_traces.call_args.kwargs["deadline"]
        followup = self.metrics.compare_replicas_and_node.call_args
        self.assertLess(metric_deadline, trace_deadline)
        self.assertEqual(trace_deadline, 123.)
        self.assertLessEqual(followup.kwargs["deadline"], 123.)

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
