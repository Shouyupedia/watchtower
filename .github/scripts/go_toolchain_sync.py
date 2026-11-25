#!/usr/bin/env python3
"""Synchronize the repository's Go toolchain version and golang base images.

Subcommands:
    check    Read-only check against go.dev, trusted first-seen index, Docker
             Hub, and open sync PR state. Supports --dry-run to print planned
             changes with no writes.
    prepare  Apply planned edits against a trusted checkout of main (running
             ``go mod tidy`` for cross-minor upgrades) and write a JSON bundle.
             Runs before minting any GitHub App token.
    publish  Recheck main/branch state and publish the prepared bundle as a
             single GitHub-signed commit on ``automation/go-toolchain`` plus
             create or update its pull request.

Must run cleanly under ``python -I`` using only the Python standard library
plus sibling module ``cooldown.py``.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Protocol
import urllib.error
import urllib.parse
import urllib.request

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import cooldown  # noqa: E402

SYNC_BRANCH = "automation/go-toolchain"
DEFAULT_BASE_BRANCH = "main"
DEFAULT_UPDATER_LOGIN = "watchtower-updater[bot]"
COOLDOWN_DAYS = 7

GO_DL_URL = "https://go.dev/dl/?mode=json"
GO_TOOLCHAIN_MODULE = "golang.org/toolchain"
DOCKER_REPOSITORY = "library/golang"

GO_VERSION_FILE = ".go-version"
DOCKERFILES: tuple[str, ...] = (
    "dockerfiles/Dockerfile",
    "dockerfiles/Dockerfile.self-contained",
    "dockerfiles/Dockerfile.dev-self-contained",
)
GO_MODS: tuple[str, ...] = ("go.mod", "tools/go.mod")
GO_MOD_AND_SUMS: tuple[str, ...] = (
    "go.mod",
    "go.sum",
    "tools/go.mod",
    "tools/go.sum",
)
SAME_MINOR_ALLOWED_PATHS: frozenset[str] = frozenset((GO_VERSION_FILE, *DOCKERFILES))
CROSS_MINOR_ALLOWED_PATHS: frozenset[str] = frozenset(
    (GO_VERSION_FILE, *DOCKERFILES, *GO_MOD_AND_SUMS)
)

GO_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
GO_DL_VERSION_RE = re.compile(r"^go(\d+)\.(\d+)\.(\d+)$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
GO_DIRECTIVE_RE = re.compile(r"^go\s+(\d+)\.(\d+)(?:\.(\d+))?\s*$")
FROM_ANY_GOLANG_RE = re.compile(
    r"^\s*FROM\s+(?:--platform=\S+\s+)?(?:(?:docker\.io/)?(?:library/)?)?golang(?:[:@\s]|$)",
    re.IGNORECASE,
)
FROM_GOLANG_RE = re.compile(
    r"^(?P<prefix>\s*FROM\s+(?:--platform=\S+\s+)?golang:)"
    r"(?P<tag>\d+\.\d+\.\d+)-alpine@sha256:(?P<digest>[0-9a-f]{64})"
    r"(?P<suffix>\s*(?:AS\s+\S+)?\s*)$",
    re.IGNORECASE,
)

GITHUB_API_VERSION = "2022-11-28"
DEFAULT_USER_AGENT = "watchtower-go-toolchain-sync"
DEFAULT_TIMEOUT_SECONDS = 30

CREATE_COMMIT_ON_BRANCH_MUTATION = """\
mutation CreateCommitOnBranch($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit {
      oid
    }
  }
}
"""


class SyncError(Exception):
    """Raised when inputs, invariants, or GitHub operations fail."""


@dataclass(frozen=True, order=True)
class GoVersion:
    """Exact Go patch version ``major.minor.patch``."""

    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def series(self) -> tuple[int, int]:
        return (self.major, self.minor)

    @property
    def go_mod_directive(self) -> str:
        return f"{self.major}.{self.minor}.0"


@dataclass(frozen=True)
class OpenPR:
    """Subset of pull request fields used by the sync workflow."""

    number: int
    head_sha: str
    head_ref: str
    head_repo_full_name: str
    base_ref: str
    author_login: str
    title: str
    body: str
    html_url: str = ""


@dataclass(frozen=True)
class BranchCommit:
    """Commit metadata used to verify branch ancestry and exact tree match."""

    sha: str
    parents: tuple[str, ...]
    verified: bool
    files: dict[str, str]
    removed_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class SyncPlan:
    """Result of the read-only check phase."""

    needs_sync: bool
    reason: str
    current_version: GoVersion
    target_version: GoVersion | None = None
    cross_minor: bool = False
    docker_digest: str | None = None
    toolchain_first_seen: datetime | None = None
    docker_last_pushed: datetime | None = None
    file_edits: dict[str, bytes] | None = None
    open_pr: OpenPR | None = None
    branch_sha: str | None = None
    # Set for push-triggered replays: publish must find this same open PR.
    expected_pr_number: int | None = None
    commit_headline: str = ""
    commit_body: str = ""
    pr_title: str = ""
    pr_body: str = ""


@dataclass(frozen=True)
class SyncBundle:
    """Prepared commit payload passed from ``prepare`` to ``publish``."""

    main_sha: str
    current_version: GoVersion
    target_version: GoVersion
    cross_minor: bool
    docker_digest: str
    toolchain_first_seen: datetime
    docker_last_pushed: datetime
    commit_headline: str
    commit_body: str
    pr_title: str
    pr_body: str
    files: dict[str, bytes]
    expected_pr_number: int | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "main_sha": self.main_sha,
            "current_version": str(self.current_version),
            "target_version": str(self.target_version),
            "cross_minor": self.cross_minor,
            "docker_digest": self.docker_digest,
            "toolchain_first_seen": _format_utc(self.toolchain_first_seen),
            "docker_last_pushed": _format_utc(self.docker_last_pushed),
            "commit_headline": self.commit_headline,
            "commit_body": self.commit_body,
            "pr_title": self.pr_title,
            "pr_body": self.pr_body,
            "files": {
                path: base64.b64encode(content).decode("ascii")
                for path, content in sorted(self.files.items())
            },
            "expected_pr_number": self.expected_pr_number,
        }

    @classmethod
    def from_json_dict(cls, data: Mapping[str, Any]) -> SyncBundle:
        if not isinstance(data, Mapping):
            raise SyncError("sync bundle must be a JSON object")
        raw_files = data.get("files")
        if not isinstance(raw_files, Mapping):
            raise SyncError("sync bundle 'files' must be an object")
        decoded_files: dict[str, bytes] = {}
        for path, b64_content in raw_files.items():
            if not isinstance(path, str) or not isinstance(b64_content, str):
                raise SyncError("sync bundle file entries must map str -> base64 str")
            try:
                decoded_files[path] = base64.b64decode(b64_content.encode("ascii"), validate=True)
            except Exception as exc:
                raise SyncError(f"invalid base64 content for {path}: {exc}") from exc
        main_sha = data.get("main_sha")
        if not isinstance(main_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", main_sha):
            raise SyncError(f"invalid main_sha in bundle: {main_sha!r}")
        cross_minor = data.get("cross_minor")
        if not isinstance(cross_minor, bool):
            raise SyncError("sync bundle 'cross_minor' must be a bool")
        return cls(
            main_sha=main_sha,
            current_version=parse_go_version(str(data.get("current_version", ""))),
            target_version=parse_go_version(str(data.get("target_version", ""))),
            cross_minor=cross_minor,
            docker_digest=validate_digest(str(data.get("docker_digest", ""))),
            toolchain_first_seen=_parse_iso_utc(str(data.get("toolchain_first_seen", ""))),
            docker_last_pushed=_parse_iso_utc(str(data.get("docker_last_pushed", ""))),
            commit_headline=str(data.get("commit_headline", "")),
            commit_body=str(data.get("commit_body", "")),
            pr_title=str(data.get("pr_title", "")),
            pr_body=str(data.get("pr_body", "")),
            files=decoded_files,
            expected_pr_number=validate_optional_pr_number(
                data.get("expected_pr_number")
            ),
        )


@dataclass(frozen=True)
class PublishResult:
    """Outcome of the ``publish`` phase."""

    commit_sha: str
    reused_commit: bool
    pr_number: int | None
    pr_url: str
    pr_action: str


class GitHubClient(Protocol):
    """Injectable GitHub API client contract."""

    def get_branch_sha(self, branch: str) -> str | None: ...

    def get_commit(self, sha: str) -> BranchCommit: ...

    def find_open_sync_pr(
        self, branch: str, base: str, updater_login: str
    ) -> OpenPR | None: ...

    def create_ref(self, branch: str, sha: str) -> None: ...

    def delete_ref(self, branch: str) -> None: ...

    def create_commit_on_branch(
        self,
        branch: str,
        expected_head_oid: str,
        headline: str,
        body: str,
        additions: Mapping[str, bytes],
    ) -> str: ...

    def compare_and_swap_ref(
        self,
        branch: str,
        expected_old_oid: str,
        new_oid: str,
        source_branch: str,
    ) -> None: ...

    def create_pull_request(
        self, head: str, base: str, title: str, body: str
    ) -> OpenPR: ...

    def update_pull_request(
        self, number: int, title: str, body: str
    ) -> OpenPR: ...


TidyRunner = Callable[[Path, GoVersion], None]


def _format_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise SyncError(f"datetime must be timezone-aware: {dt!r}")
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso_utc(text: str) -> datetime:
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SyncError(f"invalid ISO-8601 timestamp {text!r}: {exc}") from exc
    if parsed.tzinfo is None:
        raise SyncError(f"timestamp must include timezone: {text!r}")
    return parsed.astimezone(timezone.utc)


def normalize_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def git_blob_sha(content: bytes) -> str:
    """Compute the exact Git blob SHA-1 for ``content``."""
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def allowed_paths(cross_minor: bool) -> frozenset[str]:
    return CROSS_MINOR_ALLOWED_PATHS if cross_minor else SAME_MINOR_ALLOWED_PATHS


def parse_go_version(text: str) -> GoVersion:
    """Parse an exact ``MAJOR.MINOR.PATCH`` Go version string."""
    value = text.strip()
    match = GO_VERSION_RE.match(value)
    if not match:
        raise SyncError(
            f"Go version must be exact MAJOR.MINOR.PATCH like 1.26.11, got {value!r}"
        )
    return GoVersion(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def validate_digest(digest: str) -> str:
    """Validate a Docker image digest in ``sha256:<64 hex>`` format."""
    value = digest.strip()
    if not DIGEST_RE.match(value):
        raise SyncError(f"invalid Docker digest {digest!r}")
    return value


def validate_optional_pr_number(
    value: Any, what: str = "expected_pr_number"
) -> int | None:
    """Accept ``None`` or a positive int; reject bools and everything else."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SyncError(f"{what} must be a positive integer or null, got {value!r}")
    return value


