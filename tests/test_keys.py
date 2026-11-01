import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import analyst  # noqa: E402
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
            self.assertEqual(keys.status(), {"gemini": False, "grok": False})

    def test_default_models(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(keys.get_model("grok"), "grok-4")
        with mock.patch.dict(os.environ, {"GEMINI_MODEL": "custom"}, clear=True):
            self.assertEqual(keys.get_model("gemini"), "custom")

    def test_example_env_has_no_real_keys(self):
        path = os.path.join(os.path.dirname(__file__), "..", ".env.example")
        with open(path) as f:
            for line in f:
                if line.startswith(("GEMINI_API_KEY", "XAI_API_KEY")):
                    self.assertEqual(line.strip().split("=", 1)[1], "")


class AnalystTest(unittest.TestCase):
    def test_prompt_extracted(self):
        self.assertTrue(analyst.analyst_prompt().startswith("You are ANALYST"))

    def test_parse_plan_strips_fences(self):
        self.assertEqual(analyst.parse_plan('```json\n{"a": 1}\n```'), {"a": 1})


if __name__ == "__main__":
    unittest.main()
