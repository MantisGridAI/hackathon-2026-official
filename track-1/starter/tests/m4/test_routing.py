import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.m4.helpers import case, response, state, Transport
from agents.rca.routing import CHEAP, STRONG, Router, load_config, snapshot_usage, usage_delta
from llm import LLM, completion_options


class TransportTests(unittest.TestCase):
    def wrapper(self, raw):
        return LLM(client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: raw))))

    def test_error_body_wins_even_if_choices_present(self):
        result = self.wrapper({"error": {"message": "secret never logged"}, "choices": [{}],
                               "usage": {"prompt_tokens": 9, "completion_tokens": 2}}).request(
                               CHEAP[0], [], timeout=1)
        self.assertEqual(result.error, "provider_error_body")
        self.assertEqual(result.prompt_tokens, 9)

    def test_empty_choices_and_content(self):
        for raw in ({}, {"choices": []}, {"choices": [{"message": {"content": ""}}]}):
            self.assertIsNotNone(self.wrapper(raw).request(CHEAP[0], [], timeout=1).error)

    def test_missing_usage_remains_unknown(self):
        result = self.wrapper({"choices": [{"message": {"content": "<think>hidden</think>{}"}}]}).request(CHEAP[0], [], timeout=1)
        self.assertEqual(result.text, "{}")
        self.assertIsNone(result.prompt_tokens)

    def test_transport_error_redacts_message(self):
        def failure(**kwargs):
            raise RuntimeError("Bearer secret-token")
        llm = LLM(client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=failure))))
        self.assertEqual(llm.request(CHEAP[0], [], timeout=1).error, "transport_RuntimeError")

    def test_only_allowed_models(self):
        with self.assertRaises(ValueError):
            self.wrapper({}).request("other/provider", [], timeout=1)

    def test_reasoning_is_counted_but_never_used_as_final_answer(self):
        raw = {"choices": [{"finish_reason": "length", "message": {
            "content": "", "reasoning_content": "private reasoning"}}],
            "usage": {"completion_tokens": 1200, "completion_tokens_details": {"reasoning_tokens": 1198}}}
        result = self.wrapper(raw).request(CHEAP[0], [], timeout=1)
        self.assertEqual(result.error, "output_truncated")
        self.assertIsNone(result.text)
        self.assertEqual(result.finish_reason, "length")
        self.assertEqual(result.reasoning_chars, 17)
        self.assertEqual(result.reasoning_tokens, 1198)
        self.assertNotIn("private reasoning", repr(result))
        raw["choices"][0].update(finish_reason="stop")
        raw["choices"][0]["message"]["content"] = "{}"
        result = self.wrapper(raw).request(CHEAP[0], [], timeout=1)
        self.assertIsNone(result.error)
        self.assertEqual(result.text, "{}")

    def test_injected_client_uses_same_template_options_as_worker(self):
        calls = []
        def create(**kwargs):
            calls.append(kwargs)
            return {"choices": [{"message": {"content": "{}"}}]}
        client = LLM(client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
        for model in CHEAP:
            client.request(model, [], timeout=1)
            self.assertEqual(calls[-1]["extra_body"], completion_options(model))


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = state(self.tmp.name)

    def router(self, deadline=None):
        return Router(case(), self.state, deadline=deadline or time.monotonic() + 30)

    def test_bad_json_fallback_and_usage(self):
        self.state.client = Transport([lambda m: response(m, "not json"), lambda m: response(m, '{"ok":true}')])
        before = snapshot_usage(self.state)
        router = self.router()
        result = router.request("flash", [], reason="test", validate=json.loads)
        self.assertEqual(result, {"ok": True})
        self.assertTrue(router.events[-1]["fallback"])
        self.assertEqual(router.events[0]["status"], "invalid_response")
        self.assertEqual(sum(v["calls"] for v in usage_delta(self.state, before).values()), 2)
        self.assertEqual(sum(v["prompt_tokens"] for v in self.state.usage_ledger.values()), 40)

    def test_unknown_usage_retains_reservation(self):
        self.state.client = Transport([lambda m: response(m, "{}", None, None)])
        router = self.router()
        router.request("flash", [], reason="test", validate=json.loads)
        self.assertGreater(self.state.estimated_cost_usd, 0)
        self.assertIsNone(router.events[-1]["estimated_cost_usd"])
        self.assertIsNone(router.events[-1]["prompt_tokens"])
        self.assertEqual(self.state.usage_ledger[CHEAP[0]]["unknown_usage_calls"], 1)

    def test_budget_rejects_before_call(self):
        self.state.config.run_cost_limit_usd = 0
        self.state.client = Transport([])
        self.router().request("flash", [], reason="test", validate=json.loads)
        self.assertEqual(self.state.client.calls, [])

    def test_deadline_rejects_before_call(self):
        self.state.client = Transport([])
        self.router(time.monotonic() - 1).request("flash", [], reason="test", validate=json.loads)
        self.assertEqual(self.state.client.calls, [])

    def test_single_model_disables_fallback(self):
        self.state.config.pinned_model = STRONG[0]
        self.state.client = Transport([lambda m: response(m, error="provider_error_body")])
        self.assertIsNone(self.router().request("flash", [], reason="test", validate=json.loads))
        self.assertEqual([m for m, _ in self.state.client.calls], [STRONG[0]])

    def test_shared_circuit_breaker_across_cases(self):
        self.state.config.pinned_model = CHEAP[0]
        self.state.client = Transport([lambda m: response(m, error="empty_choices")] * 2)
        for _ in range(3):
            self.router().request("flash", [], reason="test", validate=json.loads)
            self.state.invocation_index += 1
        self.assertEqual(len(self.state.client.calls), 2)

    def test_output_and_local_budget_failures_do_not_poison_later_cases(self):
        self.state.config.pinned_model = CHEAP[0]
        for failure in ("output_truncated", "empty_content", "wall_deadline_exceeded",
                        "transport_TimeoutError", "transport_timeout", "invalid_response"):
            self.state.client = Transport([lambda m: response(m, error=failure)] * 2 + [lambda m: response(m)])
            for _ in range(3):
                router = self.router()
                router.request("flash", [], reason="test", validate=json.loads)
            self.assertEqual(len(self.state.client.calls), 3)
            self.assertEqual(router.events[-1]["status"], "valid")
            self.assertFalse(router.events[-1]["breaker_affected"])

    def test_insufficient_useful_time_does_not_start_or_poison_model(self):
        self.state.client = Transport([])
        router = self.router(time.monotonic() + 6)
        router.request("flash", [], reason="test", validate=json.loads)
        self.assertEqual(self.state.client.calls, [])
        self.assertEqual(self.state.model_health, {})

    def test_forced_thinking_output_cap_is_included_in_reservation(self):
        model = "zai-org/GLM-5.3-Flash"
        self.state.config.pinned_model = model
        self.state.client = Transport([lambda m: response(m, pt=None, ct=None)])
        router = self.router()
        expected = router._reserve([], model)
        router.request("flash", [], reason="test", validate=json.loads)
        self.assertEqual(self.state.client.calls[0][1]["max_tokens"], 2400)
        self.assertEqual(self.state.estimated_cost_usd, expected)

    def test_total_http_limit_counts_failures(self):
        self.state.config.max_http_attempts_per_case = 1
        self.state.client = Transport([lambda m: response(m, error="empty_choices")])
        router = self.router()
        router.request("flash", [], reason="test", validate=json.loads)
        router.request("strong", [], reason="test", validate=json.loads)
        self.assertEqual(len(self.state.client.calls), 1)

    def test_strong_profile_applies_to_pinned_flash_stage(self):
        self.state.config.pinned_model = 'zai-org/GLM-5.2'
        self.state.client = Transport([lambda m: response(m)])
        self.router(time.monotonic() + 21).request('flash', [], reason='test', validate=json.loads)
        self.assertEqual(self.state.client.calls, [])
        self.router(time.monotonic() + 40).request('flash', [], reason='test', validate=json.loads)
        self.assertEqual(self.state.client.calls[0][1]['timeout'], 30.)
        self.assertEqual(self.state.client.calls[0][1]['max_tokens'], 2400)
        self.assertTrue(completion_options('zai-org/GLM-5.2')['chat_template_kwargs']['enable_thinking'])

    def test_per_case_usage_is_delta(self):
        self.state.client = Transport([lambda m: response(m)] * 2)
        self.router().request("flash", [], reason="test", validate=json.loads)
        before = snapshot_usage(self.state)
        self.router().request("flash", [], reason="test", validate=json.loads)
        self.assertEqual(usage_delta(self.state, before)[CHEAP[0]]["calls"], 1)
        self.assertEqual(usage_delta(self.state, before)[CHEAP[0]]["prompt_tokens"], 20)

    def test_unexpected_provider_model_keeps_identity_and_unknown_cost(self):
        self.state.config.pinned_model = CHEAP[0]
        self.state.client = Transport([response("unexpected/provider", None, 100, 20, "unexpected_model")])
        router = self.router()
        router.request("flash", [], reason="test", validate=json.loads)
        self.assertNotIn(CHEAP[0], self.state.usage_ledger)
        self.assertEqual(self.state.usage_ledger["unexpected/provider"]["prompt_tokens"], 100)
        self.assertIsNone(router.events[-1]["estimated_cost_usd"])
        self.assertGreater(router.events[-1]["retained_reservation_usd"], 0)
        self.assertGreater(self.state.estimated_cost_usd, 0)

    def test_deterministic_no_client_or_credentials(self):
        self.state.config.mode = "deterministic"
        with patch("agents.rca.routing.LLM", side_effect=AssertionError("client must not initialize")):
            self.router().request("flash", [], reason="test", validate=json.loads)
        self.assertIsNone(self.state.client)
        self.assertEqual(self.state.usage_ledger, {})

    def test_config_rejects_non_glm(self):
        with patch.dict(os.environ, {"RCA_MODEL": "invalid/model"}):
            with self.assertRaises(ValueError):
                load_config()

    def test_routes_are_json_lines_in_out(self):
        self.router().bypass("synthetic_test")
        row = json.loads((Path(self.tmp.name) / "diagnostics/routes.jsonl").read_text())
        self.assertEqual(row["case_key"], "synthetic-case")
        self.assertEqual(row["event"], "bypass")


if __name__ == "__main__":
    unittest.main()
