"""The release reservation, immutable asset manifest, and publication journal."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from release_plan import ReleaseError, validate_sha, version

ARCHIVES = (
    "watchtower_linux_386.tar.gz",
    "watchtower_linux_amd64.tar.gz",
    "watchtower_linux_arm64v8.tar.gz",
    "watchtower_linux_armhf.tar.gz",
    "watchtower_windows_386.zip",
    "watchtower_windows_amd64.zip",
    "watchtower_darwin_amd64.tar.gz",
    "watchtower_darwin_arm64.tar.gz",
)
RECORD = "release-record.json"
BODY_START = "<!-- watchtower-release-v1\n"
BODY_END = "\n-->"
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def checksums_name(tag: str) -> str:
    version(tag)
    return f"watchtower_{tag[1:]}_checksums.txt"


def asset_names(tag: str) -> set[str]:
    return {*ARCHIVES, checksums_name(tag)}


def file_sha256(path: Path) -> str:
    with path.open("rb") as contents:
        return hashlib.file_digest(contents, "sha256").hexdigest()


@dataclass(frozen=True)
class Record:
    tag: str
    sha: str
    automatic: bool
    assets: dict[str, str] = field(default_factory=dict)
    archives_attested: bool = False
    image_digest: str = ""
    latest_done: bool = False

    def __post_init__(self) -> None:
        expected = asset_names(self.tag)
        validate_sha(self.sha)
        if any(type(flag) is not bool for flag in (self.automatic, self.archives_attested, self.latest_done)):
            raise ReleaseError("invalid release record flags")
        if not isinstance(self.assets, dict) or (self.assets and set(self.assets) != expected):
            raise ReleaseError("invalid release record asset inventory")
        if any(not HEX_DIGEST.fullmatch(sha) for sha in self.assets.values()):
            raise ReleaseError("invalid release asset hash")
        if self.archives_attested and not self.assets:
            raise ReleaseError("archive attestation requires asset hashes")
        if self.image_digest and (not self.archives_attested or not DIGEST.fullmatch(self.image_digest)):
            raise ReleaseError("invalid image digest or missing archive attestation")
        if self.latest_done and not self.image_digest:
            raise ReleaseError("latest promotion requires a recorded image digest")

    def manifest(self) -> bytes:
        """The immutable asset excludes mutable publication checkpoints."""
        return (json.dumps({"tag": self.tag, "sha": self.sha, "assets": self.assets},
                           sort_keys=True, indent=2) + "\n").encode()

    def body(self, notes: str) -> str:
        return f"{notes.rstrip()}\n\n{BODY_START}{json.dumps(asdict(self), sort_keys=True)}{BODY_END}"

    def with_assets(self, dist: Path) -> "Record":
        checksums = checksums_name(self.tag)
        hashes = {name: file_sha256(dist / name) for name in sorted(ARCHIVES)}
        expected_lines = {f"{digest}  {name}" for name, digest in hashes.items()}
        contents = (dist / checksums).read_bytes()
        lines = contents.decode("ascii").splitlines()
        if len(lines) != len(expected_lines) or set(lines) != expected_lines:
            raise ReleaseError("checksum file does not match the eight release archives")
        hashes[checksums] = hashlib.sha256(contents).hexdigest()
        if self.assets and self.assets != hashes:
            raise ReleaseError("rebuilt assets differ from the reserved release hashes")
        return replace(self, assets=hashes)


def parse_body(body: str) -> tuple[str, Record | None]:
    notes, separator, payload = body.partition(BODY_START)
    if not separator:
        return body, None
    if not payload.endswith(BODY_END):
        raise ReleaseError("malformed release journal in release notes")
    try:
        return notes.rstrip(), Record(**json.loads(payload.removesuffix(BODY_END)))
    except (TypeError, ValueError) as exc:
        raise ReleaseError(f"invalid release journal: {exc}") from exc


def missing_assets(record: Record, assets: list[dict]) -> set[str]:
    """Compare the API's uploaded checksums to the write-ahead inventory."""
    missing = asset_names(record.tag)
    seen = set()
    for asset in assets:
        name = asset["name"]
        if name in seen or name not in missing | {RECORD}:
            raise ReleaseError(f"unexpected or duplicate release asset {name!r}")
        seen.add(name)
        digest = (hashlib.sha256(record.manifest()).hexdigest() if name == RECORD
                  else record.assets.get(name))
        if asset["state"] == "starter":
            raise ReleaseError(f"{name}: interrupted upload left a GitHub starter asset; review the draft before retrying")
        if not digest or asset["state"] != "uploaded" or asset.get("digest") != f"sha256:{digest}":
            raise ReleaseError(f"{name}: stored asset does not match the release record")
        missing.discard(name)
    return missing
