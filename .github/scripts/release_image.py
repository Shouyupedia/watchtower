"""Verify and assign GHCR tags using the digest from the release journal."""

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_cli import output
from release_gh import GitHub, run
from release_plan import LEGACY_TAG, ReleaseError, highest_on_main, validate_sha, version
from release_record import DIGEST

ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
INDEX = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}


class Registry:
    def __init__(self, repo: str):
        self.repo = repo.lower()
        self.image = f"ghcr.io/{self.repo}"
        creds = f"{os.environ['GITHUB_ACTOR']}:{os.environ['GH_TOKEN']}".encode()
        query = urlencode({"service": "ghcr.io", "scope": f"repository:{self.repo}:pull,push"})
        request = urllib.request.Request(f"https://ghcr.io/token?{query}", headers={
            "Authorization": f"Basic {base64.b64encode(creds).decode()}",
        })
        with urllib.request.urlopen(request, timeout=30) as response:
            self.token = json.load(response)["token"]

    def digest(self, ref: str) -> str | None:
        request = urllib.request.Request(f"https://ghcr.io/v2/{self.repo}/manifests/{ref}", headers={
            "Authorization": f"Bearer {self.token}", "Accept": ACCEPT,
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                digest = response.headers.get("Docker-Content-Digest", "")
                if not DIGEST.fullmatch(digest) or response.headers.get_content_type() not in INDEX:
                    raise ReleaseError(f"{ref}: expected a multi-platform image index and digest")
                return digest
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                errors = json.load(exc)["errors"]
                if errors and all(error["code"] in {"MANIFEST_UNKNOWN", "NAME_UNKNOWN"} for error in errors):
                    return None
            raise

    def assign(self, tag: str, digest: str) -> None:
        run("docker", "buildx", "imagetools", "create", "--tag", f"{self.image}:{tag}", f"{self.image}@{digest}")
        if self.digest(tag) != digest:
            raise ReleaseError(f"{tag}: GHCR did not resolve to the recorded digest")


def verify_version(current: str | None, expected: str) -> None:
    if current is not None and current != expected:
        raise ReleaseError("existing version tag differs from the recorded image digest")


def verify_attested(registry: Registry, github: GitHub, digest: str, sha: str) -> None:
    if registry.digest(digest) != digest:
        raise ReleaseError("recorded image digest is missing from GHCR")
    run("gh", "attestation", "verify", f"oci://{registry.image}@{digest}", "--repo", github.repo,
        "--signer-workflow", f"{github.repo}/.github/workflows/docker-publish.yml",
        "--source-digest", sha, "--deny-self-hosted-runners")


def handle(github: GitHub, operation: str, tag: str, sha: str, digest: str) -> None:
    if version(tag) <= version(LEGACY_TAG):
        raise ReleaseError("historical versions are outside the managed release boundary")
    validate_sha(sha)
    if digest and not DIGEST.fullmatch(digest):
        raise ReleaseError("invalid pinned image digest")
    # For a draft, release.yml supplies the tuple from its write-authorized
    # journal job. The registry jobs only need read access to repository contents.
    if not digest and operation != "stage":
        raise ReleaseError("tag assignment requires a journaled image digest")
    if operation == "latest":
        release = github.releases().get(tag)
        record = release.record if release else None
        if not record or release.draft or record.sha != sha or record.image_digest != digest:
            raise ReleaseError("latest requires a published release with a matching image record")

    head = github.head()
    source = github.tags(head).get(tag)
    if not source or not source.on_main or source.sha != sha:
        raise ReleaseError("image tag does not match a verified commit in main history")

    registry = Registry(github.repo)
    image_tag = tag[1:]
    current = registry.digest(image_tag)
    verify_version(current, digest)
    if not digest:
        output(build="true")
        return
    verify_attested(registry, github, digest, sha)
    if operation == "stage":
        output(build="false")
        return
    if operation == "version":
        if current is None:
            registry.assign(image_tag, digest)
        return

    if current != digest:
        raise ReleaseError("version image must exist before latest promotion")
    latest = registry.digest("latest")
    # Refresh main and tags immediately before making the promotion decision.
    highest = highest_on_main(github.tags(github.head()).values())
    if not highest or highest.name != tag or highest.sha != sha:
        print(f"Skipping latest promotion because {tag} is no longer the highest formal tag on main.")
        output(promoted="false")
        return
    if latest != digest:
        registry.assign("latest", digest)
    output(promoted="true")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("stage", "version", "latest"))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--digest", default="")
    args = parser.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", repo):
        raise ReleaseError("invalid repository name")
    handle(GitHub(repo, Path.cwd()), args.operation, args.tag, args.sha, args.digest)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        sys.exit(f"release image: {exc}")
