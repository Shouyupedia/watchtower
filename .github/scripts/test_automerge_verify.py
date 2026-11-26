#!/usr/bin/env python3
"""Offline unit tests for .github/scripts/automerge_verify.py.

Every API payload here is synthetic: the JSON files under ``fixtures/`` are
hand-written shapes (not captured real-world samples) and the rest of the
payloads are built in memory.  No test performs network I/O; the GitHub reader
and the cooldown HTTP getter are in-memory doubles that fail on any unexpected
request.

The suite is run exactly as CI runs it::

    python -I -m unittest discover -s .github/scripts -p "test_*.py" -v
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import automerge_verify as av  # noqa: E402
import cooldown  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
REPO = "example-org/example-repo"
OWNER = "example-org"
HEAD_SHA = "a" * 40
TOOLCHAIN_SHA = "b" * 40
BASE_SHA = "1" * 40
MAIN_SHA = "2" * 40
OTHER_SHA = "3" * 40
MERGE_PREVIEW_SHA = "e" * 40
MIDDLE_COMMIT_SHA = "c" * 40

DEPENDABOT = "dependabot[bot]"
UPDATER = "watchtower-updater[bot]"
SIGNAL = "dependabot-body-v1"
CALIBRATED = frozenset({SIGNAL})

TRUSTED_OLD = "2026-11-01T00:00:00Z"
GO_HINT = "2026-10-31T23:00:00Z"
TOOLCHAIN_HINT = "2026-10-31T22:00:00Z"
NEWDEP_HINT = "2026-10-30T00:00:00Z"
TOOLCHAIN_VERSION = "v0.0.1-go1.26.11.linux-amd64"

DIGEST_A = "11" * 32
DIGEST_B = "22" * 32
DIGEST_C = "33" * 32
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64

GOLANG_DOCKERFILES = {
    "dockerfiles/Dockerfile",
    "dockerfiles/Dockerfile.self-contained",
    "dockerfiles/Dockerfile.dev-self-contained",
}

EXPECTED_REQUIRED_WORKFLOWS = {
    "Pull Request": ".github/workflows/pull-request.yml",
    "CodeQL": ".github/workflows/codeql-analysis.yml",
}

# Deliberately specified here, independently of the module under test, so a
# drift in either direction fails a test.
EXPECTED_REQUIRED_CHECKS = {
    "CI / lint": "github-actions",
    "CI / test (ubuntu-latest)": "github-actions",
    "CI / test (macos-latest)": "github-actions",
    "CI / test (windows-latest)": "github-actions",
    "CI / mod-check": "github-actions",
    "CI / version-consistency": "github-actions",
    "CI / vuln": "github-actions",
    "CI / script-tests": "github-actions",
    "CI / goreleaser-snapshot": "github-actions",
    "CI / docker-build": "github-actions",
    "CI / docs-build": "github-actions",
    "CI / workflow-lint": "github-actions",
    "dependency-review": "github-actions",
    "Analyze (go)": "github-actions",
    "CodeQL": "github-advanced-security",
}

_UNSET = object()


# ---------------------------------------------------------------------------
# Synthetic payload builders
# ---------------------------------------------------------------------------


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def jsonb(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def ndjson(items) -> bytes:
    return ("\n".join(json.dumps(item) for item in items) + "\n").encode("utf-8")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def go_escape(value: str) -> str:
    """The documented Go module proxy case encoding (uppercase -> !lowercase)."""
    return "".join("!" + ch.lower() if "A" <= ch <= "Z" else ch for ch in value)


def go_info_url(module: str, version: str) -> str:
    return (
        "https://proxy.golang.org/"
        + urllib.parse.quote(go_escape(module), safe="/+.-_~")
        + "/@v/"
        + urllib.parse.quote(go_escape(version), safe="+.-_~")
        + ".info"
    )


def go_index_url(hint: str) -> str:
    since = parse_iso(hint) - timedelta(seconds=60)
    text = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "https://index.golang.org/index?" + urllib.parse.urlencode(
        {"since": text, "limit": 2000}
    )


def docker_tag_url(repository: str, tag: str) -> str:
    if "/" not in repository:
        repository = f"library/{repository}"
    return (
        f"https://hub.docker.com/v2/repositories/"
        f"{urllib.parse.quote(repository, safe='/')}"
        f"/tags/{urllib.parse.quote(tag, safe='')}"
    )


def pypi_json_url(package: str, version: str) -> str:
    return (
        f"https://pypi.org/pypi/{urllib.parse.quote(package, safe='')}"
        f"/{urllib.parse.quote(version, safe='.+!_-')}/json"
    )


def make_blob(text: str) -> tuple[str, dict]:
    raw = text.encode("utf-8")
    sha = hashlib.sha1(raw).hexdigest()
    return sha, {
        "sha": sha,
        "encoding": "base64",
        "size": len(raw),
        "content": base64.b64encode(raw).decode("ascii"),
    }


def make_tree(files: dict[str, str], modes: dict[str, str]) -> tuple[dict, dict]:
    tree = []
    blobs = {}
    for path in sorted(files):
        sha, blob = make_blob(files[path])
        blobs[sha] = blob
        tree.append(
            {
                "path": path,
                "mode": modes.get(path, "100644"),
                "type": "blob",
                "sha": sha,
            }
        )
    return {"sha": "0" * 40, "truncated": False, "tree": tree}, blobs


def make_commit(
    sha: str,
    parent: str | None,
    author: str,
    *,
    committer: str = "web-flow",
    verified: bool = True,
    parents=None,
) -> dict:
    if parents is None:
        parents = [] if parent is None else [{"sha": parent}]
    return {
        "sha": sha,
        "parents": parents,
        "author": {"login": author},
        "committer": {"login": committer},
        "commit": {"verification": {"verified": verified}},
    }


def make_workflow_run(
    name: str,
    path: str,
    head_sha: str,
    head_branch: str,
    *,
    id: int = 1000,
    event: str = "pull_request",
    status: str = "completed",
    conclusion: str | None = "success",
    attempt: int = 1,
    repo: str = REPO,
) -> dict:
    return {
        "id": id,
        "name": name,
        "path": path,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "run_attempt": attempt,
        "head_sha": head_sha,
        "head_branch": head_branch,
        "head_repository": {"full_name": repo},
    }


def make_check_run(
    name: str,
    app_slug: str,
    head_sha: str,
    *,
    id: int = 1,
    status: str = "completed",
    conclusion: str | None = "success",
    started_at: str | None = "2026-11-25T10:00:00Z",
) -> dict:
    return {
        "id": id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "head_sha": head_sha,
        "app": {"slug": app_slug},
        "started_at": started_at,
    }


def go_mod_text(
    *,
    module: str = "example.com/project",
    go_version: str = "1.26",
    requires=(("example.com/mod", "v1.2.3"),),
    extra=(),
    toolchain: str | None = None,
) -> str:
    lines = [f"module {module}", "", f"go {go_version}"]
    if toolchain is not None:
        lines.append(f"toolchain {toolchain}")
    lines.extend(extra)
    lines.append("")
    lines.append("require (")
    lines.extend(f"\t{name} {version}" for name, version in requires)
    lines.append(")")
    return "\n".join(lines) + "\n"


def dockerfile_text(tag: str, digest: str) -> str:
    return f"FROM golang:{tag}@sha256:{digest}\nRUN echo synthetic\n"


GO_MOD_BASE = go_mod_text()
GO_MOD_HEAD = go_mod_text(requires=(("example.com/mod", "v1.2.4"),))
GO_SUM_BASE = "example.com/mod v1.2.3 h1:c3l0e0FTZXRoZXRpYw==\n"
GO_SUM_HEAD = "example.com/mod v1.2.4 h1:c3l0e0FTZXRoZXRpYw==\n"

COMMON_FILES = {
    "README.md": "# synthetic repository\n",
    ".github/scripts/cooldown.py": "# synthetic script placeholder\n",
    ".github/workflows/pull-request.yml": "name: Pull Request\non: pull_request\njobs: {}\n",
    ".github/actions/synthetic/action.yml": "name: synthetic\nruns:\n  using: composite\n  steps: []\n",
    ".github/CODEOWNERS": "* @example-owner\n",
    ".github/dependabot.yml": "version: 2\nupdates: []\n",
}


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class StrictHttp:
    """In-memory HttpGet double that records and rejects unknown URLs."""

    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = dict(responses)
        self.calls: list[str] = []
        self.unexpected: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url not in self.responses:
            self.unexpected.append(url)
            raise cooldown.CooldownError(f"no synthetic response for {url}")
        return self.responses[url]


class FakeGitHub:
    """Strict in-memory GitHubGetter keyed by endpoint and scenario state."""

    def __init__(self, scenario: "Scenario") -> None:
        self.s = scenario
        self.calls: list[str] = []
        self._pr_detail_calls = 0

    @staticmethod
    def _expect(q: dict, expected: dict[str, str]) -> None:
        for key, value in expected.items():
            if q.get(key) != [value]:
                raise AssertionError(
                    f"unexpected query {key}={q.get(key)!r}; expected {value!r}"
                )

    def get(self, path: str):
        self.calls.append(path)
        s = self.s
        repo = s.repo
        parsed = urllib.parse.urlsplit(path)
        p = parsed.path
        q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        if p == f"/repos/{repo}/pulls":
            self._expect(
                q,
                {
                    "state": "open",
                    "head": f"{OWNER}:{s.head_branch}",
                    "per_page": "100",
                    "page": "1",
                },
            )
            return copy.deepcopy(s.pulls)

        prefix = f"/repos/{repo}/pulls/"
        if p.startswith(prefix):
            rest = p[len(prefix):]
            if rest.endswith("/commits") and rest[: -len("/commits")].isdigit():
                self._expect(q, {"per_page": "100", "page": "1"})
                return copy.deepcopy(s.commits)
            if rest == str(s.pr_number):
                self._pr_detail_calls += 1
                if self._pr_detail_calls == 1:
                    return copy.deepcopy(s.pr)
                return copy.deepcopy(s.final_pr)

        if p == f"/repos/{repo}/actions/runs":
            self._expect(
                q, {"head_sha": s.head_sha, "per_page": "100", "page": "1"}
            )
            total = s.workflow_runs_total
            return {
                "total_count": (
                    len(s.workflow_runs) if total is None else total
                ),
                "workflow_runs": copy.deepcopy(s.workflow_runs),
            }

        check_prefix = f"/repos/{repo}/commits/"
        if p.startswith(check_prefix) and p.endswith("/check-runs"):
            sha = p[len(check_prefix): -len("/check-runs")]
            self._expect(
                q,
                {
                    "per_page": "100",
                    "page": "1",
                },
            )
            if sha != s.head_sha:
                raise AssertionError(f"check-runs queried for unexpected sha {sha}")
            total = s.check_runs_total
            return {
                "total_count": len(s.check_runs) if total is None else total,
                "check_runs": copy.deepcopy(s.check_runs),
            }

        if p == f"/repos/{repo}/commits/main":
            return copy.deepcopy(s.main_commit)

        compare_prefix = f"/repos/{repo}/compare/"
        if p.startswith(compare_prefix):
            return copy.deepcopy(s.compare)

        tree_prefix = f"/repos/{repo}/git/trees/"
        if p.startswith(tree_prefix):
            sha = p[len(tree_prefix):]
            self._expect(q, {"recursive": "1"})
            if sha not in s.trees:
                raise AssertionError(f"no synthetic tree for sha {sha}")
            return copy.deepcopy(s.trees[sha])

        blob_prefix = f"/repos/{repo}/git/blobs/"
        if p.startswith(blob_prefix):
            sha = p[len(blob_prefix):]
            if sha not in s.blobs:
                raise AssertionError(f"no synthetic blob for sha {sha}")
            return copy.deepcopy(s.blobs[sha])

        raise AssertionError(f"unexpected GitHub API path: {path}")


# ---------------------------------------------------------------------------
# Scenario builder
# ---------------------------------------------------------------------------


class Scenario:
    """A fully synthetic, otherwise-eligible pull request plus its API world."""

    def __init__(self, kind: str = "go_modules") -> None:
        self.kind = kind
        self.repo = REPO
        self.head_sha = HEAD_SHA
        self.head_branch = ""
        self.pr_number = 0
        self.author = ""
        self.pr_fixture = "dependabot_normal_pr.json"
        self.pr_body: str | None = None
        self.merge_base_sha = MAIN_SHA
        self.main_sha = MAIN_SHA
        self.base_files: dict[str, str] = {}
        self.head_files: dict[str, str] = {}
        self.base_modes: dict[str, str] = {}
        self.head_modes: dict[str, str] = {}
        self.http_map: dict[str, bytes] = {}
        self.workflow_runs: list[dict] = []
        self.check_runs: list[dict] = []
        self.check_runs_total: int | None = None
        self.workflow_runs_total: int | None = None
        self.pulls: list = []
        self.final_pr: dict = {}
        self.commits: list[dict] = []
        setup = {
            "go_modules": self._setup_go_modules,
            "docker": self._setup_docker,
            "pip": self._setup_pip,
            "toolchain": self._setup_toolchain,
        }[kind]
        setup()
        self._build_common()
        self.rebuild()

    # -- setup ---------------------------------------------------------

    def _setup_go_modules(self) -> None:
        self.author = DEPENDABOT
        self.head_branch = "dependabot/go_modules/example.com/mod-1.2.4"
        self.pr_number = 42
        base = dict(COMMON_FILES)
        base["go.mod"] = GO_MOD_BASE
        base["go.sum"] = GO_SUM_BASE
        head = dict(base)
        head["go.mod"] = GO_MOD_HEAD
        head["go.sum"] = GO_SUM_HEAD
        self.base_files = base
        self.head_files = head
        self.add_go_evidence(
            "example.com/mod", "v1.2.4", hint=GO_HINT, trusted=TRUSTED_OLD
        )

    def _setup_docker(self) -> None:
        self.author = DEPENDABOT
        self.head_branch = "dependabot/docker/dockerfiles/Dockerfile"
        self.pr_number = 41
        self.pr_body = (
            "Bumps golang from 1.26.10-alpine to 1.26.11-alpine.\n"
            "<details>\n<summary>Commits</summary>\n<p>Synthetic.</p>\n</details>\n"
        )
        base = dict(COMMON_FILES)
        for path in GOLANG_DOCKERFILES:
            base[path] = dockerfile_text("1.26.10-alpine", DIGEST_A)
        head = dict(base)
        head["dockerfiles/Dockerfile"] = dockerfile_text(
            "1.26.10-alpine", DIGEST_B
        )
        self.base_files = base
        self.head_files = head
        self.add_docker_evidence(
            "library/golang", "1.26.10-alpine", DIGEST_B, TRUSTED_OLD
        )

    def _setup_pip(self) -> None:
        self.author = DEPENDABOT
        self.head_branch = "dependabot/pip/docs-requirements-txt-abc123"
        self.pr_number = 43
        self.pr_body = (
            "Bumps [mkdocs](https://pypi.org/project/mkdocs/) from 1.6.0 to 1.6.1.\n"
            "<details>\n<summary>Changelog</summary>\n<p>Synthetic.</p>\n</details>\n"
        )
        base = dict(COMMON_FILES)
        base["docs-requirements.txt"] = f"mkdocs==1.6.0 --hash=sha256:{HASH_A}\n"
        head = dict(COMMON_FILES)
        head["docs-requirements.txt"] = f"mkdocs==1.6.1 --hash=sha256:{HASH_B}\n"
        self.base_files = base
        self.head_files = head
        self.add_pypi_evidence("mkdocs", "1.6.1", [(HASH_B, TRUSTED_OLD)])

    def _setup_toolchain(self) -> None:
        self.author = UPDATER
        self.head_sha = TOOLCHAIN_SHA
        self.pr_fixture = "toolchain_pr.json"
        self.pr_number = 44
        self.head_branch = "automation/go-toolchain"
        base = dict(COMMON_FILES)
        base[".go-version"] = "1.26.10\n"
        base["go.mod"] = "module example.com/project\n\ngo 1.26\n"
        for path in GOLANG_DOCKERFILES:
            base[path] = dockerfile_text("1.26.10-alpine", DIGEST_A)
        head = dict(base)
        head[".go-version"] = "1.26.11\n"
        for path in GOLANG_DOCKERFILES:
            head[path] = dockerfile_text("1.26.11-alpine", DIGEST_B)
        self.base_files = base
        self.head_files = head
        self.add_go_evidence(
            "golang.org/toolchain",
            TOOLCHAIN_VERSION,
            hint=TOOLCHAIN_HINT,
            trusted=TRUSTED_OLD,
        )
        self.add_docker_evidence(
            "library/golang", "1.26.11-alpine", DIGEST_B, TRUSTED_OLD
        )

    def _build_common(self) -> None:
        event = fixture("workflow_run_event.json")
        event["repository"]["full_name"] = self.repo
        run = event["workflow_run"]
        run["head_sha"] = self.head_sha
        run["head_branch"] = self.head_branch
        run["head_repository"]["full_name"] = self.repo
        self.event = event

        pr = fixture(self.pr_fixture)
        pr["number"] = self.pr_number
        pr["user"]["login"] = self.author
        pr["head"]["sha"] = self.head_sha
        pr["head"]["ref"] = self.head_branch
        pr["head"]["repo"]["full_name"] = self.repo
        pr["base"]["ref"] = "main"
        pr["base"]["sha"] = BASE_SHA
        pr["commits"] = 1
        if self.pr_body is not None:
            pr["body"] = self.pr_body
        self.pr = pr
        list_pr = copy.deepcopy(pr)
        list_pr.pop("commits", None)
        self.pulls = [list_pr]
        self.final_pr = copy.deepcopy(pr)

        self.commits = [
            make_commit(self.head_sha, self.merge_base_sha, self.author)
        ]
        self.main_commit = {"sha": self.main_sha}
        self.compare = {
            "status": "ahead",
            "ahead_by": 1,
            "merge_base_commit": {"sha": self.merge_base_sha},
        }
        self.workflow_runs = [
            make_workflow_run(
                "Pull Request",
                EXPECTED_REQUIRED_WORKFLOWS["Pull Request"],
                self.head_sha,
                self.head_branch,
                id=1001,
            ),
            make_workflow_run(
                "CodeQL",
                EXPECTED_REQUIRED_WORKFLOWS["CodeQL"],
                self.head_sha,
                self.head_branch,
                id=1002,
            ),
        ]
        self.check_runs = [
            make_check_run(name, app, self.head_sha, id=index)
            for index, (name, app) in enumerate(
                EXPECTED_REQUIRED_CHECKS.items(), start=1
            )
        ]

    # -- evidence helpers ---------------------------------------------

    def add_go_evidence(
        self, module: str, version: str, *, hint: str, trusted: str, path=None
    ) -> None:
        self.http_map[go_info_url(module, version)] = jsonb(
            {"Version": version, "Time": hint}
        )
        self.http_map[go_index_url(hint)] = ndjson(
            [
                {
                    "Path": module if path is None else path,
                    "Version": version,
                    "Timestamp": trusted,
                }
            ]
        )

    def add_docker_evidence(
        self, repository: str, tag: str, digest: str, pushed: str
    ) -> None:
        self.http_map[docker_tag_url(repository, tag)] = jsonb(
            {"name": tag, "digest": f"sha256:{digest}", "tag_last_pushed": pushed}
        )

    def add_pypi_evidence(
        self, package: str, version: str, files: list[tuple[str, str | None]]
    ) -> None:
        self.http_map[pypi_json_url(package, version)] = jsonb(
            {
                "info": {"version": version},
                "urls": [
                    {
                        "digests": {"sha256": sha},
                        "upload_time_iso_8601": uploaded,
                    }
                    for sha, uploaded in files
                ],
            }
        )

    # -- mutation helpers ---------------------------------------------

    def rebuild(self) -> None:
        self.base_tree, base_blobs = make_tree(self.base_files, self.base_modes)
        self.head_tree, head_blobs = make_tree(self.head_files, self.head_modes)
        self.trees = {
            self.merge_base_sha: self.base_tree,
            self.head_sha: self.head_tree,
        }
        self.blobs = dict(base_blobs)
        self.blobs.update(head_blobs)

    def set_base_file(self, path: str, text: str) -> None:
        self.base_files[path] = text
        self.base_modes.pop(path, None)
        self.rebuild()

    def set_head_file(self, path: str, text: str) -> None:
        self.head_files[path] = text
        self.head_modes.pop(path, None)
        self.rebuild()

    def remove_head_file(self, path: str) -> None:
        del self.head_files[path]
        self.rebuild()

    def set_head_branch(self, branch: str) -> None:
        self.head_branch = branch
        self.event["workflow_run"]["head_branch"] = branch
        self.pr["head"]["ref"] = branch
        self.final_pr["head"]["ref"] = branch
        for entry in self.pulls:
            if isinstance(entry, dict) and isinstance(entry.get("head"), dict):
                entry["head"]["ref"] = branch
        for run in self.workflow_runs:
            run["head_branch"] = branch

    def set_merge_base(self, sha: str) -> None:
        self.merge_base_sha = sha
        self.compare["merge_base_commit"]["sha"] = sha
        self.commits[0]["parents"] = [{"sha": sha}]
        self.rebuild()

    def workflow_run(self, name: str) -> dict:
        return next(run for run in self.workflow_runs if run["name"] == name)

    def check_run(self, name: str) -> dict:
        return next(run for run in self.check_runs if run["name"] == name)

    def head_blob_sha(self, path: str) -> str:
        return next(
            entry["sha"]
            for entry in self.head_tree["tree"]
            if entry["path"] == path
        )

    # -- execution -----------------------------------------------------

    def verify(
        self,
        *,
        signals=CALIBRATED,
        now=NOW,
        repo=REPO,
        event=_UNSET,
    ):
        self.http = StrictHttp(self.http_map)
        self.github = FakeGitHub(self)
        payload = copy.deepcopy(self.event) if event is _UNSET else event
        return av.verify_automerge(
            payload,
            client=self.github,
            http_get=self.http,
            now=now,
            repo=repo,
            calibrated_signals=signals,
        )

    def verify_with_module_defaults(self, *, now=NOW, repo=REPO):
        """Verify without passing ``calibrated_signals``, exercising the default."""
        self.http = StrictHttp(self.http_map)
        self.github = FakeGitHub(self)
        return av.verify_automerge(
            copy.deepcopy(self.event),
            client=self.github,
            http_get=self.http,
            now=now,
            repo=repo,
        )


class AutomergeTestCase(unittest.TestCase):
    def assertRejected(self, decision, contains: str) -> None:
        self.assertFalse(decision.eligible, decision.reason)
        self.assertTrue(decision.reason.strip())
        self.assertNotIn("unexpected verification error", decision.reason)
        self.assertIn(contains, decision.reason)

    def assertEligible(self, decision, *, pr_number: int, head_sha: str) -> None:
        self.assertTrue(decision.eligible, decision.reason)
        self.assertEqual(decision.pr_number, pr_number)
        self.assertEqual(decision.head_sha, head_sha)


# ---------------------------------------------------------------------------
# Constants pinned by the design
# ---------------------------------------------------------------------------


class ConstantsTests(AutomergeTestCase):
    def test_required_check_names_and_apps(self) -> None:
        self.assertEqual(av.REQUIRED_CHECKS, EXPECTED_REQUIRED_CHECKS)

    def test_required_workflow_names_and_paths(self) -> None:
        self.assertEqual(av.REQUIRED_WORKFLOWS, EXPECTED_REQUIRED_WORKFLOWS)

    def test_calibrated_signals_are_intentionally_empty(self) -> None:
        self.assertEqual(av.CALIBRATED_NORMAL_UPDATE_SIGNALS, frozenset())
        self.assertEqual(av.DEPENDABOT_BODY_SIGNAL, SIGNAL)

    def test_allowed_authors_and_ecosystems(self) -> None:
        self.assertEqual(av.ALLOWED_AUTHORS, {DEPENDABOT, UPDATER})
        self.assertEqual(av.ALLOWED_ECOSYSTEMS, {"go_modules", "docker", "pip"})

    def test_sensitive_paths_and_whitelists(self) -> None:
        self.assertEqual(
            av.SENSITIVE_PREFIXES,
            (
                ".github/workflows/",
                ".github/actions/",
                ".github/scripts/",
            ),
        )
        self.assertEqual(
            av.SENSITIVE_BASENAMES,
            {"CODEOWNERS", "dependabot.yml", "dependabot.yaml"},
        )
        self.assertEqual(
            av.GO_MODULE_FILES,
            {"go.mod", "go.sum", "tools/go.mod", "tools/go.sum"},
        )
        self.assertEqual(av.PIP_FILE, "docs-requirements.txt")
        self.assertEqual(av.GOLANG_DOCKERFILES, GOLANG_DOCKERFILES)

    def test_cooldown_constants(self) -> None:
        self.assertEqual(av.GO_PATCH_COOLDOWN_DAYS, 3)
        self.assertEqual(av.GO_MINOR_COOLDOWN_DAYS, 7)
        self.assertEqual(av.GO_NEW_MODULE_COOLDOWN_DAYS, 7)
        self.assertEqual(av.DOCKER_COOLDOWN_DAYS, 7)
        self.assertEqual(av.PIP_COOLDOWN_DAYS, 7)
        self.assertEqual(av.TOOLCHAIN_COOLDOWN_DAYS, 7)


# ---------------------------------------------------------------------------
# Event parsing and binding
# ---------------------------------------------------------------------------


class EventParsingTests(AutomergeTestCase):
    def test_non_object_event_rejected(self) -> None:
        scenario = Scenario()
        self.assertRejected(
            scenario.verify(event=[]), "event payload is missing or not an object"
        )

    def test_event_action_not_completed(self) -> None:
        scenario = Scenario()
        scenario.event["action"] = "requested"
        self.assertRejected(scenario.verify(), "event action is not 'completed'")

    def test_missing_workflow_run(self) -> None:
        scenario = Scenario()
        del scenario.event["workflow_run"]
        self.assertRejected(scenario.verify(), "workflow_run is missing")

    def test_missing_workflow_name(self) -> None:
        scenario = Scenario()
        del scenario.event["workflow_run"]["name"]
        self.assertRejected(scenario.verify(), "workflow_run.name is missing")

    def test_unknown_workflow_name(self) -> None:
        scenario = Scenario()
        scenario.event["workflow_run"]["name"] = "Docs"
        self.assertRejected(scenario.verify(), "is not a required workflow")

    def test_workflow_event_not_pull_request(self) -> None:
        scenario = Scenario()
        scenario.event["workflow_run"]["event"] = "push"
        self.assertRejected(
            scenario.verify(), "workflow_run.event is not pull_request"
        )

    def test_workflow_status_not_completed(self) -> None:
        scenario = Scenario()
        scenario.event["workflow_run"]["status"] = "in_progress"
        self.assertRejected(
            scenario.verify(), "workflow_run.status is not completed"
        )

    def test_invalid_head_sha(self) -> None:
        scenario = Scenario()
        scenario.event["workflow_run"]["head_sha"] = "abc"
        self.assertRejected(scenario.verify(), "workflow_run.head_sha is invalid")

    def test_missing_head_branch(self) -> None:
        scenario = Scenario()
        del scenario.event["workflow_run"]["head_branch"]
        self.assertRejected(scenario.verify(), "workflow_run.head_branch is missing")

    def test_missing_head_repository(self) -> None:
        scenario = Scenario()
        del scenario.event["workflow_run"]["head_repository"]
        self.assertRejected(
            scenario.verify(), "workflow_run.head_repository is missing"
        )

    def test_head_repository_is_not_this_repository(self) -> None:
        scenario = Scenario()
        scenario.event["workflow_run"]["head_repository"]["full_name"] = "other/repo"
        self.assertRejected(
            scenario.verify(), "workflow run head repository is not this repository"
        )

    def test_invalid_repository_name(self) -> None:
        scenario = Scenario()
        self.assertRejected(scenario.verify(repo="not a repo!"), "repository is invalid")

    def test_missing_repository(self) -> None:
        scenario = Scenario()
        self.assertRejected(
            scenario.verify(event={}, repo=None), "repository is missing"
        )

    def test_naive_verification_time_rejected(self) -> None:
        scenario = Scenario()
        naive = datetime(2026, 11, 25, 12, 0, 0)
        self.assertRejected(
            scenario.verify(now=naive), "timezone-aware datetime"
        )

    def test_merge_preview_sha_in_event_matches_no_pr(self) -> None:
        scenario = Scenario()
        scenario.event["workflow_run"]["head_sha"] = MERGE_PREVIEW_SHA
        decision = scenario.verify()
        self.assertRejected(decision, "expected exactly one open target PR")
        self.assertEqual(decision.head_sha, MERGE_PREVIEW_SHA)


class PullRequestBindingTests(AutomergeTestCase):
    def test_head_sha_mismatch_matches_no_pr(self) -> None:
        scenario = Scenario()
        scenario.pulls[0]["head"]["sha"] = OTHER_SHA
        self.assertRejected(
            scenario.verify(), "expected exactly one open target PR for head sha, found 0"
        )

    def test_merge_preview_sha_fields_are_not_used_for_binding(self) -> None:
        # The temporary refs/pull/<n>/merge commit must never be the SHA the
        # decision binds to or passes to the merge call.
        scenario = Scenario()
        scenario.pulls[0]["merge_commit_sha"] = MERGE_PREVIEW_SHA
        scenario.pr["merge_commit_sha"] = MERGE_PREVIEW_SHA
        scenario.final_pr["merge_commit_sha"] = MERGE_PREVIEW_SHA
        decision = scenario.verify()
        self.assertEligible(decision, pr_number=42, head_sha=HEAD_SHA)

    def test_closed_pr_matches_nothing(self) -> None:
        scenario = Scenario()
        scenario.pulls[0]["state"] = "closed"
        self.assertRejected(
            scenario.verify(), "expected exactly one open target PR for head sha, found 0"
        )

    def test_draft_pr_matches_nothing(self) -> None:
        scenario = Scenario()
        scenario.pulls[0]["draft"] = True
        self.assertRejected(
            scenario.verify(), "expected exactly one open target PR for head sha, found 0"
        )

    def test_zero_matching_prs(self) -> None:
        scenario = Scenario()
        scenario.pulls = []
        self.assertRejected(
            scenario.verify(), "expected exactly one open target PR for head sha, found 0"
        )

    def test_more_than_one_matching_pr(self) -> None:
        scenario = Scenario()
        duplicate = copy.deepcopy(scenario.pulls[0])
        duplicate["number"] = 99
        scenario.pulls.append(duplicate)
        self.assertRejected(
            scenario.verify(), "expected exactly one open target PR for head sha, found 2"
        )

    def test_pr_list_entry_without_head_repository_is_skipped(self) -> None:
        scenario = Scenario()
        scenario.pulls = [
            {
                "number": 98,
                "state": "open",
                "draft": False,
                "head": {"sha": HEAD_SHA, "ref": scenario.head_branch, "repo": None},
                "base": {"ref": "main"},
            }
        ]
        self.assertRejected(
            scenario.verify(), "expected exactly one open target PR for head sha, found 0"
        )

    def test_pr_list_non_object_entry(self) -> None:
        scenario = Scenario()
        scenario.pulls = ["not-a-pr"]
        self.assertRejected(
            scenario.verify(), "pull request list contains a non-object entry"
        )

    def test_pr_head_ref_mismatch_rejected(self) -> None:
        scenario = Scenario()
        scenario.pr["head"]["ref"] = "some/other-branch"
        self.assertRejected(
            scenario.verify(), "pull request head branch changed"
        )

    def test_pr_body_not_a_string_rejected(self) -> None:
        scenario = Scenario()
        scenario.pr["body"] = None
        self.assertRejected(scenario.verify(), "pull request body is missing")

    def test_pr_label_not_an_object_rejected(self) -> None:
        scenario = Scenario()
        scenario.pr["labels"] = ["security"]
        self.assertRejected(
            scenario.verify(), "pull request label is missing or not an object"
        )

    def test_pr_labels_not_a_list_rejected(self) -> None:
        scenario = Scenario()
        scenario.pr["labels"] = {"name": "x"}
        self.assertRejected(
            scenario.verify(), "pull request labels is missing or not a list"
        )

    def test_pr_becomes_closed_during_verification(self) -> None:
        scenario = Scenario()
        scenario.final_pr["state"] = "closed"
        self.assertRejected(scenario.verify(), "pull request is no longer open")

    def test_pr_becomes_draft_during_verification(self) -> None:
        scenario = Scenario()
        scenario.final_pr["draft"] = True
        self.assertRejected(scenario.verify(), "pull request became a draft")

    def test_head_sha_changes_during_verification(self) -> None:
        scenario = Scenario()
        scenario.final_pr["head"]["sha"] = OTHER_SHA
        self.assertRejected(
            scenario.verify(), "pull request head changed during verification"
        )

    def test_head_branch_changes_during_verification(self) -> None:
        scenario = Scenario()
        scenario.final_pr["head"]["ref"] = "some/other-branch"
        self.assertRejected(
            scenario.verify(), "pull request head branch changed"
        )

    def test_base_ref_changes_during_verification(self) -> None:
        scenario = Scenario()
        scenario.final_pr["base"]["ref"] = "develop"
        self.assertRejected(scenario.verify(), "pull request base changed")

    def test_head_repository_changes_during_verification(self) -> None:
        scenario = Scenario()
        scenario.final_pr["head"]["repo"]["full_name"] = "other/repo"
        self.assertRejected(
            scenario.verify(), "pull request head repository changed"
        )

    def test_real_github_list_shape_without_commits_uses_detail_endpoint(self) -> None:
        scenario = Scenario()
        self.assertNotIn("commits", scenario.pulls[0])
        self.assertEqual(scenario.pr["commits"], 1)
        decision = scenario.verify()
        self.assertEligible(decision, pr_number=42, head_sha=HEAD_SHA)
        detail_path = f"/repos/{REPO}/pulls/42"
        self.assertEqual(scenario.github.calls.count(detail_path), 2)

    def test_detail_endpoint_number_mismatch_rejected(self) -> None:
        scenario = Scenario()
        self.assertEqual(scenario.pulls[0]["number"], 42)
        scenario.pr["number"] = 43
        self.assertRejected(
            scenario.verify(), "pull request number mismatch"
        )


# ---------------------------------------------------------------------------
# GitHub API transport
# ---------------------------------------------------------------------------


class GitHubApiTests(unittest.TestCase):
    def test_authenticated_redirects_stay_on_same_https_origin(self) -> None:
        opener = av._GITHUB_OPENER
        handler = next(h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler))
        for target, allowed in (
            ("https://API.EXAMPLE:443/moved", True),
            ("https://other.example/moved", False),
            ("https://api.example:8443/moved", False),
            ("http://api.example/moved", False),
        ):
            with self.subTest(target=target), mock.patch.object(opener, "open") as dispatch:
                request = urllib.request.Request(
                    "https://api.example/start", headers={"Authorization": "Bearer dummy"}
                )
                request.timeout = 1
                args = (request, io.BytesIO(), 302, "Found", {"location": target})
                if allowed:
                    handler.http_error_302(*args)
                    dispatch.assert_called_once()
                    redirected = dispatch.call_args.args[0]
                    self.assertEqual(redirected.full_url, target)
                    self.assertEqual(redirected.get_header("Authorization"), "Bearer dummy")
                else:
                    with self.assertRaises(av.Ineligible):
                        handler.http_error_302(*args)
                    dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Required workflows and checks
# ---------------------------------------------------------------------------


class RequiredWorkflowTests(AutomergeTestCase):
    def test_missing_required_workflow(self) -> None:
        scenario = Scenario()
        scenario.workflow_runs = [
            run for run in scenario.workflow_runs if run["name"] != "CodeQL"
        ]
        self.assertRejected(
            scenario.verify(), "required workflow CodeQL has no pull_request run"
        )

    def test_required_workflow_incomplete(self) -> None:
        scenario = Scenario()
        run = scenario.workflow_run("CodeQL")
        run["status"] = "in_progress"
        run["conclusion"] = None
        self.assertRejected(
            scenario.verify(), "workflow CodeQL is not completed"
        )

    def test_required_workflow_failed(self) -> None:
        scenario = Scenario()
        scenario.workflow_run("Pull Request")["conclusion"] = "failure"
        self.assertRejected(
            scenario.verify(), "required workflow Pull Request concluded failure"
        )

    def test_newer_failed_attempt_wins(self) -> None:
        scenario = Scenario()
        scenario.workflow_runs.append(
            make_workflow_run(
                "Pull Request",
                EXPECTED_REQUIRED_WORKFLOWS["Pull Request"],
                scenario.head_sha,
                scenario.head_branch,
                id=scenario.workflow_run("Pull Request")["id"],
                attempt=2,
                conclusion="failure",
            )
        )
        self.assertRejected(
            scenario.verify(), "required workflow Pull Request concluded failure"
        )

    def test_newer_run_takes_precedence_over_older_rerun(self) -> None:
        for older_status, status, conclusion, reason in (
            ("completed", "completed", "failure", "concluded failure"),
            ("completed", "queued", None, "is not completed"),
            ("queued", "completed", "success", None),
        ):
            with self.subTest(older_status=older_status, status=status):
                scenario = Scenario()
                older = scenario.workflow_run("Pull Request")
                older.update(run_attempt=3, status=older_status,
                             conclusion="success" if older_status == "completed" else None)
                scenario.workflow_runs.append({
                    **older, "id": older["id"] + 10, "run_attempt": 1,
                    "status": status, "conclusion": conclusion,
                })
                decision = scenario.verify()
                if reason is None:
                    self.assertEligible(decision, pr_number=42, head_sha=HEAD_SHA)
                else:
                    self.assertRejected(decision, f"workflow Pull Request {reason}")

    def test_ambiguous_latest_attempt(self) -> None:
        scenario = Scenario()
        duplicate = copy.deepcopy(scenario.workflow_run("Pull Request"))
        scenario.workflow_runs.append(duplicate)
        self.assertRejected(
            scenario.verify(),
            "required workflow Pull Request has an ambiguous latest run",
        )

    def test_workflow_run_for_merge_preview_sha(self) -> None:
        scenario = Scenario()
        scenario.workflow_runs.append(
            make_workflow_run(
                "Pull Request",
                EXPECTED_REQUIRED_WORKFLOWS["Pull Request"],
                MERGE_PREVIEW_SHA,
                scenario.head_branch,
                id=1005,
            )
        )
        self.assertRejected(
            scenario.verify(), "workflow run is for another sha"
        )

    def test_workflow_run_for_other_repository(self) -> None:
        scenario = Scenario()
        scenario.workflow_run("CodeQL")["head_repository"]["full_name"] = "other/repo"
        self.assertRejected(
            scenario.verify(), "workflow run is not for this repository"
        )

    def test_workflow_run_with_unexpected_path(self) -> None:
        scenario = Scenario()
        scenario.workflow_run("CodeQL")["path"] = ".github/workflows/other.yml"
        self.assertRejected(scenario.verify(), "has an unexpected path")

    def test_workflow_run_for_other_branch_is_ignored(self) -> None:
        scenario = Scenario()
        scenario.workflow_runs.append(
            make_workflow_run(
                "Pull Request",
                EXPECTED_REQUIRED_WORKFLOWS["Pull Request"],
                scenario.head_sha,
                "some/other-branch",
                id=1006,
                conclusion="failure",
            )
        )
        decision = scenario.verify()
        self.assertEligible(
            decision, pr_number=42, head_sha=HEAD_SHA
        )

    def test_workflow_run_missing_run_attempt(self) -> None:
        scenario = Scenario()
        del scenario.workflow_run("Pull Request")["run_attempt"]
        self.assertRejected(
            scenario.verify(), "run_attempt is missing or invalid"
        )

    def test_workflow_run_non_object_entry(self) -> None:
        scenario = Scenario()
        scenario.workflow_runs.append("not-a-run")
        self.assertRejected(
            scenario.verify(), "workflow run is missing or not an object"
        )


class RequiredCheckTests(AutomergeTestCase):
    def test_missing_required_check(self) -> None:
        scenario = Scenario()
        scenario.check_runs = [
            run for run in scenario.check_runs if run["name"] != "CI / lint"
        ]
        self.assertRejected(
            scenario.verify(),
            "required check CI / lint is missing (expected github-actions)",
        )

    def test_required_check_still_running(self) -> None:
        scenario = Scenario()
        run = scenario.check_run("CI / test (ubuntu-latest)")
        run["status"] = "in_progress"
        run["conclusion"] = None
        self.assertRejected(
            scenario.verify(),
            "required check CI / test (ubuntu-latest) is still running",
        )

    def test_required_check_failed(self) -> None:
        scenario = Scenario()
        scenario.check_run("CI / vuln")["conclusion"] = "failure"
        self.assertRejected(
            scenario.verify(), "required check CI / vuln concluded failure"
        )

    def test_required_check_wrong_app(self) -> None:
        scenario = Scenario()
        scenario.check_run("CodeQL")["app"]["slug"] = "github-actions"
        self.assertRejected(
            scenario.verify(),
            "required check CodeQL is missing (expected github-advanced-security)",
        )

    def test_missing_analyze_go_check_rejected(self) -> None:
        scenario = Scenario()
        scenario.check_runs = [
            run for run in scenario.check_runs if run["name"] != "Analyze (go)"
        ]
        self.assertRejected(
            scenario.verify(),
            "required check Analyze (go) is missing (expected github-actions)",
        )

    def test_analyze_go_check_wrong_app_rejected(self) -> None:
        scenario = Scenario()
        scenario.check_run("Analyze (go)")["app"]["slug"] = "github-advanced-security"
        self.assertRejected(
            scenario.verify(),
            "required check Analyze (go) is missing (expected github-actions)",
        )

    def test_stale_success_outranked_by_queued_rerun(self) -> None:
        scenario = Scenario()
        scenario.check_runs.append(
            make_check_run(
                "CI / lint",
                "github-actions",
                scenario.head_sha,
                id=99,
                status="queued",
                conclusion=None,
                started_at=None,
            )
        )
        self.assertRejected(
            scenario.verify(), "required check CI / lint is still running"
        )

    def test_ambiguous_latest_check_run(self) -> None:
        scenario = Scenario()
        scenario.check_runs.append(copy.deepcopy(scenario.check_run("CI / lint")))
        self.assertRejected(
            scenario.verify(), "required check CI / lint has an ambiguous latest run"
        )

    def test_check_run_for_merge_preview_sha(self) -> None:
        scenario = Scenario()
        scenario.check_runs.append(
            make_check_run(
                "CI / lint", "github-actions", MERGE_PREVIEW_SHA, id=100
            )
        )
        self.assertRejected(scenario.verify(), "check run is for another sha")

    def test_check_run_missing_app(self) -> None:
        scenario = Scenario()
        del scenario.check_run("CI / lint")["app"]
        self.assertRejected(
            scenario.verify(), "check run CI / lint app is missing or not an object"
        )

    def test_check_run_invalid_started_at(self) -> None:
        scenario = Scenario()
        scenario.check_run("CI / lint")["started_at"] = 12345
        self.assertRejected(
            scenario.verify(), "check run CI / lint has an invalid started_at"
        )

    def test_incomplete_check_pagination_fails_closed(self) -> None:
        scenario = Scenario()
        scenario.check_runs_total = len(scenario.check_runs) + 1
        self.assertRejected(scenario.verify(), "pagination is incomplete")


# ---------------------------------------------------------------------------
# Identity and commit chain
# ---------------------------------------------------------------------------


class IdentityAndCommitTests(AutomergeTestCase):
    def test_disallowed_human_author(self) -> None:
        scenario = Scenario()
        scenario.pr["user"]["login"] = "some-human"
        scenario.commits[0]["author"]["login"] = "some-human"
        self.assertRejected(scenario.verify(), "author some-human is not allowed")

    def test_github_actions_bot_is_not_allowed(self) -> None:
        scenario = Scenario()
        scenario.pr["user"]["login"] = "github-actions[bot]"
        scenario.commits[0]["author"]["login"] = "github-actions[bot]"
        self.assertRejected(
            scenario.verify(), "author github-actions[bot] is not allowed"
        )

    def test_commit_count_mismatch(self) -> None:
        scenario = Scenario()
        scenario.commits.append(
            make_commit(MIDDLE_COMMIT_SHA, HEAD_SHA, DEPENDABOT)
        )
        self.assertRejected(
            scenario.verify(), "PR commit count mismatch (2 of 1)"
        )

    def test_unverified_signature(self) -> None:
        scenario = Scenario()
        scenario.commits[0]["commit"]["verification"]["verified"] = False
        self.assertRejected(scenario.verify(), "signature is not verified")

    def test_missing_verification_object(self) -> None:
        scenario = Scenario()
        del scenario.commits[0]["commit"]["verification"]
        self.assertRejected(
            scenario.verify(), "verification is missing or not an object"
        )

    def test_wrong_commit_author(self) -> None:
        scenario = Scenario()
        scenario.commits[0]["author"]["login"] = "mallory"
        self.assertRejected(
            scenario.verify(), "author is not dependabot[bot]"
        )

    def test_committer_not_web_flow_or_bot(self) -> None:
        scenario = Scenario()
        scenario.commits[0]["committer"]["login"] = "mallory"
        self.assertRejected(
            scenario.verify(), "committer is not web-flow or dependabot[bot]"
        )

    def test_merge_commit_rejected(self) -> None:
        scenario = Scenario()
        scenario.commits[0]["parents"] = [
            {"sha": MAIN_SHA},
            {"sha": OTHER_SHA},
        ]
        self.assertRejected(scenario.verify(), "is a merge or root commit")

    def test_root_commit_rejected(self) -> None:
        scenario = Scenario()
        scenario.commits[0]["parents"] = []
        self.assertRejected(scenario.verify(), "is a merge or root commit")

    def test_non_linear_commit_chain(self) -> None:
        scenario = Scenario()
        scenario.pr["commits"] = 2
        scenario.commits = [
            make_commit(MIDDLE_COMMIT_SHA, MAIN_SHA, DEPENDABOT),
            make_commit(HEAD_SHA, OTHER_SHA, DEPENDABOT),
        ]
        self.assertRejected(
            scenario.verify(), f"does not follow {MIDDLE_COMMIT_SHA}"
        )

    def test_last_commit_is_not_head_sha(self) -> None:
        scenario = Scenario()
        scenario.commits = [make_commit(MIDDLE_COMMIT_SHA, MAIN_SHA, DEPENDABOT)]
        self.assertRejected(
            scenario.verify(), "last commit is not the workflow_run head sha"
        )

    def test_duplicate_commit_sha(self) -> None:
        scenario = Scenario()
        scenario.pr["commits"] = 2
        scenario.commits = [
            make_commit(HEAD_SHA, MAIN_SHA, DEPENDABOT),
            make_commit(HEAD_SHA, HEAD_SHA, DEPENDABOT),
        ]
        self.assertRejected(
            scenario.verify(), f"duplicate commit {HEAD_SHA}"
        )

    def test_invalid_commit_sha(self) -> None:
        scenario = Scenario()
        scenario.commits[0]["sha"] = "short"
        self.assertRejected(scenario.verify(), "commit #0 sha is invalid")

    def test_commit_chain_not_starting_at_merge_base(self) -> None:
        scenario = Scenario()
        scenario.commits[0]["parents"] = [{"sha": OTHER_SHA}]
        self.assertRejected(
            scenario.verify(), "commit chain does not start at the merge base"
        )


# ---------------------------------------------------------------------------
# Sensitive paths
# ---------------------------------------------------------------------------


class SensitivePathTests(AutomergeTestCase):
    def test_workflow_file_is_hard_blocked(self) -> None:
        scenario = Scenario()
        scenario.set_head_file(
            ".github/workflows/pull-request.yml",
            "name: Pull Request\non: pull_request\njobs: {}\n# changed\n",
        )
        self.assertRejected(
            scenario.verify(),
            "sensitive path changed: .github/workflows/pull-request.yml",
        )

    def test_composite_action_is_hard_blocked(self) -> None:
        scenario = Scenario()
        scenario.set_head_file(
            ".github/actions/synthetic/action.yml",
            "name: synthetic\nruns:\n  using: composite\n  steps: []\n# changed\n",
        )
        self.assertRejected(
            scenario.verify(),
            "sensitive path changed: .github/actions/synthetic/action.yml",
        )

    def test_automation_script_is_hard_blocked(self) -> None:
        scenario = Scenario()
        scenario.set_head_file(
            ".github/scripts/cooldown.py", "# synthetic script placeholder\n# changed\n"
        )
        self.assertRejected(
            scenario.verify(),
            "sensitive path changed: .github/scripts/cooldown.py",
        )

    def test_codeowners_is_hard_blocked(self) -> None:
        scenario = Scenario()
        scenario.set_head_file(".github/CODEOWNERS", "* @someone-else\n")
        self.assertRejected(
            scenario.verify(), "sensitive file changed: .github/CODEOWNERS"
        )

    def test_dependabot_config_is_hard_blocked(self) -> None:
        scenario = Scenario()
        scenario.set_head_file(
            ".github/dependabot.yml", "version: 2\nupdates: [{package-ecosystem: pip}]\n"
        )
        self.assertRejected(
            scenario.verify(), "sensitive file changed: .github/dependabot.yml"
        )


# ---------------------------------------------------------------------------
# Normal-update calibration and security updates
# ---------------------------------------------------------------------------


class DependabotSignalTests(AutomergeTestCase):
    def test_default_calibration_rejects_every_dependabot_pr(self) -> None:
        scenario = Scenario()
        # Do not pass calibrated_signals at all: the module default (empty) is
        # the behaviour under test.
        decision = scenario.verify_with_module_defaults()
        self.assertRejected(
            decision, "body cannot distinguish version and security updates"
        )
        self.assertIn("manual review required", decision.reason)
        # The same scenario becomes eligible only once the signal is added.
        self.assertEligible(
            scenario.verify(signals=CALIBRATED),
            pr_number=42,
            head_sha=HEAD_SHA,
        )

    def test_explicitly_calibrated_signal_enables_the_update(self) -> None:
        scenario = Scenario()
        decision = scenario.verify(signals=CALIBRATED)
        self.assertEligible(decision, pr_number=42, head_sha=HEAD_SHA)

    def test_unrelated_signal_does_not_enable_the_update(self) -> None:
        scenario = Scenario()
        self.assertRejected(
            scenario.verify(signals={"some-other-signal"}),
            "body cannot distinguish version and security updates",
        )

    def test_security_label_rejected_even_when_calibrated(self) -> None:
        scenario = Scenario()
        # Only the labels differ from the ordinary synthetic update; the body
        # stays free of security markers so this isolates the label signal.
        security = fixture("dependabot_security_pr.json")
        scenario.pr["labels"] = security["labels"]
        self.assertRejected(
            scenario.verify(signals=CALIBRATED), "PR carries a security label"
        )

    def test_security_body_markers_rejected_even_when_calibrated(self) -> None:
        normal_body = fixture("dependabot_normal_pr.json")["body"]
        for marker in (
            "This update includes a security fix.",
            "CVE-2026-00001",
            "GHSA-0000-0000-0000",
            "https://github.com/advisories/GHSA-0000-0000-0000",
            "severity: high",
            "a vulnerability was found",
        ):
            with self.subTest(marker=marker):
                scenario = Scenario()
                scenario.pr["body"] = normal_body + f"\n{marker}\n"
                self.assertRejected(
                    scenario.verify(signals=CALIBRATED),
                    "security-update metadata",
                )

    def test_unrecognized_body_shape_requires_manual_review(self) -> None:
        scenario = Scenario()
        scenario.pr["body"] = "Please merge this synthetic update.\n"
        self.assertRejected(
            scenario.verify(signals=CALIBRATED), "no positive signal"
        )

    def test_missing_body_rejected(self) -> None:
        scenario = Scenario()
        del scenario.pr["body"]
        self.assertRejected(
            scenario.verify(signals=CALIBRATED), "pull request body is missing"
        )

    def test_github_actions_updates_are_never_eligible(self) -> None:
        scenario = Scenario()
        scenario.set_head_branch("dependabot/github_actions/actions/checkout-5")
        self.assertRejected(
            scenario.verify(signals=CALIBRATED),
            "github_actions updates are never auto-merged",
        )

    def test_unknown_ecosystem_is_not_auto_mergeable(self) -> None:
        scenario = Scenario()
        scenario.set_head_branch("dependabot/npm_and_yarn/lodash-4.17.21")
        self.assertRejected(
            scenario.verify(signals=CALIBRATED),
            "ecosystem npm_and_yarn is not auto-mergeable",
        )

    def test_malformed_dependabot_branch(self) -> None:
        scenario = Scenario()
        scenario.set_head_branch("dependabot/go_modules")
        self.assertRejected(
            scenario.verify(signals=CALIBRATED),
            "Dependabot branch name is malformed",
        )


# ---------------------------------------------------------------------------
# go_modules
# ---------------------------------------------------------------------------


class GoModulesTests(AutomergeTestCase):
    def test_positive_go_modules_update_is_eligible(self) -> None:
        scenario = Scenario("go_modules")
        decision = scenario.verify()
        self.assertEligible(decision, pr_number=42, head_sha=HEAD_SHA)
        self.assertEqual(scenario.http.unexpected, [])
        self.assertIn(
            f"/repos/{REPO}/pulls/42",
            [call.split("?")[0] for call in scenario.github.calls],
        )

    def test_file_outside_whitelist(self) -> None:
        scenario = Scenario()
        scenario.set_head_file("README.md", "# synthetic repository\n# changed\n")
        self.assertRejected(
            scenario.verify(), "go_modules update changes README.md"
        )

    def test_go_sum_must_change(self) -> None:
        scenario = Scenario()
        scenario.set_head_file("go.sum", GO_SUM_BASE)
        self.assertRejected(
            scenario.verify(), "go_modules update does not change go.sum"
        )

    def test_both_module_roots(self) -> None:
        scenario = Scenario()
        scenario.set_base_file("tools/go.sum", "example.com/tool v1.0.0 h1:AA==\n")
        scenario.set_head_file("tools/go.sum", "example.com/tool v1.0.1 h1:BB==\n")
        self.assertRejected(
            scenario.verify(), "go_modules update touches both module roots"
        )

    def test_file_mode_change(self) -> None:
        scenario = Scenario()
        scenario.head_modes["go.mod"] = "100755"
        scenario.rebuild()
        self.assertRejected(scenario.verify(), "changes its file mode")

    def test_directive_changes_are_rejected(self) -> None:
        cases = {
            "go": go_mod_text(go_version="1.27"),
            "toolchain": go_mod_text(toolchain="go1.26.11"),
            "module": go_mod_text(module="example.com/project2"),
            "replace": go_mod_text(extra=("replace example.com/old => example.com/new v1.0.1",)),
            "exclude": go_mod_text(extra=("exclude example.com/bad v1.0.1",)),
            "retract": go_mod_text(extra=("retract v1.0.1",)),
            "tool": go_mod_text(extra=("tool example.com/tool2",)),
            "godebug": go_mod_text(extra=("godebug example=2",)),
        }
        base_variants = {
            "go": go_mod_text(),
            "toolchain": go_mod_text(toolchain="go1.26.10"),
            "module": go_mod_text(),
            "replace": go_mod_text(extra=("replace example.com/old => example.com/new v1.0.0",)),
            "exclude": go_mod_text(extra=("exclude example.com/bad v1.0.0",)),
            "retract": go_mod_text(extra=("retract v1.0.0",)),
            "tool": go_mod_text(extra=("tool example.com/tool",)),
            "godebug": go_mod_text(extra=("godebug example=1",)),
        }
        for name, head_text in cases.items():
            with self.subTest(directive=name):
                scenario = Scenario()
                scenario.set_base_file("go.mod", base_variants[name])
                scenario.set_head_file("go.mod", head_text)
                self.assertRejected(
                    scenario.verify(),
                    "go.mod changes a directive other than require",
                )

    def test_major_upgrade_rejected(self) -> None:
        scenario = Scenario()
        scenario.set_head_file(
            "go.mod", go_mod_text(requires=(("example.com/mod", "v2.0.0"),))
        )
        self.assertRejected(scenario.verify(), "crosses a major version")

    def test_vn_path_mismatch_rejected(self) -> None:
        scenario = Scenario()
        base = go_mod_text(requires=(("example.com/mod/v2", "v1.2.3"),))
        head = go_mod_text(requires=(("example.com/mod/v2", "v1.2.4"),))
        scenario.set_base_file("go.mod", base)
        scenario.set_head_file("go.mod", head)
        self.assertRejected(
            scenario.verify(), "version does not match its /vN path"
        )

    def test_major_version_without_vn_path_rejected(self) -> None:
        scenario = Scenario()
        scenario.set_base_file(
            "go.mod", go_mod_text(requires=(("example.com/mod", "v2.0.0"),))
        )
        scenario.set_head_file(
            "go.mod", go_mod_text(requires=(("example.com/mod", "v2.0.1"),))
        )
        self.assertRejected(scenario.verify(), "needs a /vN path")

    def test_vn_path_rejects_incompatible_suffix(self) -> None:
        scenario = Scenario()
        scenario.set_base_file(
            "go.mod",
            go_mod_text(requires=(("example.com/mod/v2", "v2.0.0+incompatible"),)),
        )
        scenario.set_head_file(
            "go.mod",
            go_mod_text(requires=(("example.com/mod/v2", "v2.0.1+incompatible"),)),
        )
        self.assertRejected(
            scenario.verify(), "uses +incompatible with a /vN path"
        )

    def test_downgrade_rejected(self) -> None:
        scenario = Scenario()
        scenario.set_base_file(
            "go.mod", go_mod_text(requires=(("example.com/mod", "v1.2.4"),))
        )
        scenario.set_head_file(
            "go.mod", go_mod_text(requires=(("example.com/mod", "v1.2.3"),))
        )
        self.assertRejected(scenario.verify(), "is not an upgrade")

    def test_build_metadata_only_change_rejected(self) -> None:
        scenario = Scenario()
        scenario.set_head_file(
            "go.mod", go_mod_text(requires=(("example.com/mod", "v1.2.3+build.1"),))
        )
        self.assertRejected(
            scenario.verify(), "changes only build metadata"
        )

    def test_go_mod_without_require_change_rejected(self) -> None:
        scenario = Scenario()
        scenario.set_head_file("go.mod", GO_MOD_BASE + "\n// synthetic comment\n")
        self.assertRejected(scenario.verify(), "go.mod has no require changes")

    def test_removed_module_rejected(self) -> None:
        scenario = Scenario()
        scenario.set_head_file("go.mod", go_mod_text(requires=()))
        self.assertRejected(
            scenario.verify(), "go.mod removes required module example.com/mod"
        )

    def test_indirect_removals_with_upgrade_are_eligible(self) -> None:
        scenario = Scenario()
        scenario.set_base_file(
            "go.mod",
            go_mod_text(
                requires=(
                    ("example.com/mod", "v1.2.3"),
                    ("example.com/old", "v1.0.0 // indirect"),
                )
            )
            + "\nrequire example.com/another v1.0.0 // indirect; explanation\n",
        )
        self.assertEligible(scenario.verify(), pr_number=42, head_sha=HEAD_SHA)

        scenario.check_run("CI / mod-check")["conclusion"] = "failure"
        self.assertRejected(
            scenario.verify(), "required check CI / mod-check concluded failure"
        )

    def test_indirect_removal_requires_an_existing_module_upgrade(self) -> None:
        for added in ((), (("example.com/newdep", "v0.1.0"),)):
            with self.subTest(added=added):
                scenario = Scenario()
                scenario.set_base_file(
                    "go.mod",
                    GO_MOD_BASE + "\nrequire example.com/old v1.0.0 // indirect\n",
                )
                scenario.set_head_file(
                    "go.mod", go_mod_text(requires=(("example.com/mod", "v1.2.3"), *added))
                )
                self.assertRejected(
                    scenario.verify(), "go.mod removes indirect modules without an allowed upgrade"
                )

    def test_removal_with_upgrade_requires_indirect_marker_in_base(self) -> None:
        for comment in ("", " // indirectly", " // explanation indirect"):
            with self.subTest(comment=comment):
                scenario = Scenario()
                scenario.set_base_file(
                    "go.mod", GO_MOD_BASE + f"\nrequire example.com/old v1.0.0{comment}\n"
                )
                self.assertRejected(
                    scenario.verify(), "go.mod removes required module example.com/old"
                )

    def test_missing_trusted_first_seen_record(self) -> None:
        scenario = Scenario()
        scenario.http_map[go_index_url(GO_HINT)] = ndjson(
            [
                {
                    "Path": "example.com/other",
                    "Version": "v9.9.9",
                    "Timestamp": "2026-10-01T00:00:00Z",
                }
            ]
        )
        self.assertRejected(
            scenario.verify(), "no trusted first-seen evidence for example.com/mod@v1.2.4"
        )

    def test_upgrade_that_is_not_aged(self) -> None:
        scenario = Scenario()
        scenario.http_map[go_index_url(GO_HINT)] = ndjson(
            [
                {
                    "Path": "example.com/mod",
                    "Version": "v1.2.4",
                    "Timestamp": "2026-11-24T00:00:00Z",
                }
            ]
        )
        self.assertRejected(
            scenario.verify(), "has not been public for 3 days"
        )

    def test_new_module_without_cooldown(self) -> None:
        scenario = Scenario()
        head = go_mod_text(
            requires=(
                ("example.com/mod", "v1.2.4"),
                ("example.com/newdep", "v0.1.0"),
            )
        )
        scenario.set_head_file("go.mod", head)
        scenario.add_go_evidence(
            "example.com/newdep",
            "v0.1.0",
            hint=NEWDEP_HINT,
            trusted="2026-11-24T00:00:00Z",
        )
        self.assertRejected(
            scenario.verify(), "example.com/newdep@v0.1.0 has not been public for 7 days"
        )

    def test_new_module_with_aged_evidence_is_eligible(self) -> None:
        scenario = Scenario()
        head = go_mod_text(
            requires=(
                ("example.com/mod", "v1.2.4"),
                ("example.com/newdep", "v0.1.0"),
            )
        )
        scenario.set_head_file("go.mod", head)
        scenario.add_go_evidence(
            "example.com/newdep",
            "v0.1.0",
            hint=NEWDEP_HINT,
            trusted=TRUSTED_OLD,
        )
        decision = scenario.verify()
        self.assertEligible(decision, pr_number=42, head_sha=HEAD_SHA)

    def test_malformed_go_mod_shapes_rejected(self) -> None:
        cases = {
            "duplicate-require": (
                "module example.com/project\n\ngo 1.26\n\nrequire (\n"
                "\texample.com/mod v1.2.3\n\texample.com/mod v1.2.4\n)\n",
                "duplicate require for example.com/mod",
            ),
            "unclosed-block": (
                "module example.com/project\n\ngo 1.26\n\nrequire (\n"
                "\texample.com/mod v1.2.4\n",
                "unclosed go.mod block",
            ),
            "malformed-require": (
                "module example.com/project\n\ngo 1.26\n\nrequire example.com/mod\n",
                "malformed require entry",
            ),
        }
        for name, (text, reason) in cases.items():
            with self.subTest(shape=name):
                scenario = Scenario()
                scenario.set_head_file("go.mod", text)
                self.assertRejected(scenario.verify(), reason)


GROUP = "go-minor-patch"
GROUP_SIGNAL = "dependabot-group-v1"
GROUP_BRANCH = f"dependabot/go_modules/{GROUP}-0123456789"
GROUP_CONFIG = (
    "version: 2\nupdates:\n"
    '  - package-ecosystem: "gomod"\n    directory: "/"\n'
    '    schedule:\n      interval: "weekly"\n'
    f'    groups:\n      {GROUP}:\n        update-types:\n          - "minor"\n'
)


def group_commit_message(entries) -> str:
    lines = [f"chore(deps): bump the {GROUP} group", "", "---", "updated-dependencies:"]
    for name, update_type, group in entries:
        lines += [
            f"- dependency-name: {name}",
            "  dependency-version: 1.2.4",
            "  dependency-type: direct:production",
            f"  update-type: {update_type}",
            f"  dependency-group: {group}",
        ]
    lines += ["...", "", "Signed-off-by: dependabot[bot] <support@github.com>", ""]
    return "\n".join(lines)


class GroupSignalTests(AutomergeTestCase):
    def group_scenario(
        self,
        *,
        message=None,
        config: str = GROUP_CONFIG,
        branch: str = GROUP_BRANCH,
    ) -> Scenario:
        scenario = Scenario()
        scenario.base_files[".github/dependabot.yml"] = config
        scenario.head_files[".github/dependabot.yml"] = config
        scenario.rebuild()
        scenario.set_head_branch(branch)
        if message is None:
            message = group_commit_message(
                [("example.com/mod", "version-update:semver-patch", GROUP)]
            )
        scenario.commits[0]["commit"]["message"] = message
        return scenario

    def test_signed_group_update_is_eligible_only_when_enabled(self) -> None:
        scenario = self.group_scenario()
        decision = scenario.verify(signals=frozenset({GROUP_SIGNAL}))
        self.assertEligible(decision, pr_number=42, head_sha=HEAD_SHA)
        self.assertRejected(
            scenario.verify(signals=CALIBRATED),
            "update-kind signal dependabot-group-v1 is not enabled",
        )

    def test_group_metadata_must_match_trusted_group_and_diff(self) -> None:
        patch = "version-update:semver-patch"
        # GitHub cuts very long messages off mid-entry, dropping the terminator.
        truncated = group_commit_message([("example.com/mod", patch, GROUP)]).replace(
            "\n...\n", "\n- dependency-na...\n"
        )
        cases = {
            "truncated": (truncated, "malformed updated-dependencies line"),
            "other-group": (
                group_commit_message([("example.com/mod", patch, "other")]),
                "example.com/mod is not in the go-minor-patch group",
            ),
            "major": (
                group_commit_message(
                    [("example.com/mod", "version-update:semver-major", GROUP)]
                ),
                "is not minor or patch",
            ),
            "not-upgraded": (
                group_commit_message(
                    [
                        ("example.com/mod", patch, GROUP),
                        ("example.com/untouched", patch, GROUP),
                    ]
                ),
                "example.com/untouched is listed but not upgraded in go.mod",
            ),
        }
        for name, (message, reason) in cases.items():
            with self.subTest(case=name):
                scenario = self.group_scenario(message=message)
                self.assertRejected(
                    scenario.verify(signals=frozenset({GROUP_SIGNAL})), reason
                )

    def test_group_branch_and_config_must_be_trusted(self) -> None:
        security_config = GROUP_CONFIG + '        applies-to: "security-updates"\n'
        cases = {
            "tools-branch": (
                {"branch": f"dependabot/go_modules/tools/{GROUP}-0123456789"},
                "is not the go-minor-patch group branch",
            ),
            "security-group-config": (
                {"config": security_config},
                "configures security-update groups",
            ),
            "missing-group": (
                {"config": "version: 2\nupdates: []\n"},
                "does not define group go-minor-patch",
            ),
        }
        for name, (kwargs, reason) in cases.items():
            with self.subTest(case=name):
                scenario = self.group_scenario(**kwargs)
                self.assertRejected(
                    scenario.verify(signals=frozenset({GROUP_SIGNAL})), reason
                )

    def test_group_update_must_be_a_single_commit(self) -> None:
        scenario = self.group_scenario()
        message = scenario.commits[0]["commit"]["message"]
        scenario.pr["commits"] = 2
        scenario.commits = [
            make_commit(MIDDLE_COMMIT_SHA, MAIN_SHA, DEPENDABOT),
            make_commit(HEAD_SHA, MIDDLE_COMMIT_SHA, DEPENDABOT),
        ]
        scenario.commits[-1]["commit"]["message"] = message
        self.assertRejected(
            scenario.verify(signals=frozenset({GROUP_SIGNAL})),
            "grouped update must be a single commit",
        )

    def test_security_label_still_vetoes_group_update(self) -> None:
        scenario = self.group_scenario()
        for pr in (scenario.pr, scenario.final_pr, *scenario.pulls):
            pr["labels"] = [{"name": "security"}]
        self.assertRejected(
            scenario.verify(signals=frozenset({GROUP_SIGNAL})),
            "PR carries a security label",
        )


# ---------------------------------------------------------------------------
# docker
# ---------------------------------------------------------------------------


class DockerTests(AutomergeTestCase):
    def test_positive_docker_update_is_eligible(self) -> None:
        scenario = Scenario("docker")
        decision = scenario.verify()
        self.assertEligible(decision, pr_number=41, head_sha=HEAD_SHA)
        self.assertEqual(scenario.http.unexpected, [])

    def test_tag_change_rejected(self) -> None:
        scenario = Scenario("docker")
        scenario.set_head_file(
            "dockerfiles/Dockerfile",
            dockerfile_text("1.26.11-alpine", DIGEST_B),
        )
        self.assertRejected(
            scenario.verify(), "changes more than the FROM digest"
        )

    def test_image_name_change_rejected(self) -> None:
        scenario = Scenario("docker")
        scenario.set_head_file(
            "dockerfiles/Dockerfile",
            f"FROM debian:1.26.10-alpine@sha256:{DIGEST_B}\nRUN echo synthetic\n",
        )
        self.assertRejected(scenario.verify(), "changes the image name")

    def test_multiline_change_rejected(self) -> None:
        scenario = Scenario("docker")
        scenario.set_head_file(
            "dockerfiles/Dockerfile",
            f"FROM golang:1.26.10-alpine@sha256:{DIGEST_B}\nRUN echo changed\n",
        )
        self.assertRejected(
            scenario.verify(), "must change exactly one line, found 2"
        )

    def test_line_count_change_rejected(self) -> None:
        scenario = Scenario("docker")
        scenario.set_head_file(
            "dockerfiles/Dockerfile",
            dockerfile_text("1.26.10-alpine", DIGEST_B) + "RUN echo extra\n",
        )
        self.assertRejected(scenario.verify(), "changes its line count")

    def test_unpinned_from_line_rejected(self) -> None:
        scenario = Scenario("docker")
        scenario.set_head_file(
            "dockerfiles/Dockerfile",
            "FROM golang:1.26.10-alpine\nRUN echo synthetic\n",
        )
        self.assertRejected(
            scenario.verify(), "not a pinned FROM line"
        )

    def test_non_docker_hub_registry_rejected(self) -> None:
        scenario = Scenario("docker")
        scenario.set_base_file(
            "dockerfiles/Dockerfile",
            f"FROM ghcr.io/example/image:1.0.0@sha256:{DIGEST_A}\nRUN echo synthetic\n",
        )
        scenario.set_head_file(
            "dockerfiles/Dockerfile",
            f"FROM ghcr.io/example/image:1.0.0@sha256:{DIGEST_B}\nRUN echo synthetic\n",
        )
        self.assertRejected(scenario.verify(), "is not on Docker Hub")

    def test_missing_tag_last_pushed_rejected(self) -> None:
        scenario = Scenario("docker")
        url = docker_tag_url("library/golang", "1.26.10-alpine")
        scenario.http_map[url] = jsonb(
            {"name": "1.26.10-alpine", "digest": f"sha256:{DIGEST_B}"}
        )
        decision = scenario.verify()
        self.assertRejected(decision, "no trusted Docker Hub evidence")
        self.assertIn("tag_last_pushed is missing", decision.reason)

    def test_tag_not_aged_rejected(self) -> None:
        scenario = Scenario("docker")
        scenario.add_docker_evidence(
            "library/golang", "1.26.10-alpine", DIGEST_B, "2026-11-24T00:00:00Z"
        )
        self.assertRejected(
            scenario.verify(),
            "Docker tag 1.26.10-alpine has not been public for 7 days",
        )

    def test_digest_mismatch_against_pr_rejected(self) -> None:
        scenario = Scenario("docker")
        scenario.add_docker_evidence(
            "library/golang", "1.26.10-alpine", DIGEST_C, TRUSTED_OLD
        )
        self.assertRejected(
            scenario.verify(), "does not match the PR"
        )

    def test_same_tag_with_different_digests_rejected(self) -> None:
        scenario = Scenario("docker")
        scenario.set_head_file(
            "dockerfiles/Dockerfile.self-contained",
            dockerfile_text("1.26.10-alpine", DIGEST_C),
        )
        self.assertRejected(
            scenario.verify(), "uses different digests for 1.26.10-alpine"
        )

    def test_non_dockerfile_path_rejected(self) -> None:
        scenario = Scenario("docker")
        scenario.set_head_file("README.md", "# synthetic repository\n# changed\n")
        self.assertRejected(
            scenario.verify(), "docker update changes README.md"
        )

    def test_added_file_rejected(self) -> None:
        scenario = Scenario("docker")
        scenario.set_head_file(
            "dockerfiles/Dockerfile.new",
            dockerfile_text("1.26.10-alpine", DIGEST_B),
        )
        self.assertRejected(scenario.verify(), "is added, not modified")


# ---------------------------------------------------------------------------
# pip
# ---------------------------------------------------------------------------


class PipTests(AutomergeTestCase):
    def test_positive_pip_update_is_eligible(self) -> None:
        scenario = Scenario("pip")
        decision = scenario.verify()
        self.assertEligible(decision, pr_number=43, head_sha=HEAD_SHA)
        self.assertEqual(scenario.http.unexpected, [])

    def test_missing_hash_rejected(self) -> None:
        scenario = Scenario("pip")
        scenario.set_head_file("docs-requirements.txt", "mkdocs==1.6.1\n")
        self.assertRejected(scenario.verify(), "requirement has no --hash")

    def test_index_url_rejected(self) -> None:
        scenario = Scenario("pip")
        scenario.set_head_file(
            "docs-requirements.txt",
            f"mkdocs==1.6.1 --hash=sha256:{HASH_B}\n"
            "--index-url https://example.com/simple\n",
        )
        self.assertRejected(
            scenario.verify(), "options such as --index-url are not allowed"
        )

    def test_extra_index_url_rejected(self) -> None:
        scenario = Scenario("pip")
        scenario.set_head_file(
            "docs-requirements.txt",
            f"mkdocs==1.6.1 --hash=sha256:{HASH_B}\n"
            "--extra-index-url https://example.com/simple\n",
        )
        self.assertRejected(
            scenario.verify(), "options such as --index-url are not allowed"
        )

    def test_git_url_rejected(self) -> None:
        scenario = Scenario("pip")
        scenario.set_head_file(
            "docs-requirements.txt",
            "mkdocs @ git+https://example.com/mkdocs.git\n",
        )
        self.assertRejected(
            scenario.verify(), "not an exact name==version pin"
        )

    def test_major_upgrade_rejected(self) -> None:
        scenario = Scenario("pip")
        scenario.set_head_file(
            "docs-requirements.txt", f"mkdocs==2.0.0 --hash=sha256:{HASH_B}\n"
        )
        self.assertRejected(scenario.verify(), "crosses a major version")

    def test_epoch_change_rejected(self) -> None:
        scenario = Scenario("pip")
        scenario.set_head_file(
            "docs-requirements.txt", f"mkdocs==1!1.6.1 --hash=sha256:{HASH_B}\n"
        )
        self.assertRejected(scenario.verify(), "changes epoch")

    def test_downgrade_rejected(self) -> None:
        scenario = Scenario("pip")
        scenario.set_base_file(
            "docs-requirements.txt", f"mkdocs==1.6.1 --hash=sha256:{HASH_A}\n"
        )
        scenario.set_head_file(
            "docs-requirements.txt", f"mkdocs==1.6.0 --hash=sha256:{HASH_B}\n"
        )
        self.assertRejected(scenario.verify(), "is not an upgrade")

    def test_missing_upload_time_rejected(self) -> None:
        scenario = Scenario("pip")
        scenario.add_pypi_evidence("mkdocs", "1.6.1", [(HASH_B, None)])
        decision = scenario.verify()
        self.assertRejected(decision, "no trusted PyPI evidence")
        self.assertIn("has no upload timestamp", decision.reason)

    def test_young_upload_rejected(self) -> None:
        scenario = Scenario("pip")
        scenario.add_pypi_evidence(
            "mkdocs", "1.6.1", [(HASH_B, "2026-11-24T00:00:00Z")]
        )
        self.assertRejected(
            scenario.verify(), "has a file uploaded less than 7 days ago"
        )

    def test_allowed_hash_without_timestamp_fails_closed(self) -> None:
        scenario = Scenario("pip")
        scenario.set_head_file(
            "docs-requirements.txt",
            f"mkdocs==1.6.1 --hash=sha256:{HASH_B} --hash=sha256:{HASH_C}\n",
        )
        scenario.add_pypi_evidence(
            "mkdocs", "1.6.1", [(HASH_B, TRUSTED_OLD), (HASH_C, None)]
        )
        decision = scenario.verify()
        self.assertRejected(decision, "no trusted PyPI evidence")
        self.assertIn(f"sha256:{HASH_C} has no upload timestamp", decision.reason)

    def test_allowed_hash_missing_from_pypi_fails_closed(self) -> None:
        scenario = Scenario("pip")
        scenario.set_head_file(
            "docs-requirements.txt",
            f"mkdocs==1.6.1 --hash=sha256:{HASH_B} --hash=sha256:{HASH_C}\n",
        )
        scenario.add_pypi_evidence("mkdocs", "1.6.1", [(HASH_B, TRUSTED_OLD)])
        decision = scenario.verify()
        self.assertRejected(decision, "no trusted PyPI evidence")
        self.assertIn(f"matching sha256:{HASH_C}", decision.reason)

    def test_hash_change_without_version_change_rejected(self) -> None:
        scenario = Scenario("pip")
        scenario.set_head_file(
            "docs-requirements.txt", f"mkdocs==1.6.0 --hash=sha256:{HASH_B}\n"
        )
        self.assertRejected(
            scenario.verify(), "changes hashes without a version change"
        )

    def test_removed_package_rejected(self) -> None:
        scenario = Scenario("pip")
        scenario.set_base_file(
            "docs-requirements.txt",
            f"mkdocs==1.6.0 --hash=sha256:{HASH_A}\n"
            f"jinja2==3.1.4 --hash=sha256:{HASH_C}\n",
        )
        self.assertRejected(scenario.verify(), "removes package jinja2")

    def test_duplicate_requirement_rejected(self) -> None:
        scenario = Scenario("pip")
        scenario.set_head_file(
            "docs-requirements.txt",
            f"mkdocs==1.6.1 --hash=sha256:{HASH_B}\n"
            f"mkdocs==1.6.1 --hash=sha256:{HASH_B}\n",
        )
        self.assertRejected(
            scenario.verify(), "duplicate requirement for mkdocs"
        )

    def test_wrong_file_rejected(self) -> None:
        scenario = Scenario("pip")
        # Keep the lockfile identical so the only changed path is README.md.
        scenario.set_head_file(
            "docs-requirements.txt", f"mkdocs==1.6.0 --hash=sha256:{HASH_A}\n"
        )
        scenario.set_head_file("README.md", "# synthetic repository\n# changed\n")
        self.assertRejected(
            scenario.verify(), "pip update changes README.md"
        )

    def test_multiple_changed_files_rejected(self) -> None:
        scenario = Scenario("pip")
        scenario.set_head_file("README.md", "# synthetic repository\n# changed\n")
        self.assertRejected(
            scenario.verify(), "pip update must change exactly one file"
        )


# ---------------------------------------------------------------------------
# Go toolchain sync (watchtower-updater[bot])
# ---------------------------------------------------------------------------


class ToolchainTests(AutomergeTestCase):
    def test_positive_same_minor_toolchain_update_is_eligible(self) -> None:
        scenario = Scenario("toolchain")
        decision = scenario.verify()
        self.assertEligible(decision, pr_number=44, head_sha=TOOLCHAIN_SHA)
        self.assertEqual(scenario.http.unexpected, [])

    def test_cross_minor_update_rejected(self) -> None:
        scenario = Scenario("toolchain")
        scenario.set_head_file(".go-version", "1.27.0\n")
        self.assertRejected(
            scenario.verify(), "crosses a major or minor version"
        )

    def test_patch_increment_not_exactly_plus_one(self) -> None:
        scenario = Scenario("toolchain")
        scenario.set_head_file(".go-version", "1.26.12\n")
        self.assertRejected(
            scenario.verify(), "patch increment is not exactly +1"
        )

    def test_patch_decrement_rejected(self) -> None:
        scenario = Scenario("toolchain")
        scenario.set_head_file(".go-version", "1.26.9\n")
        self.assertRejected(
            scenario.verify(), "patch increment is not exactly +1"
        )

    def test_go_mod_change_rejected(self) -> None:
        scenario = Scenario("toolchain")
        scenario.set_head_file(
            "go.mod", "module example.com/project\n\ngo 1.27\n"
        )
        self.assertRejected(
            scenario.verify(), ".go-version and exactly the three Dockerfiles"
        )

    def test_extra_changed_file_rejected(self) -> None:
        scenario = Scenario("toolchain")
        scenario.set_head_file("README.md", "# synthetic repository\n# changed\n")
        self.assertRejected(
            scenario.verify(), ".go-version and exactly the three Dockerfiles"
        )

    def test_too_few_changed_files_rejected(self) -> None:
        scenario = Scenario("toolchain")
        for path in GOLANG_DOCKERFILES:
            scenario.set_head_file(path, dockerfile_text("1.26.10-alpine", DIGEST_A))
        self.assertRejected(
            scenario.verify(), ".go-version and exactly the three Dockerfiles"
        )

    def test_old_dockerfile_tag_not_synchronized(self) -> None:
        scenario = Scenario("toolchain")
        for path in GOLANG_DOCKERFILES:
            scenario.set_base_file(path, dockerfile_text("1.26.9-alpine", DIGEST_A))
        self.assertRejected(
            scenario.verify(), "old tag does not match .go-version"
        )

    def test_new_dockerfile_tag_not_synchronized(self) -> None:
        scenario = Scenario("toolchain")
        for path in GOLANG_DOCKERFILES:
            scenario.set_head_file(path, dockerfile_text("1.26.12-alpine", DIGEST_B))
        self.assertRejected(
            scenario.verify(), "new tag does not match .go-version"
        )

    def test_non_official_golang_image_rejected(self) -> None:
        scenario = Scenario("toolchain")
        for path in GOLANG_DOCKERFILES:
            scenario.set_base_file(
                path, f"FROM golangci/golang:1.26.10-alpine@sha256:{DIGEST_A}\nRUN echo synthetic\n"
            )
            scenario.set_head_file(
                path, f"FROM golangci/golang:1.26.11-alpine@sha256:{DIGEST_B}\nRUN echo synthetic\n"
            )
        self.assertRejected(
            scenario.verify(), "does not use the official golang image"
        )

    def test_dockerfiles_must_share_one_digest(self) -> None:
        scenario = Scenario("toolchain")
        scenario.set_head_file(
            "dockerfiles/Dockerfile.self-contained",
            dockerfile_text("1.26.11-alpine", DIGEST_C),
        )
        self.assertRejected(
            scenario.verify(), "do not use the same golang digest"
        )

    def test_unpinned_toolchain_dockerfile_rejected(self) -> None:
        scenario = Scenario("toolchain")
        for path in GOLANG_DOCKERFILES:
            scenario.set_head_file(
                path, "FROM golang:1.26.11-alpine\nRUN echo synthetic\n"
            )
        self.assertRejected(
            scenario.verify(), "not a pinned FROM line"
        )

    def test_multiline_toolchain_dockerfile_change_rejected(self) -> None:
        scenario = Scenario("toolchain")
        for path in GOLANG_DOCKERFILES:
            scenario.set_head_file(
                path,
                f"FROM golang:1.26.11-alpine@sha256:{DIGEST_B}\nRUN echo changed\n",
            )
        self.assertRejected(
            scenario.verify(), "must change exactly one line, found 2"
        )

    def test_malformed_go_version_rejected(self) -> None:
        scenario = Scenario("toolchain")
        scenario.set_head_file(".go-version", "v1.26.11\n")
        self.assertRejected(
            scenario.verify(), "not an exact MAJOR.MINOR.PATCH version"
        )

    def test_more_than_one_commit_rejected(self) -> None:
        scenario = Scenario("toolchain")
        scenario.pr["commits"] = 2
        scenario.commits = [
            make_commit(MIDDLE_COMMIT_SHA, MAIN_SHA, UPDATER),
            make_commit(TOOLCHAIN_SHA, MIDDLE_COMMIT_SHA, UPDATER),
        ]
        self.assertRejected(
            scenario.verify(), "toolchain update must be a single commit"
        )

    def test_branch_not_based_on_current_main(self) -> None:
        scenario = Scenario("toolchain")
        scenario.set_merge_base(OTHER_SHA)
        self.assertRejected(
            scenario.verify(), "toolchain branch is not based on the current main tip"
        )

    def test_toolchain_not_aged(self) -> None:
        scenario = Scenario("toolchain")
        scenario.http_map[go_index_url(TOOLCHAIN_HINT)] = ndjson(
            [
                {
                    "Path": "golang.org/toolchain",
                    "Version": TOOLCHAIN_VERSION,
                    "Timestamp": "2026-11-20T00:00:00Z",
                }
            ]
        )
        self.assertRejected(
            scenario.verify(),
            "Go toolchain 1.26.11 has not been public for 7 days",
        )

    def test_toolchain_missing_first_seen_record(self) -> None:
        scenario = Scenario("toolchain")
        scenario.http_map[go_index_url(TOOLCHAIN_HINT)] = ndjson(
            [
                {
                    "Path": "golang.org/toolchain",
                    "Version": "v0.0.1-go1.26.10.linux-amd64",
                    "Timestamp": "2026-10-01T00:00:00Z",
                }
            ]
        )
        self.assertRejected(
            scenario.verify(), "no trusted first-seen evidence for the Go toolchain"
        )

    def test_toolchain_docker_tag_not_aged(self) -> None:
        scenario = Scenario("toolchain")
        scenario.add_docker_evidence(
            "library/golang", "1.26.11-alpine", DIGEST_B, "2026-11-20T00:00:00Z"
        )
        self.assertRejected(
            scenario.verify(),
            "Docker tag 1.26.11-alpine has not been public for 7 days",
        )

    def test_toolchain_docker_digest_mismatch(self) -> None:
        scenario = Scenario("toolchain")
        scenario.add_docker_evidence(
            "library/golang", "1.26.11-alpine", DIGEST_C, TRUSTED_OLD
        )
        self.assertRejected(scenario.verify(), "does not match the PR")

    def test_updater_commit_author_mismatch(self) -> None:
        scenario = Scenario("toolchain")
        scenario.commits[0]["author"]["login"] = DEPENDABOT
        self.assertRejected(
            scenario.verify(), "author is not watchtower-updater[bot]"
        )

    def test_updater_commit_committer_bot_allowed(self) -> None:
        scenario = Scenario("toolchain")
        scenario.commits[0]["committer"]["login"] = UPDATER
        decision = scenario.verify()
        self.assertEligible(decision, pr_number=44, head_sha=TOOLCHAIN_SHA)


# ---------------------------------------------------------------------------
# Unknown tree/blob shapes
# ---------------------------------------------------------------------------


class TreeAndBlobShapeTests(AutomergeTestCase):
    def test_truncated_tree_rejected(self) -> None:
        scenario = Scenario()
        scenario.base_tree["truncated"] = True
        self.assertRejected(
            scenario.verify(), "git tree is truncated"
        )

    def test_missing_truncated_field_rejected(self) -> None:
        scenario = Scenario()
        del scenario.base_tree["truncated"]
        self.assertRejected(
            scenario.verify(), "git tree is truncated"
        )

    def test_submodule_tree_entry_rejected(self) -> None:
        scenario = Scenario()
        scenario.base_tree["tree"].append(
            {"path": "vendor/mod", "mode": "160000", "type": "commit", "sha": OTHER_SHA}
        )
        self.assertRejected(
            scenario.verify(), "unsupported commit entry"
        )

    def test_duplicate_tree_path_rejected(self) -> None:
        scenario = Scenario()
        scenario.base_tree["tree"].append(copy.deepcopy(scenario.base_tree["tree"][0]))
        self.assertRejected(
            scenario.verify(), "git tree has duplicate path"
        )

    def test_blob_with_wrong_sha_rejected(self) -> None:
        scenario = Scenario()
        blob_sha = scenario.head_blob_sha("go.mod")
        scenario.blobs[blob_sha]["sha"] = OTHER_SHA
        self.assertRejected(
            scenario.verify(), "git blob response is for a different sha"
        )

    def test_blob_with_wrong_encoding_rejected(self) -> None:
        scenario = Scenario()
        blob_sha = scenario.head_blob_sha("go.mod")
        scenario.blobs[blob_sha]["encoding"] = "utf-8"
        self.assertRejected(
            scenario.verify(), "git blob is not base64 encoded"
        )

    def test_blob_with_wrong_size_rejected(self) -> None:
        scenario = Scenario()
        blob_sha = scenario.head_blob_sha("go.mod")
        scenario.blobs[blob_sha]["size"] += 1
        self.assertRejected(
            scenario.verify(), "git blob size mismatch"
        )

    def test_blob_that_is_not_utf8_rejected(self) -> None:
        scenario = Scenario()
        blob_sha = scenario.head_blob_sha("go.mod")
        scenario.blobs[blob_sha]["content"] = base64.b64encode(
            b"\xff\xfe\x00"
        ).decode("ascii")
        scenario.blobs[blob_sha]["size"] = 3
        self.assertRejected(
            scenario.verify(), "git blob is not valid UTF-8 text"
        )

    def test_identical_trees_rejected(self) -> None:
        scenario = Scenario()
        scenario.head_files = dict(scenario.base_files)
        scenario.rebuild()
        self.assertRejected(
            scenario.verify(), "pull request changes no files"
        )

    def test_compare_status_identical_rejected(self) -> None:
        scenario = Scenario()
        scenario.compare["status"] = "identical"
        self.assertRejected(
            scenario.verify(), "commit comparison status is identical"
        )


# ---------------------------------------------------------------------------
# CLI behaviour
# ---------------------------------------------------------------------------


class CliTests(AutomergeTestCase):
    def _run_main(self, argv: list[str], out_path: str) -> tuple[int, str, str]:
        env = {
            "GITHUB_EVENT_PATH": "",
            "GITHUB_REPOSITORY": "",
            "GITHUB_TOKEN": "",
            "GH_TOKEN": "",
            "GITHUB_OUTPUT": "",
        }
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(
                urllib.request.OpenerDirector,
                "open",
                side_effect=AssertionError("network access attempted"),
            ):
                with contextlib.redirect_stdout(stdout):
                    code = av.main(argv)
        written = Path(out_path).read_text(encoding="utf-8") if os.path.exists(out_path) else ""
        return code, stdout.getvalue(), written

    def test_malformed_event_payload_writes_false_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "output.txt")
            code, stdout, written = self._run_main(
                [
                    "--event",
                    str(FIXTURES / "malformed_event.json"),
                    "--repo",
                    REPO,
                    "--github-output",
                    out,
                ],
                out,
            )
        self.assertEqual(code, 0)
        self.assertIn("eligible=false", stdout)
        self.assertIn("eligible=false", written)
        self.assertIn("cannot read event payload", stdout)

    def test_missing_event_file_writes_false_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "output.txt")
            code, stdout, written = self._run_main(
                [
                    "--event",
                    os.path.join(tmp, "absent-event.json"),
                    "--repo",
                    REPO,
                    "--github-output",
                    out,
                ],
                out,
            )
        self.assertEqual(code, 0)
        self.assertIn("eligible=false", stdout)
        self.assertIn("eligible=false", written)
        self.assertIn("cannot read event payload", stdout)

    def test_missing_event_path_env_writes_false_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "output.txt")
            code, stdout, written = self._run_main(
                ["--repo", REPO, "--github-output", out], out
            )
        self.assertEqual(code, 0)
        self.assertIn("eligible=false", stdout)
        self.assertIn("eligible=false", written)
        self.assertIn("GITHUB_EVENT_PATH is missing", stdout)

    def test_empty_event_without_token_writes_false_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = os.path.join(tmp, "event.json")
            Path(event_path).write_text("{}", encoding="utf-8")
            out = os.path.join(tmp, "output.txt")
            code, stdout, written = self._run_main(
                [
                    "--event",
                    event_path,
                    "--repo",
                    REPO,
                    "--github-output",
                    out,
                ],
                out,
            )
        self.assertEqual(code, 0)
        self.assertIn("eligible=false", stdout)
        self.assertIn("eligible=false", written)
        self.assertIn("GitHub token is missing", stdout)


if __name__ == "__main__":
    unittest.main()
