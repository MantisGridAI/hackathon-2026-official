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


if __name__ == "__main__":
    unittest.main()
