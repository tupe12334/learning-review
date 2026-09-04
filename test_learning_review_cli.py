import importlib.util
import json
import os
import unittest
from unittest import mock
from pathlib import Path


MODULE = Path(__file__).with_name("learning_review_cli.py")
spec = importlib.util.spec_from_file_location("learning_review_cli", MODULE)
assert spec is not None and spec.loader is not None
learning_review_cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(learning_review_cli)


class ExportContextTests(unittest.TestCase):
    def test_session_export_builds_the_plugin_context_without_gateway_state(self):
        context = learning_review_cli.context_from_export_record(
            {
                "id": "session-1",
                "session_key": "cli:session-1",
                "messages": [
                    {"role": "user", "content": "learn-review"},
                    {"role": "assistant", "content": "Review ready"},
                ],
            }
        )

        self.assertEqual(context["session_id"], "session-1")
        self.assertEqual(context["session_key"], "cli:session-1")
        self.assertEqual(
            context["transcript"],
            [
                {"role": "user", "content": "learn-review"},
                {"role": "assistant", "content": "Review ready"},
            ],
        )


class ActiveSessionTests(unittest.TestCase):
    def test_rejects_export_from_a_different_active_session(self):
        with mock.patch.dict(
            os.environ,
            {"HERMES_SESSION_ID": "active-session"},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "active Hermes session"):
                learning_review_cli.require_active_session("other-session")


if __name__ == "__main__":
    unittest.main()
