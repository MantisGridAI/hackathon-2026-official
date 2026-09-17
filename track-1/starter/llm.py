"""Lazy Featherless transport; RCA routing owns retries, budgets and audit."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from cost import PRICES

DEFAULT_BASE_URL = "https://api.featherless.ai/v1"


class ModelUnavailable(RuntimeError):
    pass


def _field(value: Any, name: str, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _tokens(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass
class AttemptResult:
    model: str
    text: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    error: str | None = None


class LLM:
    def __init__(self, retries: int = 0, backoff: float = 0,
                 breaker: int = 2, *, client=None) -> None:
        if client is None:
            key = os.environ.get("FEATHERLESS_API_KEY")
            if not key:
                raise ModelUnavailable("FEATHERLESS_API_KEY is not set")
            from openai import OpenAI
            client = OpenAI(api_key=key, base_url=os.environ.get(
                "FEATHERLESS_BASE_URL", DEFAULT_BASE_URL), max_retries=0)
        self.client = client
        self.usage: dict[str, dict] = {}
        self.down: dict[str, int] = {}
        self.failures: list[str] = []
        self.breaker = max(1, breaker)
        self.retries = min(1, max(0, retries))

    def request(self, model: str, messages: list[dict], *, timeout: float,
                max_tokens: int = 1200) -> AttemptResult:
        if model not in PRICES:
            raise ValueError("Only the documented GLM model IDs are allowed")
        if timeout <= 0:
            raise ValueError("A positive request timeout is required")
        try:
            response = self.client.chat.completions.create(
                model=model, messages=messages, timeout=timeout,
                max_tokens=max_tokens, temperature=0)
        except Exception as exc:
            # Never record exception text: it may contain credentials/request data.
            return AttemptResult(model, None, None, None,
                                 "transport_" + type(exc).__name__)
        actual = _field(response, "model") or model
        if not isinstance(actual, str):
            actual = "invalid-model-field"
        usage = _field(response, "usage")
        pt = _tokens(_field(usage, "prompt_tokens"))
        ct = _tokens(_field(usage, "completion_tokens"))
        error = None
        text = None
        if _field(response, "error") is not None:
            error = "provider_error_body"
        elif actual not in PRICES:
            error = "unexpected_model"
        else:
            choices = _field(response, "choices")
            if not isinstance(choices, (list, tuple)) or not choices:
                error = "empty_choices"
            else:
                content = _field(_field(choices[0], "message"), "content")
                if not isinstance(content, str):
                    error = "empty_content"
                else:
                    text = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
                    if not text:
                        error = "empty_content"
        return AttemptResult(str(actual), text, pt, ct, error)

    def ask(self, model: str | list[str], prompt: str | list[dict], **kwargs) -> str:
        """Bounded compatibility method; RCA uses request to audit every attempt."""
        models = [model] if isinstance(model, str) else list(model)
        if not models or any(name not in PRICES for name in models):
            raise ValueError("ask requires documented GLM models")
        messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt
        for name in models[:2]:
            for _ in range(self.retries + 1):
                if self.down.get(name, 0) >= self.breaker:
                    break
                result = self.request(name, messages, timeout=min(30., kwargs.get("timeout", 30.)),
                                      max_tokens=min(1200, kwargs.get("max_tokens", 1200)))
                rec = self.usage.setdefault(result.model, dict(calls=0, prompt_tokens=0,
                                                              completion_tokens=0))
                rec["calls"] += 1
                for field in ("prompt_tokens", "completion_tokens"):
                    value = getattr(result, field)
                    if value is not None:
                        rec[field] += value
                if result.error is None:
                    return result.text
                self.failures.append(result.error)
                self.down[name] = self.down.get(name, 0) + 1
        raise ModelUnavailable("All offered models failed or have open circuit breakers")
