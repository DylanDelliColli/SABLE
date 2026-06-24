"""Tests for the SABLE scope-creep gate check-script (SABLE-vj4x.3 / SABLE-bijh).

The gate asserts that the files a worker actually changed (the implementation
-summary "Changed Files" section) all fall within the bead's DECLARED scope.
Out-of-scope edits fail the gate; an explicit [scope-override] reason waives it;
a missing/empty scope declaration FAILS OPEN (warn + pass) for Phase 1, because
the base decomposition does not yet record per-bead scope (sable-decomposition
in Phase 3 will).

Production resolves the summary + declared scope via GC_BEAD_ID/bd; these tests
pass the summary path and a comma-separated scope list positionally.

Runnable with `python3 -m pytest` or `python3 -m unittest`.
"""
from __future__ import annotations

import pathlib
import subprocess
import tempfile
import textwrap
import unittest

PACK_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = PACK_ROOT / "assets" / "scripts" / "checks" / "scope-creep-diff.sh"


def _summary(changed_files: str, extra: str = "") -> str:
    return textwrap.dedent(
        """\
        ---
        schema: gc.build.implementation-summary.v1
        status: approved
        ---
        # Implementation Summary

        ## Summary
        Did the thing.{extra}

        ## Intended Behavior
        It works.

        ## Changed Files
        {changed_files}

        ## Verification
        - Unit tests: ok
        - Integration tests: ok

        ## Remaining Risks
        None.
        """
    ).format(changed_files=changed_files, extra=extra)


def _run(summary_text: str | None, scope: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "implementation-summary.md"
        if summary_text is not None:
            path.write_text(summary_text, encoding="utf-8")
        proc = subprocess.run(
            ["bash", str(SCRIPT), str(path), scope],
            capture_output=True,
            text=True,
        )
        return proc.returncode


class ScopeCreepGateTests(unittest.TestCase):
    def test_all_changed_files_in_scope_pass(self) -> None:
        summary = _summary("- src/foo/bar.py\n- src/foo/baz.py")
        self.assertEqual(_run(summary, "src/foo"), 0)

    def test_exact_file_scope_pass(self) -> None:
        summary = _summary("- src/foo.py")
        self.assertEqual(_run(summary, "src/foo.py,docs/readme.md"), 0)

    def test_out_of_scope_file_fails(self) -> None:
        summary = _summary("- src/foo/bar.py\n- src/unrelated/secrets.py")
        self.assertEqual(_run(summary, "src/foo"), 1)

    def test_empty_scope_fails_open(self) -> None:
        summary = _summary("- src/anything.py\n- src/whatever.py")
        self.assertEqual(_run(summary, ""), 0)

    def test_scope_override_marker_passes(self) -> None:
        summary = _summary(
            "- src/foo/bar.py\n- src/unrelated/secrets.py",
            extra="\n[scope-override] intentionally touched shared config",
        )
        self.assertEqual(_run(summary, "src/foo"), 0)

    def test_missing_summary_file_fails(self) -> None:
        self.assertEqual(_run(None, "src/foo"), 1)


if __name__ == "__main__":
    unittest.main()
