#!/usr/bin/env python3
"""Fork-compatible dependency vulnerability diff review backed by OSV.dev.

Compares dependency manifests between a base revision/directory and a head
revision/directory, then queries the OSV.dev batch API for every dependency
added or changed in head. Fails closed on malformed manifests, unpinned Python
requirements, OSV query/network/protocol failures, or any known vulnerability
on an added or updated dependency.

Supported manifests:
* Go modules: ``go.mod`` and ``**/go.mod`` (``require`` and ``replace``)
* Python lockfiles: ``*requirements*.txt`` (exact ``name==version`` pins)
* GitHub Actions: ``.github/workflows/*.{yml,yaml}`` and
  ``.github/actions/**/*.{yml,yaml}`` (external ``uses:`` references)

Usage:
    python -I scripts/dependency-review.py --base <git-ref> --head <git-ref>
    python -I scripts/dependency-review.py --base-dir <dir> --head-dir <dir>

Exit status: 0 = pass, 1 = vulnerable dependency or fail-closed error,
2 = usage error.
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_BATCH_SIZE = 500
USER_AGENT = "dependency-review/1.0"

SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
PEP503_RE = re.compile(r"[-_.]+")
PIP_PIN_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[[A-Za-z0-9._,\s-]+\])?"
    r"==(?P<version>[^\s;\\#]+)$"
)
# Matches external GitHub Actions references in both block and flow YAML:
#   uses: owner/repo@sha
#   - uses: "owner/repo/path@v1.2.3"
#   { name: foo, uses: owner/repo@sha }
YAML_USES_RE = re.compile(
    r"""(?:^|[\s,{])(?:-\s+)?["']?uses["']?\s*:\s*(?P<quote>["']?)(?P<ref>[^"'\s,#}\]]+)(?P=quote)"""
)
ACTION_REF_RE = re.compile(
    r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:/[^@]+)?@(?P<rev>[^\s]+)$"
)


class ReviewError(Exception):
    """Raised on malformed manifests, unreadable inputs, or OSV query failures."""


@dataclass(frozen=True, order=True)
class Dependency:
    ecosystem: str
    name: str
    version: str
    commit: str = ""

    def label(self):
        if self.commit:
            return f"{self.ecosystem} {self.name}@{self.commit}"
        return f"{self.ecosystem} {self.name}@{self.version}"

    def osv_query(self):
        if self.commit:
            return {"commit": self.commit}
        return {
            "package": {"ecosystem": self.ecosystem, "name": self.name},
            "version": self.version,
        }


def _strip_go_comment(line):
    in_quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if in_quote:
            if ch == "\\" and in_quote == '"':
                i += 2
                continue
            if ch == in_quote:
                in_quote = None
        else:
            if ch in ('"', "`"):
                in_quote = ch
            elif ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                return line[:i]
        i += 1
    return line


def _unquote_go_token(token):
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "`"):
        return token[1:-1]
    return token


def _is_local_go_replace(target):
    return (
        target.startswith("./")
        or target.startswith("../")
        or target.startswith("/")
        or target in (".", "..")
    )


