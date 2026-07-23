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
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import sable_gate_promote_lib as promote_lib  # noqa: E402

# The ci-verify clean-room is tmux+pytest only -- no bd/dolt by design. The
# real-sandbox attention-record test drives a REAL sandbox promote() call
# (which shells to bd), so it self-skips when bd is absent, matching the
# bd/dolt-suites-self-skip contract in ci-verify.yml.
HAVE_BD = shutil.which("bd") is not None

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


# --------------------------------------------------------------------------
# SABLE-21rug.1: seat-attention instrumentation — per-landing attention
# record, additive tier-journal verdict+writer_identity fields, and the
# baseline computation. THE MANDATORY FIRST bead of the merge-seat epic: no
# sibling may claim a reduction in attended time without this baseline.
# --------------------------------------------------------------------------

def test_attention_record_round_trips_with_unknown_field_tolerance():
    """UNIT: an AttentionRecord survives to_dict -> from_dict with every field
    typed, and a future/unrecognized key in the dict must not break the
    reconstruction — the same additive-field discipline the tier journal's
    schema already carries, applied here to the attention record."""
    record = promote_lib.AttentionRecord(
        bead="SABLE-x", branch="wk-x",
        verdict_source=promote_lib.VerdictSource.PRECOMPUTED,
        hand_run_suites=("test-a.sh", "test-b.sh"),
        red_triage_events=("base_moved",),
        attention_span_seconds=42.5)
    data = record.to_dict()
    data["some_future_field_nobody_wrote_yet"] = "surprise"
    restored = promote_lib.AttentionRecord.from_dict(data)
    assert restored == record
    assert isinstance(restored.verdict_source, promote_lib.VerdictSource)
    assert isinstance(restored.hand_run_suites, tuple)
    assert isinstance(restored.red_triage_events, tuple)
    assert isinstance(restored.attention_span_seconds, float)


def test_attention_record_from_dict_tolerates_a_bare_minimum_dict():
    """Negative-space companion: a dict missing every optional key entirely
    (not just carrying an extra one) must still reconstruct instead of
    raising KeyError."""
    restored = promote_lib.AttentionRecord.from_dict({"bead": "SABLE-y", "branch": "wk-y"})
    assert restored.verdict_source == promote_lib.VerdictSource.WAITED
    assert restored.hand_run_suites == ()
    assert restored.red_triage_events == ()
    assert restored.attention_span_seconds == 0.0


def test_suites_from_impact_detail_parses_the_green_suite_list():
    detail = "impact tier GREEN on the combined tree: test-a.sh, test-b.sh"
    assert promote_lib._suites_from_impact_detail(detail) == ("test-a.sh", "test-b.sh")


def test_suites_from_impact_detail_empty_for_an_unrecognized_format():
    """An override's detail names no suites at all — this must degrade to an
    empty tuple, not guess or crash."""
    assert promote_lib._suites_from_impact_detail("impact tier override reported green") == ()


def test_compute_attention_baseline_raises_on_empty_landing_set():
    """Vacuous guard, load-bearing: an empty landing set must RAISE, never
    return zero-as-a-number — '0 attended minutes' is indistinguishable from
    'we measured nothing', the exact failure this epic exists to fix."""
    with pytest.raises(promote_lib.EmptyLandingSetError):
        promote_lib.compute_attention_baseline([])


def test_compute_attention_baseline_computes_a_figure_from_a_landing_fixture():
    records = [
        promote_lib.AttentionRecord(bead="SABLE-a", branch="wk-a",
                                    verdict_source=promote_lib.VerdictSource.WAITED,
                                    attention_span_seconds=60.0),
        promote_lib.AttentionRecord(bead="SABLE-b", branch="wk-b",
                                    verdict_source=promote_lib.VerdictSource.PRECOMPUTED,
                                    attention_span_seconds=120.0),
    ]
    baseline = promote_lib.compute_attention_baseline(records)
    assert baseline["n"] == 2
    assert baseline["mean_attended_minutes"] == 1.5
    assert baseline["median_attended_minutes"] == 1.5
    assert baseline["total_attended_minutes"] == 3.0


def test_impact_tier_phase_report_ignores_additive_verdict_and_writer_identity_keys(
        tmp_path, monkeypatch):
    """REGRESSION (priority 1): impact_tier_phase_report (the tier journal's
    reader) must be BYTE-IDENTICAL whether or not the 'end' record also
    carries the additive 'verdict'/'writer_identity' keys — existing readers
    ignore additions. Real journal files on disk, not mocked I/O."""
    tree = "a" * 12
    start_line = json.dumps({"schema": 2, "event": "start", "pid": 1, "at": 1000.0, "tree": tree,
                             "waited": 0.0})
    end_record = {"schema": 2, "event": "end", "pid": 1, "at": 1010.0, "tree": tree,
                  "waited": 0.0, "phases": [{"name": "setup", "seconds": 1.0},
                                            {"name": "shell:x.sh", "seconds": 9.0}]}

    without_path = tmp_path / "without.jsonl"
    without_path.write_text(start_line + "\n" + json.dumps(end_record) + "\n")
    monkeypatch.setenv("SABLE_MG_IMPACT_WINDOW_LOG", str(without_path))
    report_without = promote_lib.impact_tier_phase_report(".")

    augmented_end = dict(end_record, verdict=promote_lib.IMPACT_GREEN, writer_identity="gate")
    with_path = tmp_path / "with.jsonl"
    with_path.write_text(start_line + "\n" + json.dumps(augmented_end) + "\n")
    monkeypatch.setenv("SABLE_MG_IMPACT_WINDOW_LOG", str(with_path))
    report_with = promote_lib.impact_tier_phase_report(".")

    assert json.dumps(report_with, sort_keys=True) == json.dumps(report_without, sort_keys=True), (
        "the additive verdict/writer_identity keys on the 'end' record changed the phase "
        "report — existing readers must ignore additions")
    assert report_with["tiers_with_phase_data"] == 1


