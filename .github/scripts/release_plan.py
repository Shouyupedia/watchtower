"""Pure release selection and validation shared by both entry workflows."""

import re
from collections.abc import Iterable
from dataclasses import dataclass

FORMAL_TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")
MARKER = "watchtower-auto-release sha="
LEGACY_TAG = "v1.0.0"


class ReleaseError(ValueError):
    """Release state is inconsistent or a precondition failed."""


def version(tag: str) -> tuple[int, int, int]:
    match = FORMAL_TAG.fullmatch(tag)
    if not match:
        raise ReleaseError(f"expected a formal tag, got {tag!r}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def validate_sha(sha: str) -> str:
    if not SHA.fullmatch(sha):
        raise ReleaseError(f"expected a commit SHA, got {sha!r}")
    return sha


@dataclass(frozen=True)
class Tag:
    name: str
    sha: str
    on_main: bool
    annotation: str = ""

    @property
    def marked(self) -> bool:
        if self.annotation.startswith(MARKER) and self.annotation != MARKER + self.sha:
            raise ReleaseError(f"{self.name}: annotation does not match peeled commit")
        return self.annotation == MARKER + self.sha


@dataclass(frozen=True)
class Candidate:
    mode: str
    tag: str = ""
    sha: str = ""


def highest_on_main(tags: Iterable[Tag]) -> Tag | None:
    return max((tag for tag in tags if tag.on_main), key=lambda tag: version(tag.name), default=None)


def scheduled_candidate(
    head: str,
    tags: dict[str, Tag],
    untagged_reservations: dict[str, str],
    incomplete: set[str],
    pending_promotion: set[str],
    changed: bool,
) -> Candidate:
    """Select a reservation, an interrupted marked tag, or a new patch."""
    highest = highest_on_main(tags.values())
    base = max(version(LEGACY_TAG), version(highest.name) if highest else (0, 0, 0))
    major, minor, patch = base
    next_tag = f"v{major}.{minor}.{patch + 1}"
    if untagged_reservations:
        if set(untagged_reservations) != {next_tag} or next_tag in tags:
            raise ReleaseError("untagged reservation conflicts with current version history")
        return Candidate("reserved", next_tag, untagged_reservations[next_tag])
    if highest and version(highest.name) > version(LEGACY_TAG) and highest.marked:
        if highest.name in incomplete:
            return Candidate("recover", highest.name, highest.sha)
        if highest.name in pending_promotion:
            return Candidate("recover_published", highest.name, highest.sha)
    if not changed:
        return Candidate("skip")
    if next_tag in tags:
        raise ReleaseError(f"{next_tag} already exists outside main")
    return Candidate("new", next_tag, head)


def manual_candidate(tag: Tag | None, name: str) -> Candidate:
    if version(name) <= version(LEGACY_TAG):
        return Candidate("skip")
    if tag is None or not tag.on_main:
        raise ReleaseError(f"{name}: tag must identify a commit in main history")
    if tag.marked:
        raise ReleaseError(f"{name}: use the scheduled release workflow for marked tags")
    return Candidate("manual", name, tag.sha)
