"""Featherless JSON transport with a killable process per production request.

Socket inactivity timeouts are insufficient when providers trickle response bytes.
The parent enforces a monotonic wall deadline across worker startup, DNS/TLS and
response reading, then kills and reaps the worker. Remote completion charges may
still occur after the local connection closes; interrupted usage stays unknown.
"""
from __future__ import annotations

import os
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from dataclasses import asdict
from typing import Any

from cost import PRICES

DEFAULT_BASE_URL = "https://api.featherless.ai/v1"
MAX_WORKER_INPUT_BYTES = 200_000
MAX_RESPONSE_BYTES = 262_144


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
    request_started: bool = True


def _decode_response(model, response):
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        # Never forward the authorization header beyond the configured endpoint.
        return None


class _IsolatedClient:
    """Compatibility close facade; production holds no persistent HTTP client."""
    closed = False

    def close(self):
        self.closed = True


def _worker_request(payload):
    model = payload.get("model", "unknown")
    key = os.environ.get("FEATHERLESS_API_KEY")
    if not key:
        return AttemptResult(model, None, None, None, "missing_api_key", False)
    base = os.environ.get("FEATHERLESS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return AttemptResult(model, None, None, None, "invalid_base_url", False)
    if model not in PRICES:
        return AttemptResult(model, None, None, None, "invalid_model", False)
    remaining = payload["deadline"] - time.monotonic()
    if remaining <= 0:
        return AttemptResult(model, None, None, None, "worker_startup_deadline", False)
    body = json.dumps({"model": model, "messages": payload["messages"],
                       "max_tokens": payload["max_tokens"], "temperature": 0},
                      ensure_ascii=False, allow_nan=False).encode("utf-8")
    request = urllib.request.Request(base + "/chat/completions", data=body, method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect())
    # A flushed marker distinguishes initialization failure from an HTTP attempt.
    print(json.dumps({"phase": "request_started"}), flush=True)
    try:
        with opener.open(request, timeout=max(.001, remaining)) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return AttemptResult(model, None, None, None, "response_size_limit")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return AttemptResult(model, None, None, None, "invalid_json_http_body")
        return _decode_response(model, data)
    except urllib.error.HTTPError as exc:
        errors = {401: "AuthenticationError", 403: "PermissionError", 404: "NotFoundError",
                  429: "RateLimitError"}
        return AttemptResult(model, None, None, None,
                             "transport_" + errors.get(exc.code, "InternalServerError" if exc.code >= 500 else "HTTP" + str(exc.code)))
    except Exception as exc:
        # Never include an exception message, raw response body, URL or headers.
        return AttemptResult(model, None, None, None, "transport_" + type(exc).__name__)


def _worker_main():
    model = "unknown"
    try:
        raw = sys.stdin.buffer.read(MAX_WORKER_INPUT_BYTES + 1)
        if len(raw) > MAX_WORKER_INPUT_BYTES:
            raise ValueError("Input exceeds worker limit")
        payload = json.loads(raw)
        model = payload.get("model", "unknown")
        result = _worker_request(payload)
    except Exception as exc:
        result = AttemptResult(model, None, None, None, "worker_" + type(exc).__name__, False)
    print(json.dumps({"result": asdict(result)}, allow_nan=False), flush=True)


class LLM:
    def __init__(self, retries: int = 0, backoff: float = 0,
                 breaker: int = 2, *, client=None) -> None:
        if client is None:
            key = os.environ.get("FEATHERLESS_API_KEY")
            if not key:
                raise ModelUnavailable("FEATHERLESS_API_KEY is not set")
            # Actual network/client initialization occurs only in the bounded worker.
            client = _IsolatedClient()
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
        if isinstance(self.client, _IsolatedClient):
            if self.client.closed:
                return AttemptResult(model, None, None, None, "client_closed", False)
            return self._isolated_request(model, messages, timeout=timeout, max_tokens=max_tokens)
        # Explicitly injected clients are deterministic test doubles, not runtime
        # network clients. Production calls always take the isolated path above.
        try:
            response = self.client.chat.completions.create(
                model=model, messages=messages, timeout=timeout,
                max_tokens=max_tokens, temperature=0)
        except Exception as exc:
            # Never record exception text: it may contain credentials/request data.
            return AttemptResult(model, None, None, None,
                                 "transport_" + type(exc).__name__)
        return _decode_response(model, response)

    def _isolated_request(self, model, messages, *, timeout, max_tokens):
        deadline = time.monotonic() + timeout
        payload = json.dumps(dict(model=model, messages=messages, deadline=deadline,
                                  max_tokens=max_tokens), ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(payload) > MAX_WORKER_INPUT_BYTES:
            return AttemptResult(model, None, None, None, "request_size_limit", False)
        # On Windows a venv python.exe can be a launcher process. The stdlib-only
        # worker uses the real base interpreter so terminate targets the HTTP owner.
        executable = getattr(sys, "_base_executable", None) or sys.executable
        worker = None
        output = b""
        timed_out = False
        try:
            worker = subprocess.Popen([executable, "-B", "-u", str(Path(__file__).resolve()), "--bounded-http-worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(worker.args, timeout)
            output, _ = worker.communicate(payload, timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            if worker is not None:
                worker.kill()
                # The worker has no descendants and holds the only pipe writer.
                # communicate also waits for process exit; no background call survives.
                output, _ = worker.communicate()
        except Exception as exc:
            if worker is not None and worker.poll() is None:
                worker.kill()
                worker.communicate()
            return AttemptResult(model, None, None, None, "worker_" + type(exc).__name__, False)
        started = False
        result = None
        for line in output.splitlines():
            try:
                message = json.loads(line)
                started = started or message.get("phase") == "request_started"
                if "result" in message:
                    result = AttemptResult(**message["result"])
            except (ValueError, TypeError, AttributeError):
                continue
        if timed_out:
            return AttemptResult(model, None, None, None, "wall_deadline_exceeded", started)
        return result or AttemptResult(model, None, None, None, "worker_no_result", started)

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


if __name__ == "__main__" and sys.argv[1:] == ["--bounded-http-worker"]:
    _worker_main()
