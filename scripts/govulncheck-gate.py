#!/usr/bin/env python3
"""Fail a govulncheck run unless every *called* vulnerability is exempted.

Policy
------
govulncheck's default scan level is ``symbol``; at that level it only treats a
finding as a failure when the vulnerable code is reachable through a symbol the
program actually calls (its text reporter exits 3 only for "called" findings,
and ``-format json`` reports imported/required findings for visibility without
changing the verdict). This gate mirrors that semantics exactly:

* every finding whose trace contains a called symbol (a frame with
  ``{"function": ...}``) is a failure unless its OSV id is listed in the
  exceptions file and the exception has not expired;
* findings that are only imported (``{"package": ...}``) or only required
  (``{"module": ...}``) are reported as informational and do not fail the gate;
  they are fixed by upgrading the module or, when the vulnerability is
  reachable in practice, by reviewing the code;
* an expired exception is itself a failure (dead configuration must be removed
  or renewed, never silently ignored);
* malformed/empty reports, a missing or malformed exceptions file, and a
  scanner exit code other than 0 (no findings) or 3 (findings found) are
  failures. ``-format json`` currently exits 0 even when findings exist, so the
  gate never relies on the exit code to decide the verdict, only to detect a
  crashed scanner.

Exceptions file format: a JSON list of objects with exactly ``id``, ``reason``
and ``expires`` (``YYYY-MM-DD``) fields.

Usage
-----
    set -o pipefail
    govulncheck -format json ./... > report.json; scanner_exit=$?
    python scripts/govulncheck-gate.py --scanner-exit "$scanner_exit" report.json
    python scripts/govulncheck-gate.py --scanner-exit 0 < report.json

Exit status: 0 = pass, 1 = policy failure, 2 = usage error.
"""

import json
import re
import sys
from datetime import date

DEFAULT_EXCEPTIONS = ".github/govulncheck-exceptions.json"
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

EXCEPTION_FIELDS = {"id", "reason", "expires"}
REQUIRED_EXCEPTION_FIELDS = {"id", "reason", "expires"}

# govulncheck: 0 = nothing found, 3 = vulnerabilities found, anything else is
# a scanner failure (bad flags, broken toolchain, network error, ...).
SCANNER_OK_EXIT_CODES = (0, 3)

# Increasing severity; a finding is as severe as its most severe trace.
SEVERITY = ("required", "imported", "called")


class GateError(Exception):
    """Raised for malformed input or configuration."""


def _parse_date(value):
    if not isinstance(value, str) or not ISO_DATE_RE.match(value):
        raise GateError(
            f"invalid 'expires' date {value!r}; expected YYYY-MM-DD"
        )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise GateError(
            f"invalid 'expires' date {value!r}; expected YYYY-MM-DD"
        ) from exc


def _finding_level(finding):
    """Classify a govulncheck finding as 'called', 'imported' or 'required'.

    A finding's ``trace`` is the list of frames from the vulnerable symbol down
    to the module. A frame is a called symbol when it has a ``function`` key, an
    imported package when it has ``package``, and a required module when it has
    ``module``. The most severe level present in the trace wins.
    """
    levels = set()
    trace = finding.get("trace")
    if not isinstance(trace, list):
        return "required"
    for frame in trace:
        if not isinstance(frame, dict):
            continue
        if frame.get("function"):
            levels.add("called")
        elif frame.get("package"):
            levels.add("imported")
        elif frame.get("module"):
            levels.add("required")
    for level in ("called", "imported", "required"):
        if level in levels:
            return level
    return "required"


def parse_stream(text):
    """Return {osv_id: level} for every finding in a govulncheck JSON stream.

    The stream is a sequence of JSON values separated by whitespace (not a
    single JSON array). The report must start with a ``config`` object that
    proves it is a symbol-level source scan, and every ``finding`` must carry a
    well-formed OSV id and trace. Blank input, malformed JSON, a missing config,
    or a structurally broken finding are errors: a truncated or foreign report
    must never look like a clean one.
    """
    decoder = json.JSONDecoder()
    findings = {}
    saw_config = False
    index = 0
    text = text.lstrip("\ufeff")
    length = len(text)
    while True:
        while index < length and text[index] in " \t\r\n":
            index += 1
        if index >= length:
            break
        try:
            event, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            raise GateError(
                f"malformed JSON near byte {exc.pos}: {exc.msg}"
            ) from exc
        if not isinstance(event, dict):
            raise GateError("report contains a JSON value that is not an object")
        if "config" in event:
            config = event["config"]
            if not isinstance(config, dict):
                raise GateError("report config is not a JSON object")
            if config.get("scan_level") != "symbol":
                raise GateError(
                    "report is not a symbol-level scan (scan_level="
                    f"{config.get('scan_level')!r}); called-vulnerability "
                    "semantics require -scan symbol (the default)"
                )
            saw_config = True
            continue
        if "finding" not in event:
            continue
        finding = event["finding"]
        if not isinstance(finding, dict):
            raise GateError("finding is not a JSON object")
        osv = finding.get("osv")
        if not isinstance(osv, str) or not osv.startswith("GO-"):
            raise GateError(f"finding has a malformed OSV id: {osv!r}")
        trace = finding.get("trace")
        if not isinstance(trace, list) or not trace:
            raise GateError(f"finding {osv} has no trace frames")
        if not all(isinstance(frame, dict) for frame in trace):
            raise GateError(f"finding {osv} has a non-object trace frame")
        level = _finding_level(finding)
        previous = findings.get(osv)
        if previous is not None:
            level = max(previous, level, key=SEVERITY.index)
        findings[osv] = level
    if not saw_config:
        raise GateError(
            "empty or truncated report: no govulncheck config object found"
        )
    return findings


