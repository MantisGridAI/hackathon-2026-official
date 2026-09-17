from copy import deepcopy
import json
from tempfile import TemporaryDirectory
import time
import unittest

from agents.rca.prompts import MAX_PROMPT_BYTES, build_messages, prompt_bytes, validate_selection
from agents.rca.ranking import Ranked
from agents.rca.routing import Router
from tests.m4.helpers import candidate, case, evidence, state, Transport


class PromptBudgetTests(unittest.TestCase):
    def test_large_views_fit_global_utf8_budget_and_keep_support_ids(self):
        records, ranked = [], []
        for i in range(70):
            record = evidence(f"observed-evidence-{i}", f"synthetic-component-{i}")
            record.values = {"baseline_n": 8, "baseline_median": 2.5,
                             "samples": [{f"measure-{k}": list(range(40)) for k in range(24)}] * 20,
                             "long_text": "合成遥测" * 1000}
            record.limitations = ["synthetic warning " * 100] * 30
            records.append(record)
            item = candidate(f"candidate-{i}", f"synthetic-component-{i}", eid=record.evidence_id)
            item.features = {"metrics.strength": 12., "huge_details": ["synthetic" * 200] * 30}
            ranked.append(Ranked(item, 70 - i, {"metric_strength": 3.}))
        untouched = deepcopy(records[0])
        messages, offered, displayed = build_messages(case(2), ranked, records, stage="flash")
        self.assertLessEqual(prompt_bytes(messages), MAX_PROMPT_BYTES)
        self.assertGreaterEqual(len(offered), 2)
        payload = json.loads(messages[1]["content"])
        self.assertGreater(payload["omissions"]["candidate_count"], 0)
        self.assertEqual(records[0], untouched, "Prompt shaping must not change the evidence ledger")
        ids = {record.evidence_id for record in displayed}
        original_ids = {item.candidate.candidate_id for item in ranked}
        for view in payload["candidates"]:
            self.assertIn(view["candidate_id"], original_ids)
            self.assertTrue(view["supporting_ids"])
            self.assertFalse(set(view["supporting_ids"] + view["contradicting_ids"]) - ids)
        self.assertTrue(all(view["values"]["baseline_median"] == 2.5 for view in payload["evidence"]))
        picks = offered[:2]
        response = dict(selected_candidate_ids=[p.candidate_id for p in picks], confidence="low",
                        supporting_evidence_ids=[p.supporting_ids[0] for p in picks], unresolved=[], next_query=None)
        validate_selection(json.dumps(response), case(2), offered, displayed)

    def test_router_rejects_over_budget_message_before_client_or_http(self):
        with TemporaryDirectory() as output:
            run = state(output)
            run.client = Transport([])
            router = Router(case(), run, deadline=time.monotonic() + 30)
            self.assertIsNone(router.request("flash", [{"role": "user", "content": "x" * MAX_PROMPT_BYTES}],
                reason="synthetic", validate=json.loads))
            self.assertEqual(run.client.calls, [])
            self.assertEqual(router.events[-1]["reason"], "prompt_size_limit")

    def test_required_twenty_distinct_faults_get_space_before_optional_details(self):
        records = [evidence('e' + str(i), 'synthetic-' + str(i)) for i in range(24)]
        ranked = []
        for i, record in enumerate(records):
            record.values = {'baseline_n': 40, 'window_n': 120, 'baseline_median': 2.5, 'window_max': 9.5,
                             'large_details': [{'irrelevant': 'x' * 200}] * 50}
            item = candidate('c' + str(i), 'synthetic-' + str(i), eid=record.evidence_id)
            ranked.append(Ranked(item, 30 - i, {}))
        messages, offered, _ = build_messages(case(20), ranked, records, stage='flash')
        self.assertLessEqual(prompt_bytes(messages), MAX_PROMPT_BYTES)
        self.assertTrue({'c' + str(i) for i in range(20)} <= {c.candidate_id for c in offered})

    def test_twenty_required_candidates_do_not_lose_space_to_extra_supports(self):
        records, ranked = [], []
        for i in range(20):
            item = candidate('c' + str(i), 'synthetic-' + str(i), eid='metric-' + str(i))
            item.supporting_ids.append('trace-' + str(i))
            item.contradicting_ids = ['opposed-' + str(i)]
            records.extend([evidence(item.supporting_ids[0], item.component),
                            evidence(item.supporting_ids[1], item.component, kind='trace'),
                            evidence(item.contradicting_ids[0], item.component)])
            ranked.append(Ranked(item, 30 - i, {}))
        messages, offered, displayed = build_messages(case(20), ranked, records, stage='flash')
        self.assertEqual(len(offered), 20)
        self.assertLessEqual(prompt_bytes(messages), MAX_PROMPT_BYTES)
        views = json.loads(messages[1]['content'])['candidates']
        self.assertTrue(all(v.get('omitted_contradictions', 0) or v['contradicting_ids'] for v in views))
        shown = {r.evidence_id for r in displayed}
        self.assertTrue(all(set(v['supporting_ids'] + v['contradicting_ids']) <= shown for v in views))

    def test_log_excerpts_preserve_literal_match_and_location(self):
        record = evidence(kind='log')
        record.values = {'searched_rows': 100, 'matched_rows': 1, 'match_samples': [
            {'text': 'x' * 500 + 'timeout contacting dependency', 'matched_patterns': ['timeout'],
             'record_index': 74, 'timestamp_s': 123., 'text_truncated': False}]}
        messages, _, _ = build_messages(case(), [Ranked(candidate(), 5., {})], [record], stage='flash')
        excerpt = json.loads(messages[1]['content'])['evidence'][0]['values']['match_samples'][0]
        self.assertIn('timeout contacting dependency', excerpt['text_excerpt'])
        self.assertEqual(excerpt['record_index'], 74)
        self.assertEqual(excerpt['excerpt_start_character'], 420)

    def test_episode_values_survive_many_incidental_fields(self):
        record = evidence()
        record.values = {f"incidental-{i}": i for i in range(20)}
        record.values.update(baseline_n=10, baseline_median=0, strength=9,
                             episodes=[{"first_change_s": 123., "persistence_samples": 4}])
        messages, _, _ = build_messages(case(), [Ranked(candidate(), 5., {})], [record], stage="flash")
        view = json.loads(messages[1]["content"])["evidence"][0]["values"]
        self.assertEqual(view["episodes"][0]["first_change_s"], 123.)
        self.assertEqual(view["baseline_median"], 0)

    def test_fused_candidate_shows_representative_kpi_and_weak_flag(self):
        wrong, right = evidence("e1"), evidence("e2")
        wrong.transform_params = {"kpi_name": "other"}
        right.transform_params = {"kpi_name": "representative"}
        item = candidate()
        item.supporting_ids = ["e1", "e2"]
        item.features = {f"incidental-{i}": i for i in range(20)}
        item.features.update({"metrics.kpi_name": "representative", "m4.weak_reason": True})
        messages, _, _ = build_messages(case(), [Ranked(item, 5., {})], [wrong, right], stage="flash")
        view = json.loads(messages[1]["content"])["candidates"][0]
        self.assertEqual(view["supporting_ids"], ["e2"])
        self.assertTrue(view["features"]["m4.weak_reason"])


if __name__ == "__main__":
    unittest.main()
