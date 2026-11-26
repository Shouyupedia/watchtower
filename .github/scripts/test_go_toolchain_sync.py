"""Unit tests for .github/scripts/go_toolchain_sync.py.

Run from the repository root with:

    python -I -m unittest discover -s .github/scripts -p "test_*.py" -v
"""

from __future__ import annotations

import base64
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
import email.message
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.parse
import urllib.request

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import cooldown  # noqa: E402
import go_toolchain_sync as gts  # noqa: E402

REPO_ROOT = _SCRIPTS_DIR.parent.parent
CHECK_GO_VERSION_SCRIPT = REPO_ROOT / "scripts" / "check-go-version.py"

_cgv_spec = importlib.util.spec_from_file_location(
    "check_go_version_under_test", CHECK_GO_VERSION_SCRIPT
)
assert _cgv_spec is not None and _cgv_spec.loader is not None
_cgv = importlib.util.module_from_spec(_cgv_spec)
_cgv_spec.loader.exec_module(_cgv)

# --- deterministic fixtures -------------------------------------------------

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
AGED_8D = NOW - timedelta(days=8)
AGED_EXACT_7D = NOW - timedelta(days=7)
AGED_SHORT_1S = NOW - timedelta(days=7) + timedelta(seconds=1)

MAIN_SHA = "a" * 40
OLD_MAIN_SHA = "b" * 40
NEW_MAIN_SHA = "c" * 40
BRANCH_SHA = "d" * 40
NEW_COMMIT_SHA = "e" * 40

OLD_DIGEST = "sha256:" + ("1" * 64)
NEW_DIGEST = "sha256:" + ("2" * 64)

CURRENT_VER = "1.26.11"
SAME_MINOR_TARGET = "1.26.12"
CROSS_MINOR_TARGET = "1.27.1"


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_repo_files(
    version: str = CURRENT_VER,
    digest: str = OLD_DIGEST,
    go_mod_directive: str = "1.26.0",
    tools_go_mod_directive: str = "1.26.4",
    *,
    crlf: bool = False,
) -> dict[str, bytes]:
    nl = "\r\n" if crlf else "\n"
    return {
        ".go-version": f"{version}{nl}".encode("utf-8"),
        "dockerfiles/Dockerfile": (
            f"FROM --platform=$BUILDPLATFORM golang:{version}-alpine@{digest} AS builder{nl}"
            f"RUN go build ./...{nl}"
            f"FROM scratch{nl}"
        ).encode("utf-8"),
        "dockerfiles/Dockerfile.self-contained": (
            f"FROM golang:{version}-alpine@{digest} AS build{nl}"
            f"RUN go build -o /out/app .{nl}"
        ).encode("utf-8"),
        "dockerfiles/Dockerfile.dev-self-contained": (
            f"FROM golang:{version}-alpine@{digest}{nl}"
            f"RUN echo dev{nl}"
        ).encode("utf-8"),
        "go.mod": f"module example.com/watchtower{nl}{nl}go {go_mod_directive}{nl}".encode(
            "utf-8"
        ),
        "go.sum": f"example.com/dep v1.0.0 h1:abc={nl}".encode("utf-8"),
        "tools/go.mod": f"module example.com/watchtower/tools{nl}{nl}go {tools_go_mod_directive}{nl}".encode(
            "utf-8"
        ),
        "tools/go.sum": f"example.com/tool v0.1.0 h1:xyz={nl}".encode("utf-8"),
        "tools/actionlint/go.mod": f"module example.com/watchtower/tools/actionlint{nl}{nl}go {tools_go_mod_directive}{nl}".encode(
            "utf-8"
        ),
        "tools/actionlint/go.sum": f"example.com/lint v0.1.0 h1:xyz={nl}".encode("utf-8"),
    }


def write_repo_tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def make_http_get(
    *,
    go_dl_payload: bytes | Exception | None = None,
    target_version: str = SAME_MINOR_TARGET,
    proxy_hint: datetime | Exception | None = AGED_8D,
    index_first_seen: datetime | Exception | None = AGED_8D,
    docker_digest: str = NEW_DIGEST,
    docker_last_pushed: datetime | Exception | None = AGED_8D,
    docker_raw: bytes | Exception | None = None,
    recorded_urls: list[str] | None = None,
) -> cooldown.HttpGet:
    tc_ver = f"v0.0.1-go{target_version}.linux-amd64"
    tag = f"{target_version}-alpine"
    proxy_url = (
        f"{cooldown.PROXY_URL}/{gts.GO_TOOLCHAIN_MODULE}/@v/{tc_ver}.info"
    )
    docker_url = (
        f"{cooldown.DOCKER_HUB_URL}/{gts.DOCKER_REPOSITORY}/tags/{tag}"
    )

    if go_dl_payload is None:
        go_dl_payload = json.dumps(
            [
                {"version": f"go{target_version}", "stable": True},
                {"version": f"go{CURRENT_VER}", "stable": True},
            ]
        ).encode("utf-8")

    def _get(url: str) -> bytes:
        if recorded_urls is not None:
            recorded_urls.append(url)
        if url == gts.GO_DL_URL:
            if isinstance(go_dl_payload, Exception):
                raise go_dl_payload
            return go_dl_payload
        if url == proxy_url:
            if isinstance(proxy_hint, Exception):
                raise proxy_hint
            if proxy_hint is None:
                raise cooldown.CooldownError("404 proxy .info not found")
            return json.dumps(
                {"Version": tc_ver, "Time": _fmt_utc(proxy_hint)}
            ).encode("utf-8")
        if url.startswith(f"{cooldown.INDEX_URL}?"):
            if isinstance(index_first_seen, Exception):
                raise index_first_seen
            if index_first_seen is None:
                return b""
            entry = {
                "Path": gts.GO_TOOLCHAIN_MODULE,
                "Version": tc_ver,
                "Timestamp": _fmt_utc(index_first_seen),
            }
            return (json.dumps(entry) + "\n").encode("utf-8")
        if url == docker_url:
            if docker_raw is not None:
                if isinstance(docker_raw, Exception):
                    raise docker_raw
                return docker_raw
            if isinstance(docker_last_pushed, Exception):
                raise docker_last_pushed
            if docker_last_pushed is None:
                raise cooldown.CooldownError("404 docker tag not found")
            return json.dumps(
                {
                    "name": tag,
                    "digest": docker_digest,
                    "tag_last_pushed": _fmt_utc(docker_last_pushed),
                }
            ).encode("utf-8")
        raise AssertionError(f"unexpected HTTP URL in test: {url}")

    return _get