def load_exceptions(text):
    """Return a mapping {id: expires-date} from the exceptions JSON text."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GateError(f"exceptions file is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, list):
        raise GateError("exceptions file must be a JSON list")

    exceptions = {}
    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise GateError(f"exception #{index} is not a JSON object")
        unknown = set(entry) - EXCEPTION_FIELDS
        missing = REQUIRED_EXCEPTION_FIELDS - set(entry)
        if missing:
            raise GateError(
                f"exception #{index} is missing required field(s): "
                + ", ".join(sorted(missing))
            )
        if unknown:
            raise GateError(
                f"exception #{index} has unknown field(s): "
                + ", ".join(sorted(unknown))
            )
        osv_id = entry["id"]
        if not isinstance(osv_id, str) or not osv_id.strip():
            raise GateError(f"exception #{index} has an empty id")
        reason = entry["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise GateError(f"exception {osv_id} has an empty reason")
        if osv_id in exceptions:
            raise GateError(f"duplicate exception for {osv_id}")
        exceptions[osv_id] = _parse_date(entry["expires"])
    return exceptions


def evaluate(findings, exceptions, today=None):
    """Return (unexempted called ids, expired exception ids)."""
    today = today or date.today()
    expired = sorted(osv_id for osv_id, expiry in exceptions.items() if expiry < today)
    called = {osv_id for osv_id, level in findings.items() if level == "called"}
    unexempted = sorted(called - set(exceptions))
    return unexempted, expired


def _read_text(path):
    if path is None:
        return sys.stdin.read()
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise GateError(f"cannot read {path}: {exc.strerror or exc}") from exc


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    report_path = None
    exceptions_path = DEFAULT_EXCEPTIONS
    scanner_exit = None

    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-h", "--help"):
            print(__doc__)
            return 0
        if arg in ("--scanner-exit", "--exceptions"):
            index += 1
            if index >= len(argv):
                print(f"error: {arg} needs a value", file=sys.stderr)
                return 2
            value = argv[index]
            if arg == "--exceptions":
                exceptions_path = value
            else:
                try:
                    scanner_exit = int(value)
                except ValueError:
                    print(
                        f"error: --scanner-exit must be an integer, got {value!r}",
                        file=sys.stderr,
                    )
                    return 2
        elif arg.startswith("-"):
            print(f"error: unknown option {arg}", file=sys.stderr)
            return 2
        elif report_path is None:
            report_path = arg
        else:
            print("error: at most one report path may be given", file=sys.stderr)
            return 2
        index += 1

    if scanner_exit is None:
        print(
            "error: --scanner-exit is required (pass govulncheck's exit status)",
            file=sys.stderr,
        )
        return 2
    if scanner_exit not in SCANNER_OK_EXIT_CODES:
        print(
            f"govulncheck gate: FAIL: scanner exited with status {scanner_exit}; "
            "the report is incomplete and cannot be trusted",
            file=sys.stderr,
        )
        return 1

    try:
        findings = parse_stream(_read_text(report_path))
        exceptions = load_exceptions(_read_text(exceptions_path))
    except GateError as exc:
        print(f"govulncheck gate: FAIL: {exc}", file=sys.stderr)
        return 1

    unexempted, expired = evaluate(findings, exceptions)

    informational = sorted(
        osv_id for osv_id, level in findings.items() if level != "called"
    )
    for osv_id in informational:
        print(f"govulncheck gate: info: {osv_id} is not called ({findings[osv_id]})")

    failure = False
    if expired:
        failure = True
        print(
            "govulncheck gate: FAIL: expired exception(s) must be removed or "
            "renewed:",
            file=sys.stderr,
        )
        for osv_id in expired:
            print(f"  - {osv_id}", file=sys.stderr)
    if unexempted:
        failure = True
        print(
            "govulncheck gate: FAIL: called vulnerabilities without an "
            "exception:",
            file=sys.stderr,
        )
        for osv_id in unexempted:
            print(f"  - {osv_id}", file=sys.stderr)
    if failure:
        return 1

    called = sum(1 for level in findings.values() if level == "called")
    print(
        f"govulncheck gate: ok ({called} called finding(s), all exempted; "
        f"{len(findings) - called} informational; "
        f"{len(exceptions)} active exception(s))"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
