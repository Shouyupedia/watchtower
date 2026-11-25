#!/usr/bin/env python3
"""Independent, fail-closed automerge verifier for ``automerge.yml``.

The workflow lives on ``main`` and is triggered by ``workflow_run`` after the
``Pull Request`` or ``CodeQL`` workflow completes.  It never checks out PR
code and never consumes PR artifacts.  This module re-derives every fact it
needs from the GitHub API and from authoritative package registries.

Design rules enforced here
--------------------------
* Every missing, ambiguous, failed or unexpected piece of evidence produces
  ``eligible=False`` with a short reason; the workflow job itself still exits
  successfully so the PR simply waits for a human reviewer.
* A PR is bound to exactly one open, non-draft PR in this repository whose
  live ``head.sha`` equals the ``workflow_run.head_sha``.  Workflow runs,
  check runs, commits, trees and blobs are all read for that same SHA.
* The commit chain must be linear, every commit must be signature-verified,
  authored by the PR robot, committed by ``web-flow`` or that robot, and the
  chain must start at the merge base.  Go-toolchain PRs must additionally be
  one commit whose parent is the current ``main`` tip.
* Sensitive paths (workflows, composite actions, automation scripts,
  CODEOWNERS, Dependabot config) are a hard block for every author.
* Dependabot PRs are only considered for the exact ecosystems ``go_modules``,
  ``docker`` and ``pip``; ``github_actions`` is never eligible.  File and
  line-level whitelists, major-version rejection and registry cooldowns are
  applied per ecosystem.
* Dependabot security updates must never be auto-merged.  Although M1 enabled
  Dependabot security updates on 2026-11-25 (``d3484b7``), this repository has
  no real normal/security-update PR pair yet, so there is **no calibrated
  positive signal** that a Dependabot PR is an ordinary version update.  The
  default ``CALIBRATED_NORMAL_UPDATE_SIGNALS`` set is intentionally empty:
  every Dependabot PR is therefore ineligible and goes to manual review.  Add
  a signal only after it has been calibrated against real samples in M3.
* Go-toolchain PRs from ``watchtower-updater[bot]`` may be eligible only for a
  single same-minor patch increment (exactly ``+1``) that synchronises
  ``.go-version`` and all three Dockerfiles, with both the Go toolchain and the
  ``golang:<version>-alpine`` image past their seven-day cooldowns.

Usage
-----
    python -I .github/scripts/automerge_verify.py \
        --event "$GITHUB_EVENT_PATH" --repo "$GITHUB_REPOSITORY"

The script always exits 0 after writing ``eligible=false`` for a rejected PR.
It exits 2 only for command-line usage errors and 1 if the output file cannot
be written.  The GitHub token is read from ``GITHUB_TOKEN`` or ``GH_TOKEN`` and
is never printed.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import shlex
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

# ``python -I`` removes the script directory from sys.path.  The sibling
# cooldown module must be importable without relying on the caller's cwd.
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import cooldown  # noqa: E402  (path is set up immediately above)

DEPENDABOT_AUTHOR = "dependabot[bot]"
UPDATER_AUTHOR = "watchtower-updater[bot]"
ALLOWED_AUTHORS = frozenset({DEPENDABOT_AUTHOR, UPDATER_AUTHOR})

ALLOWED_ECOSYSTEMS = frozenset({"go_modules", "docker", "pip"})

REQUIRED_WORKFLOWS = {
    "Pull Request": ".github/workflows/pull-request.yml",
    "CodeQL": ".github/workflows/codeql-analysis.yml",
}

# All 14 ``github-actions`` status checks required by the active M2
# ``main-checks`` ruleset (``d3484b7``) plus the ``CodeQL`` code scanning
# check run.  Check-run names are frozen after M2; changing a job name
# requires a matching ruleset change.
REQUIRED_CHECKS = {
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

SENSITIVE_PREFIXES = (
    ".github/workflows/",
    ".github/actions/",
    ".github/scripts/",
)
SENSITIVE_BASENAMES = frozenset({"CODEOWNERS", "dependabot.yml", "dependabot.yaml"})

GO_MODULE_FILES = frozenset({"go.mod", "go.sum", "tools/go.mod", "tools/go.sum"})
PIP_FILE = "docs-requirements.txt"
DOCKER_DIR = "dockerfiles/"
GO_VERSION_FILE = ".go-version"
GOLANG_DOCKERFILES = frozenset(
    {
        "dockerfiles/Dockerfile",
        "dockerfiles/Dockerfile.self-contained",
        "dockerfiles/Dockerfile.dev-self-contained",
    }
)

GO_PATCH_COOLDOWN_DAYS = 3
GO_MINOR_COOLDOWN_DAYS = 7
GO_NEW_MODULE_COOLDOWN_DAYS = 7
DOCKER_COOLDOWN_DAYS = 7
PIP_COOLDOWN_DAYS = 7
TOOLCHAIN_COOLDOWN_DAYS = 7

MAX_CHANGED_FILES = 50
MAX_BLOB_BYTES = 5 * 1024 * 1024
MAX_PR_COMMITS = 250
MAX_PAGES = 10
PER_PAGE = 100

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GO_VERSION_FILE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
GO_SEMVER_RE = re.compile(
    r"^v(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)
MODULE_MAJOR_SUFFIX_RE = re.compile(r"^v([0-9]+)$")
GOPKG_MAJOR_SUFFIX_RE = re.compile(r"\.v([0-9]+)$")
DOCKER_FROM_RE = re.compile(
    r"^(?P<prefix>\s*FROM\s+(?:--platform=\S+\s+)?)"
    r"(?P<image>[^\s@]+)@sha256:(?P<digest>[0-9a-f]{64})"
    r"(?P<suffix>\s*(?:AS\s+\S+)?\s*)$"
)
DOCKER_REPO_RE = re.compile(
    r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*$"
)
DOCKER_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
PIP_SPEC_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"==(?P<version>[A-Za-z0-9][A-Za-z0-9._+!-]*)$"
)
PIP_HASH_RE = re.compile(r"^--hash=sha256:([0-9a-fA-F]{64})$")
PEP440_RE = re.compile(
    r"^(?:(?P<epoch>[0-9]+)!)?"
    r"(?P<release>[0-9]+(?:\.[0-9]+)*)"
    r"(?:(?P<pre>alpha|beta|preview|pre|rc|a|b|c)(?P<pre_n>[0-9]+)?)?"
    r"(?:\.post(?P<post>[0-9]+))?"
    r"(?:\.dev(?P<dev>[0-9]+))?"
    r"(?:\+(?P<local>[A-Za-z0-9.-]+))?$",
    re.IGNORECASE,
)
PEP503_RE = re.compile(r"[-_.]+")

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")

# A candidate signal is *detected* from Dependabot's generated PR body, but it
# is not trusted until it has been calibrated against real samples.  M1 is
# complete (2026-11-25, ``d3484b7``), but no real Dependabot normal/security
# PR pair exists yet; the set stays empty until M3 can compare real samples.
# Until then: manual review.
CALIBRATED_NORMAL_UPDATE_SIGNALS: frozenset[str] = frozenset()
DEPENDABOT_BODY_SIGNAL = "dependabot-body-v1"

_SECURITY_BODY_MARKERS = (
    "this update includes a security fix",
    "vulnerabilities",
    "vulnerability",
    "security advisory",
    "dependabot alert",
    "cve-",
    "ghsa-",
    "github.com/advisories/",
    "severity:",
)


class Ineligible(Exception):
    """Raised by a check when the PR must not be auto-merged."""


@dataclass(frozen=True)
class Decision:
    eligible: bool
    reason: str
    pr_number: int | None = None
    head_sha: str | None = None


@dataclass(frozen=True)
class EventContext:
    repo: str
    head_sha: str
    head_branch: str
    run_name: str


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str  # "modified", "added" or "deleted"
    base_mode: str | None
    head_mode: str | None
    base_blob: str | None
    head_blob: str | None


class GitHubGetter(Protocol):
    """The single injected GitHub API surface used by the decision logic."""

    def get(self, path: str) -> Any:
        """Return the decoded JSON body for a repository-relative API path."""


def _safe(value: Any, limit: int = 200) -> str:
    """Make an untrusted value safe for a one-line workflow log/output."""
    text = value if isinstance(value, str) else repr(value)
    text = _CONTROL_RE.sub(" ", text)
    return text[:limit]


def _https_origin(url: str) -> tuple[str, int]:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise Ineligible("GitHub API URL has an invalid origin") from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None:
        raise Ineligible("GitHub API URL must have an HTTPS origin")
    return parsed.hostname, 443 if port is None else port


class GitHubApi:
    """Minimal read-only GitHub REST client with strict HTTPS handling."""

    def __init__(
        self,
        token: str,
        api_url: str = "https://api.github.com",
        timeout: float = 20.0,
        max_bytes: int = 32 * 1024 * 1024,
    ):
        if not isinstance(token, str) or not token:
            raise Ineligible("GitHub token is missing")
        self._api_origin = _https_origin(api_url)
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout
        self._max_bytes = max_bytes

    def get(self, path: str) -> Any:
        if not isinstance(path, str) or not path.startswith("/"):
            raise Ineligible("GitHub API path must start with '/'")
        parsed = urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc:
            raise Ineligible("GitHub API path must not be an absolute URL")
        if any(segment in (".", "..") for segment in parsed.path.split("/")):
            raise Ineligible("GitHub API path contains an unsafe segment")
        url = self._api_url + path
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "watchtower-automerge-verify/1.0",
            },
        )
        try:
            with _GITHUB_OPENER.open(request, timeout=self._timeout) as response:
                if _https_origin(response.geturl()) != self._api_origin:
                    raise Ineligible("GitHub API response came from an unexpected origin")
                body = response.read(self._max_bytes + 1)
        except Ineligible:
            raise
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                raw = exc.read(4096)
                data = json.loads(raw)
                if isinstance(data, dict) and isinstance(data.get("message"), str):
                    detail = f": {_safe(data['message'])}"
            except Exception:
                pass
            raise Ineligible(
                f"GitHub API HTTP {exc.code} for {_safe(parsed.path)}{detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise Ineligible(
                f"GitHub API request failed for {_safe(parsed.path)}: {_safe(exc)}"
            ) from exc
        if len(body) > self._max_bytes:
            raise Ineligible("GitHub API response is too large")
        if not body:
            raise Ineligible(f"GitHub API returned an empty body for {_safe(parsed.path)}")
        try:
            return json.loads(body)
        except ValueError as exc:
            raise Ineligible(
                f"GitHub API returned invalid JSON for {_safe(parsed.path)}"
            ) from exc


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        if _https_origin(newurl) != _https_origin(req.full_url):
            raise Ineligible("GitHub API redirected to an unexpected origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_GITHUB_OPENER = urllib.request.build_opener(_SameOriginRedirectHandler())


# ---------------------------------------------------------------------------
# Generic API and validation helpers
# ---------------------------------------------------------------------------


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise Ineligible(reason)


def _require_dict(value: Any, what: str) -> dict:
    _require(isinstance(value, dict), f"{what} is missing or not an object")
    return value


def _require_list(value: Any, what: str) -> list:
    _require(isinstance(value, list), f"{what} is missing or not a list")
    return value


def _require_str(value: Any, what: str) -> str:
    _require(isinstance(value, str) and bool(value.strip()), f"{what} is missing")
    return value


def _require_int(value: Any, what: str, minimum: int = 0) -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value >= minimum,
        f"{what} is missing or invalid",
    )
    return value


def _require_sha(value: Any, what: str) -> str:
    _require(isinstance(value, str) and bool(SHA_RE.match(value)), f"{what} is invalid")
    return value


def _with_query(path: str, params: Mapping[str, str]) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{urllib.parse.urlencode(params)}"


def _fetch_pages(
    client: GitHubGetter,
    path: str,
    *,
    items_key: str | None = None,
    params: Mapping[str, str] | None = None,
    per_page: int = PER_PAGE,
    max_pages: int = MAX_PAGES,
) -> list:
    """Fetch every page of a list endpoint, failing closed on truncation."""
    items: list = []
    for page in range(1, max_pages + 1):
        query = dict(params or {})
        query["per_page"] = str(per_page)
        query["page"] = str(page)
        data = client.get(_with_query(path, query))
        if items_key is None:
            _require(isinstance(data, list), f"GitHub API {path} did not return a list")
            batch = data
            total = None
        else:
            _require(isinstance(data, dict), f"GitHub API {path} did not return an object")
            batch = data.get(items_key)
            _require(isinstance(batch, list), f"GitHub API {path} has no {items_key} list")
            total = data.get("total_count")
            if total is not None:
                _require(
                    isinstance(total, int) and not isinstance(total, bool) and total >= 0,
                    f"GitHub API {path} has an invalid total_count",
                )
        items.extend(batch)
        if len(batch) < per_page:
            if total is not None and len(items) != total:
                raise Ineligible(
                    f"GitHub API {path} pagination is incomplete "
                    f"({len(items)} of {total} items)"
                )
            return items
    raise Ineligible(f"GitHub API {path} exceeded {max_pages} pages")


def _parse_event(event: Any, repo: Any) -> EventContext:
    event = _require_dict(event, "event payload")
    _require(event.get("action") == "completed", "event action is not 'completed'")
    workflow_run = _require_dict(event.get("workflow_run"), "workflow_run")
    run_name = _require_str(workflow_run.get("name"), "workflow_run.name")
    _require(
        run_name in REQUIRED_WORKFLOWS,
        f"workflow {_safe(run_name)} is not a required workflow",
    )
    _require(
        workflow_run.get("event") == "pull_request",
        "workflow_run.event is not pull_request",
    )
    _require(
        workflow_run.get("status") == "completed",
        "workflow_run.status is not completed",
    )
    head_sha = _require_sha(workflow_run.get("head_sha"), "workflow_run.head_sha")
    head_branch = _require_str(workflow_run.get("head_branch"), "workflow_run.head_branch")
    head_repository = _require_dict(
        workflow_run.get("head_repository"), "workflow_run.head_repository"
    )
    head_repo_name = _require_str(head_repository.get("full_name"), "head_repository.full_name")
    _require(isinstance(repo, str) and bool(REPO_RE.match(repo)), "repository is invalid")
    _require(head_repo_name == repo, "workflow run head repository is not this repository")
    return EventContext(repo=repo, head_sha=head_sha, head_branch=head_branch, run_name=run_name)


def _validate_pr_shape(pr: Any, ctx: EventContext) -> dict:
    pr = _require_dict(pr, "pull request")
    number = _require_int(pr.get("number"), "pull request number", minimum=1)
    _require(pr.get("state") == "open", "pull request is not open")
    _require(pr.get("draft") is False, "pull request is a draft")
    base = _require_dict(pr.get("base"), "pull request base")
    head = _require_dict(pr.get("head"), "pull request head")
    _require(base.get("ref") == "main", "pull request base is not main")
    _require_sha(base.get("sha"), "pull request base sha")
    _require(head.get("sha") == ctx.head_sha, "pull request head sha changed")
    _require(head.get("ref") == ctx.head_branch, "pull request head branch changed")
    head_repo = _require_dict(head.get("repo"), "pull request head repository")
    _require(head_repo.get("full_name") == ctx.repo, "pull request head is not this repository")
    user = _require_dict(pr.get("user"), "pull request user")
    _require_str(user.get("login"), "pull request user login")
    _require_int(pr.get("commits"), "pull request commit count", minimum=1)
    _require(isinstance(pr.get("body"), str), "pull request body is missing")
    labels = _require_list(pr.get("labels"), "pull request labels")
    for label in labels:
        label = _require_dict(label, "pull request label")
        _require_str(label.get("name"), "pull request label name")
    _require(number == pr.get("number"), "pull request number changed")
    return pr


def _find_target_pr(client: GitHubGetter, ctx: EventContext) -> dict:
    owner = ctx.repo.split("/", 1)[0]
    prs = _fetch_pages(
        client,
        f"/repos/{ctx.repo}/pulls",
        params={"state": "open", "head": f"{owner}:{ctx.head_branch}"},
        max_pages=5,
    )
    matches = []
    for pr in prs:
        _require(isinstance(pr, dict), "pull request list contains a non-object entry")
        head = pr.get("head")
        base = pr.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise Ineligible("pull request list entry is missing head or base")
        head_repo = head.get("repo")
        if not isinstance(head_repo, dict):
            continue
        if (
            pr.get("state") == "open"
            and pr.get("draft") is False
            and base.get("ref") == "main"
            and head_repo.get("full_name") == ctx.repo
            and head.get("sha") == ctx.head_sha
        ):
            matches.append(pr)
    if len(matches) != 1:
        raise Ineligible(
            f"expected exactly one open target PR for head sha, found {len(matches)}"
        )
    number = _require_int(matches[0].get("number"), "pull request number", minimum=1)
    detail = client.get(f"/repos/{ctx.repo}/pulls/{number}")
    pr = _validate_pr_shape(detail, ctx)
    _require(pr["number"] == number, "pull request number mismatch")
    return pr


def _check_required_workflows(client: GitHubGetter, ctx: EventContext) -> None:
    runs = _fetch_pages(
        client,
        f"/repos/{ctx.repo}/actions/runs",
        params={"head_sha": ctx.head_sha},
        items_key="workflow_runs",
        max_pages=5,
    )
    by_name: dict[str, list[dict]] = {}
    for run in runs:
        run = _require_dict(run, "workflow run")
        _require(run.get("head_sha") == ctx.head_sha, "workflow run is for another sha")
        if run.get("event") != "pull_request":
            continue
        if run.get("head_branch") != ctx.head_branch:
            # The same commit can be pushed to another branch; only runs for
            # this PR branch are evidence for this decision.
            continue
        head_repo = _require_dict(run.get("head_repository"), "workflow run head_repository")
        _require(
            head_repo.get("full_name") == ctx.repo,
            "workflow run is not for this repository",
        )
        name = _require_str(run.get("name"), "workflow run name")
        if name not in REQUIRED_WORKFLOWS:
            continue
        _require(
            run.get("path") == REQUIRED_WORKFLOWS[name],
            f"workflow {_safe(name)} has an unexpected path",
        )
        _require(
            run.get("head_branch") == ctx.head_branch,
            f"workflow {_safe(name)} is for another branch",
        )
        run_id = _require_int(run.get("id"), f"workflow {_safe(name)} id", 1)
        attempt = _require_int(run.get("run_attempt"), f"workflow {_safe(name)} run_attempt", 1)
        # Attempts restart at one for each new workflow run.
        by_name.setdefault(name, []).append({**run, "_rank": (run_id, attempt)})

    for name in REQUIRED_WORKFLOWS:
        candidates = by_name.get(name, [])
        _require(bool(candidates), f"required workflow {name} has no pull_request run")
        newest = max(candidate["_rank"] for candidate in candidates)
        latest = [candidate for candidate in candidates if candidate["_rank"] == newest]
        _require(len(latest) == 1, f"required workflow {name} has an ambiguous latest run")
        run = latest[0]
        _require(
            run.get("status") == "completed",
            f"workflow {name} is not completed",
        )
        _require(
            run.get("conclusion") == "success",
            f"required workflow {name} concluded {_safe(run.get('conclusion'))}",
        )


def _check_required_checks(client: GitHubGetter, ctx: EventContext) -> None:
    runs = _fetch_pages(
        client,
        f"/repos/{ctx.repo}/commits/{ctx.head_sha}/check-runs",
        items_key="check_runs",
        max_pages=5,
    )
    by_name: dict[str, list[dict]] = {}
    for run in runs:
        run = _require_dict(run, "check run")
        _require(run.get("head_sha") == ctx.head_sha, "check run is for another sha")
        name = _require_str(run.get("name"), "check run name")
        _require_str(run.get("status"), f"check run {_safe(name)} status")
        app = _require_dict(run.get("app"), f"check run {_safe(name)} app")
        app_slug = _require_str(app.get("slug"), f"check run {_safe(name)} app slug")
        started = run.get("started_at")
        if started is not None and not isinstance(started, str):
            raise Ineligible(f"check run {_safe(name)} has an invalid started_at")
        run_id = run.get("id")
        _require_int(run_id, f"check run {_safe(name)} id", 1)
        by_name.setdefault(name, []).append(
            {**run, "_app_slug": app_slug, "_started": started or "", "_id": run_id}
        )

    for name, expected_app in REQUIRED_CHECKS.items():
        candidates = [
            run
            for run in by_name.get(name, [])
            if run["_app_slug"] == expected_app and run.get("name") == name
        ]
        _require(
            bool(candidates),
            f"required check {name} is missing (expected {expected_app})",
        )
        # Check-run ids are monotonically increasing per repository, so a
        # queued re-run (whose started_at is null) must outrank an older
        # completed run.  Never let a stale success win.
        latest_key = max((candidate["_id"], candidate["_started"]) for candidate in candidates)
        latest = [
            candidate
            for candidate in candidates
            if (candidate["_id"], candidate["_started"]) == latest_key
        ]
        _require(len(latest) == 1, f"required check {name} has an ambiguous latest run")
        run = latest[0]
        _require(
            run.get("status") == "completed",
            f"required check {name} is still running",
        )
        _require(
            run.get("conclusion") == "success",
            f"required check {name} concluded {_safe(run.get('conclusion'))}",
        )


def _check_identity_and_commits(
    client: GitHubGetter, ctx: EventContext, pr: dict
) -> list[dict]:
    author = pr["user"]["login"]
    _require(author in ALLOWED_AUTHORS, f"author {_safe(author)} is not allowed")
    total = pr["commits"]
    _require(
        total <= MAX_PR_COMMITS,
        f"PR has {total} commits; refusing to verify more than {MAX_PR_COMMITS}",
    )
    raw_commits = _fetch_pages(
        client,
        f"/repos/{ctx.repo}/pulls/{pr['number']}/commits",
        max_pages=5,
    )
    _require(
        len(raw_commits) == total,
        f"PR commit count mismatch ({len(raw_commits)} of {total})",
    )
    commits = []
    seen = set()
    for index, raw in enumerate(raw_commits):
        raw = _require_dict(raw, f"commit #{index}")
        sha = _require_sha(raw.get("sha"), f"commit #{index} sha")
        _require(sha not in seen, f"duplicate commit {sha}")
        seen.add(sha)
        parents = _require_list(raw.get("parents"), f"commit {sha} parents")
        _require(len(parents) == 1, f"commit {sha} is a merge or root commit")
        parent = _require_dict(parents[0], f"commit {sha} parent")
        parent_sha = _require_sha(parent.get("sha"), f"commit {sha} parent sha")
        if index > 0:
            previous_sha = commits[index - 1]["sha"]
            _require(
                parent_sha == previous_sha,
                f"commit {sha} does not follow {previous_sha}",
            )
        commit_author = _require_dict(raw.get("author"), f"commit {sha} author")
        _require(
            commit_author.get("login") == author,
            f"commit {sha} author is not {author}",
        )
        committer = _require_dict(raw.get("committer"), f"commit {sha} committer")
        committer_login = _require_str(committer.get("login"), f"commit {sha} committer login")
        _require(
            committer_login in {"web-flow", author},
            f"commit {sha} committer is not web-flow or {author}",
        )
        commit_data = _require_dict(raw.get("commit"), f"commit {sha} data")
        verification = _require_dict(
            commit_data.get("verification"), f"commit {sha} verification"
        )
        _require(
            verification.get("verified") is True,
            f"commit {sha} signature is not verified",
        )
        commits.append({"sha": sha, "parent": parent_sha, "raw": raw})
    _require(
        commits[-1]["sha"] == ctx.head_sha,
        "last commit is not the workflow_run head sha",
    )
    return commits


def _parse_go_mod(text: str, path: str):
    """Return (non_require_directives, require_map) for a go.mod file.

    ``non_require_directives`` preserves order and includes every directive
    other than ``require`` (including unknown ones) so any change to them is a
    rejection.  ``require_map`` maps module path to version.
    """
    directives: list[tuple[str, tuple[str, ...]]] = []
    requires: dict[str, str] = {}
    block: str | None = None

    def parse_entry(directive: str, entry: str, lineno: int) -> None:
        try:
            tokens = tuple(shlex.split(entry, comments=False, posix=True))
        except ValueError as exc:
            raise Ineligible(f"{path}:{lineno}: malformed go.mod entry: {exc}") from exc
        if not directive or not tokens:
            raise Ineligible(f"{path}:{lineno}: empty go.mod directive")
        if directive == "require":
            _require(len(tokens) == 2, f"{path}:{lineno}: malformed require entry")
            module, version = tokens
            _require(
                module not in requires,
                f"{path}:{lineno}: duplicate require for {_safe(module)}",
            )
            requires[module] = version
            return
        if directive in {"module", "go", "toolchain", "tool", "godebug"}:
            _require(
                len(tokens) == 1,
                f"{path}:{lineno}: malformed {directive} directive",
            )
        elif directive in {"exclude", "replace"}:
            _require(
                len(tokens) >= 2,
                f"{path}:{lineno}: malformed {directive} directive",
            )
        elif directive == "retract":
            _require(
                len(tokens) >= 1,
                f"{path}:{lineno}: malformed retract directive",
            )
        directives.append((directive, tokens))

    for lineno, raw in enumerate(text.split("\n"), start=1):
        line = _strip_go_comment(raw).strip()
        if not line:
            continue
        if block is not None:
            if line == ")":
                block = None
                continue
            if line.endswith(")"):
                entry = line[:-1].strip()
                if entry:
                    parse_entry(block, entry, lineno)
                block = None
                continue
            parse_entry(block, line, lineno)
            continue

        try:
            parts = shlex.split(line, comments=False, posix=True)
        except ValueError as exc:
            raise Ineligible(f"{path}:{lineno}: malformed go.mod line: {exc}") from exc
        _require(bool(parts), f"{path}:{lineno}: empty go.mod line")
        directive = parts[0]
        rest = parts[1:]
        _require(
            re.match(r"^[A-Za-z][A-Za-z0-9_]*$", directive) is not None,
            f"{path}:{lineno}: malformed go.mod directive",
        )
        if rest and rest[0] == "(":
            _require(len(rest) == 1, f"{path}:{lineno}: tokens after '('")
            block = directive
            continue
        if rest and rest[0].startswith("(") and rest[-1].endswith(")"):
            inner = " ".join(rest)[1:-1].strip()
            if inner:
                parse_entry(directive, inner, lineno)
            continue
        parse_entry(directive, " ".join(rest), lineno)
    _require(block is None, f"{path}: unclosed go.mod block")
    _require(
        sum(1 for name, _ in directives if name == "module") == 1,
        f"{path}: expected exactly one module directive",
    )
    _require(
        sum(1 for name, _ in directives if name == "go") == 1,
        f"{path}: expected exactly one go directive",
    )
    _require(
        sum(1 for name, _ in directives if name == "toolchain") <= 1,
        f"{path}: multiple toolchain directives",
    )
    return directives, requires


def _strip_go_comment(line: str) -> str:
    in_quote = None
    index = 0
    while index < len(line):
        char = line[index]
        if in_quote:
            if char == "\\" and in_quote == '"':
                index += 2
                continue
            if char == in_quote:
                in_quote = None
        else:
            if char in ('"', "`"):
                in_quote = char
            elif char == "/" and index + 1 < len(line) and line[index + 1] == "/":
                return line[:index]
        index += 1
    return line


def _go_semver_key(version: str) -> tuple:
    match = GO_SEMVER_RE.match(version)
    _require(match is not None, f"unsupported Go module version {_safe(version)}")
    pre = match.group("pre")
    if pre is None:
        pre_key: tuple = (1,)
    else:
        parts = []
        for identifier in pre.split("."):
            _require(bool(identifier), f"malformed prerelease in {_safe(version)}")
            if identifier.isdigit():
                _require(
                    not (len(identifier) > 1 and identifier.startswith("0")),
                    f"leading zero in prerelease of {_safe(version)}",
                )
                parts.append((0, int(identifier), ""))
            else:
                parts.append((1, 0, identifier))
        pre_key = (0, tuple(parts))
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        pre_key,
    )


def _module_path_major(module: str) -> int | None:
    last = module.rsplit("/", 1)[-1]
    match = MODULE_MAJOR_SUFFIX_RE.match(last)
    if match is None and module.startswith("gopkg.in/"):
        # gopkg.in spells its major suffix as .vN rather than /vN.  Other
        # hosts do not get that special case.
        match = GOPKG_MAJOR_SUFFIX_RE.search(last)
    if match is None:
        return None
    major = int(match.group(1))
    return major if major >= 2 else None


def _validate_go_require_upgrade(
    module: str, old_version: str, new_version: str
) -> int:
    """Return the cooldown in days for an allowed upgrade."""
    old_key = _go_semver_key(old_version)
    new_key = _go_semver_key(new_version)
    _require(
        old_key != new_key or old_version == new_version,
        f"{_safe(module)} changes only build metadata",
    )
    _require(
        new_key > old_key,
        f"{_safe(module)} {_safe(old_version)} -> {_safe(new_version)} is not an upgrade",
    )
    _require(
        new_key[0] == old_key[0],
        f"{_safe(module)} crosses a major version ({old_key[0]} -> {new_key[0]})",
    )
    path_major = _module_path_major(module)
    if path_major is not None:
        _require(
            old_key[0] == path_major and new_key[0] == path_major,
            f"{_safe(module)} version does not match its /vN path",
        )
        _require(
            not old_version.endswith("+incompatible")
            and not new_version.endswith("+incompatible"),
            f"{_safe(module)} uses +incompatible with a /vN path",
        )
    else:
        for version, key in ((old_version, old_key), (new_version, new_key)):
            if key[0] >= 2:
                _require(
                    version.endswith("+incompatible"),
                    f"{_safe(module)} {_safe(version)} needs a /vN path",
                )
            else:
                _require(
                    not version.endswith("+incompatible"),
                    f"{_safe(module)} {_safe(version)} cannot be +incompatible",
                )
    if new_key[:3] == old_key[:3]:
        return GO_MINOR_COOLDOWN_DAYS
    if new_key[1] != old_key[1]:
        return GO_MINOR_COOLDOWN_DAYS
    return GO_PATCH_COOLDOWN_DAYS


def _check_go_cooldown(
    module: str, version: str, days: int, http_get: cooldown.HttpGet, now: datetime
) -> None:
    try:
        first_seen = cooldown.go_first_seen(module, version, http_get=http_get)
    except cooldown.CooldownError as exc:
        raise Ineligible(
            f"no trusted first-seen evidence for {_safe(module)}@{_safe(version)}: "
            f"{_safe(exc)}"
        ) from exc
    _require(
        cooldown.is_aged(first_seen, days, now),
        f"{_safe(module)}@{_safe(version)} has not been public for {days} days",
    )


def _validate_go_mod_change(
    base_text: str,
    head_text: str,
    http_get: cooldown.HttpGet,
    now: datetime,
) -> None:
    base_directives, base_requires = _parse_go_mod(base_text, "base go.mod")
    head_directives, head_requires = _parse_go_mod(head_text, "head go.mod")
    _require(
        base_directives == head_directives,
        "go.mod changes a directive other than require "
        "(go/toolchain/replace/exclude/retract/tool/module)",
    )
    _require(
        base_requires != head_requires,
        "go.mod has no require changes",
    )

    removed = set(base_requires) - set(head_requires)
    if removed:
        raise Ineligible(
            f"go.mod removes required module {_safe(sorted(removed)[0])}"
        )

    changes = 0
    for module in sorted(base_requires):
        old_version = base_requires[module]
        new_version = head_requires[module]
        if old_version == new_version:
            continue
        days = _validate_go_require_upgrade(module, old_version, new_version)
        _check_go_cooldown(module, new_version, days, http_get, now)
        changes += 1
    for module in sorted(set(head_requires) - set(base_requires)):
        _check_go_cooldown(
            module,
            head_requires[module],
            GO_NEW_MODULE_COOLDOWN_DAYS,
            http_get,
            now,
        )
        changes += 1
    _require(changes > 0, "go.mod has no allowed require change")


def _parse_docker_image(image: str) -> tuple[str, str]:
    _require(isinstance(image, str) and bool(image), "Docker image reference is empty")
    _require("@" not in image, "Docker image reference contains a digest")
    parts = image.split("/")
    if len(parts) > 1 and (
        "." in parts[0] or ":" in parts[0] or parts[0].lower() == "localhost"
    ):
        registry = parts[0].lower()
        _require(
            registry in {"docker.io", "index.docker.io", "registry-1.docker.io"},
            f"Docker image is not on Docker Hub: {_safe(image)}",
        )
        parts = parts[1:]
    _require(bool(parts) and all(parts), f"Docker image reference is malformed: {_safe(image)}")
    last = parts[-1]
    _require(":" in last, f"Docker image has no tag: {_safe(image)}")
    repository_last, tag = last.rsplit(":", 1)
    _require(bool(repository_last) and bool(tag), f"Docker image tag is malformed: {_safe(image)}")
    parts[-1] = repository_last
    if len(parts) == 1:
        repository = f"library/{parts[0]}"
    elif len(parts) == 2:
        repository = "/".join(parts)
    else:
        raise Ineligible(f"Docker Hub image has too many path segments: {_safe(image)}")
    _require(
        DOCKER_REPO_RE.match(repository) is not None,
        f"Docker repository is malformed: {_safe(repository)}",
    )
    _require(
        DOCKER_TAG_RE.match(tag) is not None,
        f"Docker tag is malformed: {_safe(tag)}",
    )
    return repository, tag


def _validate_docker_digest_change(
    path: str, base_text: str, head_text: str, *, allow_tag_change: bool = False
) -> tuple[str, str, str, str]:
    """Validate a one-line pinned-FROM change.

    By default only the digest may change (Dependabot docker updates).
    ``allow_tag_change`` permits the tag to change as well, which is required
    for a Go toolchain version bump; the caller must still pin the exact old
    and new tags.  Returns ``(repo, old_tag, new_tag, new_digest)``.
    """
    base_lines = base_text.split("\n")
    head_lines = head_text.split("\n")
    _require(
        len(base_lines) == len(head_lines),
        f"{_safe(path)} changes its line count",
    )
    changed = [
        index
        for index, (old, new) in enumerate(zip(base_lines, head_lines))
        if old != new
    ]
    _require(
        len(changed) == 1,
        f"{_safe(path)} must change exactly one line, found {len(changed)}",
    )
    old_line = base_lines[changed[0]]
    new_line = head_lines[changed[0]]
    old_match = DOCKER_FROM_RE.match(old_line)
    new_match = DOCKER_FROM_RE.match(new_line)
    _require(
        old_match is not None and new_match is not None,
        f"{_safe(path)} changes a line that is not a pinned FROM line",
    )
    _require(
        old_match.group("prefix") == new_match.group("prefix")
        and old_match.group("suffix") == new_match.group("suffix"),
        f"{_safe(path)} changes more than the FROM image and digest",
    )
    _require(
        old_match.group("digest") != new_match.group("digest"),
        f"{_safe(path)} does not change the FROM digest",
    )
    old_image = old_match.group("image")
    new_image = new_match.group("image")
    old_repo, old_tag = _parse_docker_image(old_image)
    new_repo, new_tag = _parse_docker_image(new_image)
    _require(old_repo == new_repo, f"{_safe(path)} changes the image name")
    if allow_tag_change:
        # The raw repository spelling must be unchanged; only the tag and the
        # digest may differ.  This rejects e.g. switching golang: to
        # docker.io/library/golang: in the same edit.
        _require(
            old_image.rsplit(":", 1)[0] == new_image.rsplit(":", 1)[0],
            f"{_safe(path)} changes the image repository spelling",
        )
        _require(old_tag != new_tag, f"{_safe(path)} does not change the image tag")
    else:
        _require(old_image == new_image, f"{_safe(path)} changes more than the FROM digest")
        _require(old_tag == new_tag, f"{_safe(path)} changes the image tag")
    return new_repo, old_tag, new_tag, new_match.group("digest")


def _check_docker_cooldown(
    repository: str,
    tag: str,
    expected_digest: str,
    http_get: cooldown.HttpGet,
    now: datetime,
) -> None:
    try:
        digest, last_pushed = cooldown.docker_tag_info(
            repository, tag, http_get=http_get
        )
    except cooldown.CooldownError as exc:
        raise Ineligible(
            f"no trusted Docker Hub evidence for {_safe(repository)}:{_safe(tag)}: "
            f"{_safe(exc)}"
        ) from exc
    _require(
        digest == f"sha256:{expected_digest}",
        f"Docker Hub digest for {_safe(tag)} does not match the PR",
    )
    _require(
        cooldown.is_aged(last_pushed, DOCKER_COOLDOWN_DAYS, now),
        f"Docker tag {_safe(tag)} has not been public for "
        f"{DOCKER_COOLDOWN_DAYS} days",
    )


def _normalize_package_name(name: str) -> str:
    return PEP503_RE.sub("-", name).lower()


def _parse_pip_version(version: str) -> tuple:
    match = PEP440_RE.match(version)
    _require(match is not None, f"unsupported PyPI version {_safe(version)}")
    _require(match.group("local") is None, f"local PyPI version is not supported: {_safe(version)}")
    epoch = int(match.group("epoch") or 0)
    release = tuple(int(part) for part in match.group("release").split("."))
    pre = match.group("pre")
    pre_n = int(match.group("pre_n") or 0)
    post = match.group("post")
    dev = match.group("dev")
    if dev is not None and pre is None:
        phase = (0, int(dev))
    elif pre is not None:
        label = {
            "alpha": "a",
            "beta": "b",
            "preview": "rc",
            "pre": "rc",
            "c": "rc",
        }.get(pre.lower(), pre.lower())
        order = {"a": 0, "b": 1, "rc": 2}[label]
        phase = (
            1,
            order,
            pre_n,
            0 if dev is not None else 1,
            int(dev) if dev is not None else 0,
        )
    elif post is not None:
        phase = (3, int(post))
    else:
        phase = (2, 0)
    return epoch, release, phase


def _compare_pip_versions(old: tuple, new: tuple) -> int:
    old_epoch, old_release, old_phase = old
    new_epoch, new_release, new_phase = new
    if old_epoch != new_epoch:
        return -1 if new_epoch < old_epoch else 1
    length = max(len(old_release), len(new_release))
    padded_old = old_release + (0,) * (length - len(old_release))
    padded_new = new_release + (0,) * (length - len(new_release))
    if padded_old != padded_new:
        return -1 if padded_new < padded_old else 1
    if old_phase != new_phase:
        return -1 if new_phase < old_phase else 1
    return 0


def _parse_requirements_lock(text: str, path: str) -> dict[str, tuple[str, frozenset[str]]]:
    logical: list[tuple[int, str]] = []
    current = ""
    start = 1
    for lineno, raw in enumerate(text.split("\n"), start=1):
        stripped = raw.strip()
        if not current:
            start = lineno
        if stripped.endswith("\\"):
            current += " " + stripped[:-1].strip()
        else:
            current += " " + stripped
            logical.append((start, current.strip()))
            current = ""
    if current.strip():
        logical.append((start, current.strip()))

    packages: dict[str, tuple[str, frozenset[str]]] = {}
    for lineno, raw_line in logical:
        line = re.sub(r"\s+#.*$", "", raw_line).strip()
        if not line or line.startswith("#"):
            continue
        _require(
            not line.startswith("-"),
            f"{path}:{lineno}: options such as --index-url are not allowed",
        )
        tokens = line.split()
        _require(bool(tokens), f"{path}:{lineno}: empty requirement")
        spec = tokens[0]
        match = PIP_SPEC_RE.match(spec)
        _require(
            match is not None,
            f"{path}:{lineno}: requirement is not an exact name==version pin",
        )
        hashes = set()
        for token in tokens[1:]:
            hash_match = PIP_HASH_RE.match(token)
            _require(
                hash_match is not None,
                f"{path}:{lineno}: only --hash=sha256:<hex> options are allowed",
            )
            hashes.add(hash_match.group(1).lower())
        _require(bool(hashes), f"{path}:{lineno}: requirement has no --hash")
        name = _normalize_package_name(match.group("name"))
        _require(
            name not in packages,
            f"{path}:{lineno}: duplicate requirement for {_safe(name)}",
        )
        packages[name] = (match.group("version"), frozenset(hashes))
    _require(bool(packages), f"{path}: no pinned requirements found")
    return packages


def _check_pip_cooldown(
    name: str, version: str, hashes: frozenset[str], http_get: cooldown.HttpGet, now: datetime
) -> None:
    try:
        last_upload = cooldown.pip_hashes_last_upload(
            name, version, hashes, http_get=http_get
        )
    except cooldown.CooldownError as exc:
        raise Ineligible(
            f"no trusted PyPI evidence for {_safe(name)}=={_safe(version)}: "
            f"{_safe(exc)}"
        ) from exc
    _require(
        cooldown.is_aged(last_upload, PIP_COOLDOWN_DAYS, now),
        f"{_safe(name)}=={_safe(version)} has a file uploaded less than "
        f"{PIP_COOLDOWN_DAYS} days ago",
    )


def _validate_pip_change(
    base_text: str,
    head_text: str,
    http_get: cooldown.HttpGet,
    now: datetime,
) -> None:
    base_packages = _parse_requirements_lock(base_text, f"base {PIP_FILE}")
    head_packages = _parse_requirements_lock(head_text, f"head {PIP_FILE}")
    removed = set(base_packages) - set(head_packages)
    if removed:
        raise Ineligible(
            f"{PIP_FILE} removes package {_safe(sorted(removed)[0])}"
        )
    changes = 0
    for name in sorted(base_packages):
        old_version, old_hashes = base_packages[name]
        new_version, new_hashes = head_packages[name]
        if old_version == new_version:
            _require(
                old_hashes == new_hashes,
                f"{PIP_FILE} changes hashes without a version change for "
                f"{_safe(name)}",
            )
            continue
        old_parsed = _parse_pip_version(old_version)
        new_parsed = _parse_pip_version(new_version)
        _require(
            new_parsed[0] == old_parsed[0],
            f"{_safe(name)} changes epoch",
        )
        _require(
            new_parsed[1][0] == old_parsed[1][0],
            f"{_safe(name)} crosses a major version "
            f"({old_parsed[1][0]} -> {new_parsed[1][0]})",
        )
        _require(
            _compare_pip_versions(old_parsed, new_parsed) > 0,
            f"{_safe(name)} {_safe(old_version)} -> {_safe(new_version)} "
            "is not an upgrade",
        )
        _check_pip_cooldown(name, new_version, new_hashes, http_get, now)
        changes += 1
    for name in sorted(set(head_packages) - set(base_packages)):
        version, hashes = head_packages[name]
        _check_pip_cooldown(name, version, hashes, http_get, now)
        changes += 1
    _require(changes > 0, f"{PIP_FILE} has no allowed package change")


# ---------------------------------------------------------------------------
# Git tree and blob helpers
# ---------------------------------------------------------------------------


def _fetch_tree(client: GitHubGetter, repo: str, sha: str) -> dict[str, tuple[str, str]]:
    data = _require_dict(
        client.get(f"/repos/{repo}/git/trees/{sha}?recursive=1"), "git tree"
    )
    _require(
        data.get("truncated") is False,
        "git tree is truncated or its truncated field is missing",
    )
    tree = _require_list(data.get("tree"), "git tree entries")
    entries: dict[str, tuple[str, str]] = {}
    for entry in tree:
        entry = _require_dict(entry, "git tree entry")
        entry_type = _require_str(entry.get("type"), "git tree entry type")
        if entry_type == "tree":
            continue
        # Commit entries are submodules.  This repository has none, and a
        # submodule could otherwise be added without appearing in the blob
        # diff or the path whitelist.
        _require(
            entry_type == "blob",
            f"git tree contains an unsupported {_safe(entry_type)} entry",
        )
        path = _require_str(entry.get("path"), "git tree entry path")
        _require(
            not path.startswith("/") and "\\" not in path,
            f"git tree path is not a clean POSIX path: {_safe(path)}",
        )
        _require(
            all(segment not in ("", ".", "..") for segment in path.split("/")),
            f"git tree path is unsafe: {_safe(path)}",
        )
        mode = _require_str(entry.get("mode"), f"git tree mode for {_safe(path)}")
        blob = _require_sha(entry.get("sha"), f"git tree blob for {_safe(path)}")
        _require(path not in entries, f"git tree has duplicate path {_safe(path)}")
        entries[path] = (mode, blob)
    return entries


def _diff_trees(
    base: dict[str, tuple[str, str]], head: dict[str, tuple[str, str]]
) -> list[ChangedFile]:
    changes = []
    for path in sorted(set(base) | set(head)):
        base_entry = base.get(path)
        head_entry = head.get(path)
        if base_entry is not None and head_entry is not None:
            if base_entry == head_entry:
                continue
            status = "modified"
        elif head_entry is not None:
            status = "added"
        else:
            status = "deleted"
        changes.append(
            ChangedFile(
                path=path,
                status=status,
                base_mode=base_entry[0] if base_entry else None,
                head_mode=head_entry[0] if head_entry else None,
                base_blob=base_entry[1] if base_entry else None,
                head_blob=head_entry[1] if head_entry else None,
            )
        )
    _require(bool(changes), "pull request changes no files")
    _require(
        len(changes) <= MAX_CHANGED_FILES,
        f"pull request changes {len(changes)} files; refusing to verify more "
        f"than {MAX_CHANGED_FILES}",
    )
    return changes


def _fetch_blob_text(
    client: GitHubGetter, repo: str, blob_sha: str, cache: dict[str, str]
) -> str:
    if blob_sha in cache:
        return cache[blob_sha]
    data = _require_dict(
        client.get(f"/repos/{repo}/git/blobs/{blob_sha}"), "git blob"
    )
    _require(
        data.get("sha") == blob_sha,
        f"git blob response is for a different sha than {_safe(blob_sha)}",
    )
    encoding = _require_str(data.get("encoding"), "git blob encoding")
    _require(encoding == "base64", "git blob is not base64 encoded")
    size = _require_int(data.get("size"), "git blob size", minimum=0)
    _require(size <= MAX_BLOB_BYTES, "git blob is too large")
    content = _require_str(data.get("content"), "git blob content")
    compact = "".join(content.split())
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Ineligible("git blob content is not valid base64") from exc
    _require(
        len(decoded) == size,
        f"git blob size mismatch ({len(decoded)} of {size})",
    )
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Ineligible("git blob is not valid UTF-8 text") from exc
    cache[blob_sha] = text
    return text


def _texts_for(
    changes: Sequence[ChangedFile],
    paths: set[str],
    client: GitHubGetter,
    repo: str,
    cache: dict[str, str],
) -> dict[str, tuple[str | None, str | None]]:
    texts: dict[str, tuple[str | None, str | None]] = {}
    for change in changes:
        if change.path not in paths:
            continue
        base_text = (
            _fetch_blob_text(client, repo, change.base_blob, cache)
            if change.base_blob
            else None
        )
        head_text = (
            _fetch_blob_text(client, repo, change.head_blob, cache)
            if change.head_blob
            else None
        )
        texts[change.path] = (base_text, head_text)
    return texts


def _check_sensitive_paths(changes: Sequence[ChangedFile]) -> None:
    for change in changes:
        path = change.path
        _require(
            not any(path.startswith(prefix) for prefix in SENSITIVE_PREFIXES),
            f"sensitive path changed: {_safe(path)}",
        )
        basename = path.rsplit("/", 1)[-1]
        _require(
            basename not in SENSITIVE_BASENAMES,
            f"sensitive file changed: {_safe(path)}",
        )


# ---------------------------------------------------------------------------
# Dependabot checks
# ---------------------------------------------------------------------------


def _parse_dependabot_ecosystem(head_ref: str) -> str:
    _require(isinstance(head_ref, str) and head_ref.startswith("dependabot/"),
             "Dependabot branch name is malformed")
    parts = head_ref.split("/")
    _require(len(parts) >= 3 and bool(parts[2]), "Dependabot branch name is malformed")
    ecosystem = parts[1]
    _require(
        ecosystem != "github_actions",
        "github_actions updates are never auto-merged",
    )
    _require(
        ecosystem in ALLOWED_ECOSYSTEMS,
        f"ecosystem {_safe(ecosystem)} is not auto-mergeable",
    )
    return ecosystem


def _require_modified_mode(change: ChangedFile) -> None:
    _require(
        change.status == "modified",
        f"{_safe(change.path)} is {change.status}, not modified",
    )
    _require(
        change.base_mode == "100644" and change.head_mode == "100644",
        f"{_safe(change.path)} changes its file mode",
    )


def _validate_dependabot_go_modules(
    changes: Sequence[ChangedFile],
    texts: dict[str, tuple[str | None, str | None]],
    http_get: cooldown.HttpGet,
    now: datetime,
) -> None:
    for change in changes:
        _require(
            change.path in GO_MODULE_FILES,
            f"go_modules update changes {_safe(change.path)}",
        )
        _require_modified_mode(change)
    roots = {"tools" if change.path.startswith("tools/") else "." for change in changes}
    _require(len(roots) == 1, "go_modules update touches both module roots")
    root = roots.pop()
    prefix = "" if root == "." else "tools/"
    go_mod = f"{prefix}go.mod"
    go_sum = f"{prefix}go.sum"
    changed_paths = {change.path for change in changes}
    _require(go_mod in changed_paths, f"go_modules update does not change {go_mod}")
    _require(
        go_sum in changed_paths,
        f"go_modules update does not change {go_sum}",
    )
    _require(
        changed_paths <= {go_mod, go_sum},
        "go_modules update changes an unexpected file",
    )
    base_mod, head_mod = texts[go_mod]
    _require(base_mod is not None and head_mod is not None, f"{go_mod} content is missing")
    _validate_go_mod_change(base_mod, head_mod, http_get, now)


def _validate_dependabot_docker(
    changes: Sequence[ChangedFile],
    texts: dict[str, tuple[str | None, str | None]],
    http_get: cooldown.HttpGet,
    now: datetime,
) -> None:
    _require(bool(changes), "docker update changes no Dockerfile")
    verified: dict[tuple[str, str], str] = {}
    for change in changes:
        _require(
            change.path.startswith(DOCKER_DIR)
            and change.path.rsplit("/", 1)[-1].startswith("Dockerfile"),
            f"docker update changes {_safe(change.path)}",
        )
        _require_modified_mode(change)
        base_text, head_text = texts[change.path]
        _require(
            base_text is not None and head_text is not None,
            f"{_safe(change.path)} content is missing",
        )
        repository, _old_tag, tag, digest = _validate_docker_digest_change(
            change.path, base_text, head_text
        )
        key = (repository, tag)
        _require(
            key not in verified or verified[key] == digest,
            f"docker update uses different digests for {_safe(tag)}",
        )
        verified[key] = digest
    for (repository, tag), digest in sorted(verified.items()):
        _check_docker_cooldown(repository, tag, digest, http_get, now)


def _validate_dependabot_pip(
    changes: Sequence[ChangedFile],
    texts: dict[str, tuple[str | None, str | None]],
    http_get: cooldown.HttpGet,
    now: datetime,
) -> None:
    _require(len(changes) == 1, "pip update must change exactly one file")
    change = changes[0]
    _require(change.path == PIP_FILE, f"pip update changes {_safe(change.path)}")
    _require_modified_mode(change)
    base_text, head_text = texts[PIP_FILE]
    _require(base_text is not None and head_text is not None, f"{PIP_FILE} content is missing")
    _validate_pip_change(base_text, head_text, http_get, now)


def _has_security_label(pr: dict) -> bool:
    for label in pr["labels"]:
        if "security" in label["name"].lower():
            return True
    return False


def _has_security_markers(body: str) -> bool:
    lowered = body.lower()
    return any(marker in lowered for marker in _SECURITY_BODY_MARKERS)


def _detect_normal_update_signal(pr: dict) -> str | None:
    """Return a candidate signal that this is an ordinary Dependabot update.

    Detection is deliberately conservative and structural: Dependabot's
    generated body starts with a bump line and contains at least one details
    section.  Detection alone is never enough; the signal must also be in the
    calibrated allowlist (empty until real samples exist).
    """
    body = pr.get("body")
    if not isinstance(body, str) or not body.strip():
        return None
    if _has_security_markers(body):
        return None
    first_line = body.strip().splitlines()[0].strip()
    single = re.match(r"^Bumps?\s+.+\s+from\s+.+\s+to\s+.+\.$", first_line, re.I)
    grouped = re.match(
        r"^Bumps?\s+the\s+.+\s+group\s+with\s+[0-9]+\s+updates?:?$",
        first_line,
        re.I,
    )
    if single is None and grouped is None:
        return None
    lowered = body.lower()
    if not any(
        f"<summary>{section}</summary>" in lowered
        for section in ("release notes", "changelog", "commits")
    ):
        return None
    return DEPENDABOT_BODY_SIGNAL


def _check_dependabot_update_kind(
    pr: dict, calibrated_signals: frozenset[str] | set[str]
) -> None:
    _require(not _has_security_label(pr), "PR carries a security label")
    body = pr.get("body")
    _require(isinstance(body, str), "Dependabot PR body is missing")
    _require(
        not _has_security_markers(body),
        "Dependabot PR body contains security-update metadata",
    )
    signal = _detect_normal_update_signal(pr)
    _require(
        signal is not None,
        "no positive signal that this is an ordinary version update; "
        "manual review required",
    )
    _require(
        signal in calibrated_signals,
        "ordinary-update signal is not calibrated against real samples; "
        "manual review required",
    )


# ---------------------------------------------------------------------------
# Go toolchain sync PR checks
# ---------------------------------------------------------------------------


def _current_main_sha(client: GitHubGetter, repo: str) -> str:
    data = _require_dict(client.get(f"/repos/{repo}/commits/main"), "main commit")
    return _require_sha(data.get("sha"), "main commit sha")


def _validate_toolchain_changes(
    changes: Sequence[ChangedFile],
    texts: dict[str, tuple[str | None, str | None]],
    http_get: cooldown.HttpGet,
    now: datetime,
) -> None:
    expected = {GO_VERSION_FILE} | set(GOLANG_DOCKERFILES)
    actual = {change.path for change in changes}
    _require(
        actual == expected,
        "toolchain update must change .go-version and exactly the three "
        f"Dockerfiles; got {_safe(sorted(actual))}",
    )
    for change in changes:
        _require_modified_mode(change)

    base_version_text, head_version_text = texts[GO_VERSION_FILE]
    _require(
        base_version_text is not None and head_version_text is not None,
        ".go-version content is missing",
    )
    base_match = GO_VERSION_FILE_RE.match(base_version_text.strip())
    head_match = GO_VERSION_FILE_RE.match(head_version_text.strip())
    _require(
        base_match is not None and head_match is not None,
        ".go-version is not an exact MAJOR.MINOR.PATCH version",
    )
    old_version = tuple(int(part) for part in base_match.groups())
    new_version = tuple(int(part) for part in head_match.groups())
    _require(
        old_version[:2] == new_version[:2],
        "toolchain update crosses a major or minor version",
    )
    _require(
        new_version[2] == old_version[2] + 1,
        ".go-version patch increment is not exactly +1",
    )
    old_text = f"{old_version[0]}.{old_version[1]}.{old_version[2]}"
    new_text = f"{new_version[0]}.{new_version[1]}.{new_version[2]}"

    new_digests = {}
    for path in sorted(GOLANG_DOCKERFILES):
        base_text, head_text = texts[path]
        _require(
            base_text is not None and head_text is not None,
            f"{_safe(path)} content is missing",
        )
        repository, old_tag, new_tag, digest = _validate_docker_digest_change(
            path, base_text, head_text, allow_tag_change=True
        )
        _require(
            repository == "library/golang",
            f"{_safe(path)} does not use the official golang image",
        )
        _require(
            old_tag == f"{old_text}-alpine",
            f"{_safe(path)} old tag does not match .go-version",
        )
        _require(
            new_tag == f"{new_text}-alpine",
            f"{_safe(path)} new tag does not match .go-version",
        )
        new_digests[path] = digest
    _require(
        len(set(new_digests.values())) == 1,
        "the three Dockerfiles do not use the same golang digest",
    )
    digest = next(iter(new_digests.values()))

    toolchain_version = f"v0.0.1-go{new_text}.linux-amd64"
    try:
        first_seen = cooldown.go_first_seen(
            "golang.org/toolchain", toolchain_version, http_get=http_get
        )
    except cooldown.CooldownError as exc:
        raise Ineligible(
            "no trusted first-seen evidence for the Go toolchain: "
            f"{_safe(exc)}"
        ) from exc
    _require(
        cooldown.is_aged(first_seen, TOOLCHAIN_COOLDOWN_DAYS, now),
        f"Go toolchain {_safe(new_text)} has not been public for "
        f"{TOOLCHAIN_COOLDOWN_DAYS} days",
    )
    _check_docker_cooldown(
        "library/golang", f"{new_text}-alpine", digest, http_get, now
    )


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


def _collect_changes(
    client: GitHubGetter, ctx: EventContext, pr: dict, commits: list[dict]
) -> tuple[str, list[ChangedFile]]:
    base_sha = pr["base"]["sha"]
    compare = _require_dict(
        client.get(f"/repos/{ctx.repo}/compare/{base_sha}...{ctx.head_sha}"),
        "commit comparison",
    )
    _require(
        compare.get("status") in {"ahead", "diverged"},
        f"commit comparison status is {_safe(compare.get('status'))}",
    )
    merge_base = _require_dict(compare.get("merge_base_commit"), "merge base commit")
    merge_base_sha = _require_sha(merge_base.get("sha"), "merge base sha")
    _require(
        commits[0]["parent"] == merge_base_sha,
        "commit chain does not start at the merge base",
    )
    base_tree = _fetch_tree(client, ctx.repo, merge_base_sha)
    head_tree = _fetch_tree(client, ctx.repo, ctx.head_sha)
    changes = _diff_trees(base_tree, head_tree)
    return merge_base_sha, changes


def _verify(
    event: Any,
    client: GitHubGetter,
    http_get: cooldown.HttpGet,
    now: datetime,
    repo: str,
    calibrated_signals: frozenset[str] | set[str],
) -> tuple[int, str]:
    ctx = _parse_event(event, repo)
    pr = _find_target_pr(client, ctx)
    _check_required_workflows(client, ctx)
    _check_required_checks(client, ctx)
    commits = _check_identity_and_commits(client, ctx, pr)
    merge_base_sha, changes = _collect_changes(client, ctx, pr, commits)
    _check_sensitive_paths(changes)

    author = pr["user"]["login"]
    blob_cache: dict[str, str] = {}
    if author == DEPENDABOT_AUTHOR:
        ecosystem = _parse_dependabot_ecosystem(pr["head"]["ref"])
        if ecosystem == "go_modules":
            paths = GO_MODULE_FILES
        elif ecosystem == "docker":
            paths = {
                change.path
                for change in changes
                if change.path.startswith(DOCKER_DIR)
            }
        elif ecosystem == "pip":
            paths = {PIP_FILE}
        else:  # pragma: no cover - _parse_dependabot_ecosystem already rejects
            raise Ineligible("unsupported ecosystem")
        texts = _texts_for(changes, set(paths), client, ctx.repo, blob_cache)
        if ecosystem == "go_modules":
            _validate_dependabot_go_modules(changes, texts, http_get, now)
        elif ecosystem == "docker":
            _validate_dependabot_docker(changes, texts, http_get, now)
        else:
            _validate_dependabot_pip(changes, texts, http_get, now)
        _check_dependabot_update_kind(pr, calibrated_signals)
    elif author == UPDATER_AUTHOR:
        _require(pr["commits"] == 1, "toolchain update must be a single commit")
        main_sha = _current_main_sha(client, ctx.repo)
        _require(
            merge_base_sha == main_sha,
            "toolchain branch is not based on the current main tip",
        )
        _require(
            commits[0]["parent"] == main_sha,
            "toolchain commit parent is not the current main tip",
        )
        paths = {GO_VERSION_FILE} | set(GOLANG_DOCKERFILES)
        texts = _texts_for(changes, paths, client, ctx.repo, blob_cache)
        _validate_toolchain_changes(changes, texts, http_get, now)
    else:  # pragma: no cover - _check_identity_and_commits already rejects
        raise Ineligible("author is not allowed")

    _confirm_head_unchanged(client, ctx, pr["number"])
    return pr["number"], ctx.head_sha


def _confirm_head_unchanged(client: GitHubGetter, ctx: EventContext, number: int) -> None:
    pr = _require_dict(client.get(f"/repos/{ctx.repo}/pulls/{number}"), "pull request")
    _require(pr.get("state") == "open", "pull request is no longer open")
    _require(pr.get("draft") is False, "pull request became a draft")
    base = _require_dict(pr.get("base"), "pull request base")
    head = _require_dict(pr.get("head"), "pull request head")
    _require(base.get("ref") == "main", "pull request base changed")
    _require(head.get("sha") == ctx.head_sha, "pull request head changed during verification")
    _require(head.get("ref") == ctx.head_branch, "pull request head branch changed")
    head_repo = _require_dict(head.get("repo"), "pull request head repository")
    _require(head_repo.get("full_name") == ctx.repo, "pull request head repository changed")


def verify_automerge(
    event: Any,
    *,
    client: GitHubGetter,
    http_get: cooldown.HttpGet = cooldown.http_get,
    now: datetime | None = None,
    repo: str | None = None,
    calibrated_signals: frozenset[str] | set[str] = CALIBRATED_NORMAL_UPDATE_SIGNALS,
) -> Decision:
    """Return the fail-closed automerge decision for a ``workflow_run`` event."""
    head_sha = None
    if isinstance(event, dict):
        run = event.get("workflow_run")
        if isinstance(run, dict) and isinstance(run.get("head_sha"), str):
            if SHA_RE.match(run["head_sha"]):
                head_sha = run["head_sha"]
    try:
        if now is None:
            now = datetime.now(timezone.utc)
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise Ineligible("verification time must be a timezone-aware datetime")
        if repo is None:
            repository = event.get("repository") if isinstance(event, dict) else None
            if isinstance(repository, dict) and isinstance(repository.get("full_name"), str):
                repo = repository["full_name"]
        if not isinstance(repo, str):
            raise Ineligible("repository is missing")
        pr_number, verified_sha = _verify(
            event, client, http_get, now, repo, calibrated_signals
        )
        return Decision(True, "all automerge checks passed", pr_number, verified_sha)
    except Ineligible as exc:
        return Decision(False, _safe(str(exc)), None, head_sha)
    except cooldown.CooldownError as exc:
        return Decision(False, _safe(f"cooldown evidence error: {exc}"), None, head_sha)
    except Exception as exc:  # noqa: BLE001 - fail closed on every unexpected shape
        traceback.print_exc(file=sys.stderr)
        return Decision(
            False,
            _safe(f"unexpected verification error: {type(exc).__name__}: {exc}"),
            None,
            head_sha,
        )


# A short alias for callers that prefer the workflow's job name.
verify = verify_automerge


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_outputs(path: str | None, decision: Decision) -> None:
    if not path:
        return
    values = {
        "eligible": "true" if decision.eligible else "false",
        "pr_number": str(decision.pr_number) if decision.pr_number is not None else "",
        "head_sha": decision.head_sha or "",
    }
    try:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")
    except OSError as exc:
        raise SystemExit(f"cannot write GitHub output: {exc}") from exc


def _read_event(path: str | None) -> Decision | Any:
    if not path:
        return Decision(False, "GITHUB_EVENT_PATH is missing", None, None)
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        return Decision(False, f"cannot read event payload: {_safe(exc)}", None, None)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed automerge verifier")
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH"))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    parser.add_argument("--now", help="timezone-aware ISO-8601 time (for tests)")
    args = parser.parse_args(argv)

    if args.now is not None:
        try:
            now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        except ValueError:
            parser.error("--now must be an ISO-8601 timestamp")
        if now.tzinfo is None:
            parser.error("--now must include a timezone")
    else:
        now = datetime.now(timezone.utc)

    event = _read_event(args.event)
    if isinstance(event, Decision):
        decision = event
    elif not args.repo:
        decision = Decision(False, "GITHUB_REPOSITORY is missing", None, None)
    else:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            decision = Decision(False, "GitHub token is missing", None, None)
        else:
            try:
                client = GitHubApi(token)
            except Ineligible as exc:
                decision = Decision(False, _safe(str(exc)), None, None)
            else:
                decision = verify_automerge(
                    event, client=client, now=now, repo=args.repo
                )

    print(
        f"automerge verify: eligible={'true' if decision.eligible else 'false'}: "
        f"{decision.reason}"
    )
    _write_outputs(args.github_output, decision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
