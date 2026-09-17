"""rca-v1 routing policy: finite attempts, shared breakers and conservative costs."""
from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path

from agents.rca.contracts import RunConfig
from cost import PRICES
from llm import LLM

CHEAP = ("zai-org/GLM-4.7-Flash", "zai-org/GLM-5.3-Flash")
STRONG = ("zai-org/GLM-5.2", "zai-org/GLM-5.1")
POLICY_VERSION = "routing.v1"
OUTPUT_TOKENS = 1200
RENDER_RESERVE_S = 2.0
BREAKER_FAILURES = 2


def load_config() -> RunConfig:
    mode = os.environ.get("RCA_MODE", "routed").lower()
    pinned = os.environ.get("RCA_MODEL") or None
    if mode not in ("deterministic", "routed"):
        raise ValueError("RCA_MODE must be deterministic or routed")
    if pinned is not None and pinned not in PRICES:
        raise ValueError("RCA_MODEL must be a documented GLM model ID")
    return RunConfig(schema_version="rca-v1", mode=mode, pinned_model=pinned,
                     reference_minutes=10, case_soft_seconds=45., run_soft_seconds=1080.,
                     case_cost_limit_usd=2.5, run_cost_limit_usd=20., max_followups=1,
                     max_model_stages=2, max_http_attempts_per_case=4)


def snapshot_usage(state) -> dict:
    return copy.deepcopy(state.usage_ledger)


def usage_delta(state, before: dict) -> dict:
    """Count all actual calls, even rejected JSON and discarded model decisions.

    Token totals contain observed provider counts. unknown_usage_calls identifies
    incomplete accounting; routes retain null counts and conservative reserves.
    """
    result = {}
    for model, counts in state.usage_ledger.items():
        prior = before.get(model, {})
        delta = {key: value - prior.get(key, 0) for key, value in counts.items()}
        if delta.get("calls", 0):
            result[model] = delta
    return result


class Router:
    def __init__(self, case, state, *, deadline: float):
        self.case, self.state = case, state
        self.deadline = min(deadline, state.deadline)
        self.initial_cost = state.estimated_cost_usd
        self.attempts = 0
        self.events: list[dict] = []

    def _record(self, event: dict) -> None:
        event = {"case_key": self.case.case_key,
                 "invocation_index": self.state.invocation_index,
                 "policy_version": POLICY_VERSION, **event}
        self.events.append(event)
        # The sole diagnostic write is scoped below the runner-owned output root.
        directory = Path(self.state.out_dir) / "diagnostics"
        root = Path(self.state.out_dir).resolve()
        directory.resolve().relative_to(root)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "routes.jsonl"
        target.resolve().relative_to(root)
        with target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, allow_nan=False) + "\n")

    def bypass(self, reason: str, stage="controller") -> None:
        self._record(dict(event="bypass", stage=stage, requested_model=None,
                          actual_model=None, reason=reason, fallback=False,
                          status="bypass", latency_s=0., prompt_tokens=None,
                          completion_tokens=None, estimated_cost_usd=None))

    def _reserve(self, messages: list[dict], model: str) -> float:
        # UTF-8 byte count is a conservative token bound plus framing headroom.
        upper_input_tokens = len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 1024
        p_in, p_out = PRICES[model]
        return (upper_input_tokens * p_in + OUTPUT_TOKENS * p_out) / 1e6

    def request(self, stage: str, messages: list[dict], *, reason: str,
                validate) -> dict | None:
        cfg = self.state.config
        if cfg.mode == "deterministic":
            self.bypass("deterministic_mode", stage)
            return None
        tier = CHEAP if stage == "flash" else STRONG
        models = (cfg.pinned_model,) if cfg.pinned_model else tier
        requested_model = models[0]
        for index, model in enumerate(models):
            if self.state.model_health.get(model, 0) >= BREAKER_FAILURES:
                self.bypass("circuit_open:" + model, stage)
                continue
            remaining = self.deadline - time.monotonic() - RENDER_RESERVE_S
            reserve = self._reserve(messages, model)
            if remaining <= .05:
                self.bypass("deadline_exhausted", stage)
                return None
            if self.attempts >= cfg.max_http_attempts_per_case:
                self.bypass("http_attempt_limit", stage)
                return None
            if (self.state.estimated_cost_usd + reserve > cfg.run_cost_limit_usd or
                    self.state.estimated_cost_usd - self.initial_cost + reserve > cfg.case_cost_limit_usd):
                self.bypass("cost_reservation_limit", stage)
                return None
            if self.state.client is None:
                try:
                    self.state.client = LLM(retries=0)
                except Exception as exc:
                    self.bypass("client_unavailable:" + type(exc).__name__, stage)
                    return None
            self.attempts += 1
            self.state.estimated_cost_usd += reserve
            started = time.monotonic()
            # LLM.request catches transport failures; programming errors remain visible.
            result = self.state.client.request(model, messages,
                                               timeout=min(20., remaining),
                                               max_tokens=OUTPUT_TOKENS)
            duration = time.monotonic() - started
            actual = result.model
            accounted_model = actual if actual in PRICES else model
            usage = self.state.usage_ledger.setdefault(accounted_model, dict(
                calls=0, prompt_tokens=0, completion_tokens=0, unknown_usage_calls=0))
            usage["calls"] += 1
            for field in ("prompt_tokens", "completion_tokens"):
                value = getattr(result, field)
                if value is not None:
                    usage[field] += value
            known_usage = result.prompt_tokens is not None and result.completion_tokens is not None
            if known_usage:
                p_in, p_out = PRICES[accounted_model]
                cost = (result.prompt_tokens * p_in + result.completion_tokens * p_out) / 1e6
                self.state.estimated_cost_usd += cost - reserve
            else:
                usage["unknown_usage_calls"] = usage.get("unknown_usage_calls", 0) + 1
                cost = None
            status = result.error
            payload = None
            if status is None and cfg.pinned_model and actual != cfg.pinned_model:
                status = "pinned_model_mismatch"
            if status is None:
                try:
                    payload = validate(result.text)
                    status = "valid"
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    status = "invalid_response"
            if status != "valid":
                self.state.model_health[model] = self.state.model_health.get(model, 0) + 1
            else:
                self.state.model_health[model] = 0
            self._record(dict(event="request", stage=stage, requested_model=requested_model,
                              attempt_model=model, actual_model=actual, reason=reason,
                              fallback=index > 0, status=status, latency_s=round(duration, 6),
                              prompt_tokens=result.prompt_tokens,
                              completion_tokens=result.completion_tokens,
                              estimated_cost_usd=cost,
                              retained_reservation_usd=reserve if not known_usage else 0.))
            if payload is not None and status == "valid":
                return payload
        return None