def parse_go_mod(path, text):
    """Parse require/replace directives from go.mod and return a set of Dependency."""
    requires = []
    # Map (old_mod, old_ver_or_none) -> (new_mod, new_ver) or None (local replace)
    replaces = {}
    block = None

    def parse_require_entry(content, lineno):
        tokens = [_unquote_go_token(tok) for tok in content.split()]
        if len(tokens) != 2:
            raise ReviewError(
                f"{path}:{lineno}: malformed require entry: {content!r}"
            )
        mod, ver = tokens
        if not mod or not ver:
            raise ReviewError(
                f"{path}:{lineno}: empty module or version in require entry"
            )
        requires.append((mod, ver))

    def parse_replace_entry(content, lineno):
        if "=>" not in content:
            raise ReviewError(
                f"{path}:{lineno}: malformed replace entry (missing '=>'): {content!r}"
            )
        left, right = content.split("=>", 1)
        left_tokens = [_unquote_go_token(tok) for tok in left.split()]
        right_tokens = [_unquote_go_token(tok) for tok in right.split()]
        if len(left_tokens) == 1:
            key = (left_tokens[0], None)
        elif len(left_tokens) == 2:
            key = (left_tokens[0], left_tokens[1])
        else:
            raise ReviewError(
                f"{path}:{lineno}: malformed replace source: {left.strip()!r}"
            )
        if len(right_tokens) == 1 and _is_local_go_replace(right_tokens[0]):
            replaces[key] = None
        elif len(right_tokens) == 2:
            replaces[key] = (right_tokens[0], right_tokens[1])
        else:
            raise ReviewError(
                f"{path}:{lineno}: malformed replace target: {right.strip()!r}"
            )

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = _strip_go_comment(raw).strip()
        if not line:
            continue
        if block is not None:
            if line == ")":
                block = None
                continue
            if line.endswith(")"):
                inner = line[:-1].strip()
                if inner:
                    if block == "require":
                        parse_require_entry(inner, lineno)
                    elif block == "replace":
                        parse_replace_entry(inner, lineno)
                block = None
                continue
            if block == "require":
                parse_require_entry(line, lineno)
            elif block == "replace":
                parse_replace_entry(line, lineno)
            continue

        if line == "require" or line.startswith(("require ", "require\t", "require(")):
            rest = line[len("require"):].strip()
            if rest == "(":
                block = "require"
            elif rest.startswith("(") and rest.endswith(")"):
                inner = rest[1:-1].strip()
                if inner:
                    parse_require_entry(inner, lineno)
            elif rest:
                parse_require_entry(rest, lineno)
            else:
                raise ReviewError(f"{path}:{lineno}: bare require directive")
        elif line == "replace" or line.startswith(("replace ", "replace\t", "replace(")):
            rest = line[len("replace"):].strip()
            if rest == "(":
                block = "replace"
            elif rest.startswith("(") and rest.endswith(")"):
                inner = rest[1:-1].strip()
                if inner:
                    parse_replace_entry(inner, lineno)
            elif rest:
                parse_replace_entry(rest, lineno)
            else:
                raise ReviewError(f"{path}:{lineno}: bare replace directive")
        elif line.endswith("(") and line.split()[0] in ("exclude", "retract", "tool"):
            block = "skip"

    if block is not None:
        raise ReviewError(f"{path}: unclosed '{block}' block")

    deps = set()
    for mod, ver in requires:
        replacement = replaces.get((mod, ver))
        if replacement is None and (mod, ver) in replaces:
            continue
        if replacement is None and (mod, None) in replaces:
            replacement = replaces[(mod, None)]
            if replacement is None:
                continue
        if replacement is not None:
            mod, ver = replacement
        deps.add(Dependency("Go", mod, ver.lstrip("v")))
    return deps


def _normalize_pypi_name(name):
    return PEP503_RE.sub("-", name).lower()


def parse_requirements_txt(path, text, strict=True):
    """Parse exact pinned PyPI dependencies from a requirements.txt file.

    When ``strict`` is True (used for head), unpinned or option-bearing lines
    fail closed with ReviewError. When False (used for legacy base manifests),
    unpinned lines are skipped so newly pinned entries in head are still audited.
    """
    logical_lines = []
    current = ""
    start_line = 1
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not current:
            start_line = lineno
        if stripped.endswith("\\"):
            current += " " + stripped[:-1].strip()
        else:
            current += " " + stripped
            logical_lines.append((start_line, current.strip()))
            current = ""
    if current.strip():
        logical_lines.append((start_line, current.strip()))

    deps = set()
    for lineno, line in logical_lines:
        if not line or line.startswith("#"):
            continue
        # Strip inline comments preceded by whitespace.
        line = re.sub(r"\s+#.*$", "", line).strip()
        if not line:
            continue
        # Reject options that redirect package resolution or include hidden files.
        if line.startswith("-"):
            if strict:
                raise ReviewError(
                    f"{path}:{lineno}: unsupported pip option in lockfile: {line!r}"
                )
            continue
        # Drop trailing environment marker and --hash options.
        parts = line.split()
        spec_tokens = []
        unsupported_option = None
        for token in parts:
            if token.startswith("--hash="):
                continue
            if token.startswith("-"):
                unsupported_option = token
                break
            spec_tokens.append(token)
        if unsupported_option is not None:
            if strict:
                raise ReviewError(
                    f"{path}:{lineno}: unsupported pip option {unsupported_option!r} in lockfile"
                )
            continue
        spec = " ".join(spec_tokens)
        spec = spec.split(";", 1)[0].strip()
        match = PIP_PIN_RE.match(spec)
        if not match:
            if strict:
                raise ReviewError(
                    f"{path}:{lineno}: unpinned or malformed requirement {spec!r}; "
                    "expected exact 'name==version' pin"
                )
            continue
        name = _normalize_pypi_name(match.group("name"))
        version = match.group("version")
        deps.add(Dependency("PyPI", name, version))
    return deps