class FakeGitHubClient:
    """In-memory GitHubClient recording every write call in exact order."""

    def __init__(
        self,
        *,
        branches: dict[str, str] | None = None,
        commits: dict[str, gts.BranchCommit] | None = None,
        open_pr: gts.OpenPR | None = None,
        next_commit_sha: str = NEW_COMMIT_SHA,
        fail_create_commit: Exception | None = None,
        fail_cas: Exception | None = None,
        fail_create_ref_for: set[str] | None = None,
        fail_delete_ref: Exception | None = None,
    ) -> None:
        self.branches: dict[str, str] = dict(branches or {"main": MAIN_SHA})
        self.commits: dict[str, gts.BranchCommit] = dict(commits or {})
        self.open_pr = open_pr
        self.next_commit_sha = next_commit_sha
        self.fail_create_commit = fail_create_commit
        self.fail_cas = fail_cas
        self.fail_create_ref_for = set(fail_create_ref_for or ())
        self.fail_delete_ref = fail_delete_ref
        self.writes: list[tuple[str, dict[str, object]]] = []

    def get_branch_sha(self, branch: str) -> str | None:
        return self.branches.get(branch)

    def get_commit(self, sha: str) -> gts.BranchCommit:
        if sha not in self.commits:
            raise AssertionError(f"unknown commit sha {sha}")
        return self.commits[sha]

    def find_open_sync_pr(
        self, branch: str, base: str, updater_login: str
    ) -> gts.OpenPR | None:
        pr = self.open_pr
        if pr is None:
            return None
        if (
            pr.head_ref == branch
            and pr.base_ref == base
            and pr.author_login == updater_login
        ):
            return pr
        return None

    def create_ref(self, branch: str, sha: str) -> None:
        self.writes.append(("create_ref", {"branch": branch, "sha": sha}))
        if branch in self.fail_create_ref_for:
            raise gts.SyncError(f"simulated create_ref failure for {branch}")
        if branch in self.branches:
            raise gts.SyncError(f"ref already exists: {branch}")
        self.branches[branch] = sha

    def delete_ref(self, branch: str) -> None:
        self.writes.append(("delete_ref", {"branch": branch}))
        if self.fail_delete_ref is not None:
            raise self.fail_delete_ref
        self.branches.pop(branch, None)

    def create_commit_on_branch(
        self,
        branch: str,
        expected_head_oid: str,
        headline: str,
        body: str,
        additions: dict[str, bytes],
    ) -> str:
        self.writes.append(
            (
                "create_commit_on_branch",
                {
                    "branch": branch,
                    "expected_head_oid": expected_head_oid,
                    "headline": headline,
                    "body": body,
                    "additions": dict(additions),
                },
            )
        )
        if self.fail_create_commit is not None:
            raise self.fail_create_commit
        if self.branches.get(branch) != expected_head_oid:
            raise gts.SyncError("expectedHeadOid mismatch")
        sha = self.next_commit_sha
        self.branches[branch] = sha
        self.commits[sha] = gts.BranchCommit(
            sha=sha,
            parents=(expected_head_oid,),
            verified=True,
            files={p: gts.git_blob_sha(c) for p, c in additions.items()},
        )
        return sha

    def compare_and_swap_ref(
        self,
        branch: str,
        expected_old_oid: str,
        new_oid: str,
        source_branch: str,
    ) -> None:
        self.writes.append(
            (
                "compare_and_swap_ref",
                {
                    "branch": branch,
                    "expected_old_oid": expected_old_oid,
                    "new_oid": new_oid,
                    "source_branch": source_branch,
                },
            )
        )
        if self.fail_cas is not None:
            raise self.fail_cas
        if self.branches.get(branch) != expected_old_oid:
            raise gts.SyncError("CAS lease mismatch")
        self.branches[branch] = new_oid

    def create_pull_request(
        self, head: str, base: str, title: str, body: str
    ) -> gts.OpenPR:
        self.writes.append(
            (
                "create_pull_request",
                {"head": head, "base": base, "title": title, "body": body},
            )
        )
        pr = gts.OpenPR(
            number=101,
            head_sha=self.branches[head],
            head_ref=head,
            head_repo_full_name="owner/repo",
            base_ref=base,
            author_login=gts.DEFAULT_UPDATER_LOGIN,
            title=title,
            body=body,
            html_url="https://github.com/owner/repo/pull/101",
        )
        self.open_pr = pr
        return pr

    def update_pull_request(
        self, number: int, title: str, body: str
    ) -> gts.OpenPR:
        self.writes.append(
            (
                "update_pull_request",
                {"number": number, "title": title, "body": body},
            )
        )
        assert self.open_pr is not None and self.open_pr.number == number
        pr = gts.OpenPR(
            number=number,
            head_sha=self.branches[self.open_pr.head_ref],
            head_ref=self.open_pr.head_ref,
            head_repo_full_name=self.open_pr.head_repo_full_name,
            base_ref=self.open_pr.base_ref,
            author_login=self.open_pr.author_login,
            title=title,
            body=body,
            html_url=self.open_pr.html_url,
        )
        self.open_pr = pr
        return pr


def make_open_pr(
    *,
    number: int = 42,
    head_sha: str = BRANCH_SHA,
    title: str = "old title",
    body: str = "old body",
) -> gts.OpenPR:
    return gts.OpenPR(
        number=number,
        head_sha=head_sha,
        head_ref=gts.SYNC_BRANCH,
        head_repo_full_name="owner/repo",
        base_ref=gts.DEFAULT_BASE_BRANCH,
        author_login=gts.DEFAULT_UPDATER_LOGIN,
        title=title,
        body=body,
        html_url=f"https://github.com/owner/repo/pull/{number}",
    )


def make_sample_bundle(
    *,
    cross_minor: bool = False,
    main_sha: str = MAIN_SHA,
    expected_pr_number: int | None = None,
    files: dict[str, bytes] | None = None,
) -> gts.SyncBundle:
    current = gts.parse_go_version(CURRENT_VER)
    target = gts.parse_go_version(
        CROSS_MINOR_TARGET if cross_minor else SAME_MINOR_TARGET
    )
    repo_files = make_repo_files()
    planned = (
        files
        if files is not None
        else gts.plan_file_edits(repo_files, target, NEW_DIGEST, cross_minor)
    )
    headline, commit_body, pr_title, pr_body = gts.build_commit_and_pr_text(
        current, target, NEW_DIGEST, cross_minor, AGED_8D, AGED_8D
    )
    return gts.SyncBundle(
        main_sha=main_sha,
        current_version=current,
        target_version=target,
        cross_minor=cross_minor,
        docker_digest=NEW_DIGEST,
        toolchain_first_seen=AGED_8D,
        docker_last_pushed=AGED_8D,
        commit_headline=headline,
        commit_body=commit_body,
        pr_title=pr_title,
        pr_body=pr_body,
        files=planned,
        expected_pr_number=expected_pr_number,
    )


# --- check phase tests ------------------------------------------------------


