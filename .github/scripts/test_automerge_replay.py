#!/usr/bin/env python3
"""Offline negative replay test for .github/scripts/automerge_verify.py.

Uses the sanitized public GitHub REST API sample captured from human PR #13
(``fixtures/human_pr13_replay.json``) and exercises the normal CLI entry point
(``automerge_verify.main``) with all outbound network access blocked.

Run as part of the standard script test suite::

    python -I -m unittest discover -s .github/scripts -p "test_*.py" -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import automerge_verify as av  # noqa: E402

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "human_pr13_replay.json"


class _FakeHttpResponse:
    """Minimal context-managed HTTP response for GitHubApi._GITHUB_OPENER."""

    def __init__(self, url: str, payload: object) -> None:
        self._url = url
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self._body
        return self._body[:size]


def _parse_github_output(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key] = value
    return values


class HumanPr13OfflineReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sample = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def _run_cli_offline(
        self,
        *,
        routes: dict[str, object],
        now: str = "2026-11-25T07:00:00Z",
    ) -> tuple[int, str, dict[str, str], list[str]]:
        requested_urls: list[str] = []

        def fake_open(self_opener, req, timeout=None):  # noqa: ANN001
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if self_opener is not av._GITHUB_OPENER:
                raise AssertionError(f"unexpected outbound network request: {url}")
            requested_urls.append(url)
            if url not in routes:
                raise AssertionError(f"unexpected GitHub API URL: {url}")
            return _FakeHttpResponse(url, routes[url])

        env = {
            "GITHUB_EVENT_PATH": "",
            "GITHUB_REPOSITORY": "",
            "GITHUB_TOKEN": "offline-replay-dummy-token",
            "GH_TOKEN": "",
            "GITHUB_OUTPUT": "",
        }
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            event_path = os.path.join(tmp, "event.json")
            output_path = os.path.join(tmp, "github_output.txt")
            Path(event_path).write_text(
                json.dumps(self.sample["workflow_run_event"]),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch.object(
                    urllib.request.OpenerDirector,
                    "open",
                    new=fake_open,
                ):
                    with contextlib.redirect_stdout(stdout):
                        code = av.main(
                            [
                                "--event",
                                event_path,
                                "--repo",
                                self.sample["repo"],
                                "--github-output",
                                output_path,
                                "--now",
                                now,
                            ]
                        )
            written = (
                Path(output_path).read_text(encoding="utf-8")
                if os.path.exists(output_path)
                else ""
            )
        return code, stdout.getvalue(), _parse_github_output(written), requested_urls

    def test_observed_closed_human_pr13_rejected_via_cli(self) -> None:
        """Observed state: PR #13 is closed so GET /pulls?state=open returns []."""
        repo = self.sample["repo"]
        pulls_url = (
            f"https://api.github.com/repos/{repo}/pulls"
            "?state=open&head=example-org%3Achore%2Fruleset-dry-run&per_page=100&page=1"
        )
        code, stdout, outputs, requested = self._run_cli_offline(
            routes={pulls_url: self.sample["pulls_open_observed"]}
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            stdout.strip(),
            "automerge verify: eligible=false: "
            "expected exactly one open target PR for head sha, found 0",
        )
        self.assertEqual(
            outputs,
            {
                "eligible": "false",
                "pr_number": "",
                "head_sha": "de3c6ad554fd6862008ed03a9c8f5f56c52b2f9d",
            },
        )
        self.assertEqual(requested, [pulls_url])


if __name__ == "__main__":
    unittest.main()
