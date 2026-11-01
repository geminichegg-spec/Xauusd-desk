import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import analyst  # noqa: E402
import cardinal_chat  # noqa: E402
import jev  # noqa: E402
import serve  # noqa: E402

HERE = os.path.dirname(__file__)
with open(os.path.join(HERE, "..", "examples", "plan_long.json")) as f:
    PLAN = json.load(f)
with open(os.path.join(HERE, "..", "examples", "candles_london.json")) as f:
    CANDLES = json.load(f)
FAKE_KEY = "sk-test-DO-NOT-LEAK-1234567890"


class ServeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = tempfile.mkdtemp()
        cls.patches = [mock.patch.object(serve, "RUNS", cls.runs),
                       mock.patch.dict(os.environ, {"GEMINI_API_KEY": FAKE_KEY}, clear=True)]
        for p in cls.patches:
            p.start()
        cls.srv = serve.make_server(0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        for p in cls.patches:
            p.stop()
        shutil.rmtree(cls.runs)

    def req(self, method, path, body=None, host=None, ctype="application/json"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            headers["Content-Type"] = ctype
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, raw

    def test_index_serves_hud(self):
        code, raw = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"Desk console", raw)

    def test_status_reports_keys_without_leaking_them(self):
        code, raw = self.req("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertNotIn(FAKE_KEY.encode(), raw)
        j = json.loads(raw)
        self.assertEqual(j["keys"], {"gemini": True, "grok": False, "groq": False, "typesafe": False})
        self.assertEqual(j["config"]["min_rr"], 1.8)

    def test_foreign_host_is_refused(self):
        self.assertEqual(self.req("GET", "/api/status", host="evil.example:8765")[0], 403)
        self.assertEqual(self.req("POST", "/api/gate", {"plan": PLAN}, host="evil.example")[0], 403)

    def test_post_requires_json_content_type(self):
        code, _ = self.req("POST", "/api/gate", json.dumps({"plan": PLAN}).encode(), ctype="text/plain")
        self.assertEqual(code, 400)

    def test_gate_matches_python(self):
        code, raw = self.req("POST", "/api/gate", {"plan": PLAN, "candles": CANDLES, "pnl": -120})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["risk"]["verdict"], "APPROVE")
        code, raw = self.req("POST", "/api/gate", {"plan": PLAN, "candles": CANDLES, "pnl": -600})
        self.assertEqual(json.loads(raw)["risk"]["verdict"], "REJECT")

    def test_run_saves_record_and_decision(self):
        fake = mock.Mock(return_value=json.dumps(PLAN))
        review = {"agrees": True, "flags": [], "data_bias": "long"}
        with mock.patch.dict(analyst.CALLERS, {"gemini": fake}), \
                mock.patch.object(jev, "review", return_value=review):
            code, raw = self.req("POST", "/api/run", {"candles": CANDLES, "session": "London",
                                                      "provider": "gemini", "pnl": -120, "jev": True})
        self.assertEqual(code, 200, raw)
        r = json.loads(raw)
        self.assertEqual(r["risk"]["verdict"], "APPROVE")
        self.assertEqual(r["jev_review"], review)
        self.assertIn("Session: London", fake.call_args[0][1])
        path = os.path.join(self.runs, r["run_id"] + ".json")
        self.assertTrue(os.path.exists(path))

        code, raw = self.req("GET", "/api/runs")
        pending = [x["run_id"] for x in json.loads(raw)["runs"]]
        self.assertIn(r["run_id"], pending)

        code, raw = self.req("POST", "/api/decision", {"run_id": r["run_id"], "decision": "approved", "note": "ok"})
        self.assertEqual(code, 200, raw)
        with open(path) as f:
            self.assertEqual(json.load(f)["decision"]["value"], "approved")
        pending = [x["run_id"] for x in json.loads(self.req("GET", "/api/runs")[1])["runs"]]
        self.assertNotIn(r["run_id"], pending)

    def test_run_needs_candles_and_valid_provider(self):
        self.assertEqual(self.req("POST", "/api/run", {"session": "London"})[0], 400)
        code, raw = self.req("POST", "/api/run", {"candles": CANDLES, "session": "London", "provider": "nope"})
        self.assertEqual(code, 400)

    def test_missing_key_is_reported_not_crashing(self):
        code, raw = self.req("POST", "/api/run", {"candles": CANDLES, "session": "London", "provider": "grok"})
        self.assertEqual(code, 502)
        self.assertIn("XAI_API_KEY", json.loads(raw)["error"])

    def test_groq_caller_uses_groq_endpoint_and_key(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk-test"}), \
                mock.patch.object(analyst, "_post", return_value={"choices": [{"message": {"content": "{}"}}]}) as post:
            analyst.call_groq("sys", "user")
        url, headers, body = post.call_args[0]
        self.assertTrue(url.startswith("https://api.groq.com/"))
        self.assertEqual(headers["Authorization"], "Bearer gsk-test")
        self.assertEqual(body["model"], "openai/gpt-oss-120b")

    def test_ask_sends_state_and_sanitizes_action(self):
        fake = mock.Mock(return_value='{"reply": "Long London, RISK approves.", "action": "place_order"}')
        with mock.patch.object(cardinal_chat, "chat", fake):
            code, raw = self.req("POST", "/api/ask", {"question": "what's the plan?", "state": {"plan": PLAN},
                                                      "history": [{"role": "assistant", "text": "hi"}]})
        self.assertEqual(code, 200, raw)
        r = json.loads(raw)
        self.assertEqual(r["reply"], "Long London, RISK approves.")
        self.assertEqual(r["action"], "none")          # anything outside the HUD's own actions is dropped
        self.assertEqual(r["provider"], "gemini")
        provider, system, turns = fake.call_args[0]
        self.assertIn('"orb_high": 2660.0', system)
        self.assertEqual(turns, [{"role": "user", "text": "what's the plan?"}])  # leading assistant turn dropped
        self.assertNotIn(FAKE_KEY, system)

    def test_ask_without_any_chat_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            code, raw = self.req("POST", "/api/ask", {"question": "status?"})
        self.assertEqual(code, 502)
        self.assertIn("GEMINI_API_KEY", json.loads(raw)["error"])

    def test_chat_provider_falls_back_to_groq(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk-x"}, clear=True):
            self.assertEqual(cardinal_chat.pick_provider(), "groq")
            self.assertEqual(json.loads(self.req("GET", "/api/status")[1])["chat_provider"], "groq")

    def test_decision_rejects_path_tricks(self):
        code, _ = self.req("POST", "/api/decision", {"run_id": "../../etc/passwd", "decision": "approved"})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