def parse_optional_pr_number_arg(text: str, what: str = "--expected-pr-number") -> int | None:
    """Parse an optional positive PR number from its command-line text form."""
    raw = text.strip()
    if not raw:
        return None
    if not re.fullmatch(r"[1-9][0-9]{0,8}", raw):
        raise SyncError(f"{what} must be a positive integer, got {text!r}")
    return int(raw)


def toolchain_module_version(version: GoVersion) -> str:
    """Return the canonical linux/amd64 ``golang.org/toolchain`` module version."""
    return f"v0.0.1-go{version}.linux-amd64"


def docker_image_tag(version: GoVersion) -> str:
    """Return the official Docker Hub Alpine tag for ``version``."""
    return f"{version}-alpine"


def select_latest_stable_release(dl_payload: bytes | str) -> GoVersion:
    """Select the highest numeric stable release from ``go.dev/dl/?mode=json``."""
    try:
        raw = dl_payload.decode("utf-8") if isinstance(dl_payload, bytes) else dl_payload
        entries = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncError(f"malformed go.dev release JSON: {exc}") from exc

    if not isinstance(entries, list):
        raise SyncError("go.dev release JSON must be a list")

    candidates: list[GoVersion] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("stable") is not True:
            continue
        ver_str = entry.get("version")
        if not isinstance(ver_str, str):
            continue
        match = GO_DL_VERSION_RE.match(ver_str.strip())
        if not match:
            continue
        candidates.append(
            GoVersion(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        )

    if not candidates:
        raise SyncError("no stable Go releases found in go.dev/dl/?mode=json")
    return max(candidates)


def render_go_version(target: GoVersion) -> bytes:
    """Render ``.go-version`` contents with a trailing LF."""
    return f"{target}\n".encode("utf-8")


def update_dockerfile(
    text: str, target: GoVersion, digest: str, path: str = ""
) -> bytes:
    """Update pinned ``golang:<x.y.z>-alpine@sha256:<hex>`` FROM lines only."""
    validated_digest = validate_digest(digest)
    normalized = normalize_lf(text)
    lines = normalized.splitlines(keepends=True)
    updated_lines: list[str] = []
    replacements = 0

    for lineno, raw in enumerate(lines, start=1):
        line_ending = "\n" if raw.endswith("\n") else ""
        content = raw[:-1] if line_ending else raw
        if not FROM_ANY_GOLANG_RE.match(content):
            updated_lines.append(raw)
            continue
        match = FROM_GOLANG_RE.match(content)
        if not match:
            label = f"{path}:{lineno}" if path else f"line {lineno}"
            raise SyncError(
                f"{label}: golang FROM line is not "
                f"'golang:<x.y.z>-alpine@sha256:<64 hex>': {content!r}"
            )
        new_line = (
            f"{match.group('prefix')}{target}-alpine@{validated_digest}"
            f"{match.group('suffix')}{line_ending}"
        )
        updated_lines.append(new_line)
        replacements += 1

    if replacements == 0:
        label = path or "Dockerfile"
        raise SyncError(f"{label}: no pinned golang FROM line found")
    return "".join(updated_lines).encode("utf-8")


def update_go_mod_directive(text: str, target: GoVersion, path: str = "") -> bytes:
    """Update the ``go`` directive in a ``go.mod`` file to ``<major>.<minor>.0``."""
    normalized = normalize_lf(text)
    lines = normalized.splitlines(keepends=True)
    updated_lines: list[str] = []
    replaced = False

    for raw in lines:
        line_ending = "\n" if raw.endswith("\n") else ""
        content = raw[:-1] if line_ending else raw
        stripped = content.strip()
        if not replaced and (stripped.startswith("go ") or stripped.startswith("go\t")):
            match = GO_DIRECTIVE_RE.match(stripped)
            if not match:
                label = path or "go.mod"
                raise SyncError(f"{label}: malformed go directive {stripped!r}")
            updated_lines.append(f"go {target.go_mod_directive}{line_ending}")
            replaced = True
        else:
            updated_lines.append(raw)

    if not replaced:
        label = path or "go.mod"
        raise SyncError(f"{label}: no go directive found")
    return "".join(updated_lines).encode("utf-8")


def validate_go_version_invariants(files: Mapping[str, bytes]) -> None:
    """Assert the same invariants enforced by ``scripts/check-go-version.py``."""
    if GO_VERSION_FILE not in files:
        raise SyncError(f"missing required file {GO_VERSION_FILE}")
    go_version = parse_go_version(files[GO_VERSION_FILE].decode("utf-8"))

    for rel in DOCKERFILES:
        if rel not in files:
            raise SyncError(f"missing required file {rel}")
        text = normalize_lf(files[rel].decode("utf-8"))
        found = 0
        for lineno, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not FROM_ANY_GOLANG_RE.match(line):
                continue
            match = FROM_GOLANG_RE.match(line)
            if not match:
                raise SyncError(
                    f"{rel}:{lineno}: golang FROM line is not "
                    f"'golang:<x.y.z>-alpine@sha256:<64 hex>': {line!r}"
                )
            if match.group("tag") != str(go_version):
                raise SyncError(
                    f"{rel}:{lineno}: golang tag {match.group('tag')} does not "
                    f"match .go-version {go_version}"
                )
            found += 1
        if found == 0:
            raise SyncError(f"{rel}: no pinned golang FROM line found")

    for rel in GO_MODS:
        if rel not in files:
            raise SyncError(f"missing required file {rel}")
        text = normalize_lf(files[rel].decode("utf-8"))
        directive: tuple[int, int, int] | None = None
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("go ") or line.startswith("go\t"):
                match = GO_DIRECTIVE_RE.match(line)
                if not match:
                    raise SyncError(f"{rel}: malformed go directive {line!r}")
                mod_patch = int(match.group(3)) if match.group(3) is not None else 0
                directive = (int(match.group(1)), int(match.group(2)), mod_patch)
                break
        if directive is None:
            raise SyncError(f"{rel}: no go directive found")
        mod_major, mod_minor, mod_patch = directive
        if (mod_major, mod_minor) != go_version.series:
            raise SyncError(
                f"{rel}: go directive {mod_major}.{mod_minor} is not in the "
                f"same series as .go-version {go_version}"
            )
        if mod_patch > go_version.patch:
            raise SyncError(
                f"{rel}: go directive {mod_major}.{mod_minor}.{mod_patch} "
                f"requires a newer patch than .go-version {go_version}"
            )


def plan_file_edits(
    repo_files: Mapping[str, bytes],
    target: GoVersion,
    digest: str,
    cross_minor: bool,
) -> dict[str, bytes]:
    """Compute exact LF-normalized file edits without touching disk."""
    validated_digest = validate_digest(digest)
    required = (GO_VERSION_FILE, *DOCKERFILES, *GO_MODS)
    for rel in required:
        if rel not in repo_files:
            raise SyncError(f"missing repository file {rel}")

    merged: dict[str, bytes] = {
        rel: normalize_lf(repo_files[rel].decode("utf-8")).encode("utf-8")
        for rel in required
    }
    updated: dict[str, bytes] = {}

    new_go_version = render_go_version(target)
    merged[GO_VERSION_FILE] = new_go_version
    if new_go_version != normalize_lf(repo_files[GO_VERSION_FILE].decode("utf-8")).encode("utf-8"):
        updated[GO_VERSION_FILE] = new_go_version

    for rel in DOCKERFILES:
        orig_norm = normalize_lf(repo_files[rel].decode("utf-8"))
        new_bytes = update_dockerfile(orig_norm, target, validated_digest, path=rel)
        merged[rel] = new_bytes
        if new_bytes != orig_norm.encode("utf-8"):
            updated[rel] = new_bytes

    if cross_minor:
        for rel in GO_MODS:
            orig_norm = normalize_lf(repo_files[rel].decode("utf-8"))
            new_bytes = update_go_mod_directive(orig_norm, target, path=rel)
            merged[rel] = new_bytes
            if new_bytes != orig_norm.encode("utf-8"):
                updated[rel] = new_bytes

    validate_go_version_invariants(merged)
    return updated


def build_commit_and_pr_text(
    current: GoVersion,
    target: GoVersion,
    digest: str,
    cross_minor: bool,
    toolchain_first_seen: datetime,
    docker_last_pushed: datetime,
) -> tuple[str, str, str, str]:
    """Build deterministic commit message and PR title/body."""
    kind = "minor" if cross_minor else "patch"
    headline = f"build(deps): bump Go toolchain from {current} to {target}"
    commit_body = (
        f"Update Go toolchain ({kind} upgrade) from {current} to {target}.\n\n"
        f"- Docker image: golang:{docker_image_tag(target)}@{digest}\n"
        f"- Toolchain first seen: {_format_utc(toolchain_first_seen)}\n"
        f"- Docker tag last pushed: {_format_utc(docker_last_pushed)}"
    )
    pr_title = headline
    if cross_minor:
        scope_lines = (
            "- `.go-version`\n"
            "- `dockerfiles/Dockerfile`, `dockerfiles/Dockerfile.self-contained`, "
            "`dockerfiles/Dockerfile.dev-self-contained`\n"
            "- `go.mod`, `tools/go.mod` (`go` directive) and `go mod tidy` updates"
        )
        review_note = (
            "Cross-minor Go toolchain upgrade; requires maintainer review and approval."
        )
    else:
        scope_lines = (
            "- `.go-version`\n"
            "- `dockerfiles/Dockerfile`, `dockerfiles/Dockerfile.self-contained`, "
            "`dockerfiles/Dockerfile.dev-self-contained`"
        )
        review_note = (
            "Same-minor Go patch upgrade; eligible for automated merge once checks pass."
        )

    pr_body = (
        f"Synchronize Go toolchain from `{current}` to `{target}`.\n\n"
        f"### Updated files\n"
        f"{scope_lines}\n\n"
        f"### Cooldown verification\n"
        f"- `golang.org/toolchain` `{toolchain_module_version(target)}` "
        f"first seen: `{_format_utc(toolchain_first_seen)}`\n"
        f"- `golang:{docker_image_tag(target)}` (`{digest}`) "
        f"pushed: `{_format_utc(docker_last_pushed)}`\n\n"
        f"{review_note}"
    )
    return headline, commit_body, pr_title, pr_body


def commit_matches_plan(
    commit: BranchCommit,
    main_sha: str,
    planned_files: Mapping[str, bytes],
) -> bool:
    """Return True iff ``commit`` is verified, parented solely on ``main_sha``,
    and changes the exact set of files to the exact planned contents."""
    if not commit.verified:
        return False
    if commit.parents != (main_sha,):
        return False
    if commit.removed_files:
        return False
    expected_blobs = {path: git_blob_sha(data) for path, data in planned_files.items()}
    return commit.files == expected_blobs


def commit_matches_check_plan(
    commit: BranchCommit,
    main_sha: str,
    planned_files: Mapping[str, bytes],
    cross_minor: bool,
) -> bool:
    """Check-phase match before ``go mod tidy`` runs for cross-minor upgrades.

    A cross-minor commit is never accepted here: the check phase cannot know
    the post-tidy ``go.mod``/``go.sum`` contents, so claiming the branch is up
    to date would let a drifted package set go unnoticed forever.  ``publish``
    re-decides reuse against the exact prepared bundle, which is idempotent
    and performs no write on a match.
    """
    if cross_minor:
        return False
    return commit_matches_plan(commit, main_sha, planned_files)


def plan_sync(
    *,
    repo_files: Mapping[str, bytes],
    event_name: str,
    main_sha: str,
    now: datetime,
    github_client: GitHubClient,
    http_get: cooldown.HttpGet = cooldown.http_get,
    cooldown_days: int = COOLDOWN_DAYS,
    base_branch: str = DEFAULT_BASE_BRANCH,
    sync_branch: str = SYNC_BRANCH,
    updater_login: str = DEFAULT_UPDATER_LOGIN,
    max_index_requests: int | None = None,
) -> SyncPlan:
    """Evaluate whether a Go toolchain sync PR should be created or updated."""
    if GO_VERSION_FILE not in repo_files:
        raise SyncError(f"missing {GO_VERSION_FILE}")
    current_version = parse_go_version(repo_files[GO_VERSION_FILE].decode("utf-8"))

    open_pr = github_client.find_open_sync_pr(sync_branch, base_branch, updater_login)

    # A push-triggered replay is only allowed to update the exact PR that the
    # maintainer left open; ``publish`` enforces this before any write so a PR
    # closed in the meantime is never recreated behind their back.
    expected_pr_number = open_pr.number if event_name == "push" and open_pr else None

    if event_name == "push":
        if open_pr is None:
            return SyncPlan(
                needs_sync=False,
                reason="push event: no open same-repo updater PR to replay",
                current_version=current_version,
            )
        pr_commit = github_client.get_commit(open_pr.head_sha)
        if pr_commit.parents == (main_sha,):
            return SyncPlan(
                needs_sync=False,
                reason="push event: open sync PR is already based on current main",
                current_version=current_version,
                open_pr=open_pr,
                branch_sha=open_pr.head_sha,
            )

    try:
        dl_payload = http_get(GO_DL_URL)
    except cooldown.CooldownError as exc:
        return SyncPlan(
            needs_sync=False,
            reason=f"failed to fetch go.dev releases: {exc}",
            current_version=current_version,
            open_pr=open_pr,
        )

    target_version = select_latest_stable_release(dl_payload)
    if target_version <= current_version:
        return SyncPlan(
            needs_sync=False,
            reason=(
                f"already up to date (current={current_version}, "
                f"latest_stable={target_version})"
            ),
            current_version=current_version,
            target_version=target_version,
            open_pr=open_pr,
        )

    cross_minor = target_version.series != current_version.series
    tc_version = toolchain_module_version(target_version)

    try:
        if max_index_requests is None:
            toolchain_first_seen = cooldown.go_first_seen(
                GO_TOOLCHAIN_MODULE, tc_version, http_get=http_get
            )
        else:
            toolchain_first_seen = cooldown.go_first_seen(
                GO_TOOLCHAIN_MODULE,
                tc_version,
                http_get=http_get,
                max_requests=max_index_requests,
            )
    except cooldown.CooldownError as exc:
        return SyncPlan(
            needs_sync=False,
            reason=(
                f"do not adopt Go {target_version}: trusted first-seen lookup "
                f"failed for {GO_TOOLCHAIN_MODULE}@{tc_version}: {exc}"
            ),
            current_version=current_version,
            target_version=target_version,
            cross_minor=cross_minor,
            open_pr=open_pr,
        )

    if not cooldown.is_aged(toolchain_first_seen, cooldown_days, now):
        return SyncPlan(
            needs_sync=False,
            reason=(
                f"do not adopt Go {target_version}: toolchain first seen "
                f"{_format_utc(toolchain_first_seen)} has not aged {cooldown_days} days"
            ),
            current_version=current_version,
            target_version=target_version,
            cross_minor=cross_minor,
            toolchain_first_seen=toolchain_first_seen,
            open_pr=open_pr,
        )

    tag = docker_image_tag(target_version)
    try:
        raw_digest, docker_last_pushed = cooldown.docker_tag_info(
            DOCKER_REPOSITORY, tag, http_get=http_get
        )
    except cooldown.CooldownError as exc:
        return SyncPlan(
            needs_sync=False,
            reason=(
                f"do not adopt Go {target_version}: Docker Hub lookup failed "
                f"for {DOCKER_REPOSITORY}:{tag}: {exc}"
            ),
            current_version=current_version,
            target_version=target_version,
            cross_minor=cross_minor,
            toolchain_first_seen=toolchain_first_seen,
            open_pr=open_pr,
        )

    try:
        docker_digest = validate_digest(raw_digest)
    except SyncError as exc:
        return SyncPlan(
            needs_sync=False,
            reason=f"do not adopt Go {target_version}: {exc}",
            current_version=current_version,
            target_version=target_version,
            cross_minor=cross_minor,
            toolchain_first_seen=toolchain_first_seen,
            docker_last_pushed=docker_last_pushed,
            open_pr=open_pr,
        )

    if not cooldown.is_aged(docker_last_pushed, cooldown_days, now):
        return SyncPlan(
            needs_sync=False,
            reason=(
                f"do not adopt Go {target_version}: Docker tag {tag} pushed "
                f"{_format_utc(docker_last_pushed)} has not aged {cooldown_days} days"
            ),
            current_version=current_version,
            target_version=target_version,
            cross_minor=cross_minor,
            docker_digest=docker_digest,
            toolchain_first_seen=toolchain_first_seen,
            docker_last_pushed=docker_last_pushed,
            open_pr=open_pr,
        )

    file_edits = plan_file_edits(repo_files, target_version, docker_digest, cross_minor)
    headline, commit_body, pr_title, pr_body = build_commit_and_pr_text(
        current_version,
        target_version,
        docker_digest,
        cross_minor,
        toolchain_first_seen,
        docker_last_pushed,
    )

    branch_sha = github_client.get_branch_sha(sync_branch)
    if branch_sha is not None and open_pr is not None and open_pr.head_sha == branch_sha:
        branch_commit = github_client.get_commit(branch_sha)
        if (
            commit_matches_check_plan(branch_commit, main_sha, file_edits, cross_minor)
            and open_pr.title == pr_title
            and open_pr.body == pr_body
        ):
            return SyncPlan(
                needs_sync=False,
                reason=(
                    f"sync branch {sync_branch} and open PR #{open_pr.number} "
                    f"are already up to date for Go {target_version}"
                ),
                current_version=current_version,
                target_version=target_version,
                cross_minor=cross_minor,
                docker_digest=docker_digest,
                toolchain_first_seen=toolchain_first_seen,
                docker_last_pushed=docker_last_pushed,
                file_edits=file_edits,
                open_pr=open_pr,
                branch_sha=branch_sha,
                commit_headline=headline,
                commit_body=commit_body,
                pr_title=pr_title,
                pr_body=pr_body,
            )

    action_desc = (
        f"replay open PR #{open_pr.number} onto {main_sha[:12]} for Go {target_version}"
        if open_pr is not None
        else f"create sync PR for Go {current_version} -> {target_version}"
    )
    return SyncPlan(
        needs_sync=True,
        reason=action_desc,
        current_version=current_version,
        target_version=target_version,
        cross_minor=cross_minor,
        docker_digest=docker_digest,
        toolchain_first_seen=toolchain_first_seen,
        docker_last_pushed=docker_last_pushed,
        file_edits=file_edits,
        open_pr=open_pr,
        branch_sha=branch_sha,
        expected_pr_number=expected_pr_number,
        commit_headline=headline,
        commit_body=commit_body,
        pr_title=pr_title,
        pr_body=pr_body,
    )


def default_tidy_runner(workdir: Path, target_version: GoVersion) -> None:
    """Run ``go mod tidy`` for root and ``tools`` using the exact target Go."""
    env = {**os.environ, "GOTOOLCHAIN": "local"}
    ver_proc = subprocess.run(
        ["go", "env", "GOVERSION"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if ver_proc.returncode != 0:
        raise SyncError(f"go env GOVERSION failed: {ver_proc.stderr.strip()}")
    actual = ver_proc.stdout.strip()
    expected = f"go{target_version}"
    if actual != expected:
        raise SyncError(
            f"cross-minor tidy requires {expected} on PATH, found {actual!r}"
        )

    for rel_dir in (".", "tools"):
        proc = subprocess.run(
            ["go", "mod", "tidy"],
            cwd=workdir / rel_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise SyncError(
                f"go mod tidy failed in {rel_dir}: {proc.stderr.strip() or proc.stdout.strip()}"
            )


def read_repo_sync_files(repo_root: Path) -> dict[str, bytes]:
    """Read all files inspected or modified by Go toolchain sync."""
    result: dict[str, bytes] = {}
    for rel in (GO_VERSION_FILE, *DOCKERFILES, *GO_MOD_AND_SUMS):
        path = repo_root / rel
        if not path.is_file():
            raise SyncError(f"required file not found: {path}")
        result[rel] = path.read_bytes()
    return result


def prepare_sync_bundle(
    *,
    repo_root: Path,
    main_sha: str,
    target_version: GoVersion,
    docker_digest: str,
    toolchain_first_seen: datetime,
    docker_last_pushed: datetime,
    expected_pr_number: int | None = None,
    tidy_runner: TidyRunner = default_tidy_runner,
) -> SyncBundle:
    """Prepare the exact LF-normalized file changes from a trusted ``main`` checkout.

    Runs ``go mod tidy`` in an isolated temporary copy for cross-minor upgrades
    so the caller's checkout is never mutated and no App token is present.
    ``expected_pr_number`` binds a push-triggered replay to the PR whose
    ``check`` produced it.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", main_sha):
        raise SyncError(f"main_sha must be a 40-char hex SHA, got {main_sha!r}")

    repo_files = read_repo_sync_files(repo_root)
    current_version = parse_go_version(repo_files[GO_VERSION_FILE].decode("utf-8"))
    if target_version <= current_version:
        raise SyncError(
            f"target Go version {target_version} is not newer than current {current_version}"
        )

    cross_minor = target_version.series != current_version.series
    validated_digest = validate_digest(docker_digest)
    planned = plan_file_edits(repo_files, target_version, validated_digest, cross_minor)

    final_files: dict[str, bytes] = dict(planned)
    if cross_minor:
        with tempfile.TemporaryDirectory(prefix="go-toolchain-tidy-") as tmpdir:
            work_repo = Path(tmpdir) / "repo"
            shutil.copytree(
                repo_root,
                work_repo,
                ignore=shutil.ignore_patterns(".git", ".tools", "dist"),
            )
            for rel, content in planned.items():
                (work_repo / rel).write_bytes(content)

            tidy_runner(work_repo, target_version)

            for rel in GO_MOD_AND_SUMS:
                new_text = normalize_lf((work_repo / rel).read_text(encoding="utf-8"))
                orig_text = normalize_lf(repo_files[rel].decode("utf-8"))
                new_bytes = new_text.encode("utf-8")
                if new_bytes != orig_text.encode("utf-8"):
                    final_files[rel] = new_bytes
                else:
                    final_files.pop(rel, None)

    allowed = allowed_paths(cross_minor)
    unexpected = set(final_files) - allowed
    if unexpected:
        raise SyncError(f"unexpected files in sync bundle: {sorted(unexpected)}")
    if not final_files:
        raise SyncError("sync bundle has no file changes")

    merged_for_validation: dict[str, bytes] = {
        rel: normalize_lf(repo_files[rel].decode("utf-8")).encode("utf-8")
        for rel in (GO_VERSION_FILE, *DOCKERFILES, *GO_MODS)
    }
    for rel, content in final_files.items():
        if rel in merged_for_validation:
            merged_for_validation[rel] = content
    validate_go_version_invariants(merged_for_validation)

    headline, commit_body, pr_title, pr_body = build_commit_and_pr_text(
        current_version,
        target_version,
        validated_digest,
        cross_minor,
        toolchain_first_seen,
        docker_last_pushed,
    )
    return SyncBundle(
        main_sha=main_sha,
        current_version=current_version,
        target_version=target_version,
        cross_minor=cross_minor,
        docker_digest=validated_digest,
        toolchain_first_seen=toolchain_first_seen,
        docker_last_pushed=docker_last_pushed,
        commit_headline=headline,
        commit_body=commit_body,
        pr_title=pr_title,
        pr_body=pr_body,
        files=final_files,
        expected_pr_number=validate_optional_pr_number(expected_pr_number),
    )


def publish_sync_bundle(
    bundle: SyncBundle,
    github_client: GitHubClient,
    *,
    base_branch: str = DEFAULT_BASE_BRANCH,
    sync_branch: str = SYNC_BRANCH,
    updater_login: str = DEFAULT_UPDATER_LOGIN,
    tmp_branch_factory: Callable[[], str] | None = None,
    dry_run: bool = False,
) -> PublishResult:
    """Publish a prepared bundle as a single signed commit and create/update PR.

    Safe replacement protocol for an existing branch:
      1. Create a temporary ref rooted at ``bundle.main_sha``.
      2. Call GraphQL ``createCommitOnBranch`` on that temporary ref with
         ``expectedHeadOid == bundle.main_sha`` to obtain a single GitHub-signed
         commit.
      3. Atomically move ``sync_branch`` from its observed tip to the new commit
         via ``compare_and_swap_ref`` (``--force-with-lease``), or create the
         ref atomically if absent. The open PR is never emptied.
      4. Always delete the temporary ref in ``finally``.

    Idempotency & retry safety:
      - If ``sync_branch`` already points to a verified commit whose sole parent
        is ``bundle.main_sha`` and whose changed files match ``bundle.files``
        exactly, reuse that commit without pushing again.
      - If the branch was already published on an earlier run that failed before
        PR creation, only the missing PR is created.
      - A push-triggered bundle that names ``expected_pr_number`` is only
        published while that exact PR is still open; otherwise nothing is
        written (a maintainer who closed the PR is never overridden).
    """
    allowed = allowed_paths(bundle.cross_minor)
    unexpected = set(bundle.files) - allowed
    if unexpected:
        raise SyncError(f"refusing to publish disallowed paths: {sorted(unexpected)}")
    if not bundle.files:
        raise SyncError("refusing to publish empty sync bundle")

    live_main_sha = github_client.get_branch_sha(base_branch)
    if live_main_sha != bundle.main_sha:
        raise SyncError(
            f"{base_branch} moved during sync: expected {bundle.main_sha}, "
            f"found {live_main_sha}"
        )

    observed_branch_sha = github_client.get_branch_sha(sync_branch)
    open_pr = github_client.find_open_sync_pr(sync_branch, base_branch, updater_login)

    # Enforce the PR binding captured by a push-triggered check before any
    # write: if the maintainer closed the PR (or a different one appeared),
    # stop instead of silently recreating it.
    if bundle.expected_pr_number is not None:
        if open_pr is None:
            raise SyncError(
                f"expected open sync PR #{bundle.expected_pr_number} from the "
                f"check phase, but no open sync PR exists; refusing to recreate it"
            )
        if open_pr.number != bundle.expected_pr_number:
            raise SyncError(
                f"expected open sync PR #{bundle.expected_pr_number} from the "
                f"check phase, found #{open_pr.number}; refusing to publish"
            )

    reused_commit = False
    if observed_branch_sha is not None:
        existing_commit = github_client.get_commit(observed_branch_sha)
        reused_commit = commit_matches_plan(
            existing_commit, bundle.main_sha, bundle.files
        )

    if dry_run:
        pr_action = "create" if open_pr is None else (
            "update"
            if (open_pr.title != bundle.pr_title or open_pr.body != bundle.pr_body)
            else "unchanged"
        )
        return PublishResult(
            commit_sha=observed_branch_sha or "",
            reused_commit=reused_commit,
            pr_number=open_pr.number if open_pr is not None else None,
            pr_url=open_pr.html_url if open_pr is not None else "",
            pr_action=f"dry-run:{pr_action}",
        )

    if reused_commit and observed_branch_sha is not None:
        commit_sha = observed_branch_sha
    else:
        tmp_branch = (
            tmp_branch_factory()
            if tmp_branch_factory is not None
            else f"{sync_branch}-tmp-{secrets.token_hex(6)}"
        )
        github_client.create_ref(tmp_branch, bundle.main_sha)
        try:
            commit_sha = github_client.create_commit_on_branch(
                branch=tmp_branch,
                expected_head_oid=bundle.main_sha,
                headline=bundle.commit_headline,
                body=bundle.commit_body,
                additions=bundle.files,
            )
            if observed_branch_sha is None:
                github_client.create_ref(sync_branch, commit_sha)
            else:
                github_client.compare_and_swap_ref(
                    sync_branch,
                    expected_old_oid=observed_branch_sha,
                    new_oid=commit_sha,
                    source_branch=tmp_branch,
                )
        finally:
            try:
                github_client.delete_ref(tmp_branch)
            except SyncError as exc:
                # A leftover temporary ref is harmless and a later run uses a
                # new random name; do not mask a successful publish.
                print(
                    f"go-toolchain-sync: warning: could not delete temporary "
                    f"ref {tmp_branch}: {exc}",
                    file=sys.stderr,
                )

    if open_pr is None:
        pr = github_client.create_pull_request(
            head=sync_branch,
            base=base_branch,
            title=bundle.pr_title,
            body=bundle.pr_body,
        )
        pr_action = "created"
    elif open_pr.title != bundle.pr_title or open_pr.body != bundle.pr_body:
        pr = github_client.update_pull_request(
            number=open_pr.number,
            title=bundle.pr_title,
            body=bundle.pr_body,
        )
        pr_action = "updated"
    else:
        pr = open_pr
        pr_action = "unchanged"

    return PublishResult(
        commit_sha=commit_sha,
        reused_commit=reused_commit,
        pr_number=pr.number,
        pr_url=pr.html_url,
        pr_action=pr_action,
    )


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects to another origin so the bearer token is never forwarded."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if (new.scheme, new.netloc.lower()) != (old.scheme, old.netloc.lower()):
            raise SyncError(
                f"refusing cross-origin redirect from {old.netloc} to {new.netloc}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_GITHUB_OPENER = urllib.request.build_opener(_SameOriginRedirectHandler())


class UrllibGitHubClient:
    """Standard-library GitHub REST/GraphQL + ephemeral git CAS client."""

    def __init__(
        self,
        repository: str,
        token: str | None = None,
        *,
        api_url: str = "https://api.github.com",
        server_url: str = "https://github.com",
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if "/" not in repository or len(repository.split("/")) != 2:
            raise SyncError(f"repository must be 'owner/repo', got {repository!r}")
        self.repository = repository
        self.owner, self.repo = repository.split("/", 1)
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.server_url = server_url.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": DEFAULT_USER_AGENT,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        allow_404: bool = False,
        allow_422: bool = False,
    ) -> Any:
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = f"{self.api_url}{path}"
        data: bytes | None = None
        headers = self._headers()
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with _GITHUB_OPENER.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if allow_404 and exc.code == 404:
                return None
            if allow_422 and exc.code == 422:
                return None
            detail = exc.read().decode("utf-8", errors="replace")
            raise SyncError(
                f"GitHub API {method} {path} failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise SyncError(f"GitHub API {method} {path} network error: {exc}") from exc

    def get_branch_sha(self, branch: str) -> str | None:
        encoded = urllib.parse.quote(branch, safe="/")
        data = self._request_json(
            "GET",
            f"/repos/{self.repository}/git/ref/heads/{encoded}",
            allow_404=True,
        )
        if data is None:
            return None
        obj = data.get("object") if isinstance(data, dict) else None
        sha = obj.get("sha") if isinstance(obj, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise SyncError(f"unexpected ref payload for branch {branch!r}: {data!r}")
        return sha

    def get_commit(self, sha: str) -> BranchCommit:
        data = self._request_json("GET", f"/repos/{self.repository}/commits/{sha}")
        if not isinstance(data, dict):
            raise SyncError(f"unexpected commit payload for {sha}")
        raw_parents = data.get("parents")
        if not isinstance(raw_parents, list):
            raise SyncError(f"missing parents in commit {sha}")
        parents = tuple(
            p["sha"]
            for p in raw_parents
            if isinstance(p, dict) and isinstance(p.get("sha"), str)
        )
        verification = (data.get("commit") or {}).get("verification") or {}
        verified = verification.get("verified") is True
        files: dict[str, str] = {}
        removed: list[str] = []
        raw_files = data.get("files") or []
        if not isinstance(raw_files, list):
            raise SyncError(f"unexpected files field in commit {sha}")
        for entry in raw_files:
            if not isinstance(entry, dict):
                continue
            filename = entry.get("filename")
            status = entry.get("status")
            blob_sha = entry.get("sha")
            if not isinstance(filename, str):
                continue
            if status == "removed":
                removed.append(filename)
            elif isinstance(blob_sha, str):
                files[filename] = blob_sha
        return BranchCommit(
            sha=sha,
            parents=parents,
            verified=verified,
            files=files,
            removed_files=tuple(removed),
        )

    def find_open_sync_pr(
        self, branch: str, base: str, updater_login: str
    ) -> OpenPR | None:
        query = urllib.parse.urlencode(
            {
                "state": "open",
                "head": f"{self.owner}:{branch}",
                "per_page": "100",
            }
        )
        data = self._request_json("GET", f"/repos/{self.repository}/pulls?{query}")
        if not isinstance(data, list):
            raise SyncError("unexpected pulls list payload")
        if not data:
            return None
        if len(data) != 1:
            raise SyncError(f"multiple open PRs for managed branch {branch}")

        match data[0]:
            case {
                "state": "open",
                "number": int(number),
                "head": {"ref": str(head_ref), "sha": str(sha), "repo": {"full_name": str(repo)}},
                "base": {"ref": str(base_ref)},
                "user": {"login": str(author)},
                "title": str(title),
                "body": (str() | None) as body,
                "html_url": str(url),
            }:
                if isinstance(number, bool) or number < 1 or not re.fullmatch(r"[0-9a-f]{40}", sha) or not url:
                    raise SyncError(f"malformed open pull request for managed branch {branch}")
                if repo.lower() != self.repository.lower() or head_ref != branch:
                    raise SyncError(f"unexpected pull request head for managed branch {branch}")
                if base_ref != base or author != updater_login:
                    raise SyncError(f"unexpected pull request base or author for managed branch {branch}")
                return OpenPR(
                    number=number,
                    head_sha=sha,
                    head_ref=head_ref,
                    head_repo_full_name=repo,
                    base_ref=base_ref,
                    author_login=author,
                    title=title,
                    body=body or "",
                    html_url=url,
                )
            case _:
                raise SyncError(f"malformed open pull request for managed branch {branch}")

    def create_ref(self, branch: str, sha: str) -> None:
        self._request_json(
            "POST",
            f"/repos/{self.repository}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": sha},
        )

    def delete_ref(self, branch: str) -> None:
        encoded = urllib.parse.quote(branch, safe="/")
        self._request_json(
            "DELETE",
            f"/repos/{self.repository}/git/refs/heads/{encoded}",
            allow_404=True,
            allow_422=True,
        )

    def create_commit_on_branch(
        self,
        branch: str,
        expected_head_oid: str,
        headline: str,
        body: str,
        additions: Mapping[str, bytes],
    ) -> str:
        variables = {
            "input": {
                "branch": {
                    "repositoryNameWithOwner": self.repository,
                    "branchName": branch,
                },
                "expectedHeadOid": expected_head_oid,
                "message": {
                    "headline": headline,
                    "body": body,
                },
                "fileChanges": {
                    "additions": [
                        {
                            "path": path,
                            "contents": base64.b64encode(content).decode("ascii"),
                        }
                        for path, content in sorted(additions.items())
                    ]
                },
            }
        }
        data = self._request_json(
            "POST",
            "/graphql",
            {"query": CREATE_COMMIT_ON_BRANCH_MUTATION, "variables": variables},
        )
        if not isinstance(data, dict):
            raise SyncError("unexpected GraphQL response")
        if data.get("errors"):
            raise SyncError(f"GraphQL createCommitOnBranch failed: {data['errors']}")
        oid = (
            ((data.get("data") or {}).get("createCommitOnBranch") or {})
            .get("commit", {})
            .get("oid")
        )
        if not isinstance(oid, str) or not re.fullmatch(r"[0-9a-f]{40}", oid):
            raise SyncError(f"GraphQL createCommitOnBranch returned invalid oid: {data!r}")
        return oid

    def compare_and_swap_ref(
        self,
        branch: str,
        expected_old_oid: str,
        new_oid: str,
        source_branch: str,
    ) -> None:
        if not self.token:
            raise SyncError("compare_and_swap_ref requires an authenticated GitHub token")
        auth = base64.b64encode(f"x-access-token:{self.token}".encode("utf-8")).decode("ascii")
        remote_url = f"{self.server_url}/{self.repository}.git"
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{self.server_url}/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {auth}",
        }
        with tempfile.TemporaryDirectory(prefix="go-toolchain-cas-") as tmpdir:
            init_proc = subprocess.run(
                ["git", "init", "--bare", "--quiet", tmpdir],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if init_proc.returncode != 0:
                raise SyncError(f"git init --bare failed: {init_proc.stderr.strip()}")

            fetch_proc = subprocess.run(
                [
                    "git",
                    "--git-dir",
                    tmpdir,
                    "fetch",
                    "--quiet",
                    "--depth=1",
                    remote_url,
                    f"refs/heads/{source_branch}",
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if fetch_proc.returncode != 0:
                raise SyncError(
                    f"git fetch of staging branch failed: {fetch_proc.stderr.strip()}"
                )

            rev_proc = subprocess.run(
                ["git", "--git-dir", tmpdir, "rev-parse", "FETCH_HEAD"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            fetched_oid = rev_proc.stdout.strip()
            if rev_proc.returncode != 0 or fetched_oid != new_oid:
                raise SyncError(
                    f"staging ref mismatch: expected {new_oid}, fetched {fetched_oid!r}"
                )

            push_proc = subprocess.run(
                [
                    "git",
                    "--git-dir",
                    tmpdir,
                    "push",
                    f"--force-with-lease=refs/heads/{branch}:{expected_old_oid}",
                    remote_url,
                    f"{new_oid}:refs/heads/{branch}",
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if push_proc.returncode != 0:
                raise SyncError(
                    f"git push --force-with-lease failed for {branch}: "
                    f"{push_proc.stderr.strip() or push_proc.stdout.strip()}"
                )

    def create_pull_request(
        self, head: str, base: str, title: str, body: str
    ) -> OpenPR:
        data = self._request_json(
            "POST",
            f"/repos/{self.repository}/pulls",
            {"title": title, "body": body, "head": head, "base": base},
        )
        return _parse_pr_response(data, self.repository)

    def update_pull_request(
        self, number: int, title: str, body: str
    ) -> OpenPR:
        data = self._request_json(
            "PATCH",
            f"/repos/{self.repository}/pulls/{number}",
            {"title": title, "body": body},
        )
        return _parse_pr_response(data, self.repository)


def _parse_pr_response(data: Any, fallback_repo: str) -> OpenPR:
    if not isinstance(data, dict) or "number" not in data:
        raise SyncError(f"unexpected pull request payload: {data!r}")
    head = data.get("head") or {}
    base = data.get("base") or {}
    head_repo = head.get("repo") or {}
    user = data.get("user") or {}
    return OpenPR(
        number=int(data["number"]),
        head_sha=str(head.get("sha", "")),
        head_ref=str(head.get("ref", "")),
        head_repo_full_name=str(head_repo.get("full_name", fallback_repo)),
        base_ref=str(base.get("ref", "")),
        author_login=str(user.get("login", "")),
        title=str(data.get("title", "")),
        body=str(data.get("body") or ""),
        html_url=str(data.get("html_url", "")),
    )


def infer_repository(repo_root: Path) -> str:
    env_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if env_repo:
        return env_repo
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SyncError("cannot infer repository; pass --repository owner/repo")
    url = proc.stdout.strip()
    match = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?$", url)
    if not match:
        raise SyncError(f"cannot parse GitHub owner/repo from remote {url!r}")
    return match.group(1)


def infer_head_sha(repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SyncError(f"cannot resolve HEAD SHA in {repo_root}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def write_github_outputs(path: Path, outputs: Mapping[str, str]) -> None:
    lines: list[str] = []
    for key, value in outputs.items():
        if "\n" in value or "\r" in value:
            raise SyncError(f"GITHUB_OUTPUT value for {key!r} must be single-line")
        lines.append(f"{key}={value}\n")
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.writelines(lines)


def format_dry_run_report(
    plan: SyncPlan,
    repo_files: Mapping[str, bytes],
    *,
    repository: str,
    event_name: str,
    main_sha: str,
) -> str:
    """Format a human-readable dry-run report with unified diffs."""
    first_seen = (
        _format_utc(plan.toolchain_first_seen)
        if plan.toolchain_first_seen
        else "(unavailable)"
    )
    last_pushed = (
        _format_utc(plan.docker_last_pushed)
        if plan.docker_last_pushed
        else "(unavailable)"
    )
    open_pr_line = (
        f"open_pr:              #{plan.open_pr.number}"
        if plan.open_pr
        else "open_pr:              (none)"
    )
    lines = [
        f"repository:           {repository}",
        f"event_name:           {event_name}",
        f"main_sha:             {main_sha}",
        f"current_version:      {plan.current_version}",
        f"target_version:       {plan.target_version or '(none)'}",
        f"cross_minor:          {str(plan.cross_minor).lower()}",
        f"toolchain_first_seen: {first_seen}",
        f"docker_digest:        {plan.docker_digest or '(unavailable)'}",
        f"docker_last_pushed:   {last_pushed}",
        open_pr_line,
        f"expected_pr_number:   {plan.expected_pr_number or '(not bound)'}",
        f"needs_sync:           {str(plan.needs_sync).lower()}",
        f"reason:               {plan.reason}",
    ]
    if plan.file_edits:
        lines.append("")
        lines.append("Planned static file edits:")
        for rel in sorted(plan.file_edits):
            orig = normalize_lf(repo_files[rel].decode("utf-8")).splitlines(
                keepends=True
            )
            new = plan.file_edits[rel].decode("utf-8").splitlines(keepends=True)
            diff = difflib.unified_diff(
                orig, new, fromfile=f"a/{rel}", tofile=f"b/{rel}"
            )
            lines.extend(line.rstrip("\n") for line in diff)
        if plan.cross_minor:
            lines.append("")
            lines.append(
                "Note: cross-minor upgrade will also run `go mod tidy` in "
                "`.` and `tools/`."
            )
    return "\n".join(lines)


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _cmd_check(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    repository = args.repository or infer_repository(repo_root)
    main_sha = args.main_sha or infer_head_sha(repo_root)
    token = os.environ.get("GITHUB_TOKEN") or None
    now = _parse_iso_utc(args.now) if args.now else datetime.now(timezone.utc)

    repo_files = read_repo_sync_files(repo_root)
    client = UrllibGitHubClient(repository, token=token)
    plan = plan_sync(
        repo_files=repo_files,
        event_name=args.event_name,
        main_sha=main_sha,
        now=now,
        github_client=client,
        cooldown_days=args.cooldown_days,
        base_branch=args.base_branch,
        sync_branch=args.sync_branch,
        updater_login=args.updater_login,
        max_index_requests=args.max_index_requests,
    )

    if args.dry_run:
        print(
            format_dry_run_report(
                plan,
                repo_files,
                repository=repository,
                event_name=args.event_name,
                main_sha=main_sha,
            )
        )
    else:
        summary = {
            "needs_sync": plan.needs_sync,
            "reason": plan.reason,
            "main_sha": main_sha,
            "current_version": str(plan.current_version),
            "target_version": str(plan.target_version) if plan.target_version else "",
            "cross_minor": plan.cross_minor,
            "docker_digest": plan.docker_digest or "",
            "toolchain_first_seen": (
                _format_utc(plan.toolchain_first_seen) if plan.toolchain_first_seen else ""
            ),
            "docker_last_pushed": (
                _format_utc(plan.docker_last_pushed) if plan.docker_last_pushed else ""
            ),
            "expected_pr_number": plan.expected_pr_number or "",
        }
        print(json.dumps(summary, indent=2, sort_keys=True))

    if args.github_output:
        outputs = {
            "needs_sync": "true" if plan.needs_sync else "false",
            "main_sha": main_sha,
            "current_version": str(plan.current_version),
            "target_version": str(plan.target_version) if plan.target_version else "",
            "cross_minor": "true" if plan.cross_minor else "false",
            "docker_digest": plan.docker_digest or "",
            "toolchain_first_seen": (
                _format_utc(plan.toolchain_first_seen) if plan.toolchain_first_seen else ""
            ),
            "docker_last_pushed": (
                _format_utc(plan.docker_last_pushed) if plan.docker_last_pushed else ""
            ),
            "expected_pr_number": (
                str(plan.expected_pr_number) if plan.expected_pr_number else ""
            ),
        }
        write_github_outputs(Path(args.github_output), outputs)

    return 0


def _cmd_prepare(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    bundle = prepare_sync_bundle(
        repo_root=repo_root,
        main_sha=args.main_sha,
        target_version=parse_go_version(args.target_version),
        docker_digest=validate_digest(args.docker_digest),
        toolchain_first_seen=_parse_iso_utc(args.toolchain_first_seen),
        docker_last_pushed=_parse_iso_utc(args.docker_last_pushed),
        expected_pr_number=parse_optional_pr_number_arg(args.expected_pr_number),
    )
    out_path = Path(args.output_bundle)
    out_path.write_text(
        json.dumps(bundle.to_json_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"prepared sync bundle for Go {bundle.current_version} -> {bundle.target_version} "
        f"({len(bundle.files)} file(s)): {', '.join(sorted(bundle.files))}"
    )
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    bundle_data = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
    bundle = SyncBundle.from_json_dict(bundle_data)
    token = os.environ.get("GITHUB_TOKEN") or None
    if not token and not args.dry_run:
        raise SyncError("GITHUB_TOKEN is required for publish")
    client = UrllibGitHubClient(args.repository, token=token)
    result = publish_sync_bundle(
        bundle,
        client,
        base_branch=args.base_branch,
        sync_branch=args.sync_branch,
        updater_login=args.updater_login,
        dry_run=args.dry_run,
    )
    print(
        json.dumps(
            {
                "commit_sha": result.commit_sha,
                "reused_commit": result.reused_commit,
                "pr_number": result.pr_number,
                "pr_url": result.pr_url,
                "pr_action": result.pr_action,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synchronize Go toolchain version and Docker base images."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Shorthand for `check --dry-run` against the local checkout.",
    )
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="Read-only check and sync planning.")
    p_check.add_argument("--repo-root", default=str(_default_repo_root()))
    p_check.add_argument("--repository", default="")
    p_check.add_argument("--event-name", default="workflow_dispatch")
    p_check.add_argument("--main-sha", default="")
    p_check.add_argument("--base-branch", default=DEFAULT_BASE_BRANCH)
    p_check.add_argument("--sync-branch", default=SYNC_BRANCH)
    p_check.add_argument("--updater-login", default=DEFAULT_UPDATER_LOGIN)
    p_check.add_argument("--cooldown-days", type=int, default=COOLDOWN_DAYS)
    p_check.add_argument("--max-index-requests", type=int, default=None)
    p_check.add_argument("--now", default="")
    p_check.add_argument("--github-output", default="")
    p_check.add_argument("--dry-run", action="store_true")

    p_prepare = sub.add_parser(
        "prepare", help="Apply planned edits and write a sync bundle JSON."
    )
    p_prepare.add_argument("--repo-root", default=str(_default_repo_root()))
    p_prepare.add_argument("--main-sha", required=True)
    p_prepare.add_argument("--target-version", required=True)
    p_prepare.add_argument("--docker-digest", required=True)
    p_prepare.add_argument("--toolchain-first-seen", required=True)
    p_prepare.add_argument("--docker-last-pushed", required=True)
    p_prepare.add_argument("--expected-pr-number", default="")
    p_prepare.add_argument("--output-bundle", required=True)

    p_publish = sub.add_parser(
        "publish", help="Publish a prepared sync bundle via GitHub API."
    )
    p_publish.add_argument("--bundle", required=True)
    p_publish.add_argument("--repository", required=True)
    p_publish.add_argument("--base-branch", default=DEFAULT_BASE_BRANCH)
    p_publish.add_argument("--sync-branch", default=SYNC_BRANCH)
    p_publish.add_argument("--updater-login", default=DEFAULT_UPDATER_LOGIN)
    p_publish.add_argument("--dry-run", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        if args.dry_run:
            args = parser.parse_args(["check", "--dry-run"])
        else:
            parser.print_help(sys.stderr)
            return 2
    try:
        if args.command == "check":
            return _cmd_check(args)
        if args.command == "prepare":
            return _cmd_prepare(args)
        if args.command == "publish":
            return _cmd_publish(args)
    except SyncError as exc:
        print(f"go-toolchain-sync: ERROR: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
