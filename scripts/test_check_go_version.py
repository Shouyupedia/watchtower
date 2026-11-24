"""Tests for scripts/check-go-version.py.

The product script's module name contains a hyphen, so it is loaded by path.
Run from the repository root with:

    python -I -m unittest discover -s scripts -p "test_*.py" -v
"""

import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check-go-version.py"
REPO_ROOT = SCRIPT.resolve().parent.parent

_spec = importlib.util.spec_from_file_location("check_go_version", SCRIPT)
assert _spec is not None and _spec.loader is not None
cgv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cgv)

# --- fixtures ---------------------------------------------------------------

GO_VERSION = "1.26.11"
DIGEST = "0123456789abcdef" * 4  # exactly 64 lowercase hex characters
MISSING = object()


def pinned(tag=GO_VERSION, digest=DIGEST, stage="builder"):
    return f"FROM golang:{tag}-alpine@sha256:{digest} AS {stage}"


DEFAULT_DOCKERFILE = (
    pinned() + "\nRUN go build ./...\nFROM scratch\n"
)
DEFAULT_GO_MOD = "module example.com/app\n\ngo 1.26.0\n"
DEFAULT_TOOLS_GO_MOD = "module example.com/tools\n\ngo 1.26.4\n"


def write_file(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_repo(root, go_version="1.26.11\n", dockerfiles=None, gomods=None):
    """Populate a fake repository root; MISSING omits a file."""
    root = Path(root)
    if go_version is not MISSING:
        write_file(root / ".go-version", go_version)

    dockerfiles = dict(dockerfiles or {})
    for rel in cgv.DOCKERFILES:
        body = dockerfiles.get(rel, DEFAULT_DOCKERFILE)
        if body is not MISSING:
            write_file(root / rel, body)

    gomods = dict(gomods or {})
    for rel in cgv.GO_MODS:
        default = DEFAULT_GO_MOD if rel == "go.mod" else DEFAULT_TOOLS_GO_MOD
        body = gomods.get(rel, default)
        if body is not MISSING:
            write_file(root / rel, body)
    return root


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def collect(self, **kwargs):
        return cgv.collect(make_repo(self.tmp, **kwargs))

    def run_main(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cgv.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()


# --- collect: happy paths ---------------------------------------------------


class CollectHappyPathTests(TempDirTest):
    def test_consistent_repository_has_no_problems(self):
        self.assertEqual(self.collect(), [])

    def test_go_version_without_trailing_newline(self):
        self.assertEqual(self.collect(go_version="1.26.11"), [])

    def test_go_version_with_surrounding_whitespace(self):
        self.assertEqual(self.collect(go_version="  1.26.11  \n"), [])

    def test_crlf_dockerfile_is_accepted(self):
        body = pinned() + "\r\nFROM scratch\r\n"
        self.assertEqual(
            self.collect(dockerfiles={"dockerfiles/Dockerfile": body}), []
        )

    def test_platform_flag_and_stage_name_are_accepted(self):
        body = (
            "FROM --platform=$BUILDPLATFORM golang:1.26.11-alpine"
            f"@sha256:{DIGEST} AS builder\n"
        )
        self.assertEqual(
            self.collect(dockerfiles={"dockerfiles/Dockerfile": body}), []
        )

    def test_multi_stage_with_two_matching_golang_images(self):
        body = pinned(stage="base") + "\n" + pinned(stage="tools") + "\n"
        self.assertEqual(
            self.collect(dockerfiles={"dockerfiles/Dockerfile": body}), []
        )

    def test_patch_level_lag_in_go_directives_is_allowed(self):
        self.assertEqual(
            self.collect(
                gomods={
                    "go.mod": "module example.com/app\n\ngo 1.26.4\n",
                    "tools/go.mod": "module example.com/tools\n\ngo 1.26.0\n",
                }
            ),
            [],
        )

    def test_equal_patch_and_omitted_patch_in_go_directives_are_allowed(self):
        self.assertEqual(
            self.collect(
                gomods={
                    "go.mod": "module example.com/app\n\ngo 1.26\n",
                    "tools/go.mod": "module example.com/tools\n\ngo 1.26.11\n",
                }
            ),
            [],
        )

    def test_collect_accepts_a_string_root(self):
        make_repo(self.tmp)
        self.assertEqual(cgv.collect(str(self.tmp)), [])

    def test_real_repository_is_consistent(self):
        self.assertEqual(cgv.collect(REPO_ROOT), [])


# --- collect: failures ------------------------------------------------------


class CollectFailureTests(TempDirTest):
    def test_dockerfile_tag_mismatch_is_reported(self):
        body = pinned(tag="1.26.10") + "\n"
        problems = self.collect(dockerfiles={"dockerfiles/Dockerfile": body})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("dockerfiles/Dockerfile", problems[0])
        self.assertIn("1.26.10", problems[0])
        self.assertIn("does not match", problems[0])

    def test_dockerfile_missing_digest_is_reported(self):
        body = "FROM golang:1.26.11-alpine AS builder\n"
        problems = self.collect(dockerfiles={"dockerfiles/Dockerfile": body})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("sha256", problems[0])

    def test_dockerfile_short_digest_is_reported(self):
        body = "FROM golang:1.26.11-alpine@sha256:abc123 AS builder\n"
        problems = self.collect(dockerfiles={"dockerfiles/Dockerfile": body})
        self.assertEqual(len(problems), 1, problems)

    def test_dockerfile_non_hex_digest_is_reported(self):
        body = "FROM golang:1.26.11-alpine@sha256:" + "z" * 64 + " AS builder\n"
        problems = self.collect(dockerfiles={"dockerfiles/Dockerfile": body})
        self.assertEqual(len(problems), 1, problems)

    def test_dockerfile_without_any_pinned_golang_image_is_reported(self):
        problems = self.collect(
            dockerfiles={"dockerfiles/Dockerfile": "FROM scratch\n"}
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no pinned golang FROM line found", problems[0])

    def test_unpinned_bare_golang_stage_is_reported(self):
        body = pinned() + "\nFROM golang AS debug\n"
        problems = self.collect(dockerfiles={"dockerfiles/Dockerfile": body})
        self.assertTrue(
            problems, "unpinned 'FROM golang' stage was not reported"
        )

    def test_unpinned_tab_separated_golang_stage_is_reported(self):
        body = pinned() + "\nFROM\tgolang AS debug\n"
        problems = self.collect(dockerfiles={"dockerfiles/Dockerfile": body})
        self.assertTrue(
            problems, "unpinned tab-separated 'FROM\\tgolang' stage was not reported"
        )

    def test_multi_stage_second_golang_image_mismatch_is_reported(self):
        body = pinned(stage="base") + "\n" + pinned(tag="1.25.0", stage="tools") + "\n"
        problems = self.collect(dockerfiles={"dockerfiles/Dockerfile": body})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("1.25.0", problems[0])

    def test_each_dockerfile_is_checked(self):
        problems = self.collect(
            dockerfiles={
                "dockerfiles/Dockerfile.self-contained": pinned(tag="1.26.10")
            }
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("dockerfiles/Dockerfile.self-contained", problems[0])

    def test_go_mod_series_drift_is_reported(self):
        problems = self.collect(
            gomods={"go.mod": "module example.com/app\n\ngo 1.25\n"}
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("go.mod", problems[0])
        self.assertIn("not in the same series", problems[0])

    def test_tools_go_mod_series_drift_is_reported(self):
        problems = self.collect(
            gomods={"tools/go.mod": "module example.com/tools\n\ngo 1.27.0\n"}
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("tools/go.mod", problems[0])

    def test_go_mod_newer_patch_than_pin_is_reported(self):
        problems = self.collect(
            gomods={"go.mod": "module example.com/app\n\ngo 1.26.12\n"}
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("go.mod", problems[0])
        self.assertIn("1.26.12", problems[0])
        self.assertIn("newer patch", problems[0])

    def test_tools_go_mod_newer_patch_than_pin_is_reported(self):
        problems = self.collect(
            gomods={"tools/go.mod": "module example.com/tools\n\ngo 1.26.12\n"}
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("tools/go.mod", problems[0])
        self.assertIn("1.26.12", problems[0])
        self.assertIn("newer patch", problems[0])

    def test_major_version_drift_is_reported(self):
        problems = self.collect(
            gomods={"go.mod": "module example.com/app\n\ngo 2.0.0\n"}
        )
        self.assertEqual(len(problems), 1, problems)

    def test_go_mod_without_directive_is_reported(self):
        problems = self.collect(gomods={"go.mod": "module example.com/app\n"})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no go directive found", problems[0])

    def test_malformed_go_directive_is_reported(self):
        problems = self.collect(
            gomods={"go.mod": "module example.com/app\n\ngo 1.x\n"}
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("malformed go directive", problems[0])

    def test_missing_go_version_file_is_reported(self):
        problems = self.collect(go_version=MISSING)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(".go-version", problems[0])
        self.assertIn("cannot read", problems[0])

    def test_bad_go_version_is_reported(self):
        for value in ("1.26", "1.26.x", "v1.26.11", "", "1.26.11\n1.26.12"):
            with self.subTest(go_version=value):
                problems = self.collect(go_version=value)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("exact version", problems[0])

    def test_missing_dockerfile_is_reported(self):
        problems = self.collect(
            dockerfiles={"dockerfiles/Dockerfile.dev-self-contained": MISSING}
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("cannot read", problems[0])

    def test_missing_go_mod_is_reported(self):
        problems = self.collect(gomods={"tools/go.mod": MISSING})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("cannot read", problems[0])
        self.assertIn("go.mod", problems[0])

    def test_multiple_problems_accumulate(self):
        problems = self.collect(
            dockerfiles={"dockerfiles/Dockerfile": pinned(tag="1.26.10")},
            gomods={"go.mod": "module example.com/app\n\ngo 1.25\n"},
        )
        self.assertEqual(len(problems), 2, problems)


# --- parsers ----------------------------------------------------------------


class ParseGoVersionTests(unittest.TestCase):
    def test_valid_versions(self):
        for text, expected in (
            ("1.26.11", (1, 26, 11)),
            ("1.26.11\n", (1, 26, 11)),
            ("  1.26.11  ", (1, 26, 11)),
            ("1.0.0", (1, 0, 0)),
            ("10.20.30", (10, 20, 30)),
        ):
            with self.subTest(text=text):
                self.assertEqual(cgv.parse_go_version(text), expected)

    def test_invalid_versions(self):
        for text in ("1.26", "1.26.x", "v1.26.11", "", " ", "1.26.11-rc1",
                     "1.26.11\n1.26.12", "go1.26.11", "1.26.11.1"):
            with self.subTest(text=text):
                with self.assertRaises(cgv.CheckError):
                    cgv.parse_go_version(text)


class ParseGoDirectiveTests(unittest.TestCase):
    def test_major_minor_directive(self):
        self.assertEqual(cgv.parse_go_directive("go.mod", "go 1.26\n"), (1, 26, 0))

    def test_patch_directive_returns_major_minor_patch(self):
        self.assertEqual(
            cgv.parse_go_directive("go.mod", "module m\n\ngo 1.26.4\n"),
            (1, 26, 4),
        )

    def test_indented_directive(self):
        self.assertEqual(
            cgv.parse_go_directive("go.mod", "  go 1.26.4\n"), (1, 26, 4)
        )

    def test_tab_separated_directive(self):
        self.assertEqual(cgv.parse_go_directive("go.mod", "go\t1.26\n"), (1, 26, 0))

    def test_first_directive_wins(self):
        text = "module m\n\ngo 1.26\n\ngo 1.25\n"
        self.assertEqual(cgv.parse_go_directive("go.mod", text), (1, 26, 0))

    def test_missing_directive(self):
        with self.assertRaises(cgv.CheckError):
            cgv.parse_go_directive("go.mod", "module m\n")

    def test_malformed_directive(self):
        for text in ("go 1.x\n", "go 1\n", "go 1.26.4.5\n"):
            with self.subTest(text=text):
                with self.assertRaises(cgv.CheckError):
                    cgv.parse_go_directive("go.mod", text)


class ParseDockerfileTests(unittest.TestCase):
    def test_pinned_image_returns_tag_and_digest(self):
        images = cgv.parse_dockerfile_golang_images(
            "Dockerfile", pinned() + "\n"
        )
        self.assertEqual(images, [(GO_VERSION, DIGEST)])

    def test_two_golang_images_are_both_returned(self):
        body = pinned(stage="base") + "\n" + pinned(tag="1.26.10", stage="tools") + "\n"
        images = cgv.parse_dockerfile_golang_images("Dockerfile", body)
        self.assertEqual(
            images, [(GO_VERSION, DIGEST), ("1.26.10", DIGEST)]
        )

    def test_non_golang_from_lines_are_ignored(self):
        body = "FROM scratch\nFROM alpine:3.20\n" + pinned() + "\n"
        self.assertEqual(
            cgv.parse_dockerfile_golang_images("Dockerfile", body),
            [(GO_VERSION, DIGEST)],
        )

    def test_missing_digest_raises(self):
        with self.assertRaises(cgv.CheckError):
            cgv.parse_dockerfile_golang_images(
                "Dockerfile", "FROM golang:1.26.11-alpine AS builder\n"
            )

    def test_no_golang_image_raises(self):
        with self.assertRaises(cgv.CheckError):
            cgv.parse_dockerfile_golang_images("Dockerfile", "FROM scratch\n")


# --- main -------------------------------------------------------------------


class MainTests(TempDirTest):
    def test_consistent_root_exits_zero(self):
        make_repo(self.tmp)
        code, out, err = self.run_main(["--root", str(self.tmp)])
        self.assertEqual(code, 0, err)
        self.assertIn("check-go-version: ok", out)
        self.assertEqual(err, "")

    def test_drift_exits_one_and_lists_problems(self):
        make_repo(self.tmp, dockerfiles={"dockerfiles/Dockerfile": pinned(tag="1.26.10")})
        code, out, err = self.run_main(["--root", str(self.tmp)])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("check-go-version: FAIL", err)
        self.assertIn("does not match", err)

    def test_missing_root_argument_is_a_usage_error(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                cgv.main(["--root"])
        self.assertEqual(ctx.exception.code, 2)

    def test_unknown_option_is_a_usage_error(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                cgv.main(["--bogus"])
        self.assertEqual(ctx.exception.code, 2)

    def test_nonexistent_root_exits_one(self):
        missing = str(self.tmp / "does-not-exist")
        code, _, err = self.run_main(["--root", missing])
        self.assertEqual(code, 1)
        self.assertIn("check-go-version: FAIL", err)


if __name__ == "__main__":
    unittest.main()
