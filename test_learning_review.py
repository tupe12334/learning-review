import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock
from tempfile import TemporaryDirectory


MODULE = Path(__file__).with_name("__init__.py")
spec = importlib.util.spec_from_file_location("learning_review", MODULE)
assert spec is not None and spec.loader is not None
learning_review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(learning_review)


class WaitForLinkTests(unittest.TestCase):
    def test_returns_verified_tokenized_url(self):
        with TemporaryDirectory() as directory:
            job = Path(directory) / "job"
            job.mkdir()
            token = "review-token"
            (job / "ui.log").write_text("your url is: https://review.trycloudflare.com\n", encoding="utf-8")

            url = learning_review.wait_for_link(job, token, timeout_seconds=0.1)

            self.assertEqual(url, "https://review.trycloudflare.com/?token=review-token")
            self.assertEqual(json.loads((job / "link.json").read_text(encoding="utf-8")), {"url": url})

    def test_marks_job_failed_instead_of_telling_user_to_retry(self):
        with TemporaryDirectory() as directory:
            job = Path(directory) / "job"
            job.mkdir()

            self.assertIsNone(learning_review.wait_for_link(job, "review-token", timeout_seconds=0))
            status = json.loads((job / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "failed")
            self.assertIn("tunnel", status["message"].lower())

    def test_recovers_delayed_tunnel_link_from_an_inflight_job(self):
        with TemporaryDirectory() as directory:
            runtime = Path(directory)
            job = runtime / "job"
            job.mkdir()
            (job / "status.json").write_text(json.dumps({"state": "starting"}), encoding="utf-8")
            (job / "token").write_text("review-token", encoding="utf-8")
            (job / "ui.log").write_text(
                "Visit https://example-review.trycloudflare.com | now\n", encoding="utf-8"
            )
            with mock.patch.object(learning_review, "RUNTIME", runtime):
                self.assertEqual(
                    learning_review._recover_running_link(),
                    {"url": "https://example-review.trycloudflare.com/?token=review-token"},
                )

    def test_running_link_recovery_ignores_completed_jobs(self):
        with TemporaryDirectory() as directory:
            runtime = Path(directory)
            job = runtime / "job"
            job.mkdir()
            (job / "status.json").write_text(json.dumps({"state": "complete"}), encoding="utf-8")
            (job / "token").write_text("review-token", encoding="utf-8")
            (job / "ui.log").write_text("https://example.trycloudflare.com\n", encoding="utf-8")
            previous_runtime = learning_review.RUNTIME
            learning_review.RUNTIME = runtime
            try:
                self.assertIsNone(learning_review._recover_running_link())
            finally:
                learning_review.RUNTIME = previous_runtime

    def test_current_session_recovery_does_not_block_another_chat(self):
        with TemporaryDirectory() as directory:
            runtime = Path(directory)
            current = runtime / "current"
            other = runtime / "other"
            current.mkdir()
            other.mkdir()
            for job, session_id, host in (
                (current, "session-current", "current-review"),
                (other, "session-other", "other-review"),
            ):
                (job / "request.json").write_text(
                    json.dumps({"session_id": session_id}), encoding="utf-8"
                )
                (job / "token").write_text("review-token", encoding="utf-8")
                (job / "ui.log").write_text(
                    f"https://{host}.trycloudflare.com\n", encoding="utf-8"
                )
            with mock.patch.object(learning_review, "RUNTIME", runtime):
                self.assertEqual(
                    learning_review._recover_current_session_link("session-current"),
                    "https://current-review.trycloudflare.com/?token=review-token",
                )
                self.assertIsNone(
                    learning_review._recover_current_session_link("session-missing")
                )

    def test_handle_does_not_reuse_another_sessions_running_review(self):
        with TemporaryDirectory() as directory:
            runtime = Path(directory)
            other_job = runtime / "other-session"
            other_job.mkdir(parents=True)
            (other_job / "status.json").write_text(json.dumps({"state": "starting"}), encoding="utf-8")
            (other_job / "token").write_text("other-token", encoding="utf-8")
            (other_job / "ui.log").write_text("https://other.trycloudflare.com\n", encoding="utf-8")
            (other_job / "request.json").write_text(json.dumps({"session_id": "other-session"}), encoding="utf-8")
            current = {"session_id": "current-session", "transcript": [{"role": "user", "content": "learn-review"}]}
            with mock.patch.object(learning_review, "RUNTIME", runtime), mock.patch.object(
                learning_review, "wait_for_link", return_value="https://current.trycloudflare.com/?token=current-token"
            ), mock.patch.object(learning_review.subprocess, "Popen"), mock.patch.object(
                learning_review.time, "time", return_value=1
            ):
                token_values = iter(["job-token", "current-token"])
                with mock.patch.object(learning_review.secrets, "token_hex", return_value="job"), mock.patch.object(
                    learning_review.secrets, "token_urlsafe", side_effect=token_values
                ):
                    learning_review.CURRENT.set(current)
                    result = learning_review._handle({})

            self.assertIn("current.trycloudflare.com", result)
            self.assertNotIn("other.trycloudflare.com", result)
            self.assertEqual([item.name for item in runtime.iterdir() if item.name != "other-session"], ["1-job"])


if __name__ == "__main__":
    unittest.main()
