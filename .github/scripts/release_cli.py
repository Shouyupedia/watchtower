"""Actions entry points for release selection, draft journal, and asset uploads."""

import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

# Isolated mode omits the script directory from sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_gh import GitHub, Release, run
from release_paths import has_release_changes
from release_plan import (
    LEGACY_TAG, Candidate, ReleaseError, highest_on_main, manual_candidate,
    scheduled_candidate, validate_sha, version,
)
from release_record import RECORD, Record, missing_assets


def output(**values: str) -> None:
    print(json.dumps(values, sort_keys=True))
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")


def plan(github: GitHub, source: str, name: str = "", *, verified_sha: str = "") -> Candidate:
    head = github.head()
    tags = github.tags(head)
    if source == "manual":
        candidate = manual_candidate(tags.get(name), name)
        if candidate.mode == "skip":
            return candidate
        release = github.releases().get(name)
        if release and not release.draft:
            if not release.record:
                return Candidate("skip")
            if release.record.sha != candidate.sha or release.record.automatic:
                raise ReleaseError(f"{name}: manual release journal conflicts with tag")
            if release.record.latest_done or highest_on_main(tags.values()) != tags[name]:
                return Candidate("skip")
            return Candidate("manual_published", name, candidate.sha)
        return candidate

    releases = github.releases()
    highest = highest_on_main(tags.values())
    reservations = {}
    incomplete = set()
    pending_promotion = set()
    for tag, release in releases.items():
        record = release.record
        if not record or not record.automatic or version(tag) <= version(LEGACY_TAG):
            continue
        if tag not in tags:
            if not release.draft or not github.ancestor(record.sha, head):
                raise ReleaseError(f"{tag}: invalid untagged reservation")
            reservations[tag] = record.sha
        elif tags[tag].sha != record.sha or not tags[tag].marked:
            raise ReleaseError(f"{tag}: automatic release journal conflicts with tag")
        elif not release.draft and not record.latest_done:
            pending_promotion.add(tag)
    if highest:
        release = releases.get(highest.name)
        if release is None or release.draft:
            incomplete.add(highest.name)
    candidate = scheduled_candidate(head, tags, reservations, incomplete, pending_promotion, False)
    if candidate.mode != "skip":
        return candidate
    if verified_sha and head != verified_sha:
        return Candidate("skip")
    base = highest.sha if highest else "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    if not has_release_changes(github.changed_paths(base, head)):
        return candidate
    return scheduled_candidate(head, tags, reservations, incomplete, pending_promotion, True)


def check_record(release: Release, candidate: Candidate) -> Record:
    record = release.record
    automatic = candidate.mode not in {"manual", "manual_published"}
    if not record or record.sha != candidate.sha or record.automatic != automatic:
        raise ReleaseError(f"{candidate.tag}: release does not match the verified tag and commit")
    return record


def require_record(github: GitHub, candidate: Candidate) -> tuple[Release, Record]:
    release = github.releases().get(candidate.tag)
    if not release:
        raise ReleaseError(f"{candidate.tag}: managed release journal is missing")
    return release, check_record(release, candidate)


def prepare(github: GitHub, candidate: Candidate) -> None:
    automatic = candidate.mode not in {"manual", "manual_published"}
    current = plan(github, "scheduled" if automatic else "manual", candidate.tag, verified_sha=candidate.sha)
    # Retries can advance from new to reserved or from draft to published.
    # Only the verified tag and SHA must remain the same.
    if (current.tag, current.sha) != (candidate.tag, candidate.sha):
        print(f"Verified candidate changed to {current}; rerun the entry workflow.")
        output(active="false")
        return
    release = github.releases().get(candidate.tag)
    if not release:
        release = github.reserve(Record(candidate.tag, candidate.sha, automatic))
    elif not release.record:
        # The marked tag can replace a lost reservation. Leave any existing
        # content untouched when its origin cannot be verified.
        if current.mode != "recover" or not release.draft or github.assets(release):
            raise ReleaseError(f"{candidate.tag}: an unmanaged release already exists")
        release = github.save(release, Record(candidate.tag, candidate.sha, automatic))
    record = check_record(release, candidate)
    assets = github.assets(release)
    missing = missing_assets(record, assets)
    has_manifest = any(asset["name"] == RECORD for asset in assets)
    if not release.draft and (missing or not has_manifest or not record.image_digest):
        raise ReleaseError("published release has incomplete records or assets")
    build = bool(missing or not record.archives_attested or not has_manifest)
    output(active="true", build=str(build).lower(), image_digest=record.image_digest,
           draft=str(release.draft).lower())


