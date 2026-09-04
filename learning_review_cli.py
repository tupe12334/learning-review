#!/usr/bin/env python3
"""Launch a token-protected learning review from an exported Hermes session.

This external entrypoint is deliberately separate from the gateway command
handler: it can be started from an independent shell when gateway lifecycle
rules prohibit a child process from owning the review UI and worker.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent


def context_from_export_record(record):
    """Convert a Hermes session export into the plugin's session-scoped context."""
    session_id = str(record.get("id", "")).strip()
    if not session_id:
        raise ValueError("session export is missing id")
    transcript = [
        {"role": str(message.get("role", "unknown")), "content": str(message.get("content", ""))}
        for message in record.get("messages", [])
    ]
    if not transcript:
        raise ValueError("session export contains no transcript")
    return {
        "session_id": session_id,
        "session_key": str(record.get("session_key", "")),
        "source": {"type": "cli"},
        "transcript": transcript,
    }


def load_plugin():
    """Load the installed plugin without requiring package-style imports."""
    module_path = PLUGIN_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location("learning_review", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load learning-review plugin")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require_active_session(session_id):
    """Reject a stale exported session when invoked inside an active Hermes turn."""
    active_session_id = os.environ.get("HERMES_SESSION_ID", "").strip()
    if active_session_id and active_session_id != session_id:
        raise ValueError(
            "requested session does not match the active Hermes session; "
            "launch /learn-review from the active conversation instead"
        )


def export_context(session_id):
    """Export one saved session through the supported Hermes CLI boundary."""
    with tempfile.TemporaryDirectory(prefix="hermes-review-") as directory:
        output = Path(directory) / "session.jsonl"
        subprocess.run(
            [
                "hermes",
                "sessions",
                "export",
                "--format",
                "jsonl",
                "--session-id",
                session_id,
                str(output),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        lines = output.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1:
        raise ValueError("expected exactly one exported session")
    return context_from_export_record(json.loads(lines[0]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True, help="Hermes session ID to review")
    arguments = parser.parse_args()
    require_active_session(arguments.session_id)
    plugin = load_plugin()
    plugin.CURRENT.set(export_context(arguments.session_id))
    result = plugin._handle({})
    print(result)
    return 0 if "https://" in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