def test_stamp_impact_verdict_augments_the_end_record_in_place(isolated_lock):
    """The write side of the same regression: _stamp_impact_verdict must fold
    its typed fields onto the EXISTING 'end' line rather than appending a new
    one — the event sequence stays exactly ["start", "end"], which is also
    what bin/test_promote_decision.py's
    test_the_tier_window_log_records_both_edges (outside this bead's
    footprint) hard-asserts on the same file."""
    tree_sha = "b" * 40
    promote_lib._stamp_impact_window(".", "start", tree_sha, 0.0)
    promote_lib._stamp_impact_window(".", "end", tree_sha, 0.0,
                                     phases=[{"name": "setup", "seconds": 2.0}])
    promote_lib._stamp_impact_verdict(".", tree_sha, promote_lib.IMPACT_GREEN)

    log_path = isolated_lock / "windows.jsonl"
    lines = [json.loads(ln) for ln in log_path.read_text().splitlines() if ln.strip()]
    assert [ln["event"] for ln in lines] == ["start", "end"]
    assert lines[1]["verdict"] == promote_lib.IMPACT_GREEN
    assert lines[1]["writer_identity"] == "gate"
    assert lines[1]["phases"] == [{"name": "setup", "seconds": 2.0}]

    report = promote_lib.impact_tier_phase_report(".")
    assert report["tiers_with_phase_data"] == 1
    assert report["legacy_records_excluded"] == 0


def test_stamp_impact_verdict_is_a_noop_when_there_is_no_end_line_to_augment():
    """A caller whose _stamp_impact_window was itself stubbed out (as several
    tests in this module's sibling suite do) leaves no file to augment —
    this must degrade to a silent no-op, never raise."""
    promote_lib._stamp_impact_verdict("/does/not/exist", "c" * 40, promote_lib.IMPACT_GREEN)


# --------------------------------------------------------------------------
# INTEGRATION (SABLE-21rug.1): a REAL git sandbox (a bare origin + a real
# working clone), the real promote() landing path, and the real durable
# attention-record log — read back from the durable artifacts alone, with no
# bd dependency. The only mocked boundary is CI itself (materialize_preview /
# acquire_verdict / delete_ci_ref) — there is no real Actions run to consult
# in a test sandbox; every git fetch/push/resolve and every attention-record
# write/read below is real.
# --------------------------------------------------------------------------

def _real_two_repo_sandbox(tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "trunk", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
    for args in (("config", "user.email", "t@sable.invalid"),
                 ("config", "user.name", "SABLE Test")):
        subprocess.run(["git", "-C", str(work), *args], check=True, capture_output=True)
    (work / "README.md").write_text("trunk\n")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "init"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "trunk"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", "wk-x"], check=True,
                   capture_output=True)
    (work / "feature.txt").write_text("landing\n")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "feature"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "wk-x"], check=True,
                   capture_output=True)
    branch_sha = subprocess.run(["git", "-C", str(work), "rev-parse", "wk-x"], check=True,
                                capture_output=True, text=True).stdout.strip()
    return str(work), str(bare), branch_sha


@pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd/dolt by design; real-bd integration self-skips")
def test_a_real_landing_leaves_a_complete_attention_record_in_the_durable_artifacts(
        tmp_path, monkeypatch):
    """ACCEPTANCE (SABLE-21rug.1): a real landing in the sandbox leaves a
    complete attention record; the baseline function computes over it; the
    existing GREEN evidence path is unaffected."""
    work, bare, branch_sha = _real_two_repo_sandbox(tmp_path)
    monkeypatch.setattr(promote_lib.preview, "materialize_preview",
                        lambda *a, **kw: (branch_sha, "ci-verify/fake-ref", False))
    monkeypatch.setattr(promote_lib.preview, "acquire_verdict",
                        lambda *a, **kw: promote_lib.classify.Verdict(
                            "success", "http://run/1", branch_sha, "ci-verify/fake-ref",
                            source="waited"))
    monkeypatch.setattr(promote_lib.preview, "delete_ci_ref", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "cleanup_after_merge", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "_notify", lambda *a, **kw: None)

    rc = promote_lib.promote("SABLE-test-landing", "wk-x", "trunk", work, "origin", "chuck", None)
    assert rc == 0

    # Read back from the durable artifacts alone — a FRESH call, not the
    # in-memory record promote() built.
    records = promote_lib.read_attention_records(work)
    assert len(records) == 1
    record = records[0]
    assert record.bead == "SABLE-test-landing"
    assert record.branch == "wk-x"
    assert record.verdict_source == promote_lib.VerdictSource.WAITED
    assert record.hand_run_suites == ()
    assert record.red_triage_events == ()
    assert record.attention_span_seconds >= 0.0

    baseline = promote_lib.compute_attention_baseline(records)
    assert baseline["n"] == 1
    assert baseline["total_attended_minutes"] >= 0.0

    # The landing itself really happened — the bare origin's trunk ref (not
    # work's stale local branch, which a plain fetch never updates) now
    # points at the branch's own commit, byte-identical.
    cp = subprocess.run(["git", "-C", bare, "rev-parse", "trunk"], check=True,
                        capture_output=True, text=True)
    assert cp.stdout.strip() == branch_sha, "the landing did not fast-forward trunk"