def stage_assets(github: GitHub, candidate: Candidate, dist: Path) -> None:
    release, record = require_record(github, candidate)
    if not release.draft:
        raise ReleaseError("asset writes require a draft release")
    updated = record.with_assets(dist)
    assets = github.assets(release)
    missing = missing_assets(record, assets)
    if record != updated:
        release = github.save(release, updated)
    if not any(asset["name"] == RECORD for asset in assets):
        with tempfile.TemporaryDirectory(prefix="watchtower-release-") as temp:
            manifest = Path(temp) / RECORD
            manifest.write_bytes(updated.manifest())
            github.upload(release, manifest)

    head = github.head()
    tags = github.tags(head)
    tag = tags.get(candidate.tag)
    if tag is None:
        if not record.automatic or not github.ancestor(record.sha, head):
            raise ReleaseError("cannot create a tag outside current main history")
        highest = highest_on_main(tags.values())
        if highest and version(highest.name) >= version(record.tag):
            raise ReleaseError("a higher formal tag appeared during the build")
        github.create_tag(record)
    elif tag.sha != record.sha or not tag.on_main or tag.marked != record.automatic:
        raise ReleaseError("tag changed during the release build")
    for name in sorted(missing):
        github.upload(release, dist / name)
    assets = github.assets(release)
    if missing_assets(updated, assets) or not any(asset["name"] == RECORD for asset in assets):
        raise ReleaseError("release assets did not finish uploading")
    output(attest=str(not record.archives_attested).lower())


def verify_archive(github: GitHub, path: Path, sha: str) -> None:
    run("gh", "attestation", "verify", str(path), "--repo", github.repo,
        "--signer-workflow", f"{github.repo}/.github/workflows/release.yml",
        "--source-digest", sha, "--deny-self-hosted-runners")


def archive_attestations(github: GitHub, candidate: Candidate, dist: Path) -> None:
    _, record = require_record(github, candidate)
    subjects = []
    for name, digest in sorted(record.assets.items()):
        attestations = github.api(f"attestations/sha256:{digest}?per_page=1")["attestations"]
        if not attestations:
            subjects.append((dist / name).as_posix())
            continue
        verify_archive(github, dist / name, candidate.sha)
    # The action accepts newline-delimited subject paths.
    if subjects:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("subjects<<END_SUBJECTS\n" + "\n".join(subjects) + "\nEND_SUBJECTS\n")


def record_archives(github: GitHub, candidate: Candidate) -> None:
    release, record = require_record(github, candidate)
    if not release.draft or not record.assets or missing_assets(record, github.assets(release)):
        raise ReleaseError("cannot mark incomplete assets attested")
    github.save(release, replace(record, archives_attested=True))


def record_image(github: GitHub, candidate: Candidate, digest: str) -> None:
    release, record = require_record(github, candidate)
    if record.image_digest:
        if record.image_digest != digest:
            raise ReleaseError("image digest is already reserved with different content")
        return
    if not release.draft or not digest:
        raise ReleaseError("recording image digest requires a draft release and a digest")
    github.save(release, replace(record, image_digest=digest))


def publish(github: GitHub, candidate: Candidate) -> None:
    release, record = require_record(github, candidate)
    head = github.head()
    tags = github.tags(head)
    tag = tags.get(candidate.tag)
    if not tag or not tag.on_main or tag.sha != record.sha or tag.marked != record.automatic:
        raise ReleaseError("cannot publish: tag no longer matches the release record")
    assets = github.assets(release)
    if not record.image_digest or missing_assets(record, assets) or not any(a["name"] == RECORD for a in assets):
        raise ReleaseError("cannot publish: release assets or image digest are missing")
    if release.draft:
        github.save(release, record, publish=True, latest=highest_on_main(tags.values()) == tag)


def finish(github: GitHub, candidate: Candidate, promoted: bool) -> None:
    release, record = require_record(github, candidate)
    if release.draft:
        raise ReleaseError("cannot finish an unpublished release")
    if promoted and not record.latest_done:
        github.save(release, replace(record, latest_done=True))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "resolve", "prepare", "stage-assets", "archive-attestations",
                                             "record-archives", "record-image", "publish", "finish"))
    parser.add_argument("--mode", default="", choices=("scheduled", "manual", "new", "reserved",
                                                       "recover", "recover_published", "manual_published"))
    parser.add_argument("--tag", default="")
    parser.add_argument("--sha", default="")
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--digest", default="")
    parser.add_argument("--promoted", choices=("true", "false"), default="false")
    args = parser.parse_args(argv)
    github = GitHub(os.environ["GITHUB_REPOSITORY"], Path.cwd())
    if args.command == "plan":
        candidate = plan(github, args.mode, args.tag)
        if args.sha and candidate.sha:
            # A push can report either the annotated object or its commit.
            event_commit = run("git", "rev-parse", "--verify", f"{validate_sha(args.sha)}^{{commit}}")
            if event_commit != candidate.sha:
                raise ReleaseError("tag moved after the push event")
        output(**asdict(candidate))
        return
    if version(args.tag) <= version(LEGACY_TAG):
        raise ReleaseError("historical versions are outside the managed release boundary")
    candidate = Candidate(args.mode, args.tag, validate_sha(args.sha))
    if args.command == "resolve":
        if args.mode not in {"manual", "manual_published", "new", "reserved", "recover", "recover_published"}:
            raise ReleaseError("expected a release candidate mode")
        output(**asdict(candidate))
    elif args.command == "prepare":
        prepare(github, candidate)
    elif args.command == "stage-assets":
        stage_assets(github, candidate, args.dist)
    elif args.command == "archive-attestations":
        archive_attestations(github, candidate, args.dist)
    elif args.command == "record-archives":
        record_archives(github, candidate)
    elif args.command == "record-image":
        record_image(github, candidate, args.digest)
    elif args.command == "publish":
        publish(github, candidate)
    elif args.command == "finish":
        finish(github, candidate, args.promoted == "true")


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError) as exc:
        sys.exit(f"release: {exc}")
