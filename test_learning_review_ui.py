import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("LEARNING_REVIEW_JOB", "/tmp/learning-review-ui-test")
os.environ.setdefault("LEARNING_REVIEW_TOKEN", "test-token")
MODULE = Path(__file__).with_name("learning_review_ui.py")
spec = importlib.util.spec_from_file_location("learning_review_ui", MODULE)
assert spec is not None and spec.loader is not None
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


class DynamicPortTests(unittest.TestCase):
    def test_main_uses_bound_ephemeral_port_for_tunnel(self):
        server = type("Server", (), {"server_address": ("127.0.0.1", 45678), "serve_forever": lambda self: None})()
        with patch.object(ui, "ThreadingHTTPServer", return_value=server), patch.object(ui.threading, "Thread") as thread:
            with patch("sys.argv", ["learning_review_ui.py", "--port", "0"]):
                ui.main()

        self.assertEqual(thread.call_args.kwargs["args"], (45678,))


if __name__ == "__main__":
    unittest.main()
