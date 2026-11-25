"""Paths that can change a Watchtower release's archives or image."""

from collections.abc import Iterable
from pathlib import PurePosixPath

RELEASE_FILES = frozenset({
    ".go-version",
    ".dockerignore",
    "go.mod",
    "go.sum",
    "tools/go.mod",
    "tools/go.sum",
    "LICENSE.md",
    "goreleaser.yml",
    ".github/actions/docker-build/action.yml",
    ".github/workflows/docker-publish.yml",
    ".github/workflows/release.yml",
    ".github/scripts/release_cli.py",
    ".github/scripts/release_gh.py",
    ".github/scripts/release_image.py",
    ".github/scripts/release_record.py",
})


def affects_release(path: str) -> bool:
    """Match repository-relative paths from git diff, including deletions."""
    if path in RELEASE_FILES:
        return True
    if path.startswith((".github/", "docs/")):
        return False
    name = PurePosixPath(path).name
    if name.lower().endswith((".md", ".tmpl", ".tpl")) or name == "CODEOWNERS":
        return False
    return path.startswith("dockerfiles/") or name.endswith(".go")


def has_release_changes(paths: Iterable[str]) -> bool:
    return any(affects_release(path) for path in paths)
