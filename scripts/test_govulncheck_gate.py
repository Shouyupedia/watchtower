"""Tests for scripts/govulncheck-gate.py.

The product script's module name contains a hyphen, so it is loaded by path.
Run from the repository root with:

    python -I -m unittest discover -s scripts -p "test_*.py" -v
"""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "govulncheck-gate.py"

_spec = importlib.util.spec_from_file_location("govulncheck_gate", SCRIPT)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

# --- fixtures ---------------------------------------------------------------

CONFIG_EVENT = {
    "config": {
        "scanner_name": "govulncheck",
        "scan_level": "symbol",
        "version": "v1.1.0",
    }
}

CALLED_FRAME = {
    "module": "example.com/app",
    "package": "example.com/app/pkg",
    "function": "pkg.Do",
}
IMPORTED_FRAME = {"module": "example.com/app", "package": "example.com/app/pkg"}
REQUIRED_FRAME = {"module": "example.com/app"}

TODAY = date(2026, 6, 15)


def finding(osv="GO-2024-0001", *frames):
    return {"osv": osv, "trace": [dict(frame) for frame in frames]}


def report(*findings, config=CONFIG_EVENT, extra=()):
    events = []
    if config is not None:
        events.append(config)
    events.extend({"finding": item} for item in findings)
    events.extend(extra)
    return "\n".join(json.dumps(event) for event in events) + "\n"


def exception(osv="GO-2024-0001", reason="Not reachable from our code paths",
              expires="2099-01-01"):
    return {"id": osv, "reason": reason, "expires": expires}


def exceptions_doc(*entries):
    return json.dumps(list(entries))


