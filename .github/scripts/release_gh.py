"""GitHub release/tag operations. A failed read is always an error."""

import json
import os
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from release_plan import FORMAL_TAG, MARKER, ReleaseError, Tag, validate_sha
from release_record import Record, parse_body


def run(*command: str, cwd: Path | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, encoding="utf-8", capture_output=True, check=False)
    if result.returncode:
        raise ReleaseError(f"{command[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


@dataclass(frozen=True)
class Release:
    id: int
    tag: str
    draft: bool
    notes: str
    record: Record | None

    @classmethod
    def from_api(cls, data: dict) -> "Release":
        tag = data["tag_name"]
        notes, record = parse_body(data["body"] or "")
        if record and (record.tag != tag or data["prerelease"]):
            raise ReleaseError(f"{tag}: release journal disagrees with GitHub release metadata")
        return cls(data["id"], tag, data["draft"], notes, record)


class GitHub:
    def __init__(self, repo: str, git_dir: Path):
        self.repo = repo
        self.git_dir = git_dir

    def api(self, path: str, *, pages: bool = False, method: str = "GET", data: dict | None = None,
            missing_ok: bool = False):
        command = ["gh", "api", f"repos/{self.repo}/{path}", "--method", method]
        if pages:
            command.extend(["--paginate", "--slurp"])
        if data is not None:
            command.extend(["--input", "-"])
        result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                                encoding="utf-8", capture_output=True, check=False)
        if result.returncode:
            # The attestation list endpoint answers 404, not an empty list,
            # when the digest has no attestations yet.
            if missing_ok and "HTTP 404" in result.stderr:
                return None
            raise ReleaseError(f"GitHub {method} {path} failed: {result.stderr.strip()}")
        payload = json.loads(result.stdout)
        return [item for page in payload for item in page] if pages else payload

    def head(self) -> str:
        return validate_sha(self.api("git/ref/heads/main")["object"]["sha"])

    def ancestor(self, sha: str, head: str) -> bool:
        validate_sha(sha)
        validate_sha(head)
        for commit in (sha, head):
            probe = subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                                   cwd=self.git_dir, capture_output=True, check=False)
            if probe.returncode:
                # Checkout can predate a push, and tags may live outside its
                # histories. Compare exact commits without refreshing local refs.
                comparison = self.api(f"compare/{sha}...{head}?per_page=1")
                return comparison["status"] in {"ahead", "identical"}
        result = subprocess.run(["git", "merge-base", "--is-ancestor", sha, head],
                                cwd=self.git_dir, capture_output=True, check=False)
        if result.returncode not in (0, 1):
            raise ReleaseError("cannot verify main ancestry")
        return result.returncode == 0

    def tags(self, head: str) -> dict[str, Tag]:
        tags = {}
        # The matching-refs endpoint returns a single, unpaginated list.
        for ref in self.api("git/matching-refs/tags/v"):
            name = ref["ref"].removeprefix("refs/tags/")
            if not FORMAL_TAG.fullmatch(name):
                continue
            obj = ref["object"]
            annotation = ""
            for depth in range(8):
                if obj["type"] != "tag":
                    break
                tag_obj = self.api(f"git/tags/{validate_sha(obj['sha'])}")
                if depth == 0:
                    annotation = tag_obj["message"].rstrip("\n")
                obj = tag_obj["object"]
            if obj["type"] != "commit":
                raise ReleaseError(f"{name}: expected a tag pointing to a commit")
            sha = validate_sha(obj["sha"])
            tags[name] = Tag(name, sha, self.ancestor(sha, head), annotation)
        return tags

    def changed_paths(self, base: str, head: str) -> list[str]:
        result = subprocess.run(["git", "diff", "--name-only", "--no-renames", "-z", base, head, "--"],
                                cwd=self.git_dir, capture_output=True, check=False)
        if result.returncode:
            raise ReleaseError("live release refs could not be compared locally; rerun from current main")
        return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]

    def releases(self) -> dict[str, Release]:
        releases = {}
        for data in self.api("releases?per_page=100", pages=True):
            tag = data["tag_name"]
            if not FORMAL_TAG.fullmatch(tag):
                continue
            if tag in releases:
                raise ReleaseError(f"{tag}: duplicate GitHub releases")
            releases[tag] = Release.from_api(data)
        return releases

    def assets(self, release: Release) -> list[dict]:
        return self.api(f"releases/{release.id}/assets?per_page=100", pages=True)

    def reserve(self, record: Record) -> Release:
        payload = {"tag_name": record.tag, "target_commitish": record.sha,
                   "name": record.tag, "body": record.body(f"Watchtower {record.tag}"),
                   "draft": True, "prerelease": False, "make_latest": "false"}
        return Release.from_api(self.api("releases", method="POST", data=payload))

    def save(self, release: Release, record: Record, *, publish: bool = False, latest: bool = False) -> Release:
        # Always resend the tag name: GitHub renames a draft whose tag does not
        # exist yet to "untagged-..." when an update omits it.
        payload = {"tag_name": record.tag, "body": record.body(release.notes)}
        if publish:
            payload.update(draft=False, make_latest=str(latest).lower())
        return Release.from_api(self.api(f"releases/{release.id}", method="PATCH", data=payload))

    def upload(self, release: Release, path: Path) -> None:
        # Address the draft by ID. GitHub rejects duplicate asset names.
        query = urlencode({"name": path.name})
        with path.open("rb") as payload:
            request = urllib.request.Request(
                f"https://uploads.github.com/repos/{self.repo}/releases/{release.id}/assets?{query}",
                data=payload,
                headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                         "Content-Type": "application/octet-stream", "Content-Length": str(path.stat().st_size)},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=180) as response:
                if response.status != 201:
                    raise ReleaseError(f"upload of {path.name} returned HTTP {response.status}")

    def create_tag(self, record: Record) -> None:
        tag = self.api("git/tags", method="POST", data={
            "tag": record.tag, "message": MARKER + record.sha, "object": record.sha, "type": "commit",
        })
        self.api("git/refs", method="POST", data={
            "ref": f"refs/tags/{record.tag}", "sha": validate_sha(tag["sha"]),
        })
