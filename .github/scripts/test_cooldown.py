#!/usr/bin/env python3
"""Unit tests for .github/scripts/cooldown.py (offline, synthetic fixtures)."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import cooldown  # noqa: E402


NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


class StrictFakeHttp:
    """In-memory HttpGet double that fails loudly on any unexpected URL."""

    def __init__(self, responses: dict[str, bytes]) -> None:
        self._responses = dict(responses)
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url not in self._responses:
            raise AssertionError(f"unexpected HTTP URL: {url}")
        return self._responses[url]


def _ndjson(entries: list[dict[str, str]]) -> bytes:
    return ("\n".join(json.dumps(item) for item in entries) + "\n").encode("utf-8")


def _json_bytes(obj: object) -> bytes:
    return json.dumps(obj).encode("utf-8")


class IsAgedTests(unittest.TestCase):
    def test_exact_boundary_is_aged(self) -> None:
        ts = NOW - timedelta(days=7)
        self.assertTrue(cooldown.is_aged(ts, 7, NOW))

    def test_one_second_shy_is_not_aged(self) -> None:
        ts = NOW - timedelta(days=7) + timedelta(seconds=1)
        self.assertFalse(cooldown.is_aged(ts, 7, NOW))

    def test_zero_days_at_now_is_aged(self) -> None:
        self.assertTrue(cooldown.is_aged(NOW, 0, NOW))

    def test_future_timestamp_rejected(self) -> None:
        future = NOW + timedelta(seconds=1)
        self.assertFalse(cooldown.is_aged(future, 0, NOW))
        self.assertFalse(cooldown.is_aged(future, 7, NOW))

    def test_naive_timestamp_rejected(self) -> None:
        naive = datetime(2026, 11, 1, 0, 0, 0)
        self.assertFalse(cooldown.is_aged(naive, 7, NOW))

    def test_naive_now_rejected(self) -> None:
        ts = NOW - timedelta(days=10)
        naive_now = datetime(2026, 11, 25, 12, 0, 0)
        self.assertFalse(cooldown.is_aged(ts, 7, naive_now))

    def test_negative_days_rejected(self) -> None:
        ts = NOW - timedelta(days=10)
        self.assertFalse(cooldown.is_aged(ts, -1, NOW))

    def test_bool_days_rejected(self) -> None:
        ts = NOW - timedelta(days=10)
        self.assertFalse(cooldown.is_aged(ts, True, NOW))  # type: ignore[arg-type]
        self.assertFalse(cooldown.is_aged(ts, False, NOW))  # type: ignore[arg-type]

    def test_non_datetime_rejected(self) -> None:
        self.assertFalse(cooldown.is_aged("2026-11-01T00:00:00Z", 7, NOW))  # type: ignore[arg-type]
        self.assertFalse(cooldown.is_aged(NOW - timedelta(days=10), 7, None))  # type: ignore[arg-type]

    def test_non_utc_aware_offset_converted(self) -> None:
        tz_plus_8 = timezone(timedelta(hours=8))
        ts = (NOW - timedelta(days=7)).astimezone(tz_plus_8)
        self.assertTrue(cooldown.is_aged(ts, 7, NOW))


class GoFirstSeenTests(unittest.TestCase):
    def test_returns_index_timestamp_not_proxy_info_time(self) -> None:
        # Backdated .info Time (30 days ago) vs actual index Timestamp (2 days ago).
        info_time = "2026-10-26T12:00:00Z"
        index_ts = "2026-11-23T12:00:00.123456Z"
        since = "2026-10-26T11:59:00Z"
        http_get = StrictFakeHttp(
            {
                "https://proxy.golang.org/example.com/mod/@v/v1.2.4.info": _json_bytes(
                    {"Version": "v1.2.4", "Time": info_time}
                ),
                f"https://index.golang.org/index?since={since.replace(':', '%3A')}&limit=2000": _ndjson(
                    [
                        {
                            "Path": "example.com/other",
                            "Version": "v0.1.0",
                            "Timestamp": "2026-10-26T12:00:00Z",
                        },
                        {
                            "Path": "example.com/mod",
                            "Version": "v1.2.4",
                            "Timestamp": index_ts,
                        },
                    ]
                ),
            }
        )
        observed = cooldown.go_first_seen("example.com/mod", "v1.2.4", http_get=http_get)
        expected = datetime(2026, 11, 23, 12, 0, 0, 123456, tzinfo=timezone.utc)
        self.assertEqual(observed, expected)
        self.assertFalse(cooldown.is_aged(observed, 3, NOW))

    def test_uppercase_module_path_escaped_for_proxy_raw_for_index(self) -> None:
        info_time = "2026-11-10T00:00:00Z"
        index_ts = "2026-11-10T00:05:00Z"
        since = "2026-11-09T23:59:00Z"
        http_get = StrictFakeHttp(
            {
                "https://proxy.golang.org/github.com/%21azure/pkg/@v/v1.0.1.info": _json_bytes(
                    {"Version": "v1.0.1", "Time": info_time}
                ),
                f"https://index.golang.org/index?since={since.replace(':', '%3A')}&limit=2000": _ndjson(
                    [
                        {
                            "Path": "github.com/Azure/pkg",
                            "Version": "v1.0.1",
                            "Timestamp": index_ts,
                        }
                    ]
                ),
            }
        )
        observed = cooldown.go_first_seen("github.com/Azure/pkg", "v1.0.1", http_get=http_get)
        self.assertEqual(observed, datetime(2026, 11, 10, 0, 5, 0, tzinfo=timezone.utc))

    def test_fails_closed_when_entry_missing(self) -> None:
        info_time = "2026-11-10T00:00:00Z"
        since = "2026-11-09T23:59:00Z"
        http_get = StrictFakeHttp(
            {
                "https://proxy.golang.org/example.com/mod/@v/v1.2.4.info": _json_bytes(
                    {"Version": "v1.2.4", "Time": info_time}
                ),
                f"https://index.golang.org/index?since={since.replace(':', '%3A')}&limit=2000": _ndjson(
                    [
                        {
                            "Path": "example.com/other",
                            "Version": "v1.0.0",
                            "Timestamp": "2026-11-10T00:01:00Z",
                        }
                    ]
                ),
            }
        )
        with self.assertRaises(cooldown.CooldownError):
            cooldown.go_first_seen("example.com/mod", "v1.2.4", http_get=http_get)

    def test_fails_closed_when_match_is_beyond_max_requests(self) -> None:
        # max_requests=2 allows 1 proxy .info request + 1 index page.
        # Put a full 2000-entry page with no match, and the match on page 2.
        info_time = "2026-11-10T00:00:00Z"
        since_1 = "2026-11-09T23:59:00Z"
        base_dt = datetime(2026, 11, 10, 0, 0, 0, tzinfo=timezone.utc)
        page_1 = [
            {
                "Path": f"example.com/filler{i}",
                "Version": "v0.0.1",
                "Timestamp": (base_dt + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            for i in range(cooldown.INDEX_PAGE_LIMIT)
        ]
        last_ts = (base_dt + timedelta(seconds=cooldown.INDEX_PAGE_LIMIT - 1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        page_2 = [
            {
                "Path": "example.com/mod",
                "Version": "v1.2.4",
                "Timestamp": (base_dt + timedelta(seconds=cooldown.INDEX_PAGE_LIMIT + 10)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
        ]
        responses = {
            "https://proxy.golang.org/example.com/mod/@v/v1.2.4.info": _json_bytes(
                {"Version": "v1.2.4", "Time": info_time}
            ),
            f"https://index.golang.org/index?since={since_1.replace(':', '%3A')}&limit=2000": _ndjson(
                page_1
            ),
            f"https://index.golang.org/index?since={last_ts.replace(':', '%3A')}&limit=2000": _ndjson(
                page_2
            ),
        }
        # Budget of 2 (1 info + 1 index page) must fail closed without fetching page 2.
        http_limited = StrictFakeHttp(responses)
        with self.assertRaises(cooldown.CooldownError):
            cooldown.go_first_seen(
                "example.com/mod", "v1.2.4", http_get=http_limited, max_requests=2
            )
        self.assertEqual(len(http_limited.calls), 2)

        # Budget of 3 (1 info + 2 index pages) succeeds and returns page 2 timestamp.
        http_enough = StrictFakeHttp(responses)
        observed = cooldown.go_first_seen(
            "example.com/mod", "v1.2.4", http_get=http_enough, max_requests=3
        )
        self.assertEqual(
            observed, base_dt + timedelta(seconds=cooldown.INDEX_PAGE_LIMIT + 10)
        )
        self.assertEqual(len(http_enough.calls), 3)

    def test_rejects_mismatched_info_version(self) -> None:
        http_get = StrictFakeHttp(
            {
                "https://proxy.golang.org/example.com/mod/@v/v1.2.4.info": _json_bytes(
                    {"Version": "v1.2.3", "Time": "2026-11-10T00:00:00Z"}
                )
            }
        )
        with self.assertRaises(cooldown.CooldownError):
            cooldown.go_first_seen("example.com/mod", "v1.2.4", http_get=http_get)

    def test_rejects_out_of_order_index_timestamps(self) -> None:
        since = "2026-11-09T23:59:00Z"
        http_get = StrictFakeHttp(
            {
                "https://proxy.golang.org/example.com/mod/@v/v1.2.4.info": _json_bytes(
                    {"Version": "v1.2.4", "Time": "2026-11-10T00:00:00Z"}
                ),
                f"https://index.golang.org/index?since={since.replace(':', '%3A')}&limit=2000": _ndjson(
                    [
                        {
                            "Path": "example.com/a",
                            "Version": "v1.0.0",
                            "Timestamp": "2026-11-10T00:05:00Z",
                        },
                        {
                            "Path": "example.com/mod",
                            "Version": "v1.2.4",
                            "Timestamp": "2026-11-10T00:01:00Z",
                        },
                    ]
                ),
            }
        )
        with self.assertRaises(cooldown.CooldownError):
            cooldown.go_first_seen("example.com/mod", "v1.2.4", http_get=http_get)

    def test_rejects_unsafe_module_or_bad_max_requests(self) -> None:
        http_get = StrictFakeHttp({})
        with self.assertRaises(cooldown.CooldownError):
            cooldown.go_first_seen("example.com/../evil", "v1.0.0", http_get=http_get)
        with self.assertRaises(cooldown.CooldownError):
            cooldown.go_first_seen("example.com/mod", "v1.0.0", http_get=http_get, max_requests=0)
        with self.assertRaises(cooldown.CooldownError):
            cooldown.go_first_seen(
                "example.com/mod", "v1.0.0", http_get=http_get, max_requests=True  # type: ignore[arg-type]
            )


class DockerTagInfoTests(unittest.TestCase):
    def test_official_short_name_and_valid_response(self) -> None:
        http_get = StrictFakeHttp(
            {
                "https://hub.docker.com/v2/repositories/library/golang/tags/1.26.11-alpine": _json_bytes(
                    {
                        "name": "1.26.11-alpine",
                        "digest": f"sha256:{HASH_A}",
                        "tag_last_pushed": "2026-11-10T08:30:00Z",
                    }
                )
            }
        )
        digest, pushed = cooldown.docker_tag_info("golang", "1.26.11-alpine", http_get=http_get)
        self.assertEqual(digest, f"sha256:{HASH_A}")
        self.assertEqual(pushed, datetime(2026, 11, 10, 8, 30, 0, tzinfo=timezone.utc))

    def test_rejects_tag_name_mismatch(self) -> None:
        http_get = StrictFakeHttp(
            {
                "https://hub.docker.com/v2/repositories/library/golang/tags/1.26.11-alpine": _json_bytes(
                    {
                        "name": "1.26.10-alpine",
                        "digest": f"sha256:{HASH_A}",
                        "tag_last_pushed": "2026-11-10T08:30:00Z",
                    }
                )
            }
        )
        with self.assertRaises(cooldown.CooldownError):
            cooldown.docker_tag_info("library/golang", "1.26.11-alpine", http_get=http_get)

    def test_rejects_missing_or_malformed_digest(self) -> None:
        for bad_digest in (None, "", "md5:1234", "sha256:short", f"sha256:{'A' * 64}"):
            with self.subTest(digest=bad_digest):
                http_get = StrictFakeHttp(
                    {
                        "https://hub.docker.com/v2/repositories/library/golang/tags/1.26.11-alpine": _json_bytes(
                            {
                                "name": "1.26.11-alpine",
                                "digest": bad_digest,
                                "tag_last_pushed": "2026-11-10T08:30:00Z",
                            }
                        )
                    }
                )
                with self.assertRaises(cooldown.CooldownError):
                    cooldown.docker_tag_info("library/golang", "1.26.11-alpine", http_get=http_get)

    def test_rejects_missing_or_naive_tag_last_pushed(self) -> None:
        for bad_ts in (None, "", "2026-11-10T08:30:00"):
            with self.subTest(ts=bad_ts):
                http_get = StrictFakeHttp(
                    {
                        "https://hub.docker.com/v2/repositories/library/golang/tags/1.26.11-alpine": _json_bytes(
                            {
                                "name": "1.26.11-alpine",
                                "digest": f"sha256:{HASH_A}",
                                "tag_last_pushed": bad_ts,
                            }
                        )
                    }
                )
                with self.assertRaises(cooldown.CooldownError):
                    cooldown.docker_tag_info("library/golang", "1.26.11-alpine", http_get=http_get)


class PipHashesLastUploadTests(unittest.TestCase):
    def test_returns_latest_upload_across_all_allowed_hashes(self) -> None:
        # HASH_A is old (30d), HASH_B is recent (2d); must return HASH_B's timestamp.
        # Unrelated HASH_C without upload_time_iso_8601 is ignored.
        http_get = StrictFakeHttp(
            {
                "https://pypi.org/pypi/mkdocs-material/9.5.2/json": _json_bytes(
                    {
                        "info": {"version": "9.5.2"},
                        "urls": [
                            {
                                "digests": {"sha256": HASH_A},
                                "upload_time_iso_8601": "2026-10-26T00:00:00Z",
                            },
                            {
                                "digests": {"sha256": HASH_B},
                                "upload_time_iso_8601": "2026-11-23T00:00:00Z",
                            },
                            {
                                "digests": {"sha256": HASH_C},
                                "upload_time_iso_8601": None,
                            },
                        ],
                    }
                )
            }
        )
        latest = cooldown.pip_hashes_last_upload(
            "MkDocs_Material",
            "9.5.2",
            [f"sha256:{HASH_A}", HASH_B.upper()],
            http_get=http_get,
        )
        self.assertEqual(latest, datetime(2026, 11, 23, 0, 0, 0, tzinfo=timezone.utc))
        self.assertFalse(cooldown.is_aged(latest, 7, NOW))

    def test_fails_closed_when_allowed_hash_missing_from_pypi(self) -> None:
        http_get = StrictFakeHttp(
            {
                "https://pypi.org/pypi/mkdocs/1.6.1/json": _json_bytes(
                    {
                        "info": {"version": "1.6.1"},
                        "urls": [
                            {
                                "digests": {"sha256": HASH_A},
                                "upload_time_iso_8601": "2026-10-26T00:00:00Z",
                            }
                        ],
                    }
                )
            }
        )
        with self.assertRaises(cooldown.CooldownError):
            cooldown.pip_hashes_last_upload(
                "mkdocs", "1.6.1", [HASH_A, HASH_B], http_get=http_get
            )

    def test_fails_closed_when_allowed_hash_has_no_timestamp(self) -> None:
        http_get = StrictFakeHttp(
            {
                "https://pypi.org/pypi/mkdocs/1.6.1/json": _json_bytes(
                    {
                        "info": {"version": "1.6.1"},
                        "urls": [
                            {
                                "digests": {"sha256": HASH_A},
                                "upload_time_iso_8601": "2026-10-26T00:00:00Z",
                            },
                            {
                                "digests": {"sha256": HASH_B},
                                "upload_time_iso_8601": None,
                            },
                        ],
                    }
                )
            }
        )
        with self.assertRaises(cooldown.CooldownError):
            cooldown.pip_hashes_last_upload(
                "mkdocs", "1.6.1", [HASH_A, HASH_B], http_get=http_get
            )

    def test_fails_closed_on_version_mismatch_or_bad_hashes(self) -> None:
        http_get = StrictFakeHttp(
            {
                "https://pypi.org/pypi/mkdocs/1.6.1/json": _json_bytes(
                    {"info": {"version": "1.6.0"}, "urls": []}
                )
            }
        )
        with self.assertRaises(cooldown.CooldownError):
            cooldown.pip_hashes_last_upload("mkdocs", "1.6.1", [HASH_A], http_get=http_get)
        with self.assertRaises(cooldown.CooldownError):
            cooldown.pip_hashes_last_upload("mkdocs", "1.6.1", [], http_get=http_get)
        with self.assertRaises(cooldown.CooldownError):
            cooldown.pip_hashes_last_upload("mkdocs", "1.6.1", HASH_A, http_get=http_get)
        with self.assertRaises(cooldown.CooldownError):
            cooldown.pip_hashes_last_upload("mkdocs", "1.6.1", ["not-a-hash"], http_get=http_get)


if __name__ == "__main__":
    unittest.main()
