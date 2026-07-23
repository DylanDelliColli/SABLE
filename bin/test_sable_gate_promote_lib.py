#!/usr/bin/env python3
"""sable_gate_promote_lib's impact-tier RED-report propagation (SABLE-twpe2).

bin/sable_gate_promote_lib.py's shell-suite and pytest branches of
_run_impact_tier_locked used to report a failing suite's stdout as
`cp.stdout.strip()[-800:]` — a positional TAIL, applied only on a RED, inside
the exact reporting path SABLE-np1nx's no-tail rule exists to govern ("a tail
is fine on a green run and destroys the only useful part of a red one"). On a
real suite (hooks/test/test-ci-bd-coverage-gap.sh) that cut every inline
"FAIL: <name>" line and its detail while a trailing summary survived by
accident of layout, not because it was more useful — a suite with no trailing
epilogue would have propagated NOTHING usable on a red at all. See SABLE-1gnuj
for what that cost: three agents and an evening unable to tell which of three
conjuncts in a control had actually failed, because the one artifact that
would have said so was generated, printed, and then tailed away in transit.

These tests exercise the REAL propagation path — a real git repo, a real
`.github/ci/impact-manifest.sh`, and a real failing shell suite run through
promote_lib.run_impact_tier — rather than mocking the transport, because the
whole point of the defect is WHERE in a real byte stream the cut lands.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import sable_gate_promote_lib as promote_lib  # noqa: E402

MARKER = "distinctive-marker-alpha-7f3c"
DETAIL_LINE = "root-cause-detail-line-zzyx: rc mismatch, see conjunct 2"


@pytest.fixture()
def isolated_lock(tmp_path, monkeypatch):
    """Point the lock + window log at this test's own tmp dir, so the suite
    never contends with (or corrupts) a real merge seat's state dir. Mirrors
    bin/test_promote_decision.py's fixture of the same name and purpose."""
    monkeypatch.setenv("SABLE_MG_IMPACT_LOCK", str(tmp_path / "impact-tier.lock"))
    monkeypatch.setenv("SABLE_MG_IMPACT_WINDOW_LOG", str(tmp_path / "windows.jsonl"))
    monkeypatch.delenv("SABLE_MG_IMPACT_SERIALIZE", raising=False)
    monkeypatch.delenv("SABLE_MG_IMPACT_LOCK_TIMEOUT", raising=False)
    monkeypatch.delenv("SABLE_MG_IMPACT_TIMEOUT", raising=False)
    monkeypatch.delenv("SABLE_MG_IMPACT", raising=False)
    return tmp_path


def _real_repo_with_shell_impact_tier(tmp_path, suite_script: str):
    """A real repo whose combined-tree impact tier selects exactly one real
    shell suite, hooks/test/test-red-marker.sh, running `suite_script` for
    real. No pytest half — no bin/ path is ever touched, so the pytest branch
    never fires."""
    r = tmp_path / "repo"
    r.mkdir()
    for args in (("init", "-q", "-b", "trunk"), ("config", "user.email", "t@sable.invalid"),
                 ("config", "user.name", "SABLE Test")):
        subprocess.run(["git", "-C", str(r), *args], check=True, capture_output=True)
    (r / ".github" / "ci").mkdir(parents=True)
    (r / ".github" / "ci" / "impact-manifest.sh").write_text(
        "#!/bin/sh\necho test-red-marker.sh\n")
    (r / ".github" / "ci" / "impact-manifest.sh").chmod(0o755)
    (r / "hooks" / "test").mkdir(parents=True)
    suite_path = r / "hooks" / "test" / "test-red-marker.sh"
    suite_path.write_text(suite_script)
    suite_path.chmod(0o755)
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(r), "commit", "-q", "-m", "init"], check=True,
                   capture_output=True)
    sha = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    return str(r), sha