def run_main(argv, stdin_text=None):
    """Run gate.main(argv) with captured stdio and an optional stdin payload."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    original_stdin = sys.stdin
    try:
        if stdin_text is not None:
            sys.stdin = io.StringIO(stdin_text)
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = gate.main(list(argv))
    finally:
        sys.stdin = original_stdin
    return code, stdout.getvalue(), stderr.getvalue()


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def write(self, name, text):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def exceptions_file(self, *entries):
        return self.write("exceptions.json", exceptions_doc(*entries))


# --- parse_stream -----------------------------------------------------------


class ParseStreamTests(unittest.TestCase):
    def test_clean_report_yields_no_findings(self):
        self.assertEqual(gate.parse_stream(report()), {})

    def test_trailing_whitespace_is_ignored(self):
        self.assertEqual(gate.parse_stream(report() + "\n\t  \r\n"), {})

    def test_called_finding_is_classified_called(self):
        doc = report(finding("GO-2024-0001", CALLED_FRAME))
        self.assertEqual(gate.parse_stream(doc), {"GO-2024-0001": "called"})

    def test_imported_only_finding_is_informational(self):
        doc = report(finding("GO-2024-0002", IMPORTED_FRAME))
        self.assertEqual(gate.parse_stream(doc), {"GO-2024-0002": "imported"})

    def test_required_only_finding_is_informational(self):
        doc = report(finding("GO-2024-0003", REQUIRED_FRAME))
        self.assertEqual(gate.parse_stream(doc), {"GO-2024-0003": "required"})

    def test_frame_with_function_wins_over_package_and_module(self):
        frame = {"module": "m", "package": "p", "function": "f"}
        doc = report(finding("GO-2024-0004", frame))
        self.assertEqual(gate.parse_stream(doc), {"GO-2024-0004": "called"})

    def test_frame_without_any_known_key_is_required(self):
        doc = report(finding("GO-2024-0005", {}))
        self.assertEqual(gate.parse_stream(doc), {"GO-2024-0005": "required"})

    def test_severity_merge_imported_then_called(self):
        doc = report(
            finding("GO-2024-0006", IMPORTED_FRAME),
            finding("GO-2024-0006", CALLED_FRAME),
        )
        self.assertEqual(gate.parse_stream(doc), {"GO-2024-0006": "called"})

    def test_severity_merge_called_then_imported(self):
        doc = report(
            finding("GO-2024-0006", CALLED_FRAME),
            finding("GO-2024-0006", IMPORTED_FRAME),
        )
        self.assertEqual(gate.parse_stream(doc), {"GO-2024-0006": "called"})

    def test_severity_merge_required_then_imported(self):
        doc = report(
            finding("GO-2024-0007", REQUIRED_FRAME),
            finding("GO-2024-0007", IMPORTED_FRAME),
        )
        self.assertEqual(gate.parse_stream(doc), {"GO-2024-0007": "imported"})

    def test_called_frame_anywhere_in_the_trace_wins(self):
        doc = report(
            finding("GO-2024-0010", REQUIRED_FRAME, IMPORTED_FRAME, CALLED_FRAME)
        )
        self.assertEqual(gate.parse_stream(doc), {"GO-2024-0010": "called"})

    def test_concatenated_json_values_without_separators(self):
        doc = json.dumps(CONFIG_EVENT) + json.dumps(
            {"finding": finding("GO-2024-0008", CALLED_FRAME)}
        )
        self.assertEqual(gate.parse_stream(doc), {"GO-2024-0008": "called"})

    def test_bom_prefixed_report_is_accepted(self):
        doc = "﻿" + report(finding("GO-2024-0009", CALLED_FRAME))
        self.assertEqual(gate.parse_stream(doc), {"GO-2024-0009": "called"})

    def test_unknown_events_are_ignored(self):
        doc = report(extra=({"progress": {"message": "scanning"}},))
        self.assertEqual(gate.parse_stream(doc), {})

    def test_empty_input_is_rejected(self):
        with self.assertRaises(gate.GateError):
            gate.parse_stream("")

    def test_whitespace_only_input_is_rejected(self):
        with self.assertRaises(gate.GateError):
            gate.parse_stream(" \t\r\n  \n")

    def test_truncated_json_is_rejected(self):
        doc = (
            '{"config": {"scan_level": "symbol"}}\n'
            '{"finding": {"osv": "GO-2024-0001", "trace": [{"function": "f"}]'
        )
        with self.assertRaises(gate.GateError):
            gate.parse_stream(doc)

    def test_report_without_config_is_rejected(self):
        doc = json.dumps({"finding": finding("GO-2024-0001", CALLED_FRAME)})
        with self.assertRaises(gate.GateError):
            gate.parse_stream(doc)

    def test_config_that_is_not_an_object_is_rejected(self):
        with self.assertRaises(gate.GateError):
            gate.parse_stream(json.dumps({"config": []}))

    def test_config_without_scan_level_is_rejected(self):
        with self.assertRaises(gate.GateError):
            gate.parse_stream(json.dumps({"config": {"scanner_name": "govulncheck"}}))

    def test_non_symbol_scan_levels_are_rejected(self):
        for level in ("package", "module", "", None, 1):
            with self.subTest(scan_level=level):
                doc = report(config={"config": {"scan_level": level}})
                with self.assertRaises(gate.GateError):
                    gate.parse_stream(doc)

    def test_top_level_array_is_rejected(self):
        with self.assertRaises(gate.GateError):
            gate.parse_stream("[]")

    def test_top_level_string_is_rejected(self):
        with self.assertRaises(gate.GateError):
            gate.parse_stream('"report"')

    def test_top_level_number_is_rejected(self):
        with self.assertRaises(gate.GateError):
            gate.parse_stream("42")

    def test_non_object_value_after_config_is_rejected(self):
        doc = json.dumps(CONFIG_EVENT) + "\n[1, 2]\n"
        with self.assertRaises(gate.GateError):
            gate.parse_stream(doc)

    def test_finding_that_is_not_an_object_is_rejected(self):
        doc = report(extra=({"finding": "not-an-object"},))
        with self.assertRaises(gate.GateError):
            gate.parse_stream(doc)

    def test_bad_osv_ids_are_rejected(self):
        for value in (None, 42, "", "CVE-2024-1234", "go-2024-0001", "GO"):
            entry = {"osv": value, "trace": [dict(CALLED_FRAME)]}
            with self.subTest(osv=value):
                with self.assertRaises(gate.GateError):
                    gate.parse_stream(report(entry))

    def test_missing_osv_id_is_rejected(self):
        entry = {"trace": [dict(CALLED_FRAME)]}
        with self.assertRaises(gate.GateError):
            gate.parse_stream(report(entry))

    def test_empty_trace_is_rejected(self):
        with self.assertRaises(gate.GateError):
            gate.parse_stream(report(finding("GO-2024-0001")))

    def test_missing_trace_is_rejected(self):
        entry = {"osv": "GO-2024-0001"}
        with self.assertRaises(gate.GateError):
            gate.parse_stream(report(entry))

    def test_trace_that_is_not_a_list_is_rejected(self):
        entry = {"osv": "GO-2024-0001", "trace": "frame"}
        with self.assertRaises(gate.GateError):
            gate.parse_stream(report(entry))

    def test_non_object_trace_frame_is_rejected(self):
        entry = {"osv": "GO-2024-0001", "trace": ["frame"]}
        with self.assertRaises(gate.GateError):
            gate.parse_stream(report(entry))


# --- load_exceptions --------------------------------------------------------


class LoadExceptionsTests(unittest.TestCase):
    def test_valid_exception_is_loaded(self):
        parsed = gate.load_exceptions(exceptions_doc(exception()))
        self.assertEqual(parsed, {"GO-2024-0001": date(2099, 1, 1)})

    def test_multiple_exceptions_are_loaded(self):
        parsed = gate.load_exceptions(
            exceptions_doc(
                exception("GO-2024-0001", expires="2099-01-01"),
                exception("GO-2024-0002", expires="2030-12-31"),
            )
        )
        self.assertEqual(
            parsed,
            {"GO-2024-0001": date(2099, 1, 1), "GO-2024-0002": date(2030, 12, 31)},
        )

    def test_empty_list_is_valid(self):
        self.assertEqual(gate.load_exceptions("[]"), {})

    def test_invalid_json_is_rejected(self):
        with self.assertRaises(gate.GateError):
            gate.load_exceptions("[{")

    def test_non_list_is_rejected(self):
        for value in (json.dumps({"id": "GO-2024-0001"}), '"x"', "1"):
            with self.subTest(value=value):
                with self.assertRaises(gate.GateError):
                    gate.load_exceptions(value)

    def test_non_object_entry_is_rejected(self):
        with self.assertRaises(gate.GateError):
            gate.load_exceptions(json.dumps(["GO-2024-0001"]))

    def test_missing_fields_are_rejected(self):
        for field in ("id", "reason", "expires"):
            entry = exception()
            del entry[field]
            with self.subTest(missing=field):
                with self.assertRaises(gate.GateError):
                    gate.load_exceptions(exceptions_doc(entry))

    def test_unknown_field_is_rejected(self):
        entry = exception()
        entry["owner"] = "team"
        with self.assertRaises(gate.GateError):
            gate.load_exceptions(exceptions_doc(entry))

    def test_empty_id_is_rejected(self):
        for value in ("", "   ", None, 7):
            with self.subTest(id=value):
                with self.assertRaises(gate.GateError):
                    gate.load_exceptions(exceptions_doc(exception(osv=value)))

    def test_empty_reason_is_rejected(self):
        for value in ("", "   ", None, 7):
            with self.subTest(reason=value):
                with self.assertRaises(gate.GateError):
                    gate.load_exceptions(exceptions_doc(exception(reason=value)))

    def test_duplicate_id_is_rejected(self):
        with self.assertRaises(gate.GateError):
            gate.load_exceptions(
                exceptions_doc(exception(), exception(reason="duplicate"))
            )

    def test_invalid_dates_are_rejected(self):
        for value in ("not-a-date", "2026-13-01", "2026-02-30", "01/02/2026", None, 20260101):
            with self.subTest(expires=value):
                with self.assertRaises(gate.GateError):
                    gate.load_exceptions(exceptions_doc(exception(expires=value)))

    def test_compact_iso_date_is_rejected(self):
        """FINDING: 'expires' is documented as YYYY-MM-DD, but
        date.fromisoformat() at govulncheck-gate.py:63 also accepts the
        compact form '20261124' (and ISO week dates), so the documented
        format is not actually enforced."""
        with self.assertRaises(gate.GateError):
            gate.load_exceptions(exceptions_doc(exception(expires="20261124")))


# --- evaluate ---------------------------------------------------------------


class EvaluateTests(unittest.TestCase):
    def test_called_finding_without_exception_is_unexempted(self):
        unexempted, expired = gate.evaluate({"GO-1": "called"}, {}, today=TODAY)
        self.assertEqual((unexempted, expired), (["GO-1"], []))

    def test_called_finding_with_active_exception_is_exempt(self):
        exceptions = {"GO-1": TODAY + timedelta(days=1)}
        unexempted, expired = gate.evaluate({"GO-1": "called"}, exceptions, today=TODAY)
        self.assertEqual((unexempted, expired), ([], []))

    def test_expiry_today_is_still_active(self):
        unexempted, expired = gate.evaluate({"GO-1": "called"}, {"GO-1": TODAY}, today=TODAY)
        self.assertEqual((unexempted, expired), ([], []))

    def test_expired_exception_is_reported_but_covers_the_finding(self):
        exceptions = {"GO-1": TODAY - timedelta(days=1)}
        unexempted, expired = gate.evaluate({"GO-1": "called"}, exceptions, today=TODAY)
        self.assertEqual(unexempted, [])
        self.assertEqual(expired, ["GO-1"])

    def test_expired_exception_fails_even_with_nothing_called(self):
        exceptions = {"GO-1": TODAY - timedelta(days=1)}
        unexempted, expired = gate.evaluate({"GO-2": "imported"}, exceptions, today=TODAY)
        self.assertEqual(unexempted, [])
        self.assertEqual(expired, ["GO-1"])

    def test_imported_and_required_findings_are_never_unexempted(self):
        findings = {"GO-1": "imported", "GO-2": "required"}
        unexempted, expired = gate.evaluate(findings, {}, today=TODAY)
        self.assertEqual((unexempted, expired), ([], []))

    def test_results_are_sorted(self):
        unexempted, _ = gate.evaluate(
            {"GO-3": "called", "GO-1": "called", "GO-2": "called"}, {}, today=TODAY
        )
        self.assertEqual(unexempted, ["GO-1", "GO-2", "GO-3"])


# --- main -------------------------------------------------------------------


class MainTests(TempDirTest):
    def test_clean_report_via_file_passes(self):
        report_path = self.write("report.json", report())
        exc = self.exceptions_file()
        code, out, err = run_main(
            ["--scanner-exit", "0", "--exceptions", exc, report_path]
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("govulncheck gate: ok", out)

    def test_clean_report_via_stdin_passes(self):
        exc = self.exceptions_file()
        code, out, err = run_main(
            ["--scanner-exit", "0", "--exceptions", exc], stdin_text=report()
        )
        self.assertEqual(code, 0, err)
        self.assertIn("govulncheck gate: ok", out)

    def test_bom_prefixed_report_passes(self):
        report_path = self.write("report.json", "﻿" + report())
        exc = self.exceptions_file()
        code, _, err = run_main(
            ["--scanner-exit", "0", "--exceptions", exc, report_path]
        )
        self.assertEqual(code, 0, err)

    def test_called_finding_fails(self):
        report_path = self.write(
            "report.json", report(finding("GO-2024-0001", CALLED_FRAME))
        )
        exc = self.exceptions_file()
        code, _, err = run_main(
            ["--scanner-exit", "3", "--exceptions", exc, report_path]
        )
        self.assertEqual(code, 1)
        self.assertIn("without an exception", err)
        self.assertIn("GO-2024-0001", err)

    def test_called_finding_fails_even_when_scanner_exit_is_zero(self):
        # govulncheck -format json exits 0 even when findings exist, so the
        # gate must decide from the report, never from the scanner exit code.
        report_path = self.write(
            "report.json", report(finding("GO-2024-0001", CALLED_FRAME))
        )
        exc = self.exceptions_file()
        code, _, err = run_main(
            ["--scanner-exit", "0", "--exceptions", exc, report_path]
        )
        self.assertEqual(code, 1)
        self.assertIn("GO-2024-0001", err)

    def test_called_finding_with_active_exception_passes(self):
        report_path = self.write(
            "report.json", report(finding("GO-2024-0001", CALLED_FRAME))
        )
        exc = self.exceptions_file(exception("GO-2024-0001", expires="2099-01-01"))
        code, out, err = run_main(
            ["--scanner-exit", "3", "--exceptions", exc, report_path]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("1 called finding(s), all exempted", out)

    def test_expired_exception_fails(self):
        report_path = self.write(
            "report.json", report(finding("GO-2024-0001", CALLED_FRAME))
        )
        exc = self.exceptions_file(exception("GO-2024-0001", expires="2000-01-01"))
        code, _, err = run_main(
            ["--scanner-exit", "3", "--exceptions", exc, report_path]
        )
        self.assertEqual(code, 1)
        self.assertIn("expired exception", err)

    def test_expired_exception_fails_without_any_finding(self):
        report_path = self.write("report.json", report())
        exc = self.exceptions_file(exception("GO-2024-0001", expires="2000-01-01"))
        code, _, err = run_main(
            ["--scanner-exit", "0", "--exceptions", exc, report_path]
        )
        self.assertEqual(code, 1)
        self.assertIn("expired exception", err)

    def test_imported_only_finding_passes_but_is_reported(self):
        report_path = self.write(
            "report.json", report(finding("GO-2024-0002", IMPORTED_FRAME))
        )
        exc = self.exceptions_file()
        code, out, err = run_main(
            ["--scanner-exit", "3", "--exceptions", exc, report_path]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("info: GO-2024-0002 is not called (imported)", out)

    def test_required_only_finding_passes_but_is_reported(self):
        report_path = self.write(
            "report.json", report(finding("GO-2024-0003", REQUIRED_FRAME))
        )
        exc = self.exceptions_file()
        code, out, err = run_main(
            ["--scanner-exit", "3", "--exceptions", exc, report_path]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("info: GO-2024-0003 is not called (required)", out)

    def test_scanner_exit_three_is_accepted(self):
        report_path = self.write("report.json", report())
        exc = self.exceptions_file()
        code, _, err = run_main(
            ["--scanner-exit", "3", "--exceptions", exc, report_path]
        )
        self.assertEqual(code, 0, err)

    def test_scanner_exit_one_fails(self):
        code, _, err = run_main(["--scanner-exit", "1"])
        self.assertEqual(code, 1)
        self.assertIn("scanner exited with status 1", err)

    def test_scanner_exit_two_fails(self):
        code, _, err = run_main(["--scanner-exit", "2"])
        self.assertEqual(code, 1)
        self.assertIn("scanner exited with status 2", err)

    def test_scanner_exit_four_fails(self):
        code, _, _ = run_main(["--scanner-exit", "4"])
        self.assertEqual(code, 1)

    def test_missing_scanner_exit_is_a_usage_error(self):
        code, _, err = run_main([])
        self.assertEqual(code, 2)
        self.assertIn("--scanner-exit is required", err)

    def test_scanner_exit_without_value_is_a_usage_error(self):
        code, _, err = run_main(["--scanner-exit"])
        self.assertEqual(code, 2)
        self.assertIn("needs a value", err)

    def test_non_integer_scanner_exit_is_a_usage_error(self):
        code, _, err = run_main(["--scanner-exit", "three"])
        self.assertEqual(code, 2)
        self.assertIn("must be an integer", err)

    def test_unknown_option_is_a_usage_error(self):
        code, _, err = run_main(["--scanner-exit", "0", "--bogus"])
        self.assertEqual(code, 2)
        self.assertIn("unknown option", err)

    def test_two_report_paths_are_a_usage_error(self):
        code, _, err = run_main(["--scanner-exit", "0", "a.json", "b.json"])
        self.assertEqual(code, 2)
        self.assertIn("at most one report path", err)

    def test_missing_report_file_fails(self):
        exc = self.exceptions_file()
        missing = str(self.tmp / "does-not-exist.json")
        code, _, err = run_main(
            ["--scanner-exit", "0", "--exceptions", exc, missing]
        )
        self.assertEqual(code, 1)
        self.assertIn("cannot read", err)

    def test_missing_exceptions_file_fails(self):
        report_path = self.write("report.json", report())
        missing = str(self.tmp / "missing-exceptions.json")
        code, _, err = run_main(
            ["--scanner-exit", "0", "--exceptions", missing, report_path]
        )
        self.assertEqual(code, 1)
        self.assertIn("cannot read", err)

    def test_malformed_report_via_file_fails(self):
        report_path = self.write("report.json", "")
        exc = self.exceptions_file()
        code, _, err = run_main(
            ["--scanner-exit", "0", "--exceptions", exc, report_path]
        )
        self.assertEqual(code, 1)
        self.assertIn("no govulncheck config object found", err)

    def test_help_exits_zero_without_scanner_exit(self):
        code, out, err = run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("Usage", out)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
