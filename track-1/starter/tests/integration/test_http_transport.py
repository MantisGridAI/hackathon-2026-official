"""Real SDK over local synthetic HTTP; no external model or billing is involved."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from agents.rca.contracts import RunConfig, RunState
from agents.rca.data_access import CSVTelemetryStore
from agents.rca.routing import CHEAP, Router
from agents.rca.runtime import parse_case
from llm import LLM


@unittest.skipUnless(importlib.util.find_spec("openai"), "Install requirements.txt to exercise the real SDK on localhost")
class SDKIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.reply = lambda request: (200, {"error": {"message": "synthetic capacity error"}})
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append((self.path, request))
                status, payload = owner.reply(request)
                encoded = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.environment = patch.dict(os.environ, {
            "FEATHERLESS_API_KEY": "synthetic-local-test-only",
            "FEATHERLESS_BASE_URL": f"http://127.0.0.1:{self.server.server_port}/v1"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_sdk_error_body_and_router_family_fallback(self):
        def reply(request):
            if len(self.requests) == 1:
                return 200, {"error": {"message": "busy"}}
            return 200, {"id": "synthetic", "object": "chat.completion", "created": 0,
                         "model": request["model"], "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "{\"ok\": true}"}}],
                         "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}}
        self.reply = reply
        with tempfile.TemporaryDirectory() as tmp:
            now = time.monotonic()
            state = RunState(CSVTelemetryStore(Path(tmp)), RunConfig(), Path(tmp), now, now+20)
            case = parse_case("One failure March 20, 2022 from 09:00 to 09:30. Identify root cause reason")
            router = Router(case, state, deadline=now+20)
            result = router.request("flash", [{"role": "user", "content": "synthetic"}], reason="integration_test", validate=json.loads)
            self.assertEqual(result, {"ok": True})
            self.assertEqual([request[1]["model"] for request in self.requests], list(CHEAP))
            self.assertTrue(all(path == "/v1/chat/completions" for path, _ in self.requests))
            self.assertEqual(router.events[0]["status"], "provider_error_body")
            self.assertIsNone(router.events[0]["estimated_cost_usd"])
            self.assertTrue(router.events[1]["fallback"])
            self.assertEqual(state.usage_ledger[CHEAP[1]]["prompt_tokens"], 12)
            state.client.client.close()

    def test_sdk_has_no_hidden_retry(self):
        self.reply = lambda request: (503, {"error": {"message": "synthetic unavailable"}})
        client = LLM()
        try:
            result = client.request(CHEAP[0], [{"role": "user", "content": "synthetic"}], timeout=2)
            self.assertEqual(len(self.requests), 1)
            self.assertEqual(result.error, "transport_InternalServerError")
        finally:
            client.client.close()


if __name__ == "__main__":
    unittest.main()