def parse_workflow_actions(path, text):
    """Extract external GitHub Action references from a workflow/action YAML."""
    deps = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        # Strip full-line/trailing comments outside quotes simply by stopping at
        # unquoted ' #' when scanning for uses:.
        scan_line = raw
        in_quote = None
        for idx, ch in enumerate(raw):
            if in_quote:
                if ch == in_quote:
                    in_quote = None
            else:
                if ch in ('"', "'"):
                    in_quote = ch
                elif ch == "#" and (idx == 0 or raw[idx - 1].isspace()):
                    scan_line = raw[:idx]
                    break
        for match in YAML_USES_RE.finditer(scan_line):
            ref = match.group("ref")
            if ref.startswith("./") or ref.startswith("docker://"):
                continue
            ref_match = ACTION_REF_RE.match(ref)
            if not ref_match:
                raise ReviewError(
                    f"{path}:{lineno}: malformed external uses reference {ref!r}"
                )
            repo = ref_match.group("repo")
            rev = ref_match.group("rev")
            if SHA40_RE.match(rev):
                deps.add(Dependency("GitHub Actions", repo, rev, commit=rev))
            else:
                deps.add(Dependency("GitHub Actions", repo, rev))
    return deps


def _is_go_mod(relpath):
    return relpath == "go.mod" or relpath.endswith("/go.mod")


def _is_requirements_txt(relpath):
    name = relpath.rsplit("/", 1)[-1]
    return "requirements" in name and name.endswith(".txt")


def _is_workflow_yaml(relpath):
    if not (relpath.endswith(".yml") or relpath.endswith(".yaml")):
        return False
    return relpath.startswith(".github/workflows/") or relpath.startswith(
        ".github/actions/"
    )


def _is_manifest(relpath):
    return (
        _is_go_mod(relpath)
        or _is_requirements_txt(relpath)
        or _is_workflow_yaml(relpath)
    )


def _parse_manifest(relpath, text, strict=True):
    if _is_go_mod(relpath):
        return parse_go_mod(relpath, text)
    if _is_requirements_txt(relpath):
        return parse_requirements_txt(relpath, text, strict=strict)
    if _is_workflow_yaml(relpath):
        return parse_workflow_actions(relpath, text)
    return set()


