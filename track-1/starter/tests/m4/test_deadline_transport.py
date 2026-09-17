"""Loopback-only transport regressions: synthetic responses, no provider calls."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from llm import LLM, MAX_RESPONSE_BYTES
from agents.rca.routing import CHEAP, Router
from tests.m4.helpers import case, state


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.paths.append(self.path)
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        mode = self.server.mode
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", self.server.base_url + "/redirect-target")
            self.end_headers()
            return
        if mode == "auth":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"synthetic private error body must not be logged")
            return
        body = json.dumps({"model": CHEAP[0], "choices": [{"message": {"content": "{}"}}],
                           "usage": {"prompt_tokens": 20, "completion_tokens": 5}}).encode()
        if mode == "oversize":
            body = b" " * (MAX_RESPONSE_BYTES + 1)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body) + (200 if mode == "trickle" else 0)))
        self.end_headers()
        try:
            if mode == "trickle":
                for _ in range(200):
                    if self.server.stop_trickle.is_set():
                        return
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(.04)
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.server.connection_closed.set()


class DeadlineTransportTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.mode = "success"
        self.server.paths = []
        self.server.stop_trickle = threading.Event()
        self.server.connection_closed = threading.Event()
        self.server.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.environment = patch.dict(os.environ, {
            "FEATHERLESS_API_KEY": "synthetic-loopback-test-key",
            "FEATHERLESS_BASE_URL": self.server.base_url,
            "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        self.server.stop_trickle.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def test_success_uses_environment_endpoint_and_reports_usage(self):
        result = LLM().request(CHEAP[0], [{"role": "user", "content": "synthetic"}], timeout=3)
        self.assertIsNone(result.error)
        self.assertEqual(result.text, "{}")
        self.assertEqual(result.prompt_tokens, 20)
        self.assertEqual(self.server.paths, ["/v1/chat/completions"])

    def test_trickling_response_is_killed_and_reaped_at_wall_deadline(self):
        self.server.mode = "trickle"
        real_popen = subprocess.Popen
        children = []
        def tracked(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        started = time.monotonic()
        with patch("llm.subprocess.Popen", side_effect=tracked):
            result = LLM().request(CHEAP[0], [], timeout=.8)
        elapsed = time.monotonic() - started
        self.assertEqual(result.error, "wall_deadline_exceeded")
        self.assertTrue(result.request_started)
        self.assertIsNone(result.prompt_tokens)
        self.assertLess(elapsed, 1.8)
        self.assertGreater(elapsed, .65)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll(), "No request worker may remain alive")
        self.assertEqual(children[0].args[0], getattr(sys, "_base_executable", None) or sys.executable)
        self.assertTrue(self.server.connection_closed.wait(.5), "Worker termination must close its HTTP socket")

    def test_worker_initialization_stall_is_also_killed(self):
        real_popen = subprocess.Popen
        children = []
        def slow_start(args, **kwargs):
            child = real_popen([args[0], "-B", "-c", "import time; time.sleep(10)"], **kwargs)
            children.append(child)
            return child
        started = time.monotonic()
        with patch("llm.subprocess.Popen", side_effect=slow_start):
            result = LLM().request(CHEAP[0], [], timeout=.5)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(result.error, "wall_deadline_exceeded")
        self.assertFalse(result.request_started)
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(self.server.paths, [])

    def test_timeout_usage_is_unknown_and_budget_reservation_remains(self):
        self.server.mode = "trickle"
        with TemporaryDirectory() as output:
            run = state(output, mode="routed", pinned_model=CHEAP[0])
            router = Router(case(), run, deadline=time.monotonic() + 2.8)
            router.request("flash", [], reason="synthetic_test", validate=json.loads)
            self.assertEqual(run.usage_ledger[CHEAP[0]]["calls"], 1)
            self.assertEqual(run.usage_ledger[CHEAP[0]]["unknown_usage_calls"], 1)
            self.assertGreater(run.estimated_cost_usd, 0)
            request = next(e for e in router.events if e["event"] == "request")
            self.assertEqual(request["status"], "wall_deadline_exceeded")
            self.assertIsNone(request["estimated_cost_usd"])
            self.assertIsNone(request["prompt_tokens"])
            self.assertGreater(request["retained_reservation_usd"], 0)

    def test_redirect_is_not_followed_with_credentials(self):
        self.server.mode = "redirect"
        result = LLM().request(CHEAP[0], [], timeout=3)
        self.assertEqual(result.error, "transport_HTTP302")
        self.assertEqual(self.server.paths, ["/v1/chat/completions"])

    def test_auth_error_taxonomy_and_response_cap(self):
        self.server.mode = "auth"
        result = LLM().request(CHEAP[0], [], timeout=3)
        self.assertEqual(result.error, "transport_AuthenticationError")
        self.assertIsNone(result.text)
        self.server.mode = "oversize"
        result = LLM().request(CHEAP[0], [], timeout=3)
        self.assertEqual(result.error, "response_size_limit")


if __name__ == "__main__":
    unittest.main()
