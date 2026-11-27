"""Behavior tests for PR5 release selection, recovery, idempotency, and image promotion.

Run from the repository root with:

    python -I -m unittest discover -s .github/scripts -p "test_release*.py" -v
"""

import hashlib
import io
import json
import os
import sys
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

# Isolated mode (-I) omits the script directory from sys.path when invoked directly.
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import release_cli
import release_gh
import release_image
from release_gh import GitHub, Release
from release_paths import has_release_changes
from release_plan import MARKER, Candidate, ReleaseError, Tag
from release_record import ARCHIVES, RECORD, Record, checksums_name, file_sha256

SHA_BASE = "1" * 40
SHA_NEXT = "2" * 40
SHA_LATER = "3" * 40
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def make_dist(root: Path, tag: str, *, salt: bytes = b"v1") -> Path:
    """Create the eight release archives and matching checksums file."""
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    lines = []
    for name in sorted(ARCHIVES):
        archive = dist / name
        archive.write_bytes(f"{tag}:{name}:".encode() + salt)
        lines.append(f"{file_sha256(archive)}  {name}")
    (dist / checksums_name(tag)).write_bytes(("\n".join(lines) + "\n").encode("ascii"))
    return dist


def asset_entry(name: str, payload: bytes, state: str = "uploaded") -> dict:
    return {
        "name": name,
        "state": state,
        "digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
    }


def capture_outputs(func, *args, **kwargs) -> dict[str, str]:
    """Run a CLI helper without touching GITHUB_OUTPUT and parse its printed JSON."""
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_OUTPUT"}
    stdout = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(stdout):
        func(*args, **kwargs)
    lines = [line for line in stdout.getvalue().splitlines() if line.strip().startswith("{")]
    return json.loads(lines[-1]) if lines else {}


def make_fake_verifier(attested_source: str, calls: list[tuple[str, ...]]):
    """Simulate `gh attestation verify` for an attestation signed at `attested_source`."""
    def fake_run(*command: str, cwd: Path | None = None) -> str:
        calls.append(command)
        if command[:3] != ("gh", "attestation", "verify"):
            raise AssertionError(f"unexpected command: {command}")
        source_digest = None
        for idx, arg in enumerate(command):
            if arg == "--source-digest" and idx + 1 < len(command):
                source_digest = command[idx + 1]
                break
            if arg.startswith("--source-digest="):
                source_digest = arg.split("=", 1)[1]
                break
        if source_digest is not None and source_digest != attested_source:
            raise ReleaseError(f"gh failed: attestation source {attested_source} != {source_digest}")
        return ""

    return fake_run


class FakeGitHub:
    """In-memory stand-in for release_gh.GitHub; never invokes gh, git, or urllib."""

    def __init__(
        self,
        *,
        head: str = SHA_NEXT,
        tags: dict[str, Tag] | None = None,
        releases: dict[str, Release] | None = None,
        assets: dict[int, list[dict]] | None = None,
        changed_paths: list[str] | None = None,
        ancestors: set[tuple[str, str]] | None = None,
        repo: str = "owner/watchtower",
    ) -> None:
        self.repo = repo
        self._head = head
        self._tags = dict(tags or {})
        self._releases = dict(releases or {})
        self._assets = {rid: list(items) for rid, items in (assets or {}).items()}
        self._changed_paths = list(changed_paths or [])
        self._ancestors = set(ancestors or set())
        self._next_id = max(self._assets, default=100) + 1
        self.calls: list[tuple] = []

    def head(self) -> str:
        return self._head

    def tags(self, head: str) -> dict[str, Tag]:
        return dict(self._tags)

    def ancestor(self, sha: str, head: str) -> bool:
        return sha == head or (sha, head) in self._ancestors

    def changed_paths(self, base: str, head: str) -> list[str]:
        return list(self._changed_paths)

    def releases(self) -> dict[str, Release]:
        return dict(self._releases)

    def assets(self, release: Release) -> list[dict]:
        return list(self._assets.get(release.id, []))

    def reserve(self, record: Record) -> Release:
        self.calls.append(("reserve", record.tag, record.sha, record.automatic))
        release = Release(self._next_id, record.tag, True, f"Watchtower {record.tag}", record)
        self._next_id += 1
        self._releases[record.tag] = release
        self._assets.setdefault(release.id, [])
        return release

    def save(
        self,
        release: Release,
        record: Record,
        *,
        publish: bool = False,
        latest: bool = False,
    ) -> Release:
        self.calls.append(("save", release.tag, publish, latest, record))
        updated = Release(
            release.id,
            release.tag,
            False if publish else release.draft,
            release.notes,
            record,
        )
        self._releases[release.tag] = updated
        return updated

    def upload(self, release: Release, path: Path) -> None:
        self.calls.append(("upload", release.tag, path.name))
        items = self._assets.setdefault(release.id, [])
        if any(item["name"] == path.name for item in items):
            raise ReleaseError(f"duplicate upload of {path.name}")
        items.append(asset_entry(path.name, path.read_bytes()))

    def create_tag(self, record: Record) -> None:
        self.calls.append(("create_tag", record.tag, record.sha))
        if record.tag in self._tags:
            raise ReleaseError(f"duplicate tag {record.tag}")
        self._tags[record.tag] = Tag(record.tag, record.sha, True, MARKER + record.sha)


