import importlib.util
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


MODULE = Path(__file__).with_name("learning_review_worker.py")
spec = importlib.util.spec_from_file_location("learning_review_worker", MODULE)
assert spec is not None and spec.loader is not None
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class AnalyzeTldrTests(unittest.TestCase):
    def test_preserves_hebrew_tldr_from_analyzer_response(self):
        response = '''{"candidates":[{"id":"merge-pr","title":"Merge through PR","tldr_he":"למזג אוטומטית לאחר שכל הבדיקות ירוקות.","lesson":"Use a PR workflow.","evidence":"User requested automatic merge.","target_hint":"github-pr-workflow"}]}'''
        with TemporaryDirectory() as directory:
            job = Path(directory)
            worker.write(job, "request.json", {"transcript": [{"role": "user", "content": "Use a PR"}]})
            with patch.object(worker, "agent", return_value=response):
                worker.analyze(job)

            candidates = worker.read(job / "candidates.json", {})["candidates"]

        self.assertEqual(candidates[0]["tldr_he"], "למזג אוטומטית לאחר שכל הבדיקות ירוקות.")

    def test_persists_non_json_analyzer_output_for_diagnosis(self):
        with TemporaryDirectory() as directory:
            job = Path(directory)
            worker.write(job, "request.json", {"transcript": [{"role": "user", "content": "Learn this"}]})
            with patch.object(worker, "agent", return_value="HTTP 400: No models provided"):
                with self.assertRaisesRegex(ValueError, "No JSON object"):
                    worker.analyze(job)

            saved = (job / "agent-output.txt").read_text(encoding="utf-8")

        self.assertIn("No models provided", saved)

    def test_agent_rejects_cli_error_output_even_when_exit_code_is_zero(self):
        result = subprocess.CompletedProcess(
            [],
            0,
            "HTTP 400: No models provided",
            "Failed to parse config.yaml",
        )
        with patch.object(worker, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "Failed to parse"):
                worker.agent("prompt", Path("/tmp"))


class ApplyRecoveryTests(unittest.TestCase):
    def test_records_agent_delivery_output_for_failed_pr_diagnosis(self):
        with TemporaryDirectory() as directory:
            job = Path(directory)
            with patch.object(worker, "agent", return_value="Committed changes but forgot to create PR."):
                output = worker.run_agent(job, "prompt", Path(directory))

            self.assertIn("forgot", output)
            self.assertEqual(
                (job / "agent-output.txt").read_text(encoding="utf-8"),
                "Committed changes but forgot to create PR.",
            )

    def test_records_agent_failure_output_for_failed_pr_diagnosis(self):
        with TemporaryDirectory() as directory:
            job = Path(directory)
            with patch.object(worker, "agent", side_effect=RuntimeError("gh pr create timed out")):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    worker.run_agent(job, "prompt", Path(directory))

            self.assertIn("gh pr create timed out", (job / "agent-output.txt").read_text(encoding="utf-8"))

    def test_keeps_failed_apply_worktree_for_recovery(self):
        with TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            worktree.mkdir()
            with patch.object(worker, "run") as run:
                worker.cleanup_worktree(worktree, succeeded=False)

            self.assertTrue(worktree.exists())
            run.assert_not_called()

    def test_retries_open_pr_until_the_repair_merges_it(self):
        states = [
            {"number": 42, "url": "https://example.test/pr/42", "state": "OPEN"},
            {"number": 42, "url": "https://example.test/pr/42", "state": "MERGED"},
        ]
        with TemporaryDirectory() as directory:
            job = Path(directory)
            with patch.object(worker, "pr_state", side_effect=states), patch.object(worker, "run_agent") as repair, patch.object(worker, "run") as run:
                merged = worker.reconcile_and_merge(job, Path(directory), "learning-review/test")

        self.assertEqual(merged["state"], "MERGED")
        repair.assert_called_once()
        self.assertEqual(run.call_count, 0)

    def test_removes_worktree_only_after_successful_delivery(self):
        with TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            worktree.mkdir()
            with patch.object(worker, "run") as run:
                worker.cleanup_worktree(worktree, succeeded=True)

            run.assert_called_once_with(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=worker.REPO,
                timeout=120,
            )



    def test_missing_pr_error_includes_saved_agent_delivery_output(self):
        with TemporaryDirectory() as directory:
            job = Path(directory)
            (job / "agent-output.txt").write_text(
                "Push blocked by initial branch linecheck.", encoding="utf-8"
            )

            message = worker.missing_pr_error(job, "learning-review/job", 0)

        self.assertIn("found 0", message)
        self.assertIn("Push blocked", message)

    def test_reuses_clean_existing_recovery_worktree(self):
        with TemporaryDirectory() as directory:
            job = Path(directory) / "job"
            worktree = (
                Path(directory) / "runtime" / "learning-review-worktrees" / job.name
            )
            job.mkdir()
            worktree.mkdir(parents=True)
            branch = "learning-review/job"
            completed = [
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, f"{branch}\n", ""),
                subprocess.CompletedProcess([], 0, "", ""),
            ]
            with patch.object(worker, "HOME", Path(directory)), patch.object(
                worker, "run", side_effect=completed
            ) as run:
                result, recovered = worker.prepare_worktree(job, branch)

        self.assertEqual(result, worktree)
        self.assertTrue(recovered)
        self.assertEqual(run.call_count, 3)


    def test_recovery_delivery_prompt_does_not_reapply_skills(self):
        prompt = worker.delivery_prompt(Path("/tmp/worktree"), "learning-review/job")

        self.assertIn("Push only branch", prompt)
        self.assertIn("Create a pull request", prompt)
        self.assertNotIn("Update only skill files", prompt)

    @unittest.skipUnless(shutil.which("linecheck"), "linecheck is required")
    def test_initial_branch_push_checks_only_changes_since_origin_main(self):
        hook = MODULE.parents[2] / ".githooks" / "pre-push"
        with TemporaryDirectory() as directory:
            remote = Path(directory) / "remote.git"
            source = Path(directory) / "source"
            clone = Path(directory) / "clone"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
            (source / "linecheck.yml").write_text("max_lines: 1\n", encoding="utf-8")
            (source / "skills" / "existing" / "too-large").mkdir(parents=True)
            (source / "skills" / "existing" / "too-large" / "SKILL.md").write_text("one\ntwo\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "base"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(source), "remote", "add", "origin", str(remote)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(source), "push", "origin", "main"], check=True, capture_output=True)
            subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(clone), "checkout", "-b", "learning-review/test", "origin/main"], check=True, capture_output=True)
            (clone / "skills" / "changed" / "small").mkdir(parents=True)
            (clone / "skills" / "changed" / "small" / "SKILL.md").write_text("ok\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(clone), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(clone), "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "change"], check=True, capture_output=True)
            result = subprocess.run(
                [str(hook)],
                cwd=clone,
                input=(
                    f"refs/heads/learning-review/test {git_rev_parse(clone, 'HEAD')} "
                    f"refs/heads/learning-review/test {'0' * 40}\n"
                ),
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("validating 1 changed file", result.stdout)


def git_rev_parse(repo, ref):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref], check=True, text=True, capture_output=True).stdout.strip()


if __name__ == "__main__":
    unittest.main()
