#!/usr/bin/env python3
"""Fail-closed cooldown evidence lookups for the maintenance automations.

Every lookup in this module answers one question: *when did this exact
artifact first become publicly visible?*  The automerge verifier and the Go
toolchain sync only act on evidence from an authoritative, append-only
registry record; the ``.info`` commit time published by the Go module proxy is
deliberately **not** trusted because it is the upstream VCS commit timestamp
and can be backdated.

Trust model
-----------
* Go modules: ``index.golang.org`` is an append-only, time ordered index of
  module versions first served by the public proxy.  It cannot be queried by
  module, so the proxy's ``.info`` ``Time`` is used *only* as a start hint for
  a bounded forward scan.  The returned value is always the ``Timestamp`` of
  the exact ``(Path, Version)`` entry in the index.  A forged or backdated
  ``.info`` time can therefore only make the lookup return
  :class:`CooldownError` (not found); it can never produce an earlier trusted
  time.
* Docker: Docker Hub's ``tag_last_pushed`` for the exact tag, together with
  the tag's registry digest.
* PyPI: the registry-side ``upload_time_iso_8601`` of every file whose sha256
  is allowed by the lockfile.  The latest such upload is returned; a single
  hash without a matching file or timestamp fails the whole lookup.

Anything missing, ambiguous, unreachable or malformed raises
:class:`CooldownError`.  Callers must treat that as "not cooled down".

The public API of this module is exactly the names in ``__all__``.  ``http_get``
is the default network getter and is intentionally *not* part of the exported
contract; inject a fake ``GetMapping`` in tests instead.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

__all__ = [
    "CooldownError",
    "HttpGet",
    "go_first_seen",
    "docker_tag_info",
    "pip_hashes_last_upload",
    "is_aged",
]

HttpGet = Callable[[str], bytes]

USER_AGENT = "watchtower-cooldown/1.0"
HTTP_TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

INDEX_URL = "https://index.golang.org/index"
PROXY_URL = "https://proxy.golang.org"
DOCKER_HUB_URL = "https://hub.docker.com/v2/repositories"
PYPI_URL = "https://pypi.org/pypi"

# One proxy hint plus up to nine index pages.  The index page size is capped
# by the service at 2000 entries.  A version whose first proxy fetch happened
# long after its commit time may not be locatable within this bound; that is a
# deliberate fail-closed outcome, not an error to retry.
DEFAULT_MAX_REQUESTS = 10
INDEX_PAGE_LIMIT = 2000
# Start the scan slightly before the commit time so clock skew and second
# truncation cannot skip an entry.  A forged earlier hint only costs pages.
HINT_SKEW = timedelta(seconds=60)

_GO_MODULE_RE = re.compile(r"^[A-Za-z0-9._~+\-/]+$")
_GO_VERSION_RE = re.compile(r"^[A-Za-z0-9._~+\-]+$")
_DOCKER_REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_DOCKER_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_DOCKER_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PYPI_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_PYPI_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]*$")
_SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")
_PYPI_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class CooldownError(Exception):
    """Raised for any missing, ambiguous, unreachable, or malformed evidence."""


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects that would leave HTTPS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise CooldownError("refusing non-HTTPS redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpsOnlyRedirectHandler())


def _require_https(url):
    if not isinstance(url, str):
        raise CooldownError("URL must be a string")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise CooldownError(f"refusing non-HTTPS URL: {url!r}")
    if not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise CooldownError(f"refusing malformed URL: {url!r}")


def http_get(url: str) -> bytes:
    """Fetch ``url`` over HTTPS with a timeout and a hard response-size cap.

    This is the default :data:`HttpGet`.  It never sends credentials and never
    talks to a non-HTTPS origin, including across redirects.
    """
    _require_https(url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain;q=0.9, */*;q=0.1",
        },
    )
    try:
        with _OPENER.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                raise CooldownError(f"redirected to non-HTTPS URL: {url!r}")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except CooldownError:
        raise
    except urllib.error.HTTPError as exc:
        raise CooldownError(f"HTTP {exc.code} while fetching {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CooldownError(f"cannot reach {url}: {exc}") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise CooldownError(f"response from {url} exceeds {MAX_RESPONSE_BYTES} bytes")
    return body


def _parse_timestamp(value, what):
    """Parse a registry timestamp into an aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        raise CooldownError(f"{what} is missing")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CooldownError(f"{what} is not an ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CooldownError(f"{what} has no timezone: {value!r}")
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise CooldownError(f"{what} is out of range: {value!r}") from exc


def is_aged(timestamp: datetime, days: int, now: datetime) -> bool:
    """Return True only when ``timestamp`` is at least ``days`` old.

    A timestamp in the future (even by a moment) is never aged.  Naive
    datetimes, non-datetime values and negative day counts return False.
    """
    if not isinstance(timestamp, datetime) or not isinstance(now, datetime):
        return False
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return False
    if now.tzinfo is None or now.utcoffset() is None:
        return False
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        return False
    try:
        observed = timestamp.astimezone(timezone.utc)
        reference = now.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return False
    if observed > reference:
        return False
    return (reference - observed) >= timedelta(days=days)


def _validate_go_module(module):
    if not isinstance(module, str) or not module or len(module) > 500:
        raise CooldownError("Go module path is empty or too long")
    if not _GO_MODULE_RE.match(module):
        raise CooldownError(f"Go module path has unsupported characters: {module!r}")
    if module.startswith("/") or module.endswith("/") or "//" in module:
        raise CooldownError(f"Go module path is not a clean path: {module!r}")
    for segment in module.split("/"):
        if segment in ("", ".", ".."):
            raise CooldownError(f"Go module path has an unsafe segment: {module!r}")
    return module


def _validate_go_version(version):
    if not isinstance(version, str) or not version or len(version) > 200:
        raise CooldownError("Go module version is empty or too long")
    if not _GO_VERSION_RE.match(version):
        raise CooldownError(f"Go module version has unsupported characters: {version!r}")
    return version


def _go_escape(value):
    """Apply the Go module proxy case-encoding (uppercase -> !lowercase)."""
    return "".join(
        "!" + char.lower() if "A" <= char <= "Z" else char for char in value
    )


def _format_rfc3339(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json_object(raw, what):
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise CooldownError(f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CooldownError(f"{what} is not a JSON object")
    return data


def _parse_index_page(raw):
    """Return [(path, version, aware-utc-timestamp)] from an NDJSON index page."""
    try:
        text = raw.decode("utf-8")
    except (UnicodeDecodeError, AttributeError) as exc:
        raise CooldownError("index page is not valid UTF-8") from exc
    entries = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise CooldownError(
                f"index page line {lineno} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(item, dict):
            raise CooldownError(f"index page line {lineno} is not a JSON object")
        path = item.get("Path")
        version = item.get("Version")
        if not isinstance(path, str) or not path:
            raise CooldownError(f"index page line {lineno} has no Path")
        if not isinstance(version, str) or not version:
            raise CooldownError(f"index page line {lineno} has no Version")
        timestamp = _parse_timestamp(
            item.get("Timestamp"), f"index page line {lineno} Timestamp"
        )
        entries.append((path, version, timestamp))
    for previous, current in zip(entries, entries[1:]):
        if current[2] < previous[2]:
            raise CooldownError("index page Timestamps are not in order")
    return entries


def go_first_seen(
    module: str,
    version: str,
    *,
    http_get: HttpGet = http_get,
    max_requests: int = DEFAULT_MAX_REQUESTS,
) -> datetime:
    """Return the trusted first-public-visibility time of a Go module version.

    The value comes from the exact ``(Path, Version)`` record in
    ``index.golang.org``.  The proxy ``.info`` ``Time`` is used only to pick
    the start of a bounded forward scan; it is never returned.

    ``max_requests`` counts the proxy hint request plus every index page
    request.  A lookup that needs more than the bound fails closed.
    """
    module = _validate_go_module(module)
    version = _validate_go_version(version)
    if isinstance(max_requests, bool) or not isinstance(max_requests, int):
        raise CooldownError("max_requests must be an integer")
    if max_requests < 1:
        raise CooldownError("max_requests must be at least 1")

    escaped_module = urllib.parse.quote(
        _go_escape(module), safe="/+.-_~"
    )
    escaped_version = urllib.parse.quote(_go_escape(version), safe="+.-_~")
    info_url = f"{PROXY_URL}/{escaped_module}/@v/{escaped_version}.info"

    requests_made = 1
    info = _load_json_object(http_get(info_url), "proxy .info response")
    if info.get("Version") != version:
        raise CooldownError(
            f"proxy .info returned version {info.get('Version')!r} "
            f"for requested {version!r}"
        )
    hint = _parse_timestamp(info.get("Time"), "proxy .info Time")
    since = hint - HINT_SKEW

    previous_last = None
    while requests_made < max_requests:
        query = urllib.parse.urlencode(
            {"since": _format_rfc3339(since), "limit": INDEX_PAGE_LIMIT}
        )
        raw = http_get(f"{INDEX_URL}?{query}")
        requests_made += 1
        entries = _parse_index_page(raw)
        if not entries:
            break
        for path, entry_version, timestamp in entries:
            if path == module and entry_version == version:
                return timestamp
        last_timestamp = entries[-1][2]
        if previous_last is not None and last_timestamp <= previous_last:
            raise CooldownError("index pagination made no forward progress")
        previous_last = last_timestamp
        if len(entries) < INDEX_PAGE_LIMIT:
            break
        since = last_timestamp

    raise CooldownError(
        f"index.golang.org has no trusted record for {module}@{version} "
        f"within {requests_made} request(s)"
    )


def _validate_docker_repository(repository):
    if not isinstance(repository, str) or not repository.strip():
        raise CooldownError("Docker repository is empty")
    repository = repository.strip()
    if len(repository) > 200:
        raise CooldownError("Docker repository is too long")
    if repository.startswith("/") or repository.endswith("/") or "//" in repository:
        raise CooldownError(f"Docker repository is not a clean path: {repository!r}")
    segments = repository.split("/")
    if len(segments) > 3:
        raise CooldownError(f"Docker repository has too many segments: {repository!r}")
    if any(not _DOCKER_REPO_SEGMENT_RE.match(segment) for segment in segments):
        raise CooldownError(
            f"Docker repository has unsupported characters: {repository!r}"
        )
    return repository


def _validate_docker_tag(tag):
    if not isinstance(tag, str) or not _DOCKER_TAG_RE.match(tag):
        raise CooldownError(f"Docker tag is malformed: {tag!r}")
    return tag


def docker_tag_info(
    repository: str, tag: str, *, http_get: HttpGet = http_get
) -> tuple[str, datetime]:
    """Return ``(digest, tag_last_pushed)`` for a Docker Hub tag.

    ``repository`` is the Docker Hub repository path (``library/golang`` for
    an official image).  A single-segment name is treated as an official
    image, matching how Docker resolves ``golang`` to ``library/golang``.
    """
    repository = _validate_docker_repository(repository)
    tag = _validate_docker_tag(tag)
    if "/" not in repository:
        repository = f"library/{repository}"
    url = (
        f"{DOCKER_HUB_URL}/{urllib.parse.quote(repository, safe='/')}"
        f"/tags/{urllib.parse.quote(tag, safe='')}"
    )
    data = _load_json_object(http_get(url), "Docker Hub tag response")
    name = data.get("name")
    if name != tag:
        raise CooldownError(
            f"Docker Hub returned tag name {name!r} for requested {tag!r}"
        )
    digest = data.get("digest")
    if not isinstance(digest, str) or not _DOCKER_DIGEST_RE.match(digest):
        raise CooldownError(f"Docker Hub tag {tag!r} has no valid digest")
    last_pushed = _parse_timestamp(
        data.get("tag_last_pushed"), f"Docker Hub tag {tag!r} tag_last_pushed"
    )
    return digest, last_pushed


def _normalize_pypi_name(package):
    if not isinstance(package, str) or not package.strip():
        raise CooldownError("PyPI package name is empty")
    normalized = re.sub(r"[-_.]+", "-", package.strip()).lower()
    if not _PYPI_NAME_RE.match(normalized):
        raise CooldownError(f"PyPI package name is malformed: {package!r}")
    return normalized


def _normalize_pypi_hashes(hashes):
    if isinstance(hashes, (str, bytes)):
        raise CooldownError("hashes must be an iterable of hash strings, not a string")
    try:
        values = list(hashes)
    except TypeError as exc:
        raise CooldownError("hashes must be iterable") from exc
    if not values:
        raise CooldownError("at least one sha256 hash is required")
    normalized = set()
    for value in values:
        if not isinstance(value, str):
            raise CooldownError(f"hash is not a string: {value!r}")
        match = _SHA256_RE.match(value.strip())
        if not match:
            raise CooldownError(
                f"hash is not a sha256 digest (optionally 'sha256:' prefixed): {value!r}"
            )
        normalized.add(match.group(1).lower())
    return normalized


def pip_hashes_last_upload(
    package: str,
    version: str,
    hashes: Iterable[str],
    *,
    http_get: HttpGet = http_get,
) -> datetime:
    """Return the latest PyPI upload time among *all* allowed file hashes.

    Every hash in ``hashes`` must map to at least one file in the requested
    version's JSON metadata, and every matching file must carry an upload
    timestamp.  The maximum upload time across those files is returned, so a
    package is not considered cooled down while any allowed artifact is newer.
    """
    package = _normalize_pypi_name(package)
    if not isinstance(version, str) or not version:
        raise CooldownError("PyPI version is empty")
    if not _PYPI_VERSION_RE.match(version):
        raise CooldownError(f"PyPI version has unsupported characters: {version!r}")
    wanted = _normalize_pypi_hashes(hashes)

    url = (
        f"{PYPI_URL}/{urllib.parse.quote(package, safe='')}"
        f"/{urllib.parse.quote(version, safe='.+!_-')}/json"
    )
    data = _load_json_object(http_get(url), "PyPI JSON response")
    info = data.get("info")
    if not isinstance(info, dict):
        raise CooldownError("PyPI JSON response has no info object")
    if info.get("version") != version:
        raise CooldownError(
            f"PyPI returned version {info.get('version')!r} "
            f"for requested {version!r}"
        )
    files = data.get("urls")
    if not isinstance(files, list):
        raise CooldownError("PyPI JSON response has no urls list")

    times_by_hash = {}
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise CooldownError(f"PyPI file entry #{index} is not a JSON object")
        digests = entry.get("digests")
        if not isinstance(digests, dict):
            raise CooldownError(f"PyPI file entry #{index} has no digests object")
        sha256 = digests.get("sha256")
        if not isinstance(sha256, str) or not _PYPI_SHA256_RE.match(sha256):
            raise CooldownError(f"PyPI file entry #{index} has no sha256 digest")
        sha256 = sha256.lower()
        upload = entry.get("upload_time_iso_8601")
        if upload is None:
            # Every file whose hash the lockfile allows must have a timestamp;
            # an untimestamped artifact could be newer than the rest.
            if sha256 in wanted:
                raise CooldownError(
                    f"PyPI file for {package}=={version} with "
                    f"sha256:{sha256} has no upload timestamp"
                )
            continue
        timestamp = _parse_timestamp(
            upload, f"PyPI upload_time_iso_8601 for sha256:{sha256}"
        )
        times_by_hash.setdefault(sha256, []).append(timestamp)

    latest = None
    for sha256 in sorted(wanted):
        timestamps = times_by_hash.get(sha256)
        if not timestamps:
            raise CooldownError(
                f"PyPI files for {package}=={version} have no upload timestamp "
                f"matching sha256:{sha256}"
            )
        newest_for_hash = max(timestamps)
        if latest is None or newest_for_hash > latest:
            latest = newest_for_hash
    if latest is None:
        raise CooldownError(f"PyPI has no upload timestamp for {package}=={version}")
    return latest
