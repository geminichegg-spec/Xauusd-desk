import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import analyst  # noqa: E402
import jev  # noqa: E402
import keys  # noqa: E402


class KeysTest(unittest.TestCase):
    def test_dotenv_does_not_override_real_env(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("# comment\nGEMINI_API_KEY=from-file\nXAI_API_KEY='x-file'\n")
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "from-env"}, clear=True):
            keys.load_dotenv(f.name)
            self.assertEqual(keys.get_key("gemini"), "from-env")
            self.assertEqual(keys.get_key("grok"), "x-file")
        os.unlink(f.name)

    def test_missing_key_message(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                keys.get_key("grok")
            self.assertIn("XAI_API_KEY", str(cm.exception))
            self.assertEqual(keys.status(), {"gemini": False, "grok": False, "groq": False, "typesafe": False})

    def test_default_models(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(keys.get_model("grok"), "grok-4")
        with mock.patch.dict(os.environ, {"GEMINI_MODEL": "custom"}, clear=True):
            self.assertEqual(keys.get_model("gemini"), "custom")

    def test_example_env_has_no_real_keys(self):
        path = os.path.join(os.path.dirname(__file__), "..", ".env.example")
        with open(path) as f:
            for line in f:
                if line.startswith(("GEMINI_API_KEY", "XAI_API_KEY", "GROQ_API_KEY", "TYPESAFE_API_KEY")):
                    self.assertEqual(line.strip().split("=", 1)[1], "")


class AnalystTest(unittest.TestCase):
    def test_prompt_extracted(self):
        self.assertTrue(analyst.analyst_prompt().startswith("You are ANALYST"))

    def test_parse_plan_strips_fences(self):
        self.assertEqual(analyst.parse_plan('```json\n{"a": 1}\n```'), {"a": 1})


class JevTest(unittest.TestCase):
    PLAN = {"direction": "long"}

    def test_agrees(self):
        out = {"model": "jev-1", "answers": {
            "data_bias": {"type": "choice", "choice": "long", "confidence": 0.9, "probabilities": {}},
            "breakout_quality": {"type": "score", "score": 2.4},
            "plan_consistent": {"type": "noul", "noul": 0.9},
            "unsupported_claims": {"type": "noul", "noul": 0.1}}}
        r = jev.summarize(self.PLAN, out)
        self.assertTrue(r["agrees"])
        self.assertEqual(r["breakout_quality_0_to_3"], 2.4)

    def test_flags(self):
        out = {"answers": {
            "data_bias": {"choice": "flat"}, "plan_consistent": {"noul": 0.2},
            "unsupported_claims": {"noul": 0.8}}}
        r = jev.summarize(self.PLAN, out)
        self.assertFalse(r["agrees"])
        self.assertEqual(len(r["flags"]), 3)

    def test_question_shapes(self):
        q = jev.QUESTIONS
        self.assertIsInstance(q["data_bias"]["criteria"], dict)
        self.assertLessEqual(len(q["breakout_quality"]["criteria"]), 10)
        self.assertEqual(q["plan_consistent"]["type"], "noul")

    def test_missing_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                jev.review({}, [])
            self.assertIn("TYPESAFE_API_KEY", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