def load_manifests_from_git(ref, repo_root="."):
    """Return {relpath: text} for every tracked manifest file at git ref."""
    proc = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ReviewError(
            f"git ls-tree failed for ref {ref!r}: {proc.stderr.strip()}"
        )
    manifests = {}
    for relpath in proc.stdout.splitlines():
        if not _is_manifest(relpath):
            continue
        blob = subprocess.run(
            ["git", "show", f"{ref}:{relpath}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if blob.returncode != 0:
            raise ReviewError(
                f"git show failed for {ref}:{relpath}: {blob.stderr.strip()}"
            )
        manifests[relpath] = blob.stdout
    return manifests


def load_manifests_from_dir(root):
    """Return {relpath: text} for every manifest file under a local directory."""
    root_path = Path(root)
    if not root_path.is_dir():
        raise ReviewError(f"directory does not exist: {root}")
    manifests = {}
    for file_path in sorted(root_path.rglob("*")):
        if not file_path.is_file():
            continue
        relpath = file_path.relative_to(root_path).as_posix()
        if ".git/" in relpath or relpath.startswith(".git/"):
            continue
        if not _is_manifest(relpath):
            continue
        try:
            manifests[relpath] = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ReviewError(
                f"cannot read {file_path}: {exc.strerror or exc}"
            ) from exc
    return manifests


def collect_dependencies(manifests, strict=True):
    """Return {Dependency: sorted list of manifest paths}."""
    mapping = {}
    for relpath in sorted(manifests):
        deps = _parse_manifest(relpath, manifests[relpath], strict=strict)
        for dep in deps:
            mapping.setdefault(dep, []).append(relpath)
    return mapping


def diff_dependencies(base_manifests, head_manifests):
    """Return {Dependency: manifest paths} for dependencies added or updated in head."""
    # Parse base with strict=False so legacy unpinned base lockfiles do not
    # block a PR that pins them; head is always parsed strictly (fail-closed).
    base_deps = collect_dependencies(base_manifests, strict=False)
    head_deps = collect_dependencies(head_manifests, strict=True)
    return {
        dep: paths
        for dep, paths in head_deps.items()
        if dep not in base_deps
    }


def _post_json(url, payload, timeout):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise ReviewError(f"OSV query failed with HTTP {status}")
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise ReviewError(f"OSV query failed with HTTP {exc.code}: {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ReviewError(f"OSV query failed: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewError(f"OSV response is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ReviewError("OSV response is not a JSON object")
    return data


def query_osv(dependencies, osv_url=DEFAULT_OSV_BATCH_URL, timeout=DEFAULT_TIMEOUT_SECONDS, requester=_post_json):
    """Query OSV.dev batch API and return {Dependency: sorted list of vuln IDs}.

    Fails closed on HTTP/network errors, malformed responses, result-count
    mismatches, or unpaginated opaque results.
    """
    ordered = sorted(dependencies)
    findings = {}
    for start in range(0, len(ordered), MAX_BATCH_SIZE):
        batch = ordered[start : start + MAX_BATCH_SIZE]
        queries = [dep.osv_query() for dep in batch]
        data = requester(osv_url, {"queries": queries}, timeout)
        results = data.get("results")
        if not isinstance(results, list) or len(results) != len(batch):
            raise ReviewError(
                "OSV batch response missing or mismatched 'results' list "
                f"(expected {len(batch)}, got {type(results).__name__})"
            )
        for dep, result in zip(batch, results):
            if not isinstance(result, dict):
                raise ReviewError(f"OSV result for {dep.label()} is not a JSON object")
            vulns = result.get("vulns")
            if vulns is None:
                if result.get("next_page_token"):
                    raise ReviewError(
                        f"OSV returned pagination token without vulns for {dep.label()}"
                    )
                continue
            if not isinstance(vulns, list):
                raise ReviewError(
                    f"OSV 'vulns' for {dep.label()} is not a JSON list"
                )
            ids = []
            for vuln in vulns:
                if not isinstance(vuln, dict) or not isinstance(vuln.get("id"), str) or not vuln["id"].strip():
                    raise ReviewError(
                        f"OSV returned malformed vulnerability entry for {dep.label()}"
                    )
                ids.append(vuln["id"])
            if ids:
                findings[dep] = sorted(set(ids))
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="Base git ref/SHA to compare against")
    parser.add_argument("--head", help="Head git ref/SHA to evaluate")
    parser.add_argument("--base-dir", help="Base directory to compare against")
    parser.add_argument("--head-dir", help="Head directory to evaluate")
    parser.add_argument("--repo", default=".", help="Git repository root (default: .)")
    parser.add_argument(
        "--osv-url",
        default=DEFAULT_OSV_BATCH_URL,
        help=f"OSV querybatch endpoint (default: {DEFAULT_OSV_BATCH_URL})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    args = parser.parse_args(argv)

    use_git = args.base is not None or args.head is not None
    use_dir = args.base_dir is not None or args.head_dir is not None
    if use_git == use_dir:
        parser.error("specify either (--base and --head) or (--base-dir and --head-dir)")
    if use_git and (not args.base or not args.head):
        parser.error("--base and --head must both be provided")
    if use_dir and (not args.base_dir or not args.head_dir):
        parser.error("--base-dir and --head-dir must both be provided")

    try:
        if use_git:
            base_manifests = load_manifests_from_git(args.base, repo_root=args.repo)
            head_manifests = load_manifests_from_git(args.head, repo_root=args.repo)
        else:
            base_manifests = load_manifests_from_dir(args.base_dir)
            head_manifests = load_manifests_from_dir(args.head_dir)

        added = diff_dependencies(base_manifests, head_manifests)
        if not added:
            print("dependency-review: ok (no added or updated dependencies)")
            return 0

        findings = query_osv(added.keys(), osv_url=args.osv_url, timeout=args.timeout)
    except ReviewError as exc:
        print(f"dependency-review: FAIL: {exc}", file=sys.stderr)
        return 1

    if findings:
        print(
            f"dependency-review: FAIL: {len(findings)} added/updated "
            "dependency(ies) have known OSV vulnerabilities:",
            file=sys.stderr,
        )
        for dep in sorted(findings):
            manifests = ", ".join(added[dep])
            vuln_list = ", ".join(findings[dep])
            print(
                f"  - {dep.label()} ({manifests}): {vuln_list}",
                file=sys.stderr,
            )
        return 1

    print(
        f"dependency-review: ok ({len(added)} added/updated dependency(ies) "
        "checked against OSV, 0 vulnerabilities)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