class FakeRegistry:
    """In-memory stand-in for release_image.Registry; never contacts GHCR."""

    def __init__(self, repo: str, digests: dict[str, str] | None = None) -> None:
        self.repo = repo.lower()
        self.image = f"ghcr.io/{self.repo}"
        self.digests = dict(digests or {})
        self.assigned: list[tuple[str, str]] = []

    def digest(self, ref: str) -> str | None:
        return self.digests.get(ref)

    def assign(self, tag: str, digest: str) -> None:
        self.assigned.append((tag, digest))
        self.digests[tag] = digest


class ReleaseCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _base_tags(self) -> dict[str, Tag]:
        return {"v1.0.0": Tag("v1.0.0", SHA_BASE, True)}

    def test_draft_only_reservation_stages_tag_and_assets_idempotently(self) -> None:
        record = Record("v1.0.1", SHA_NEXT, True)
        draft = Release(10, "v1.0.1", True, "Watchtower v1.0.1", record)
        gh = FakeGitHub(head=SHA_NEXT, tags=self._base_tags(), releases={"v1.0.1": draft})

        cand = release_cli.plan(gh, "scheduled")
        self.assertEqual(cand, Candidate("reserved", "v1.0.1", SHA_NEXT))

        prep = capture_outputs(release_cli.prepare, gh, cand)
        self.assertEqual(prep, {"active": "true", "build": "true", "draft": "true", "image_digest": ""})
        self.assertEqual(gh.calls, [])

        dist = make_dist(self.tmp, "v1.0.1")
        stage = capture_outputs(release_cli.stage_assets, gh, cand, dist)
        self.assertEqual(stage, {"attest": "true"})
        self.assertEqual([c for c in gh.calls if c[0] == "create_tag"], [("create_tag", "v1.0.1", SHA_NEXT)])
        uploaded = [c[2] for c in gh.calls if c[0] == "upload"]
        self.assertEqual(set(uploaded), {*ARCHIVES, checksums_name("v1.0.1"), RECORD})
        self.assertTrue(gh.tags(SHA_NEXT)["v1.0.1"].marked)

        # Retrying stage_assets once all files and tag exist must make no further writes.
        writes_before = len(gh.calls)
        capture_outputs(release_cli.stage_assets, gh, cand, dist)
        self.assertEqual(len(gh.calls), writes_before)

    def test_marked_tag_without_draft_and_unmanaged_draft_recovery(self) -> None:
        tags = {
            **self._base_tags(),
            "v1.0.1": Tag("v1.0.1", SHA_NEXT, True, MARKER + SHA_NEXT),
        }

        with self.subTest("marked tag with no release reserves draft"):
            gh = FakeGitHub(head=SHA_NEXT, tags=tags)
            cand = release_cli.plan(gh, "scheduled")
            self.assertEqual(cand, Candidate("recover", "v1.0.1", SHA_NEXT))
            out = capture_outputs(release_cli.prepare, gh, cand)
            self.assertEqual(out, {"active": "true", "build": "true", "draft": "true", "image_digest": ""})
            self.assertEqual(gh.calls, [("reserve", "v1.0.1", SHA_NEXT, True)])

        with self.subTest("marked tag with empty unmanaged draft adopts it"):
            empty_draft = Release(11, "v1.0.1", True, "Watchtower v1.0.1", None)
            gh = FakeGitHub(head=SHA_NEXT, tags=tags, releases={"v1.0.1": empty_draft})
            cand = release_cli.plan(gh, "scheduled")
            self.assertEqual(cand, Candidate("recover", "v1.0.1", SHA_NEXT))
            capture_outputs(release_cli.prepare, gh, cand)
            self.assertEqual([c[0] for c in gh.calls], ["save"])
            self.assertEqual(gh.releases()["v1.0.1"].record, Record("v1.0.1", SHA_NEXT, True))

        with self.subTest("marked tag with unmanaged draft containing assets fails"):
            dirty_draft = Release(12, "v1.0.1", True, "Watchtower v1.0.1", None)
            gh = FakeGitHub(
                head=SHA_NEXT,
                tags=tags,
                releases={"v1.0.1": dirty_draft},
                assets={12: [asset_entry("stray.txt", b"stray")]},
            )
            cand = release_cli.plan(gh, "scheduled")
            with self.assertRaisesRegex(ReleaseError, "unmanaged"):
                capture_outputs(release_cli.prepare, gh, cand)

    def test_partial_assets_retry_uploads_only_missing_and_rejects_drift(self) -> None:
        dist = make_dist(self.tmp / "good", "v1.0.1")
        record = Record("v1.0.1", SHA_NEXT, True).with_assets(dist)
        tags = {**self._base_tags(), "v1.0.1": Tag("v1.0.1", SHA_NEXT, True, MARKER + SHA_NEXT)}

        with self.subTest("crash after saving hashes before manifest upload converges"):
            draft = Release(20, "v1.0.1", True, "Watchtower v1.0.1", record)
            gh = FakeGitHub(head=SHA_NEXT, tags=tags, releases={"v1.0.1": draft}, assets={20: []})
            cand = release_cli.plan(gh, "scheduled")
            capture_outputs(release_cli.stage_assets, gh, cand, dist)
            self.assertEqual([c for c in gh.calls if c[0] in {"save", "create_tag"}], [])
            uploaded = {c[2] for c in gh.calls if c[0] == "upload"}
            self.assertEqual(uploaded, {*ARCHIVES, checksums_name("v1.0.1"), RECORD})

        with self.subTest("partial uploaded assets uploads only missing files"):
            already = sorted(ARCHIVES)[:3]
            initial_assets = [asset_entry(RECORD, record.manifest())] + [
                asset_entry(name, (dist / name).read_bytes()) for name in already
            ]
            draft = Release(21, "v1.0.1", True, "Watchtower v1.0.1", record)
            gh = FakeGitHub(head=SHA_NEXT, tags=tags, releases={"v1.0.1": draft}, assets={21: initial_assets})
            cand = release_cli.plan(gh, "scheduled")
            capture_outputs(release_cli.stage_assets, gh, cand, dist)
            uploaded = {c[2] for c in gh.calls if c[0] == "upload"}
            self.assertEqual(uploaded, (set(ARCHIVES) - set(already)) | {checksums_name("v1.0.1")})
            self.assertEqual([c for c in gh.calls if c[0] == "create_tag"], [])

        with self.subTest("starter asset from interrupted upload fails closed"):
            draft = Release(22, "v1.0.1", True, "Watchtower v1.0.1", record)
            starter = [asset_entry(ARCHIVES[0], (dist / ARCHIVES[0]).read_bytes(), state="starter")]
            gh = FakeGitHub(head=SHA_NEXT, tags=tags, releases={"v1.0.1": draft}, assets={22: starter})
            with self.assertRaisesRegex(ReleaseError, "starter"):
                capture_outputs(release_cli.prepare, gh, Candidate("recover", "v1.0.1", SHA_NEXT))

        with self.subTest("rebuilt archives with different hashes fail closed"):
            draft = Release(23, "v1.0.1", True, "Watchtower v1.0.1", record)
            gh = FakeGitHub(head=SHA_NEXT, tags=tags, releases={"v1.0.1": draft}, assets={23: []})
            drifted_dist = make_dist(self.tmp / "drifted", "v1.0.1", salt=b"different")
            with self.assertRaisesRegex(ReleaseError, "differ"):
                capture_outputs(release_cli.stage_assets, gh, Candidate("recover", "v1.0.1", SHA_NEXT), drifted_dist)

    def test_unmarked_legacy_and_manual_tags_are_not_auto_recovered(self) -> None:
        with self.subTest("v1.0.0 without release skips when no release changes"):
            gh = FakeGitHub(head=SHA_NEXT, tags=self._base_tags(), changed_paths=["docs/index.md"])
            self.assertEqual(release_cli.plan(gh, "scheduled"), Candidate("skip"))

        with self.subTest("v1.0.0 without release advances to v1.0.1 on code change"):
            gh = FakeGitHub(head=SHA_NEXT, tags=self._base_tags(), changed_paths=["main.go"])
            self.assertEqual(release_cli.plan(gh, "scheduled"), Candidate("new", "v1.0.1", SHA_NEXT))

        with self.subTest("unmarked v1.0.1 without release is never auto-recovered"):
            tags = {**self._base_tags(), "v1.0.1": Tag("v1.0.1", SHA_NEXT, True, annotation="manual tag")}
            gh = FakeGitHub(head=SHA_NEXT, tags=tags, changed_paths=[])
            self.assertEqual(release_cli.plan(gh, "scheduled"), Candidate("skip"))

        with self.subTest("manual plan skips v1.0.0, accepts unmarked v1.0.1, rejects marked tag"):
            tags = {
                **self._base_tags(),
                "v1.0.1": Tag("v1.0.1", SHA_NEXT, True),
                "v1.0.2": Tag("v1.0.2", SHA_LATER, True, MARKER + SHA_LATER),
            }
            gh = FakeGitHub(head=SHA_LATER, tags=tags)
            self.assertEqual(release_cli.plan(gh, "manual", "v1.0.0"), Candidate("skip"))
            self.assertEqual(release_cli.plan(gh, "manual", "v1.0.1"), Candidate("manual", "v1.0.1", SHA_NEXT))
            with self.assertRaisesRegex(ReleaseError, "marked"):
                release_cli.plan(gh, "manual", "v1.0.2")

    def test_conflicting_tag_marker_record_sha_and_stale_reservation_fail_closed(self) -> None:
        with self.subTest("marker SHA disagreeing with peeled commit raises"):
            tags = {**self._base_tags(), "v1.0.1": Tag("v1.0.1", SHA_NEXT, True, MARKER + SHA_BASE)}
            gh = FakeGitHub(head=SHA_NEXT, tags=tags)
            with self.assertRaisesRegex(ReleaseError, "annotation"):
                release_cli.plan(gh, "scheduled")

        with self.subTest("record SHA disagreeing with tag SHA raises"):
            tags = {**self._base_tags(), "v1.0.1": Tag("v1.0.1", SHA_NEXT, True, MARKER + SHA_NEXT)}
            bad_draft = Release(30, "v1.0.1", True, "Watchtower v1.0.1", Record("v1.0.1", SHA_BASE, True))
            gh = FakeGitHub(head=SHA_NEXT, tags=tags, releases={"v1.0.1": bad_draft})
            with self.assertRaisesRegex(ReleaseError, "conflicts"):
                release_cli.plan(gh, "scheduled")

        with self.subTest("leftover untagged draft for older version blocks scheduled planning"):
            tags = {**self._base_tags(), "v1.0.2": Tag("v1.0.2", SHA_LATER, True)}
            stale_draft = Release(31, "v1.0.1", True, "Watchtower v1.0.1", Record("v1.0.1", SHA_NEXT, True))
            gh = FakeGitHub(
                head=SHA_LATER,
                tags=tags,
                releases={"v1.0.1": stale_draft},
                ancestors={(SHA_NEXT, SHA_LATER)},
            )
            with self.assertRaisesRegex(ReleaseError, "conflicts"):
                release_cli.plan(gh, "scheduled")

    def test_record_image_idempotency_and_digest_conflict(self) -> None:
        dist = make_dist(self.tmp, "v1.0.1")
        attested = replace(Record("v1.0.1", SHA_NEXT, True).with_assets(dist), archives_attested=True)
        draft = Release(40, "v1.0.1", True, "Watchtower v1.0.1", attested)
        gh = FakeGitHub(head=SHA_NEXT, tags=self._base_tags(), releases={"v1.0.1": draft})
        cand = Candidate("recover", "v1.0.1", SHA_NEXT)

        release_cli.record_image(gh, cand, DIGEST_A)
        self.assertEqual(gh.releases()["v1.0.1"].record.image_digest, DIGEST_A)
        self.assertEqual(len(gh.calls), 1)

        # Re-recording the same digest is idempotent (no additional save).
        release_cli.record_image(gh, cand, DIGEST_A)
        self.assertEqual(len(gh.calls), 1)

        # Recording a different digest fails closed.
        with self.assertRaisesRegex(ReleaseError, "different content"):
            release_cli.record_image(gh, cand, DIGEST_B)

        # Recording a new digest on a published (non-draft) release fails closed.
        published = Release(41, "v1.0.1", False, "Watchtower v1.0.1", attested)
        gh_pub = FakeGitHub(head=SHA_NEXT, tags=self._base_tags(), releases={"v1.0.1": published})
        with self.assertRaisesRegex(ReleaseError, "draft"):
            release_cli.record_image(gh_pub, cand, DIGEST_A)

    def test_publish_and_finish_latest_promotion_and_idempotency(self) -> None:
        dist = make_dist(self.tmp, "v1.0.1")
        ready = replace(
            Record("v1.0.1", SHA_NEXT, True).with_assets(dist),
            archives_attested=True,
            image_digest=DIGEST_A,
        )
        all_assets = [asset_entry(RECORD, ready.manifest())] + [
            asset_entry(name, (dist / name).read_bytes()) for name in sorted(ready.assets)
        ]
        tags = {**self._base_tags(), "v1.0.1": Tag("v1.0.1", SHA_NEXT, True, MARKER + SHA_NEXT)}
        cand = Candidate("recover", "v1.0.1", SHA_NEXT)

        with self.subTest("highest tag publishes with latest=True, retry is no-op, finish completes"):
            draft = Release(50, "v1.0.1", True, "Watchtower v1.0.1", ready)
            gh = FakeGitHub(head=SHA_NEXT, tags=tags, releases={"v1.0.1": draft}, assets={50: all_assets})

            release_cli.publish(gh, cand)
            self.assertEqual([(c[0], c[2], c[3]) for c in gh.calls], [("save", True, True)])
            self.assertFalse(gh.releases()["v1.0.1"].draft)

            # Second publish on already-published release makes no writes.
            release_cli.publish(gh, cand)
            self.assertEqual(len(gh.calls), 1)

            # Before finish, scheduled plan selects recover_published.
            self.assertEqual(release_cli.plan(gh, "scheduled"), Candidate("recover_published", "v1.0.1", SHA_NEXT))
            release_cli.finish(gh, cand, promoted=True)
            self.assertTrue(gh.releases()["v1.0.1"].record.latest_done)
            # After finish, scheduled plan with no new changes skips.
            self.assertEqual(release_cli.plan(gh, "scheduled"), Candidate("skip"))

        with self.subTest("non-highest tag publishes with latest=False"):
            higher_tags = {**tags, "v1.0.2": Tag("v1.0.2", SHA_LATER, True)}
            draft = Release(51, "v1.0.1", True, "Watchtower v1.0.1", ready)
            gh = FakeGitHub(head=SHA_LATER, tags=higher_tags, releases={"v1.0.1": draft}, assets={51: all_assets})
            release_cli.publish(gh, cand)
            self.assertEqual([(c[0], c[2], c[3]) for c in gh.calls], [("save", True, False)])

    def test_prepare_deactivates_only_when_unreserved_head_advances(self) -> None:
        with self.subTest("new candidate deactivates when main moved during CI"):
            gh = FakeGitHub(head=SHA_LATER, tags=self._base_tags(), changed_paths=["main.go"])
            out = capture_outputs(release_cli.prepare, gh, Candidate("new", "v1.0.1", SHA_NEXT))
            self.assertEqual(out, {"active": "false"})
            self.assertEqual(gh.calls, [])

        with self.subTest("reserved draft remains active when main moved ahead"):
            draft = Release(60, "v1.0.1", True, "Watchtower v1.0.1", Record("v1.0.1", SHA_NEXT, True))
            gh = FakeGitHub(
                head=SHA_LATER,
                tags=self._base_tags(),
                releases={"v1.0.1": draft},
                ancestors={(SHA_NEXT, SHA_LATER)},
            )
            out = capture_outputs(release_cli.prepare, gh, Candidate("reserved", "v1.0.1", SHA_NEXT))
            self.assertEqual(out["active"], "true")

    def test_scheduled_path_selection_5_4(self) -> None:
        gh_docs = FakeGitHub(head=SHA_NEXT, tags=self._base_tags(), changed_paths=["docs/usage.md", "README.md"])
        self.assertEqual(release_cli.plan(gh_docs, "scheduled"), Candidate("skip"))

        gh_code = FakeGitHub(head=SHA_NEXT, tags=self._base_tags(), changed_paths=["docs/usage.md", "pkg/api/api.go"])
        self.assertEqual(release_cli.plan(gh_code, "scheduled"), Candidate("new", "v1.0.1", SHA_NEXT))

        cases = [
            (".go-version", True),
            ("go.mod", True),
            ("tools/go.mod", True),
            ("dockerfiles/Dockerfile", True),
            ("goreleaser.yml", True),
            (".github/workflows/release.yml", True),
            ("docs/index.md", False),
            ("README.md", False),
            ("pkg/notifications/tpl/mail.tmpl", False),
            (".github/CODEOWNERS", False),
            (".github/workflows/ci.yml", False),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(has_release_changes([path]), expected)

    def test_release_image_version_conflict_and_latest_promotion(self) -> None:
        dist = make_dist(self.tmp, "v1.0.1")
        published_rec = replace(
            Record("v1.0.1", SHA_NEXT, True).with_assets(dist),
            archives_attested=True,
            image_digest=DIGEST_A,
        )
        published = Release(70, "v1.0.1", False, "Watchtower v1.0.1", published_rec)
        tags = {**self._base_tags(), "v1.0.1": Tag("v1.0.1", SHA_NEXT, True, MARKER + SHA_NEXT)}

        with self.subTest("stage rejects existing version tag when digest differs or is unrecorded"):
            gh = FakeGitHub(head=SHA_NEXT, tags=tags, releases={"v1.0.1": published})
            reg = FakeRegistry(gh.repo, {"1.0.1": DIGEST_B})
            with mock.patch.object(release_image, "Registry", return_value=reg), mock.patch.object(release_image, "run"):
                with self.assertRaisesRegex(ReleaseError, "differs"):
                    capture_outputs(release_image.handle, gh, "stage", "v1.0.1", SHA_NEXT, DIGEST_A)
                with self.assertRaisesRegex(ReleaseError, "differs"):
                    capture_outputs(release_image.handle, gh, "stage", "v1.0.1", SHA_NEXT, "")

        with self.subTest("version assigns once and is idempotent on retry"):
            gh = FakeGitHub(head=SHA_NEXT, tags=tags, releases={"v1.0.1": published})
            reg = FakeRegistry(gh.repo, {DIGEST_A: DIGEST_A})
            with mock.patch.object(release_image, "Registry", return_value=reg), mock.patch.object(release_image, "run"):
                capture_outputs(release_image.handle, gh, "version", "v1.0.1", SHA_NEXT, DIGEST_A)
                self.assertEqual(reg.assigned, [("1.0.1", DIGEST_A)])
                capture_outputs(release_image.handle, gh, "version", "v1.0.1", SHA_NEXT, DIGEST_A)
                self.assertEqual(reg.assigned, [("1.0.1", DIGEST_A)])

        with self.subTest("latest promotes once, is idempotent, and skips when superseded"):
            gh = FakeGitHub(head=SHA_NEXT, tags=tags, releases={"v1.0.1": published})
            reg = FakeRegistry(gh.repo, {DIGEST_A: DIGEST_A, "1.0.1": DIGEST_A, "latest": DIGEST_B})
            with mock.patch.object(release_image, "Registry", return_value=reg), mock.patch.object(release_image, "run"):
                out = capture_outputs(release_image.handle, gh, "latest", "v1.0.1", SHA_NEXT, DIGEST_A)
                self.assertEqual(out, {"promoted": "true"})
                self.assertEqual(reg.assigned, [("latest", DIGEST_A)])

                # Retry when latest already points at DIGEST_A makes no assign call.
                out2 = capture_outputs(release_image.handle, gh, "latest", "v1.0.1", SHA_NEXT, DIGEST_A)
                self.assertEqual(out2, {"promoted": "true"})
                self.assertEqual(reg.assigned, [("latest", DIGEST_A)])

            # Higher tag on main prevents promoting an older tag to latest.
            higher_tags = {**tags, "v1.0.2": Tag("v1.0.2", SHA_LATER, True)}
            gh_superseded = FakeGitHub(head=SHA_LATER, tags=higher_tags, releases={"v1.0.1": published})
            reg2 = FakeRegistry(gh.repo, {DIGEST_A: DIGEST_A, "1.0.1": DIGEST_A, "latest": DIGEST_B})
            with mock.patch.object(release_image, "Registry", return_value=reg2), mock.patch.object(release_image, "run"):
                out3 = capture_outputs(release_image.handle, gh_superseded, "latest", "v1.0.1", SHA_NEXT, DIGEST_A)
                self.assertEqual(out3, {"promoted": "false"})
                self.assertEqual(reg2.assigned, [])

        with self.subTest("latest rejects draft release"):
            draft = Release(71, "v1.0.1", True, "Watchtower v1.0.1", published_rec)
            gh_draft = FakeGitHub(head=SHA_NEXT, tags=tags, releases={"v1.0.1": draft})
            reg = FakeRegistry(gh_draft.repo, {DIGEST_A: DIGEST_A, "1.0.1": DIGEST_A})
            with mock.patch.object(release_image, "Registry", return_value=reg), mock.patch.object(release_image, "run"):
                with self.assertRaisesRegex(ReleaseError, "published"):
                    capture_outputs(release_image.handle, gh_draft, "latest", "v1.0.1", SHA_NEXT, DIGEST_A)

    def test_archive_attestation_verification_binds_to_candidate_sha(self) -> None:
        dist = make_dist(self.tmp, "v1.0.1")
        record = Record("v1.0.1", SHA_NEXT, True).with_assets(dist)
        draft = Release(80, "v1.0.1", True, "Watchtower v1.0.1", record)
        tags = {**self._base_tags(), "v1.0.1": Tag("v1.0.1", SHA_NEXT, True, MARKER + SHA_NEXT)}
        cand = Candidate("recover", "v1.0.1", SHA_NEXT)
        attested_name = ARCHIVES[0]
        attested_digest = record.assets[attested_name]

        def fake_api(path: str, **kwargs) -> dict:
            if path == f"attestations/sha256:{attested_digest}?per_page=1":
                return {"attestations": [{}]}
            return {"attestations": []}

        with self.subTest("rejects attestation signed for newer run SHA and writes no outputs"):
            gh = FakeGitHub(
                head=SHA_LATER,
                tags=tags,
                releases={"v1.0.1": draft},
                ancestors={(SHA_NEXT, SHA_LATER)},
            )
            out_file = self.tmp / "archive_reject.out"
            calls: list[tuple[str, ...]] = []
            env = {"GITHUB_OUTPUT": str(out_file), "GITHUB_SHA": SHA_LATER}
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(gh, "api", create=True, side_effect=fake_api),
                mock.patch.object(release_cli, "run", side_effect=make_fake_verifier(SHA_LATER, calls)),
            ):
                with self.assertRaises(ReleaseError):
                    release_cli.archive_attestations(gh, cand, dist)
            self.assertEqual(len(calls), 1)
            self.assertFalse(out_file.exists())
            self.assertEqual(gh.calls, [])

        with self.subTest("accepts attestation signed for candidate SHA during newer run"):
            gh = FakeGitHub(
                head=SHA_LATER,
                tags=tags,
                releases={"v1.0.1": draft},
                ancestors={(SHA_NEXT, SHA_LATER)},
            )
            out_file = self.tmp / "archive_accept.out"
            calls = []
            env = {"GITHUB_OUTPUT": str(out_file), "GITHUB_SHA": SHA_LATER}
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(gh, "api", create=True, side_effect=fake_api),
                mock.patch.object(release_cli, "run", side_effect=make_fake_verifier(SHA_NEXT, calls)),
            ):
                release_cli.archive_attestations(gh, cand, dist)
            self.assertEqual(len(calls), 1)
            written = out_file.read_text(encoding="utf-8")
            self.assertIn("subjects<<END_SUBJECTS\n", written)
            self.assertNotIn((dist / attested_name).as_posix(), written)
            self.assertEqual(gh.calls, [])

    def test_api_missing_ok_only_absorbs_404(self) -> None:
        github = GitHub("owner/watchtower", self.tmp)
        not_found = subprocess.CompletedProcess([], 1, "", "gh: Not Found (HTTP 404)\n")
        forbidden = subprocess.CompletedProcess([], 1, "", "gh: Forbidden (HTTP 403)\n")
        with mock.patch.object(release_gh.subprocess, "run", return_value=not_found):
            self.assertIsNone(github.api("attestations/sha256:abc", missing_ok=True))
            with self.assertRaises(ReleaseError):
                github.api("attestations/sha256:abc")
        with mock.patch.object(release_gh.subprocess, "run", return_value=forbidden):
            with self.assertRaisesRegex(ReleaseError, "HTTP 403"):
                github.api("attestations/sha256:abc", missing_ok=True)

    def test_archive_attestations_attests_subjects_the_api_reports_as_missing(self) -> None:
        dist = make_dist(self.tmp, "v1.0.1")
        record = Record("v1.0.1", SHA_NEXT, True).with_assets(dist)
        draft = Release(82, "v1.0.1", True, "Watchtower v1.0.1", record)
        gh = FakeGitHub(head=SHA_NEXT, tags=self._base_tags(), releases={"v1.0.1": draft})
        out_file = self.tmp / "archive_missing.out"
        env = {"GITHUB_OUTPUT": str(out_file)}
        # GitHub answers 404 (no list) for digests without attestations.
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(gh, "api", create=True, return_value=None),
        ):
            release_cli.archive_attestations(gh, Candidate("recover", "v1.0.1", SHA_NEXT), dist)
        written = out_file.read_text(encoding="utf-8")
        for name in record.assets:
            self.assertIn((dist / name).as_posix(), written)

    def test_image_attestation_verification_binds_to_candidate_sha(self) -> None:
        dist = make_dist(self.tmp, "v1.0.1")
        published_rec = replace(
            Record("v1.0.1", SHA_NEXT, True).with_assets(dist),
            archives_attested=True,
            image_digest=DIGEST_A,
        )
        published = Release(81, "v1.0.1", False, "Watchtower v1.0.1", published_rec)
        tags = {**self._base_tags(), "v1.0.1": Tag("v1.0.1", SHA_NEXT, True, MARKER + SHA_NEXT)}
        gh = FakeGitHub(
            head=SHA_LATER,
            tags=tags,
            releases={"v1.0.1": published},
            ancestors={(SHA_NEXT, SHA_LATER)},
        )

        for operation, initial_digests, expected_tag in (
            ("version", {DIGEST_A: DIGEST_A}, "1.0.1"),
            ("latest", {DIGEST_A: DIGEST_A, "1.0.1": DIGEST_A, "latest": DIGEST_B}, "latest"),
        ):
            with self.subTest(operation=operation, case="wrong source blocks assignment"):
                reg = FakeRegistry(gh.repo, initial_digests)
                calls: list[tuple[str, ...]] = []
                with (
                    mock.patch.dict(os.environ, {"GITHUB_SHA": SHA_LATER}),
                    mock.patch.object(release_image, "Registry", return_value=reg),
                    mock.patch.object(release_image, "run", side_effect=make_fake_verifier(SHA_LATER, calls)),
                ):
                    with self.assertRaises(ReleaseError):
                        capture_outputs(release_image.handle, gh, operation, "v1.0.1", SHA_NEXT, DIGEST_A)
                self.assertEqual(len(calls), 1)
                self.assertEqual(reg.assigned, [])

            with self.subTest(operation=operation, case="candidate source allows assignment"):
                reg = FakeRegistry(gh.repo, initial_digests)
                calls = []
                with (
                    mock.patch.dict(os.environ, {"GITHUB_SHA": SHA_LATER}),
                    mock.patch.object(release_image, "Registry", return_value=reg),
                    mock.patch.object(release_image, "run", side_effect=make_fake_verifier(SHA_NEXT, calls)),
                ):
                    out = capture_outputs(release_image.handle, gh, operation, "v1.0.1", SHA_NEXT, DIGEST_A)
                self.assertEqual(len(calls), 1)
                self.assertEqual(reg.assigned, [(expected_tag, DIGEST_A)])
                if operation == "latest":
                    self.assertEqual(out, {"promoted": "true"})

    def test_save_resends_tag_name_so_untagged_drafts_keep_it(self) -> None:
        record = Record("v1.0.1", SHA_NEXT, True)
        draft = Release(101, "v1.0.1", True, "Watchtower v1.0.1", record)
        github = GitHub("owner/watchtower", self.tmp)
        sent: list[dict] = []

        def api(path, *, pages=False, method="GET", data=None):
            sent.append(data)
            # GitHub drops the tag name of an untagged draft when it is omitted.
            return {"id": 101, "tag_name": data.get("tag_name", "untagged-0123"),
                    "body": data["body"], "draft": True, "prerelease": False}

        with mock.patch.object(github, "api", side_effect=api):
            saved = github.save(draft, record)
        self.assertEqual(sent[0]["tag_name"], "v1.0.1")
        self.assertEqual((saved.tag, saved.record), ("v1.0.1", record))


if __name__ == "__main__":
    unittest.main()
