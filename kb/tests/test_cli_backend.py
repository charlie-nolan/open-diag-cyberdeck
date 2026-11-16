"""app/llm_backend.CliModel with stand-in commands (no AI tool needed)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "app"))
from llm_backend import CliModel  # noqa: E402


class TestCli(unittest.TestCase):
    def test_stdin(self):
        m = CliModel(["cat"], system_prompt="SYS")
        out = m.generate("hello", lambda t: None)
        self.assertTrue(out.startswith("SYSTEM INSTRUCTIONS:\nSYS"))
        self.assertTrue(out.endswith("hello"))

    def test_prompt_arg(self):
        m = CliModel(["echo", "{prompt}"])
        self.assertEqual(m.generate("hi there", lambda t: None), "hi there")

    def test_errors(self):
        with self.assertRaises(RuntimeError):
            CliModel([])
        with self.assertRaises(RuntimeError):
            CliModel(["no-such-tool-xyz"]).generate("x", lambda t: None)
        with self.assertRaises(RuntimeError):
            CliModel(["false"]).generate("x", lambda t: None)


if __name__ == "__main__":
    unittest.main()
