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


def test_report_names_which_anchor_rule_matched(isolated_lock, tmp_path):
    """SABLE-1u6dr: the report must say WHICH anchor rule picked the excerpt
    start, through the real end-to-end path (run_impact_tier), not just the
    extracted helper — a bad anchor is only diagnosable at the seat if the
    name actually reaches the propagated report."""
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
    assert "[anchor: strict-fail-line]" in detail, (
        f"the report never names which anchor rule matched: {detail!r}")


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
    assert "noise" not in out, "the leading noise before the marker should be elided"
    assert "[anchor: strict-fail-line]" in out


def test_bounded_failure_detail_falls_back_to_the_head_with_no_marker():
    """No FAIL/FAILED/FAILURE line anywhere — there is no failure region to
    anchor on, so this must not crash, and must still announce truncation
    rather than silently picking an arbitrary window."""
    text = "Z" * 9000
    out = promote_lib._bounded_failure_detail(text)
    assert "truncat" in out.lower()
    assert "[anchor: no-marker-found]" in out
    assert out.split("\n", 1)[1].startswith("Z")


def test_anchor_skips_a_passing_line_that_merely_mentions_failure():
    """PLANT (SABLE-1u6dr): a PASS line that merely MENTIONS "failure" in its
    own message — the exact shape hooks/test/test-pre-dispatch-preempt.sh and
    hooks/test/test-coverage-floor-gate.sh both print in the wild — must not
    displace the real failure region. Against the pre-fix single loose regex
    (any line containing FAIL/FAILED/FAILURE anywhere), the anchor lands on
    this PASS line at position 0, and the 4000-char bound then truncates
    5000+ characters before ever reaching the real FAIL marker — this
    assertion fails against that code."""
    padding = "P" * 5000
    text = (
        "PASS: SABLE-mji: bd failure fails open (rc=0, silent allow)\n"
        + padding + "\n"
        + f"FAIL: {MARKER}\n  {DETAIL_LINE}\n"
    )
    out = promote_lib._bounded_failure_detail(text)
    assert f"FAIL: {MARKER}" in out, f"the real FAIL marker did not survive: {out!r}"
    assert DETAIL_LINE in out, f"the failure's detail line did not survive: {out!r}"
    assert "[anchor: strict-fail-line]" in out


def test_anchor_still_finds_a_loose_form_failure_with_no_strict_marker():
    """Negative control, load-bearing: a suite whose ONLY failure indication
    is a loose-form mention (no line starting with FAIL/FAILED/FAILURE at
    column 0) must still anchor on it, not fall back to the head — otherwise
    a fix that only accepts the strict fail() form silently regresses every
    suite that reports failures without that exact convention."""
    padding = "P" * 5000
    text = (
        "noise before\n" * 5
        + f"  something went wrong: FAILURE detected in {MARKER}\n  {DETAIL_LINE}\n"
        + padding
    )
    out = promote_lib._bounded_failure_detail(text)
    assert f"FAILURE detected in {MARKER}" in out, f"the loose-form marker did not survive: {out!r}"
    assert DETAIL_LINE in out
    assert "noise before" not in out
    assert "[anchor: loose-failure-mention]" in out


def test_bounded_failure_detail_names_the_anchor_rule_used():
    text = f"FAIL: {MARKER}\n" + ("Z" * 5000)
    out = promote_lib._bounded_failure_detail(text)
    assert "[anchor: strict-fail-line]" in out


# --------------------------------------------------------------------------
# SABLE-be4lo.1 regression: the fast-forward integrity check at the end of
# promote() moved from an inline `landed != preview_sha` to
# `not batch_key.tip_matches(landed, preview_sha)` when the keying module was
# consolidated. Same predicate, same GateError(4), same message — this pins
# that the abort still fires when the base's post-push tip is not the exact
# object promote just tested.
# --------------------------------------------------------------------------

REPO = "/repo"
REMOTE = "origin"
BASE = "trunk"
BRANCH = "wk-x"
BASE_SHA = "a" * 40
BRANCH_SHA = "b" * 40
PREVIEW_SHA = "c" * 40
DRIFTED_SHA = "d" * 40


def _cp(returncode=0, stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


def test_tip_equals_tested_integrity_abort_still_fires(monkeypatch):
    classify = promote_lib.classify
    preview = promote_lib.preview
    git_lib = promote_lib.git_lib

    base_ref = classify.qualify_remote_ref(REMOTE, BASE)
    branch_ref = classify.qualify_remote_ref(REMOTE, BRANCH)
    ref = "ci-verify/wk-x-ccccccc"

    # Preconditions unrelated to the integrity check itself — no-op them so
    # the test isolates exactly the tip-equals-tested assertion.
    monkeypatch.setattr(promote_lib, "assert_not_frozen", lambda repo: None)
    monkeypatch.setattr(promote_lib, "assert_landing_pair_satisfied", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "assert_coverage_floor", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "_adoption_miss_optimistic", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "cleanup_after_merge", lambda *a, **kw: None)

    monkeypatch.setattr(git_lib, "_git", lambda repo, *args, check=True: _cp(0))
    monkeypatch.setattr(preview, "materialize_preview",
                        lambda *a, **kw: (PREVIEW_SHA, ref, False))
    monkeypatch.setattr(preview, "acquire_verdict",
                        lambda repo, ref, sha: classify.Verdict(
                            "success", "http://run/1", sha, ref, source="waited"))

    # base_ref resolves to BASE_SHA for the pre-push read and the stale-base
    # check, then DRIFTED_SHA on the THIRD read — the post-push landed tip —
    # so the push itself is reported as succeeding but what landed is not the
    # object that was promoted.
    base_reads = {"n": 0}

    def _resolve(repo, ref_arg):
        if ref_arg == branch_ref:
            return BRANCH_SHA
        assert ref_arg == base_ref, f"unexpected ref resolved: {ref_arg!r}"
        base_reads["n"] += 1
        return DRIFTED_SHA if base_reads["n"] >= 3 else BASE_SHA

    monkeypatch.setattr(git_lib, "resolve_commit", _resolve)

    with pytest.raises(promote_lib.GateError) as exc:
        promote_lib.promote("SABLE-x", BRANCH, BASE, REPO, REMOTE, "optimus", None)
    assert exc.value.code == 4
    assert f"tip {DRIFTED_SHA}" in str(exc.value)
    assert f"tested preview {PREVIEW_SHA}" in str(exc.value)
