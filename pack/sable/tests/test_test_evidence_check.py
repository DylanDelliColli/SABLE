"""Tests for the SABLE test-evidence gate check-script (SABLE-vj4x.2).

The gate mechanically asserts that a work item's implementation-summary records
BOTH a unit-test proof AND an integration-test proof (the SABLE Prime
Directive), honoring [no-integration] and [no-test] escapes. It is the
self-report-independent complement to the base 3-verdict review gate (which
carries an *agent* code_review.test_evidence_verdict).

Production resolves the summary via GC_BEAD_ID/bd; these tests pass the summary
path positionally to bypass bd.

Runnable with `python3 -m pytest` or `python3 -m unittest`.
"""
from __future__ import annotations

import pathlib
import subprocess
import tempfile
import textwrap
import unittest

PACK_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = PACK_ROOT / "assets" / "scripts" / "checks" / "test-evidence.sh"


def _summary(verification: str) -> str:
    return textwrap.dedent(
        """\
        ---
        schema: gc.build.implementation-summary.v1
        status: approved
        ---
        # Implementation Summary

        ## Summary
        Did the thing.

        ## Intended Behavior
        It works.

        ## Changed Files
        - src/foo.py

        ## Verification
        {verification}

        ## Remaining Risks
        None.
        """
    ).format(verification=verification)


def _run(summary_text: str | None) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "implementation-summary.md"
        if summary_text is not None:
            path.write_text(summary_text, encoding="utf-8")
        proc = subprocess.run(
            ["bash", str(SCRIPT), str(path)],
            capture_output=True,
            text=True,
        )
        return proc.returncode


class TestEvidenceGateTests(unittest.TestCase):
    def test_both_unit_and_integration_pass(self) -> None:
        self.assertEqual(
            _run(
                _summary(
                    "- Unit tests: pytest tests/unit/test_foo.py (3 passed)\n"
                    "- Integration tests: pytest tests/integration/test_foo_db.py (2 passed)"
                )
            ),
            0,
        )

    def test_missing_integration_fails(self) -> None:
        self.assertEqual(
            _run(_summary("- Unit tests: pytest tests/unit/test_foo.py (3 passed)")),
            1,
        )

    def test_missing_unit_fails(self) -> None:
        self.assertEqual(
            _run(
                _summary(
                    "- Integration tests: pytest tests/integration/test_foo_db.py (2 passed)"
                )
            ),
            1,
        )

    def test_no_integration_escape_passes(self) -> None:
        self.assertEqual(
            _run(
                _summary(
                    "- Unit tests: pytest tests/unit/test_foo.py (3 passed)\n"
                    "- [no-integration] pure data-model change with no integration surface"
                )
            ),
            0,
        )

    def test_no_test_escape_passes(self) -> None:
        self.assertEqual(_run(_summary("- [no-test] docs-only change")), 0)

    def test_missing_summary_file_fails(self) -> None:
        self.assertEqual(_run(None), 1)


if __name__ == "__main__":
    unittest.main()