class SelectLatestStableReleaseTests(unittest.TestCase):
    def test_selects_highest_numeric_stable_release(self) -> None:
        payload = json.dumps(
            [
                {"version": "go1.27rc1", "stable": False},
                {"version": "go1.26.9", "stable": True},
                {"version": "go1.26.12", "stable": True},
                {"version": "go1.25.99", "stable": True},
                {"version": "go1.99.0", "stable": "true"},
                {"version": "go1.28.0beta1", "stable": True},
                "not-a-dict",
            ]
        ).encode("utf-8")
        self.assertEqual(
            gts.select_latest_stable_release(payload), gts.GoVersion(1, 26, 12)
        )

    def test_rejects_malformed_or_empty_payloads(self) -> None:
        bad_payloads: list[bytes | str] = [
            b"not json",
            b"\xff\xfe",
            b"{}",
            b"[]",
            json.dumps([{"version": "go1.27rc1", "stable": False}]).encode("utf-8"),
            json.dumps([{"version": "go1.27", "stable": True}]).encode("utf-8"),
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(gts.SyncError):
                    gts.select_latest_stable_release(payload)


class PlanSyncCheckTests(unittest.TestCase):
    def test_go_dl_fetch_error_returns_no_sync(self) -> None:
        plan = gts.plan_sync(
            repo_files=make_repo_files(),
            event_name="schedule",
            main_sha=MAIN_SHA,
            now=NOW,
            github_client=FakeGitHubClient(),
            http_get=make_http_get(
                go_dl_payload=cooldown.CooldownError("network down")
            ),
        )
        self.assertFalse(plan.needs_sync)
        self.assertIn("failed to fetch go.dev", plan.reason)

    def test_target_equal_or_older_than_current_no_sync(self) -> None:
        for target in (CURRENT_VER, "1.26.10", "1.25.9"):
            with self.subTest(target=target):
                payload = json.dumps(
                    [{"version": f"go{target}", "stable": True}]
                ).encode("utf-8")
                plan = gts.plan_sync(
                    repo_files=make_repo_files(),
                    event_name="schedule",
                    main_sha=MAIN_SHA,
                    now=NOW,
                    github_client=FakeGitHubClient(),
                    http_get=make_http_get(go_dl_payload=payload),
                )
                self.assertFalse(plan.needs_sync)
                self.assertIn("already up to date", plan.reason)

    def test_first_seen_lookup_failure_does_not_adopt(self) -> None:
        plan = gts.plan_sync(
            repo_files=make_repo_files(),
            event_name="schedule",
            main_sha=MAIN_SHA,
            now=NOW,
            github_client=FakeGitHubClient(),
            http_get=make_http_get(index_first_seen=None),
        )
        self.assertFalse(plan.needs_sync)
        self.assertIn("do not adopt", plan.reason)
        self.assertIn("first-seen", plan.reason)

    def test_first_seen_request_bound_exhaustion_fails_closed(self) -> None:
        # Mirrors the real-world fail-closed result: the trusted index scan is
        # bounded, and exhausting the bound means "do not adopt", not an error.
        recorded: list[str] = []
        plan = gts.plan_sync(
            repo_files=make_repo_files(),
            event_name="schedule",
            main_sha=MAIN_SHA,
            now=NOW,
            github_client=FakeGitHubClient(),
            http_get=make_http_get(index_first_seen=AGED_8D, recorded_urls=recorded),
            max_index_requests=1,
        )
        self.assertFalse(plan.needs_sync)
        self.assertIn("do not adopt", plan.reason)
        self.assertIn("within 1 request(s)", plan.reason)
        # Only the proxy hint was fetched; no index page was requested.
        self.assertEqual(len(recorded), 2)  # go.dev JSON + proxy .info

    def test_toolchain_one_second_short_of_cooldown_does_not_adopt(self) -> None:
        plan = gts.plan_sync(
            repo_files=make_repo_files(),
            event_name="schedule",
            main_sha=MAIN_SHA,
            now=NOW,
            github_client=FakeGitHubClient(),
            http_get=make_http_get(
                proxy_hint=AGED_SHORT_1S,
                index_first_seen=AGED_SHORT_1S,
                docker_last_pushed=AGED_8D,
            ),
        )
        self.assertFalse(plan.needs_sync)
        self.assertIn("do not adopt", plan.reason)
        self.assertIn("has not aged 7 days", plan.reason)

    def test_docker_lookup_failure_does_not_adopt(self) -> None:
        plan = gts.plan_sync(
            repo_files=make_repo_files(),
            event_name="schedule",
            main_sha=MAIN_SHA,
            now=NOW,
            github_client=FakeGitHubClient(),
            http_get=make_http_get(docker_last_pushed=None),
        )
        self.assertFalse(plan.needs_sync)
        self.assertIn("do not adopt", plan.reason)
        self.assertIn("Docker Hub lookup failed", plan.reason)

    def test_docker_invalid_digest_does_not_adopt(self) -> None:
        # Invalid digest in Docker Hub JSON fails closed via cooldown.CooldownError.
        bad_json = json.dumps(
            {
                "name": f"{SAME_MINOR_TARGET}-alpine",
                "digest": "sha256:not-a-valid-digest",
                "tag_last_pushed": _fmt_utc(AGED_8D),
            }
        ).encode("utf-8")
        plan = gts.plan_sync(
            repo_files=make_repo_files(),
            event_name="schedule",
            main_sha=MAIN_SHA,
            now=NOW,
            github_client=FakeGitHubClient(),
            http_get=make_http_get(docker_raw=bad_json),
        )
        self.assertFalse(plan.needs_sync)
        self.assertIn("do not adopt", plan.reason)

        # Also exercise go_toolchain_sync's own validate_digest guard directly.
        with mock.patch.object(
            cooldown, "docker_tag_info", return_value=("md5:deadbeef", AGED_8D)
        ):
            plan2 = gts.plan_sync(
                repo_files=make_repo_files(),
                event_name="schedule",
                main_sha=MAIN_SHA,
                now=NOW,
                github_client=FakeGitHubClient(),
                http_get=make_http_get(),
            )
        self.assertFalse(plan2.needs_sync)
        self.assertIn("invalid Docker digest", plan2.reason)

    def test_docker_one_second_short_of_cooldown_does_not_adopt(self) -> None:
        plan = gts.plan_sync(
            repo_files=make_repo_files(),
            event_name="schedule",
            main_sha=MAIN_SHA,
            now=NOW,
            github_client=FakeGitHubClient(),
            http_get=make_http_get(
                index_first_seen=AGED_8D,
                docker_last_pushed=AGED_SHORT_1S,
            ),
        )
        self.assertFalse(plan.needs_sync)
        self.assertIn("do not adopt", plan.reason)
        self.assertIn("has not aged 7 days", plan.reason)

    def test_exact_seven_day_boundary_adopts(self) -> None:
        plan = gts.plan_sync(
            repo_files=make_repo_files(),
            event_name="schedule",
            main_sha=MAIN_SHA,
            now=NOW,
            github_client=FakeGitHubClient(),
            http_get=make_http_get(
                proxy_hint=AGED_EXACT_7D,
                index_first_seen=AGED_EXACT_7D,
                docker_last_pushed=AGED_EXACT_7D,
            ),
        )
        self.assertTrue(plan.needs_sync)
        self.assertEqual(plan.target_version, gts.parse_go_version(SAME_MINOR_TARGET))
        self.assertFalse(plan.cross_minor)
        self.assertIsNone(plan.expected_pr_number)

    def test_push_event_with_no_open_pr_no_sync_and_no_http(self) -> None:
        urls: list[str] = []
        plan = gts.plan_sync(
            repo_files=make_repo_files(),
            event_name="push",
            main_sha=MAIN_SHA,
            now=NOW,
            github_client=FakeGitHubClient(open_pr=None),
            http_get=make_http_get(recorded_urls=urls),
        )
        self.assertFalse(plan.needs_sync)
        self.assertIn("no open", plan.reason)
        self.assertEqual(urls, [])

    def test_push_event_with_pr_already_based_on_main_no_sync(self) -> None:
        urls: list[str] = []
        pr = make_open_pr(head_sha=BRANCH_SHA)
        commit = gts.BranchCommit(
            sha=BRANCH_SHA, parents=(MAIN_SHA,), verified=True, files={}
        )
        client = FakeGitHubClient(
            branches={"main": MAIN_SHA, gts.SYNC_BRANCH: BRANCH_SHA},
            commits={BRANCH_SHA: commit},
            open_pr=pr,
        )
        plan = gts.plan_sync(
            repo_files=make_repo_files(),
            event_name="push",
            main_sha=MAIN_SHA,
            now=NOW,
            github_client=client,
            http_get=make_http_get(recorded_urls=urls),
        )
        self.assertFalse(plan.needs_sync)
        self.assertIn("already based on current main", plan.reason)
        self.assertEqual(urls, [])

    def test_push_event_with_stale_pr_replays_and_binds_expected_pr_number(self) -> None:
        pr = make_open_pr(number=77, head_sha=BRANCH_SHA)
        stale_commit = gts.BranchCommit(
            sha=BRANCH_SHA, parents=(OLD_MAIN_SHA,), verified=True, files={}
        )
        client = FakeGitHubClient(
            branches={"main": MAIN_SHA, gts.SYNC_BRANCH: BRANCH_SHA},
            commits={BRANCH_SHA: stale_commit},
            open_pr=pr,
        )
        plan = gts.plan_sync(
            repo_files=make_repo_files(),
            event_name="push",
            main_sha=MAIN_SHA,
            now=NOW,
            github_client=client,
            http_get=make_http_get(),
        )
        self.assertTrue(plan.needs_sync)
        self.assertEqual(plan.expected_pr_number, 77)

    def test_same_minor_open_pr_with_matching_commit_and_text_is_up_to_date(self) -> None:
        repo_files = make_repo_files()
        current = gts.parse_go_version(CURRENT_VER)
        target = gts.parse_go_version(SAME_MINOR_TARGET)
        edits = gts.plan_file_edits(repo_files, target, NEW_DIGEST, cross_minor=False)
        _, _, pr_title, pr_body = gts.build_commit_and_pr_text(
            current, target, NEW_DIGEST, False, AGED_8D, AGED_8D
        )
        matching_commit = gts.BranchCommit(
            sha=BRANCH_SHA,
            parents=(MAIN_SHA,),
            verified=True,
            files={p: gts.git_blob_sha(b) for p, b in edits.items()},
        )
        client = FakeGitHubClient(
            branches={"main": MAIN_SHA, gts.SYNC_BRANCH: BRANCH_SHA},
            commits={BRANCH_SHA: matching_commit},
            open_pr=make_open_pr(head_sha=BRANCH_SHA, title=pr_title, body=pr_body),
        )
        plan = gts.plan_sync(
            repo_files=repo_files,
            event_name="schedule",
            main_sha=MAIN_SHA,
            now=NOW,
            github_client=client,
            http_get=make_http_get(),
        )
        self.assertFalse(plan.needs_sync)
        self.assertIn("already up to date", plan.reason)

    def test_cross_minor_open_pr_is_never_up_to_date_from_check(self) -> None:
        repo_files = make_repo_files()
        current = gts.parse_go_version(CURRENT_VER)
        target = gts.parse_go_version(CROSS_MINOR_TARGET)
        edits = gts.plan_file_edits(repo_files, target, NEW_DIGEST, cross_minor=True)
        _, _, pr_title, pr_body = gts.build_commit_and_pr_text(
            current, target, NEW_DIGEST, True, AGED_8D, AGED_8D
        )
        matching_commit = gts.BranchCommit(
            sha=BRANCH_SHA,
            parents=(MAIN_SHA,),
            verified=True,
            files={p: gts.git_blob_sha(b) for p, b in edits.items()},
        )
        client = FakeGitHubClient(
            branches={"main": MAIN_SHA, gts.SYNC_BRANCH: BRANCH_SHA},
            commits={BRANCH_SHA: matching_commit},
            open_pr=make_open_pr(head_sha=BRANCH_SHA, title=pr_title, body=pr_body),
        )
        plan = gts.plan_sync(
            repo_files=repo_files,
            event_name="schedule",
            main_sha=MAIN_SHA,
            now=NOW,
            github_client=client,
            http_get=make_http_get(target_version=CROSS_MINOR_TARGET),
        )
        self.assertTrue(plan.needs_sync)
        self.assertTrue(plan.cross_minor)
        self.assertIsNone(plan.expected_pr_number)


# --- prepare phase tests ----------------------------------------------------


class PrepareSyncBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="test-go-sync-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _assert_check_go_version_passes(self, root: Path) -> None:
        self.assertEqual(_cgv.collect(root), [])
        proc = subprocess.run(
            [sys.executable, "-I", str(CHECK_GO_VERSION_SCRIPT), "--root", str(root)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"check-go-version.py failed:\nstdout={proc.stdout}\nstderr={proc.stderr}",
        )

    def test_same_minor_bundle_has_exact_four_files_and_passes_checker(self) -> None:
        repo = write_repo_tree(self.tmp / "repo", make_repo_files(crlf=True))
        tidy_called = False

        def fail_tidy(_workdir: Path, _ver: gts.GoVersion) -> None:
            nonlocal tidy_called
            tidy_called = True
            raise AssertionError("tidy_runner must not run for same-minor upgrade")

        bundle = gts.prepare_sync_bundle(
            repo_root=repo,
            main_sha=MAIN_SHA,
            target_version=gts.parse_go_version(SAME_MINOR_TARGET),
            docker_digest=NEW_DIGEST,
            toolchain_first_seen=AGED_8D,
            docker_last_pushed=AGED_8D,
            tidy_runner=fail_tidy,
        )
        self.assertFalse(tidy_called)
        self.assertFalse(bundle.cross_minor)
        self.assertEqual(set(bundle.files), gts.SAME_MINOR_ALLOWED_PATHS)
        self.assertEqual(len(bundle.files), 4)
        self.assertEqual(bundle.files[".go-version"], f"{SAME_MINOR_TARGET}\n".encode())
        for content in bundle.files.values():
            self.assertNotIn(b"\r", content)

        # Original repo must not be mutated.
        self.assertEqual((repo / ".go-version").read_text(encoding="utf-8").strip(), CURRENT_VER)

        # Applying bundle to a copy must satisfy check-go-version.py.
        applied = write_repo_tree(self.tmp / "applied", make_repo_files())
        for rel, content in bundle.files.items():
            (applied / rel).write_bytes(content)
        self._assert_check_go_version_passes(applied)

    def test_cross_minor_bundle_runs_tidy_updates_gomods_and_passes_checker(self) -> None:
        repo = write_repo_tree(self.tmp / "repo", make_repo_files())
        seen_calls: list[tuple[Path, gts.GoVersion]] = []

        def fake_tidy(workdir: Path, target_ver: gts.GoVersion) -> None:
            seen_calls.append((workdir, target_ver))
            # Verify tidy runs in an isolated copy with updated go.mod directives.
            self.assertNotEqual(workdir.resolve(), repo.resolve())
            self.assertIn("go 1.27.0", (workdir / "go.mod").read_text(encoding="utf-8"))
            self.assertIn(
                "go 1.27.0", (workdir / "tools/go.mod").read_text(encoding="utf-8")
            )
            # Simulate go mod tidy touching root go.sum only.
            (workdir / "go.sum").write_text(
                "example.com/dep v1.0.0 h1:tidied=\n", encoding="utf-8"
            )

        bundle = gts.prepare_sync_bundle(
            repo_root=repo,
            main_sha=MAIN_SHA,
            target_version=gts.parse_go_version(CROSS_MINOR_TARGET),
            docker_digest=NEW_DIGEST,
            toolchain_first_seen=AGED_8D,
            docker_last_pushed=AGED_8D,
            expected_pr_number=55,
            tidy_runner=fake_tidy,
        )
        self.assertTrue(bundle.cross_minor)
        self.assertEqual(len(seen_calls), 1)
        self.assertEqual(seen_calls[0][1], gts.parse_go_version(CROSS_MINOR_TARGET))
        self.assertEqual(bundle.expected_pr_number, 55)
        self.assertEqual(
            set(bundle.files),
            {
                ".go-version",
                *gts.DOCKERFILES,
                "go.mod",
                "go.sum",
                "tools/go.mod",
                "tools/actionlint/go.mod",
            },
        )
        self.assertIn(b"go 1.27.0\n", bundle.files["go.mod"])
        self.assertIn(b"go 1.27.0\n", bundle.files["tools/go.mod"])
        self.assertIn(b"go 1.27.0\n", bundle.files["tools/actionlint/go.mod"])
        self.assertEqual(bundle.files["go.sum"], b"example.com/dep v1.0.0 h1:tidied=\n")

        # Original repo untouched.
        self.assertIn("go 1.26.0", (repo / "go.mod").read_text(encoding="utf-8"))

        # Applying bundle satisfies scripts/check-go-version.py.
        applied = write_repo_tree(self.tmp / "applied", make_repo_files())
        for rel, content in bundle.files.items():
            (applied / rel).write_bytes(content)
        self._assert_check_go_version_passes(applied)

    def test_tidy_extra_files_are_not_smuggled_into_bundle(self) -> None:
        repo = write_repo_tree(self.tmp / "repo", make_repo_files())

        def messy_tidy(workdir: Path, _ver: gts.GoVersion) -> None:
            (workdir / "tools" / "extra.txt").write_text("noise\n", encoding="utf-8")
            evil = workdir / ".github" / "workflows" / "evil.yml"
            evil.parent.mkdir(parents=True, exist_ok=True)
            evil.write_text("name: evil\n", encoding="utf-8")

        bundle = gts.prepare_sync_bundle(
            repo_root=repo,
            main_sha=MAIN_SHA,
            target_version=gts.parse_go_version(CROSS_MINOR_TARGET),
            docker_digest=NEW_DIGEST,
            toolchain_first_seen=AGED_8D,
            docker_last_pushed=AGED_8D,
            tidy_runner=messy_tidy,
        )
        self.assertNotIn("tools/extra.txt", bundle.files)
        self.assertNotIn(".github/workflows/evil.yml", bundle.files)
        self.assertEqual(set(bundle.files) - gts.allowed_paths(True), set())

    def test_target_not_newer_than_current_rejected(self) -> None:
        repo = write_repo_tree(self.tmp / "repo", make_repo_files())
        for bad_target in (CURRENT_VER, "1.26.10", "1.25.0"):
            with self.subTest(target=bad_target):
                with self.assertRaises(gts.SyncError):
                    gts.prepare_sync_bundle(
                        repo_root=repo,
                        main_sha=MAIN_SHA,
                        target_version=gts.parse_go_version(bad_target),
                        docker_digest=NEW_DIGEST,
                        toolchain_first_seen=AGED_8D,
                        docker_last_pushed=AGED_8D,
                    )

    def test_check_go_version_invariants_enforced(self) -> None:
        # Dockerfile tag mismatch rejected.
        files = make_repo_files(version=SAME_MINOR_TARGET, digest=NEW_DIGEST)
        files["dockerfiles/Dockerfile"] = (
            f"FROM golang:{CURRENT_VER}-alpine@{NEW_DIGEST} AS builder\n"
        ).encode("utf-8")
        with self.assertRaises(gts.SyncError):
            gts.validate_go_version_invariants(files)

        # Unpinned golang FROM line rejected during prepare.
        bad_repo_files = make_repo_files()
        bad_repo_files["dockerfiles/Dockerfile"] = b"FROM golang:1.26.11-alpine\n"
        repo = write_repo_tree(self.tmp / "bad-df", bad_repo_files)
        with self.assertRaises(gts.SyncError):
            gts.prepare_sync_bundle(
                repo_root=repo,
                main_sha=MAIN_SHA,
                target_version=gts.parse_go_version(SAME_MINOR_TARGET),
                docker_digest=NEW_DIGEST,
                toolchain_first_seen=AGED_8D,
                docker_last_pushed=AGED_8D,
            )

        # Tidy corrupting go.mod directive to wrong series is rejected by prepare.
        repo2 = write_repo_tree(self.tmp / "bad-tidy", make_repo_files())

        def corrupt_tidy(workdir: Path, _ver: gts.GoVersion) -> None:
            (workdir / "go.mod").write_text(
                "module example.com/watchtower\n\ngo 1.26.0\n", encoding="utf-8"
            )

        with self.assertRaises(gts.SyncError):
            gts.prepare_sync_bundle(
                repo_root=repo2,
                main_sha=MAIN_SHA,
                target_version=gts.parse_go_version(CROSS_MINOR_TARGET),
                docker_digest=NEW_DIGEST,
                toolchain_first_seen=AGED_8D,
                docker_last_pushed=AGED_8D,
                tidy_runner=corrupt_tidy,
            )

    def test_bundle_json_round_trip_preserves_bytes_and_expected_pr_number(self) -> None:
        for expected_pr in (None, 88):
            with self.subTest(expected_pr=expected_pr):
                original = make_sample_bundle(expected_pr_number=expected_pr)
                encoded = json.dumps(original.to_json_dict(), sort_keys=True)
                restored = gts.SyncBundle.from_json_dict(json.loads(encoded))
                self.assertEqual(restored, original)
                self.assertEqual(restored.files, original.files)
                self.assertEqual(restored.expected_pr_number, expected_pr)

    def test_bundle_from_json_rejects_invalid_expected_pr_number(self) -> None:
        base_dict = make_sample_bundle().to_json_dict()
        for bad_pr in (True, False, 0, -5, "42"):
            with self.subTest(bad_pr=bad_pr):
                mutated = dict(base_dict, expected_pr_number=bad_pr)
                with self.assertRaises(gts.SyncError):
                    gts.SyncBundle.from_json_dict(mutated)


# --- publish phase tests ----------------------------------------------------


class PublishSyncBundleTests(unittest.TestCase):
    def test_first_publish_creates_staging_ref_signed_commit_branch_and_pr(self) -> None:
        bundle = make_sample_bundle()
        client = FakeGitHubClient(branches={"main": MAIN_SHA})
        result = gts.publish_sync_bundle(
            bundle,
            client,
            tmp_branch_factory=lambda: "automation/go-toolchain-tmp-123456",
        )
        self.assertEqual(result.commit_sha, NEW_COMMIT_SHA)
        self.assertFalse(result.reused_commit)
        self.assertEqual(result.pr_number, 101)
        self.assertEqual(result.pr_action, "created")

        op_names = [name for name, _ in client.writes]
        self.assertEqual(
            op_names,
            [
                "create_ref",
                "create_commit_on_branch",
                "create_ref",
                "delete_ref",
                "create_pull_request",
            ],
        )
        self.assertEqual(
            client.writes[0][1],
            {"branch": "automation/go-toolchain-tmp-123456", "sha": MAIN_SHA},
        )
        commit_call = client.writes[1][1]
        self.assertEqual(commit_call["branch"], "automation/go-toolchain-tmp-123456")
        self.assertEqual(commit_call["expected_head_oid"], MAIN_SHA)
        self.assertEqual(commit_call["headline"], bundle.commit_headline)
        self.assertEqual(commit_call["body"], bundle.commit_body)
        self.assertEqual(commit_call["additions"], bundle.files)

        self.assertEqual(
            client.writes[2][1],
            {"branch": gts.SYNC_BRANCH, "sha": NEW_COMMIT_SHA},
        )
        self.assertEqual(
            client.writes[3][1],
            {"branch": "automation/go-toolchain-tmp-123456"},
        )
        self.assertEqual(
            client.writes[4][1],
            {
                "head": gts.SYNC_BRANCH,
                "base": gts.DEFAULT_BASE_BRANCH,
                "title": bundle.pr_title,
                "body": bundle.pr_body,
            },
        )
        self.assertNotIn("automation/go-toolchain-tmp-123456", client.branches)

    def test_default_staging_branch_name_is_under_sync_branch_prefix(self) -> None:
        bundle = make_sample_bundle()
        client = FakeGitHubClient(branches={"main": MAIN_SHA})
        gts.publish_sync_bundle(bundle, client)
        staging_refs = [
            payload["branch"]
            for name, payload in client.writes
            if name == "create_ref" and payload["branch"] != gts.SYNC_BRANCH
        ]
        self.assertEqual(len(staging_refs), 1)
        self.assertRegex(
            staging_refs[0], r"^automation/go-toolchain-tmp-[0-9a-f]{12}$"
        )

    def test_update_existing_branch_uses_cas_never_empties_pr_and_cleans_staging(self) -> None:
        bundle = make_sample_bundle(expected_pr_number=42)
        stale_commit = gts.BranchCommit(
            sha=BRANCH_SHA, parents=(OLD_MAIN_SHA,), verified=True, files={}
        )
        client = FakeGitHubClient(
            branches={"main": MAIN_SHA, gts.SYNC_BRANCH: BRANCH_SHA},
            commits={BRANCH_SHA: stale_commit},
            open_pr=make_open_pr(number=42, head_sha=BRANCH_SHA),
        )
        result = gts.publish_sync_bundle(
            bundle,
            client,
            tmp_branch_factory=lambda: "automation/go-toolchain-tmp-abcdef",
        )
        self.assertEqual(result.commit_sha, NEW_COMMIT_SHA)
        self.assertFalse(result.reused_commit)
        self.assertEqual(result.pr_number, 42)
        self.assertEqual(result.pr_action, "updated")

        op_names = [name for name, _ in client.writes]
        self.assertEqual(
            op_names,
            [
                "create_ref",
                "create_commit_on_branch",
                "compare_and_swap_ref",
                "delete_ref",
                "update_pull_request",
            ],
        )
        self.assertEqual(
            client.writes[2][1],
            {
                "branch": gts.SYNC_BRANCH,
                "expected_old_oid": BRANCH_SHA,
                "new_oid": NEW_COMMIT_SHA,
                "source_branch": "automation/go-toolchain-tmp-abcdef",
            },
        )
        self.assertNotIn("automation/go-toolchain-tmp-abcdef", client.branches)

    def test_existing_identical_commit_reused_with_zero_writes(self) -> None:
        bundle = make_sample_bundle()
        matching_commit = gts.BranchCommit(
            sha=BRANCH_SHA,
            parents=(MAIN_SHA,),
            verified=True,
            files={p: gts.git_blob_sha(b) for p, b in bundle.files.items()},
        )
        client = FakeGitHubClient(
            branches={"main": MAIN_SHA, gts.SYNC_BRANCH: BRANCH_SHA},
            commits={BRANCH_SHA: matching_commit},
            open_pr=make_open_pr(
                number=42,
                head_sha=BRANCH_SHA,
                title=bundle.pr_title,
                body=bundle.pr_body,
            ),
        )
        result = gts.publish_sync_bundle(bundle, client)
        self.assertEqual(result.commit_sha, BRANCH_SHA)
        self.assertTrue(result.reused_commit)
        self.assertEqual(result.pr_number, 42)
        self.assertEqual(result.pr_action, "unchanged")
        self.assertEqual(client.writes, [])

    def test_branch_exists_with_matching_commit_but_no_pr_creates_only_pr(self) -> None:
        bundle = make_sample_bundle()
        matching_commit = gts.BranchCommit(
            sha=BRANCH_SHA,
            parents=(MAIN_SHA,),
            verified=True,
            files={p: gts.git_blob_sha(b) for p, b in bundle.files.items()},
        )
        client = FakeGitHubClient(
            branches={"main": MAIN_SHA, gts.SYNC_BRANCH: BRANCH_SHA},
            commits={BRANCH_SHA: matching_commit},
            open_pr=None,
        )
        result = gts.publish_sync_bundle(bundle, client)
        self.assertEqual(result.commit_sha, BRANCH_SHA)
        self.assertTrue(result.reused_commit)
        self.assertEqual(result.pr_action, "created")
        self.assertEqual([name for name, _ in client.writes], ["create_pull_request"])

    def test_expected_pr_closed_or_different_rejects_before_any_write(self) -> None:
        bundle = make_sample_bundle(expected_pr_number=42)
        for scenario, open_pr in (
            ("closed", None),
            ("different", make_open_pr(number=99, head_sha=BRANCH_SHA)),
        ):
            with self.subTest(scenario=scenario):
                client = FakeGitHubClient(
                    branches={"main": MAIN_SHA, gts.SYNC_BRANCH: BRANCH_SHA},
                    open_pr=open_pr,
                )
                with self.assertRaises(gts.SyncError):
                    gts.publish_sync_bundle(bundle, client)
                self.assertEqual(client.writes, [])

    def test_main_moved_rejects_before_any_write(self) -> None:
        bundle = make_sample_bundle(main_sha=MAIN_SHA)
        client = FakeGitHubClient(branches={"main": NEW_MAIN_SHA})
        with self.assertRaises(gts.SyncError) as ctx:
            gts.publish_sync_bundle(bundle, client)
        self.assertIn("moved during sync", str(ctx.exception))
        self.assertEqual(client.writes, [])

    def test_disallowed_paths_and_empty_bundle_rejected_before_any_write(self) -> None:
        client = FakeGitHubClient(branches={"main": MAIN_SHA})

        # Same-minor bundle with go.mod is disallowed.
        bad_same = make_sample_bundle(
            cross_minor=False,
            files={**make_sample_bundle().files, "go.mod": b"module x\n\ngo 1.26.0\n"},
        )
        with self.assertRaises(gts.SyncError):
            gts.publish_sync_bundle(bad_same, client)

        # Cross-minor bundle with workflow file is disallowed.
        bad_cross = make_sample_bundle(
            cross_minor=True,
            files={
                **make_sample_bundle(cross_minor=True).files,
                ".github/workflows/ci.yml": b"name: hacked\n",
            },
        )
        with self.assertRaises(gts.SyncError):
            gts.publish_sync_bundle(bad_cross, client)

        # Empty bundle is disallowed.
        empty_bundle = make_sample_bundle(files={})
        with self.assertRaises(gts.SyncError):
            gts.publish_sync_bundle(empty_bundle, client)

        self.assertEqual(client.writes, [])

    def test_staging_ref_deleted_even_on_cas_or_commit_failure(self) -> None:
        bundle = make_sample_bundle()
        stale_commit = gts.BranchCommit(
            sha=BRANCH_SHA, parents=(OLD_MAIN_SHA,), verified=True, files={}
        )

        # Failure during compare_and_swap_ref (lease mismatch).
        client_cas = FakeGitHubClient(
            branches={"main": MAIN_SHA, gts.SYNC_BRANCH: BRANCH_SHA},
            commits={BRANCH_SHA: stale_commit},
            open_pr=make_open_pr(head_sha=BRANCH_SHA),
            fail_cas=gts.SyncError("lease mismatch"),
        )
        with self.assertRaises(gts.SyncError):
            gts.publish_sync_bundle(
                bundle,
                client_cas,
                tmp_branch_factory=lambda: "automation/go-toolchain-tmp-fail1",
            )
        self.assertEqual(
            [name for name, _ in client_cas.writes],
            [
                "create_ref",
                "create_commit_on_branch",
                "compare_and_swap_ref",
                "delete_ref",
            ],
        )
        self.assertNotIn("automation/go-toolchain-tmp-fail1", client_cas.branches)

        # Failure during create_commit_on_branch.
        client_commit = FakeGitHubClient(
            branches={"main": MAIN_SHA},
            fail_create_commit=gts.SyncError("GraphQL error"),
        )
        with self.assertRaises(gts.SyncError):
            gts.publish_sync_bundle(
                bundle,
                client_commit,
                tmp_branch_factory=lambda: "automation/go-toolchain-tmp-fail2",
            )
        self.assertEqual(
            [name for name, _ in client_commit.writes],
            ["create_ref", "create_commit_on_branch", "delete_ref"],
        )
        self.assertNotIn("automation/go-toolchain-tmp-fail2", client_commit.branches)

    def test_staging_delete_failure_warns_without_masking_success(self) -> None:
        bundle = make_sample_bundle()
        client = FakeGitHubClient(
            branches={"main": MAIN_SHA},
            fail_delete_ref=gts.SyncError("transient 500"),
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = gts.publish_sync_bundle(
                bundle,
                client,
                tmp_branch_factory=lambda: "automation/go-toolchain-tmp-warn",
            )
        self.assertEqual(result.commit_sha, NEW_COMMIT_SHA)
        self.assertEqual(result.pr_action, "created")
        self.assertIn("could not delete temporary ref", stderr.getvalue())


# --- UrllibGitHubClient & security tests ------------------------------------


class UrllibGitHubClientTests(unittest.TestCase):
    def test_managed_head_pr_lookup_guards_publish(self) -> None:
        client = gts.UrllibGitHubClient("owner/repo")
        expected = make_open_pr()
        candidate = {
            "state": "open",
            "number": expected.number,
            "head": {"repo": {"full_name": "owner/repo"}, "ref": gts.SYNC_BRANCH, "sha": BRANCH_SHA},
            "base": {"ref": "main"},
            "user": {"login": expected.author_login},
            "title": expected.title,
            "body": expected.body,
            "html_url": expected.html_url,
        }
        bundle = make_sample_bundle()
        for label, response in (
            ("empty", []),
            ("managed", [candidate]),
            ("unexpected author", [{**candidate, "user": {"login": "other"}}]),
            ("unexpected base", [{**candidate, "base": {"ref": "release"}}]),
            ("ambiguous", [candidate, {**candidate, "number": expected.number + 1}]),
            ("malformed head", [{**candidate, "head": None}]),
            ("malformed list", None),
        ):
            with self.subTest(label=label), mock.patch.object(
                gts._GITHUB_OPENER, "open", return_value=io.BytesIO(json.dumps(response).encode())
            ) as transport:
                if label in ("empty", "managed"):
                    pr = client.find_open_sync_pr(gts.SYNC_BRANCH, "main", expected.author_login)
                    self.assertEqual(pr, expected if label == "managed" else None)
                else:
                    with mock.patch.object(client, "get_branch_sha", side_effect=[MAIN_SHA, BRANCH_SHA]):
                        with self.assertRaises(gts.SyncError):
                            gts.publish_sync_bundle(bundle, client)
                transport.assert_called_once()
                request = transport.call_args.args[0]
                self.assertEqual(request.get_method(), "GET")
                url = urllib.parse.urlsplit(request.full_url)
                self.assertEqual(url.path, "/repos/owner/repo/pulls")
                query = urllib.parse.parse_qs(url.query)
                self.assertEqual(query["state"], ["open"])
                self.assertEqual(query["head"], [f"owner:{gts.SYNC_BRANCH}"])
                self.assertNotIn("base", query)

    def test_create_commit_on_branch_graphql_payload_shape(self) -> None:
        captured: list[urllib.request.Request] = []

        class _FakeResp:
            def __enter__(self) -> _FakeResp:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {"data": {"createCommitOnBranch": {"commit": {"oid": NEW_COMMIT_SHA}}}}
                ).encode("utf-8")

        def fake_open(req: urllib.request.Request, timeout: int = 30) -> _FakeResp:
            captured.append(req)
            return _FakeResp()

        client = gts.UrllibGitHubClient("owner/repo", token="ghs_dummy_token")
        additions = {
            "dockerfiles/Dockerfile": b"FROM golang:1.26.12-alpine\n",
            ".go-version": b"1.26.12\n",
        }
        with mock.patch.object(gts._GITHUB_OPENER, "open", side_effect=fake_open):
            oid = client.create_commit_on_branch(
                branch="automation/go-toolchain-tmp-abc",
                expected_head_oid=MAIN_SHA,
                headline="build(deps): bump Go",
                body="details",
                additions=additions,
            )

        self.assertEqual(oid, NEW_COMMIT_SHA)
        self.assertEqual(len(captured), 1)
        req = captured[0]
        self.assertEqual(req.full_url, "https://api.github.com/graphql")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer ghs_dummy_token")

        body = json.loads((req.data or b"").decode("utf-8"))
        self.assertEqual(body["query"], gts.CREATE_COMMIT_ON_BRANCH_MUTATION)
        inp = body["variables"]["input"]
        self.assertEqual(
            inp["branch"],
            {
                "repositoryNameWithOwner": "owner/repo",
                "branchName": "automation/go-toolchain-tmp-abc",
            },
        )
        self.assertEqual(inp["expectedHeadOid"], MAIN_SHA)
        self.assertEqual(
            inp["message"], {"headline": "build(deps): bump Go", "body": "details"}
        )
        self.assertEqual(
            inp["fileChanges"]["additions"],
            [
                {
                    "path": ".go-version",
                    "contents": base64.b64encode(b"1.26.12\n").decode("ascii"),
                },
                {
                    "path": "dockerfiles/Dockerfile",
                    "contents": base64.b64encode(b"FROM golang:1.26.12-alpine\n").decode(
                        "ascii"
                    ),
                },
            ],
        )

    def test_compare_and_swap_ref_uses_force_with_lease_not_force(self) -> None:
        from types import SimpleNamespace

        client = gts.UrllibGitHubClient("owner/repo", token="ghs_dummy_token")
        calls: list[list[str]] = []

        def fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
            calls.append(list(args))
            if "rev-parse" in args:
                return SimpleNamespace(
                    returncode=0, stdout=f"{NEW_COMMIT_SHA}\n", stderr=""
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(gts.subprocess, "run", side_effect=fake_run):
            client.compare_and_swap_ref(
                gts.SYNC_BRANCH,
                expected_old_oid=BRANCH_SHA,
                new_oid=NEW_COMMIT_SHA,
                source_branch="automation/go-toolchain-tmp-abc",
            )

        push_calls = [args for args in calls if "push" in args]
        self.assertEqual(len(push_calls), 1)
        push_args = push_calls[0]
        self.assertIn(
            f"--force-with-lease=refs/heads/{gts.SYNC_BRANCH}:{BRANCH_SHA}", push_args
        )
        for arg in push_args:
            self.assertNotEqual(arg, "--force")
            self.assertFalse(arg.startswith("--force="))
        self.assertIn(
            f"{NEW_COMMIT_SHA}:refs/heads/{gts.SYNC_BRANCH}", push_args
        )
        self.assertIn(f"https://github.com/owner/repo.git", push_args)

        fetch_calls = [args for args in calls if "fetch" in args]
        self.assertEqual(len(fetch_calls), 1)
        self.assertIn("--depth=1", fetch_calls[0])
        self.assertIn("refs/heads/automation/go-toolchain-tmp-abc", fetch_calls[0])

    def test_never_sends_token_to_different_origin_on_redirect(self) -> None:
        # Verify the handler is installed on the module's authenticated opener.
        redirect_handlers = [
            h
            for h in gts._GITHUB_OPENER.handlers
            if isinstance(h, gts._SameOriginRedirectHandler)
        ]
        self.assertEqual(len(redirect_handlers), 1)
        handler = redirect_handlers[0]

        auth_req = urllib.request.Request(
            "https://api.github.com/repos/owner/repo/git/ref/heads/main",
            headers={"Authorization": "Bearer ghs_secret_token"},
            method="GET",
        )
        empty_headers = email.message.Message()

        for evil_url in (
            "https://evil.example/steal",
            "http://api.github.com/repos/owner/repo/git/ref/heads/main",
            "https://api.github.com:8443/repos/owner/repo/git/ref/heads/main",
        ):
            with self.subTest(evil_url=evil_url):
                with self.assertRaises(gts.SyncError) as ctx:
                    handler.redirect_request(
                        auth_req, None, 302, "Found", empty_headers, evil_url
                    )
                self.assertIn("cross-origin redirect", str(ctx.exception))

        # Same-origin redirect (case-insensitive host) is permitted.
        same_origin_url = "https://API.GITHUB.COM/repos/owner/repo/git/ref/heads/main2"
        redirected = handler.redirect_request(
            auth_req, None, 302, "Found", empty_headers, same_origin_url
        )
        self.assertIsNotNone(redirected)
        assert redirected is not None
        self.assertEqual(redirected.full_url, same_origin_url)

        # End-to-end opener test without network: a 302 to another origin aborts
        # before any second HTTP request can be sent to the foreign host.
        dispatched_hosts: list[str] = []

        class _FakeRedirectResponse:
            def __init__(self, url: str, location: str) -> None:
                self.url = url
                self.code = 302
                self.msg = "Found"
                self.headers = email.message.Message()
                self.headers["Location"] = location
                self._body = io.BytesIO(b"")

            def info(self) -> email.message.Message:
                return self.headers

            def read(self, amt: int = -1) -> bytes:
                return self._body.read(amt)

            def close(self) -> None:
                self._body.close()

        class _FakeHTTPSHandler(urllib.request.BaseHandler):
            def https_open(
                self, req: urllib.request.Request
            ) -> _FakeRedirectResponse:
                dispatched_hosts.append(urllib.parse.urlsplit(req.full_url).netloc)
                return _FakeRedirectResponse(
                    req.full_url, "https://attacker.example/collect"
                )

        test_opener = urllib.request.OpenerDirector()
        test_opener.add_handler(_FakeHTTPSHandler())
        test_opener.add_handler(urllib.request.HTTPErrorProcessor())
        test_opener.add_handler(gts._SameOriginRedirectHandler())
        with self.assertRaises(gts.SyncError):
            test_opener.open(auth_req)
        self.assertEqual(dispatched_hosts, ["api.github.com"])


if __name__ == "__main__":
    unittest.main()
