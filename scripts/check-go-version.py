#!/usr/bin/env python3
"""Assert that every place that names the Go version agrees with .go-version.

Checked invariants:

* ``.go-version`` holds one exact patch version (``MAJOR.MINOR.PATCH``).
* The three Dockerfiles build from ``golang:<version>-alpine@sha256:<digest>``
  whose <version> matches ``.go-version`` exactly and whose digest is a full
  sha256.
* The ``go`` directive in ``go.mod`` and ``tools/go.mod`` is in the same
  MAJOR.MINOR series as ``.go-version``. The directive is the *minimum*
  language version and is allowed to lag the patch level; a different
  minor/major series is drift and fails.

Usage:
    python scripts/check-go-version.py [--root DIR]

Exit status: 0 = consistent, 1 = drift or unreadable/malformed input,
2 = usage error.
"""

import argparse
import re
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

DOCKERFILES = (
    "dockerfiles/Dockerfile",
    "dockerfiles/Dockerfile.self-contained",
    "dockerfiles/Dockerfile.dev-self-contained",
)
GO_MODS = ("go.mod", "tools/go.mod")

GO_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
GO_DIRECTIVE_RE = re.compile(r"^go\s+(\d+)\.(\d+)(?:\.(\d+))?\s*$")
FROM_ANY_GOLANG_RE = re.compile(
    r"^\s*FROM\s+(?:--platform=\S+\s+)?(?:(?:docker\.io/)?(?:library/)?)?golang(?:[:@\s]|$)",
    re.IGNORECASE,
)
FROM_GOLANG_RE = re.compile(
    r"^\s*FROM\s+(?:--platform=\S+\s+)?golang:"
    r"(?P<tag>\d+\.\d+\.\d+)-alpine@sha256:(?P<digest>[0-9a-f]{64})\s*(?:AS\s+\S+)?\s*$",
    re.IGNORECASE,
)


class CheckError(Exception):
    """Raised when an input file is missing or malformed."""


def _read(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CheckError(f"cannot read {path}: {exc.strerror or exc}") from exc


def parse_go_version(text):
    """Return (major, minor, patch) from .go-version contents."""
    value = text.strip()
    match = GO_VERSION_RE.match(value)
    if not match:
        raise CheckError(
            f".go-version must be one exact version like 1.26.11, got {value!r}"
        )
    return tuple(int(part) for part in match.groups())


def parse_go_directive(path, text):
    """Return (major, minor) from the first go directive in a go.mod file."""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("go ") or line.startswith("go\t"):
            match = GO_DIRECTIVE_RE.match(line)
            if not match:
                raise CheckError(f"{path}: malformed go directive {line!r}")
            return int(match.group(1)), int(match.group(2))
    raise CheckError(f"{path}: no go directive found")


def parse_dockerfile_golang_images(path, text):
    """Return a list of (tag, digest) for every golang FROM line in a Dockerfile."""
    images = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not FROM_ANY_GOLANG_RE.match(line):
            continue
        match = FROM_GOLANG_RE.match(line)
        if not match:
            raise CheckError(
                f"{path}:{lineno}: golang FROM line is not "
                f"'golang:<x.y.z>-alpine@sha256:<64 hex>': {line!r}"
            )
        images.append((match.group("tag"), match.group("digest")))
    if not images:
        raise CheckError(f"{path}: no pinned golang FROM line found")
    return images


def collect(root):
    """Validate everything under root and return a list of problems."""
    root = Path(root)
    problems = []

    try:
        go_version = parse_go_version(_read(root / ".go-version"))
    except CheckError as exc:
        return [str(exc)]
    major, minor, patch = go_version

    for rel in DOCKERFILES:
        try:
            images = parse_dockerfile_golang_images(rel, _read(root / rel))
        except CheckError as exc:
            problems.append(str(exc))
            continue
        for tag, _digest in images:
            if tag != f"{major}.{minor}.{patch}":
                problems.append(
                    f"{rel}: golang image tag {tag} does not match "
                    f".go-version {major}.{minor}.{patch}"
                )

    for rel in GO_MODS:
        try:
            mod_major, mod_minor = parse_go_directive(rel, _read(root / rel))
        except CheckError as exc:
            problems.append(str(exc))
            continue
        if (mod_major, mod_minor) != (major, minor):
            problems.append(
                f"{rel}: go directive {mod_major}.{mod_minor} is not in the "
                f"same series as .go-version {major}.{minor}.{patch}"
            )

    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        default=str(DEFAULT_ROOT),
        help="repository root (default: parent of this script's directory)",
    )
    args = parser.parse_args(argv)

    problems = collect(args.root)
    if problems:
        print("check-go-version: FAIL", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print("check-go-version: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