def test_tier_red_report_preserves_the_failure_region(isolated_lock, tmp_path):
    """PLANT: against the pre-fix `cp.stdout.strip()[-800:]`, this fails —
    5000+ characters of padding follow the FAIL marker, so a trailing-800-byte
    tail excludes it entirely, exactly as it excluded test-ci-bd-coverage-
    gap.sh's inline FAIL lines in the wild (SABLE-1gnuj)."""
    padding = "P" * 5000
    suite = (
        "#!/bin/sh\n"
        "echo 'pre-padding: suite starting'\n"
        f"echo 'FAIL: {MARKER}'\n"
        f"echo '  {DETAIL_LINE}'\n"
        f"echo '{padding}'\n"
        "exit 1\n"
    )
    repo, sha = _real_repo_with_shell_impact_tier(tmp_path, suite)
    outcome, detail = promote_lib.run_impact_tier(repo, sha, ["hooks/test/test-red-marker.sh"])
    assert outcome == promote_lib.IMPACT_RED, detail
    assert f"FAIL: {MARKER}" in detail, (
        f"the failing suite's own FAIL marker did not survive propagation: {detail!r}")
    assert DETAIL_LINE in detail, (
        f"the failure's detail line did not survive propagation: {detail!r}")


def test_tier_green_does_not_dump_suite_stdout(isolated_lock, tmp_path):
    """Opposite polarity, load-bearing: a PASSING suite must not propagate its
    stdout at all, or every green promote becomes unreadable and the no-tail
    rule is defeated from the other side. Without this, a "fix" that always
    echoes the full suite output would pass the RED test above and make every
    healthy run noisy."""
    passing_blob = "Q" * 5000
    suite = (
        "#!/bin/sh\n"
        f"echo 'PASS: everything ({passing_blob})'\n"
        "exit 0\n"
    )
    repo, sha = _real_repo_with_shell_impact_tier(tmp_path, suite)
    outcome, detail = promote_lib.run_impact_tier(repo, sha, ["hooks/test/test-red-marker.sh"])
    assert outcome == promote_lib.IMPACT_GREEN, detail
    assert passing_blob not in detail, (
        f"a green suite's stdout leaked into the gate's report: {detail!r}")
    assert len(detail) < 500, (
        f"a green report should name what ran, not dump output: {detail!r}")


def test_report_is_bounded_but_bound_is_announced(isolated_lock, tmp_path):
    """If the anchored failure region still exceeds the size bound, the report
    must SAY truncation happened rather than silently eliding — a truncated
    report that reads as complete is the exact hazard SABLE-np1nx's no-tail
    rule forbids."""
    padding = "P" * 5000
    suite = (
        "#!/bin/sh\n"
        f"echo 'FAIL: {MARKER}'\n"
        f"echo '  {DETAIL_LINE}'\n"
        f"echo '{padding}'\n"
        "exit 1\n"
    )
    repo, sha = _real_repo_with_shell_impact_tier(tmp_path, suite)
    outcome, detail = promote_lib.run_impact_tier(repo, sha, ["hooks/test/test-red-marker.sh"])
    assert outcome == promote_lib.IMPACT_RED, detail
    assert "truncat" in detail.lower(), (
        f"the report was bounded but never says so: {detail!r}")


# --------------------------------------------------------------------------
# Direct coverage of the extracted helper — fast, no subprocess, pins the
# anchoring/announcement logic the tests above exercise end-to-end.
# --------------------------------------------------------------------------

def test_bounded_failure_detail_returns_full_text_under_the_limit():
    text = "FAIL: thing\n  detail here\n"
    assert promote_lib._bounded_failure_detail(text) == text.strip()


def test_bounded_failure_detail_anchors_on_the_first_fail_marker():
    text = "noise\n" * 5 + f"FAIL: {MARKER}\n  {DETAIL_LINE}\n" + ("Z" * 5000)
    out = promote_lib._bounded_failure_detail(text)
    assert f"FAIL: {MARKER}" in out
    assert DETAIL_LINE in out
    assert not out.startswith("noise"), "the leading noise before the marker should be elided"


def test_bounded_failure_detail_falls_back_to_the_head_with_no_marker():
    """No FAIL/FAILED/FAILURE line anywhere — there is no failure region to
    anchor on, so this must not crash, and must still announce truncation
    rather than silently picking an arbitrary window."""
    text = "Z" * 9000
    out = promote_lib._bounded_failure_detail(text)
    assert "truncat" in out.lower()
    assert out.startswith("Z")
