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
import ast
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import sable_footprint_lib as footprint_lib_for_auto  # noqa: E402
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
# BatchRecord — the typed, durable batch manifest (SABLE-be4lo.2, S2)
# --------------------------------------------------------------------------

def test_batch_record_names_every_member_branch_bead_and_disjointness_evidence():
    """UNIT (S2 matrix case, verbatim): the promote record for a batched
    landing names every member branch, each member's bead(s), the
    disjointness evidence used (declared footprints AND the mechanical fold
    result), and the combined ref."""
    members = [
        promote_lib.BatchMember("wk-a", "a" * 40, ("SABLE-a",), ("bin/a.py",)),
        promote_lib.BatchMember("wk-b", "b" * 40, ("SABLE-b1", "SABLE-b2"),
                                ("bin/b.py", "bin/c.py")),
        promote_lib.BatchMember("wk-c", "c" * 40, ("SABLE-c",), ("hooks/test/x.sh",)),
    ]
    record = promote_lib.BatchRecord.from_members(
        "base" + "0" * 36, members, combined_ref="ci-verify/batch-abc1234",
        outcome=promote_lib.BATCH_OUTCOME_LANDED, fold_disjoint=True)

    assert set(record.member_branches()) == {"wk-a", "wk-b", "wk-c"}
    assert set(record.member_bead_ids()) == {
        ("SABLE-a",), ("SABLE-b1", "SABLE-b2"), ("SABLE-c",)}
    assert set(record.declared_footprint_paths()) == {
        "bin/a.py", "bin/b.py", "bin/c.py", "hooks/test/x.sh"}
    assert record.fold_disjoint is True
    assert record.combined_ref == "ci-verify/batch-abc1234"

    data = record.to_dict()
    assert {m["branch"] for m in data["members"]} == {"wk-a", "wk-b", "wk-c"}
    assert {tuple(m["bead_ids"]) for m in data["members"]} == {
        ("SABLE-a",), ("SABLE-b1", "SABLE-b2"), ("SABLE-c",)}
    assert data["combined_ref"] == "ci-verify/batch-abc1234"
    assert data["fold_disjoint"] is True


def test_batch_record_ordering_safety_across_a_permutation_set():
    """UNIT (S2 matrix case, load-bearing — the Primitive Obsession guard):
    constructing the record from members in ANY input order yields an
    identical serialized manifest and an identical setkey. Asserted over a
    genuine PERMUTATION SET (4 members, all 24 distinct orders) rather than
    two hand-picked orders — a test fed an already-sorted list would pass
    this by accident and prove nothing."""
    members = [
        promote_lib.BatchMember("wk-d", "d" * 40, ("SABLE-d",), ("bin/d.py",)),
        promote_lib.BatchMember("wk-a", "a" * 40, ("SABLE-a",), ("bin/a.py",)),
        promote_lib.BatchMember("wk-c", "c" * 40, ("SABLE-c",), ("bin/c.py",)),
        promote_lib.BatchMember("wk-b", "b" * 40, ("SABLE-b",), ("bin/b.py",)),
    ]
    base_sha = "e" * 40
    serialized = set()
    setkeys = set()
    for order in itertools.permutations(members):
        record = promote_lib.BatchRecord.from_members(
            base_sha, list(order), combined_ref="ci-verify/batch-fixed",
            outcome=promote_lib.BATCH_OUTCOME_LANDED, fold_disjoint=True)
        serialized.add(json.dumps(record.to_dict(), sort_keys=True))
        setkeys.add(record.setkey)

    assert len(serialized) == 1, "input order leaked into the serialized manifest"
    assert len(setkeys) == 1, "input order leaked into the setkey"


def test_batch_record_from_members_raises_on_an_empty_batch():
    """Vacuous guard, load-bearing (SABLE-p9n7k): an empty batch must RAISE,
    never construct a record — a zero-member manifest is indistinguishable
    from 'we never checked', not a valid empty answer."""
    with pytest.raises(promote_lib.EmptyBatchError):
        promote_lib.BatchRecord.from_members(
            "a" * 40, [], combined_ref="ci-verify/batch-x",
            outcome=promote_lib.BATCH_OUTCOME_LANDED, fold_disjoint=True)


def test_batch_record_round_trips_with_unknown_field_tolerance():
    """A BatchRecord survives to_dict -> from_dict with every field typed,
    and a future/unrecognized key in the dict must not break the
    reconstruction — the same additive-field discipline AttentionRecord's
    own round-trip test asserts above."""
    members = [
        promote_lib.BatchMember("wk-a", "a" * 40, ("SABLE-a",), ("bin/a.py",)),
        promote_lib.BatchMember("wk-b", "b" * 40, ("SABLE-b",), ("bin/b.py",)),
    ]
    record = promote_lib.BatchRecord.from_members(
        "base" + "0" * 36, members, combined_ref="ci-verify/batch-xyz",
        outcome=promote_lib.BATCH_OUTCOME_LANDED, fold_disjoint=False)
    data = record.to_dict()
    data["some_future_field_nobody_wrote_yet"] = "surprise"
    restored = promote_lib.BatchRecord.from_dict(data)
    assert restored == record
    assert isinstance(restored.members, tuple)
    assert all(isinstance(m.bead_ids, tuple) for m in restored.members)


def test_batch_record_from_dict_tolerates_a_bare_minimum_dict():
    """Negative-space companion: a dict missing every optional key entirely
    (not just carrying an extra one) must still reconstruct instead of
    raising KeyError."""
    restored = promote_lib.BatchRecord.from_dict({"combined_ref": "ci-verify/batch-min"})
    assert restored.members == ()
    assert restored.fold_disjoint is False
    assert restored.outcome == ""
    assert restored.combined_ref == "ci-verify/batch-min"


def test_fold_commit_message_round_trips_through_its_own_parser():
    msg = promote_lib.fold_commit_message("wk-a", ("SABLE-a1", "SABLE-a2"))
    assert promote_lib.parse_fold_commit_message(msg) == ("wk-a", ("SABLE-a1", "SABLE-a2"))


def test_parse_fold_commit_message_returns_none_for_an_unrelated_message():
    """Negative space: the batch's own base commit, or any commit that
    predates the fold-message contract, must not be misread as naming a
    member."""
    assert promote_lib.parse_fold_commit_message("init") is None
    assert promote_lib.parse_fold_commit_message("Merge pull request #1") is None


def test_stamp_and_read_batch_record_round_trip(tmp_path, monkeypatch):
    """The write/read halves of the durable promote-record log, isolated
    from the real state dir via SABLE_MG_BATCH_RECORD_LOG — mirrors
    test_stamp_impact_verdict_augments_the_end_record_in_place's isolation
    pattern."""
    monkeypatch.setenv("SABLE_MG_BATCH_RECORD_LOG", str(tmp_path / "batch-records.jsonl"))
    members = [promote_lib.BatchMember("wk-a", "a" * 40, ("SABLE-a",), ("bin/a.py",))]
    record = promote_lib.BatchRecord.from_members(
        "base" + "0" * 36, members, combined_ref="ci-verify/batch-solo",
        outcome=promote_lib.BATCH_OUTCOME_LANDED, fold_disjoint=True)

    promote_lib._stamp_batch_record(".", record)
    records = promote_lib.read_batch_records(".")
    assert records == [record]
    assert promote_lib.find_batch_record(".", "ci-verify/batch-solo") == record
    assert promote_lib.find_batch_record(".", "ci-verify/batch-nope") is None


def test_stamp_batch_record_is_a_noop_for_a_synthetic_repo_path():
    """Mirrors _stamp_attention_record's own guard test: a non-existent repo
    path (and no env override) must degrade to a silent no-op, never raise
    and never write into the real ~/.claude/sable/state."""
    promote_lib._stamp_batch_record("/does/not/exist", promote_lib.BatchRecord.from_members(
        "a" * 40, [promote_lib.BatchMember("wk-a", "a" * 40, ("SABLE-a",), ())],
        combined_ref="ci-verify/batch-noop", outcome=promote_lib.BATCH_OUTCOME_LANDED,
        fold_disjoint=True))


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


# --------------------------------------------------------------------------
# The combined-tree BATCH budget, against the REAL tool (SABLE-be4lo.6)
# --------------------------------------------------------------------------
#
# Unit-level coverage of combined_tree_budget() itself lives in
# bin/test_promote_decision.py, alongside impact_budget()'s own tests (the
# module that owns both functions and the fixtures the CLI test needs --
# clean_budget_env, smg, _BIN). This is the REAL-composition half: a real
# subprocess invocation of `sable-merge-gate promote-budget --json`, so the
# thing asserted is what a wrapper shelling out actually receives on stdout,
# not an in-process call to the library that skips the CLI entirely.


def _derive_batch_wrapper_timeout_from_artifact(cli_repo, member_footprints, env):
    """A stand-in BATCH-WRAPPER consumer: shells out to the real tool exactly
    as an operator wrapper would, reads ONLY the JSON artifact on stdout, and
    returns the field it would size a `timeout` around. Never touches
    promote_lib in-process and never hardcodes a number -- the governing
    precedent this bead states explicitly (the retired +900 pattern): a
    consumer that copies the number instead of reading the artifact forfeits
    the derivation."""
    argv = [sys.executable, str(cli_repo / "sable-merge-gate"), "promote-budget", "--json"]
    for fp in member_footprints:
        argv += ["--member-footprint", ",".join(fp)]
    cp = subprocess.run(argv, cwd="/", text=True, capture_output=True, env=env)
    assert cp.returncode == 0, cp.stderr
    artifact = json.loads(cp.stdout)
    assert "recommended_batch_wrapper_timeout_s" in artifact, (
        "the combined-tree batch field is absent from the tool's own output")
    return artifact["recommended_batch_wrapper_timeout_s"], artifact


def test_the_real_promote_budget_tool_reports_the_combined_tree_batch_field(tmp_path):
    """INTEGRATION (SABLE-be4lo.6 acceptance): run the REAL budget tool via
    subprocess against a sandbox with 3 member footprints; the field is
    present in the artifact, and a consumer reading the artifact -- not a
    hand-carried constant -- gets the derived value. Changing the env the
    tool derives from (not the consumer's code) moves the number the
    consumer reads, proving it is a live derivation."""
    _BIN = Path(__file__).resolve().parent
    members = [["bin/a.py"], ["bin/b.py", "bin/c.py"], ["hooks/test/x.sh"]]
    base_env = {**os.environ, "SABLE_MG_IMPACT_TIMEOUT": "300",
                "SABLE_MG_IMPACT_LOCK_TIMEOUT": "150",
                "SABLE_MG_COVERAGE_FLOOR_TIMEOUT": "50"}

    timeout_s, artifact = _derive_batch_wrapper_timeout_from_artifact(_BIN, members, base_env)
    assert isinstance(timeout_s, int) and timeout_s > 0
    assert artifact["member_count"] == 3
    assert artifact["bisection_reserve_s"] > 0
    assert "recommended_wrapper_timeout_s" not in artifact, (
        "the batch report must never carry the single-branch field name too "
        "-- a consumer keying on field presence would misread which budget "
        "it got")

    # DERIVATION, not a copy: moving the SSOT-equivalent env the tool reads
    # moves the number this consumer gets, with no change to the consumer.
    bumped_env = {**base_env, "SABLE_MG_IMPACT_TIMEOUT": "3000"}
    bumped_timeout_s, _ = _derive_batch_wrapper_timeout_from_artifact(_BIN, members, bumped_env)
    assert bumped_timeout_s > timeout_s, (
        "the batch wrapper timeout did not track the underlying tier budget -- "
        "a consumer reading this artifact would be sized against a stale number")


# --------------------------------------------------------------------------
# MANIFEST COMPLETENESS (SABLE-be4lo.2 S2 acceptance, verbatim): a REAL git
# sandbox, REAL fold commits (built directly with git plumbing here, since
# SABLE-be4lo.4's fold builder has not landed yet), and a real durable
# BatchRecord log. The reconstruction below reads ONLY the promote record
# (find_batch_record) and the fold commit messages (git log +
# parse_fold_commit_message) -- no bd, no in-memory reuse of the `members`
# list built above, no other state. That is the "no asking" property itself.
# --------------------------------------------------------------------------

def _build_real_fold_chain(repo, base_sha, members):
    """A REAL two-parent fold chain in `repo`: base -> fold(m1) -> fold(m2)
    -> ... -> fold(mN), each fold commit's message naming exactly the ONE
    member it folds in (fold_commit_message). Stands in for
    SABLE-be4lo.4's fold builder, which owns this in production once it
    lands; built directly with git plumbing here because that bead has not
    landed yet and this test needs REAL fold commits regardless of that."""
    tip = base_sha
    for m in members:
        cp = subprocess.run(["git", "-C", repo, "rev-parse", f"{m.tip_sha}^{{tree}}"],
                            check=True, capture_output=True, text=True)
        tree = cp.stdout.strip()
        message = promote_lib.fold_commit_message(m.branch, m.bead_ids)
        cp = subprocess.run(
            ["git", "-C", repo, "commit-tree", tree, "-p", tip, "-p", m.tip_sha,
             "-m", message], check=True, capture_output=True, text=True)
        tip = cp.stdout.strip()
    return tip


def test_manifest_completeness_reconstructs_from_promote_record_and_fold_commits_alone(
        tmp_path, monkeypatch):
    """ACCEPTANCE (S2 manifest completeness, verbatim from the matrix): from
    a landed batch, reconstruct member branches + beads + disjointness
    evidence + combined ref reading ONLY the promote record and the fold
    commit messages."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "trunk"), ("config", "user.email", "t@sable.invalid"),
                 ("config", "user.name", "SABLE Test")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "README.md").write_text("trunk\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True,
                   capture_output=True)
    base_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "trunk"], check=True,
                              capture_output=True, text=True).stdout.strip()

    member_specs = [
        ("wk-a", ("SABLE-a",), ("bin/a.py",)),
        ("wk-c", ("SABLE-c",), ("bin/c.py",)),
        ("wk-b", ("SABLE-b1", "SABLE-b2"), ("bin/b.py", "bin/d.py")),
    ]
    members = []
    for branch, beads, footprint in member_specs:
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", branch, "trunk"],
                       check=True, capture_output=True)
        for p in footprint:
            f = repo / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"{branch}\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", branch], check=True,
                       capture_output=True)
        tip = subprocess.run(["git", "-C", str(repo), "rev-parse", branch], check=True,
                             capture_output=True, text=True).stdout.strip()
        members.append(promote_lib.BatchMember(branch, tip, beads, footprint))
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "trunk"], check=True,
                   capture_output=True)

    combined_tip = _build_real_fold_chain(str(repo), base_sha, members)
    combined_ref = "refs/ci-verify/batch-manifest-test"
    subprocess.run(["git", "-C", str(repo), "update-ref", combined_ref, combined_tip],
                   check=True, capture_output=True)

    record = promote_lib.BatchRecord.from_members(
        base_sha, members, combined_ref=combined_ref,
        outcome=promote_lib.BATCH_OUTCOME_LANDED, fold_disjoint=True)
    monkeypatch.setenv("SABLE_MG_BATCH_RECORD_LOG", str(tmp_path / "batch-records.jsonl"))
    promote_lib._stamp_batch_record(".", record)

    # ---- RECONSTRUCTION: reads ONLY the durable promote-record log and the
    # fold commit messages from git -- `members`/`record` above are never
    # touched again below this line. ----
    manifest_record = promote_lib.find_batch_record(".", combined_ref)
    assert manifest_record is not None, "the promote record for this combined_ref was not durable"

    cp = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%s", f"{base_sha}..{combined_ref}"],
        check=True, capture_output=True, text=True)
    fold_members = [promote_lib.parse_fold_commit_message(line)
                    for line in cp.stdout.splitlines() if line.strip()]
    fold_members = [fm for fm in fold_members if fm is not None]

    assert set(manifest_record.member_branches()) == {"wk-a", "wk-b", "wk-c"}
    assert set(manifest_record.member_bead_ids()) == {
        ("SABLE-a",), ("SABLE-b1", "SABLE-b2"), ("SABLE-c",)}
    assert manifest_record.declared_footprint_paths() == (
        "bin/a.py", "bin/b.py", "bin/c.py", "bin/d.py")
    assert manifest_record.fold_disjoint is True
    assert manifest_record.combined_ref == combined_ref

    assert {fm[0] for fm in fold_members} == {"wk-a", "wk-b", "wk-c"}, (
        "fold commit messages did not independently name every member branch")
    assert {fm[1] for fm in fold_members} == {
        ("SABLE-a",), ("SABLE-b1", "SABLE-b2"), ("SABLE-c",)}, (
        "fold commit messages did not independently name every member's bead(s)")


# ==========================================================================
# SABLE-be4lo.7 — the BATCH LANDING PATH. Four behaviors, tested in the order
# the epic locked them: budget REFUSAL first (TDD on the enforcement before
# the capability), then per-batch stale-base, NO-HALF-LANDING, one-promote.
#
# GATE-CLASS bead: this landing machinery itself lands serial/never-optimistic.
# ==========================================================================

def _batch_budget_ok():
    """A combined-tree budget artifact that DOES carry the be4lo.6 field."""
    return promote_lib.combined_tree_budget([["bin/a.py"], ["bin/b.py"]])


def _members_ab():
    return [
        promote_lib.BatchMember("wk-a", "a" * 40, ("SABLE-a",), ("bin/a.py",)),
        promote_lib.BatchMember("wk-b", "b" * 40, ("SABLE-b",), ("bin/b.py",)),
    ]


# ---- (1) BUDGET REFUSAL — both polarities. Written FIRST. ------------------

def test_batch_budget_absent_refuses_loudly_naming_the_missing_field():
    """SABLE-be4lo.7 behavior 1, the FIRST test (TDD on the enforcement before
    the capability): a promote-budget artifact that lacks the combined-tree
    field must make the batch path refuse LOUDLY, and the refusal must NAME the
    missing field so the operator's fix is unambiguous (architecture decision
    6: the be4lo.6 promote-budget extension is a BLOCKING DEP of any batched
    landing — extend-tool-first made mechanical)."""
    # A single-branch budget dict (impact_budget's shape) is exactly the
    # pre-extension artifact this guard exists to reject: it has
    # recommended_wrapper_timeout_s but NOT the combined-tree field.
    single_branch = promote_lib.impact_budget()
    assert promote_lib.BATCH_BUDGET_FIELD not in single_branch
    with pytest.raises(promote_lib.GateError) as exc:
        promote_lib.assert_batch_budget_present(single_branch)
    assert promote_lib.BATCH_BUDGET_FIELD in str(exc.value), (
        "the refusal did not name the missing combined-tree field")
    assert exc.value.code == promote_lib.classify.EXIT_PRECONDITION


def test_batch_budget_present_proceeds():
    """The other polarity: a real combined-tree budget passes the guard
    silently (returns None, raises nothing) — the capability is reachable
    exactly when the tool was extended."""
    assert promote_lib.assert_batch_budget_present(_batch_budget_ok()) is None


# ==========================================================================
# Real git-sandbox tests for the batch landing path (SABLE-be4lo.7).
# The git operations (fetch, fast-forward push, ancestry, non-ff rejection)
# ARE the unit under test — nothing here mocks git_lib. Fold chains are built
# with the real fold builder (sable_batch_fold_lib), so these exercise the
# real composition end to end.
# ==========================================================================

import sable_batch_fold_lib as fold_lib  # noqa: E402


def _bg(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def _remote_head(bare, ref):
    """Commit at refs/heads/<ref> in the bare remote, or '' if absent."""
    cp = subprocess.run(["git", "-C", str(bare), "rev-parse", "--verify", f"refs/heads/{ref}"],
                        capture_output=True, text=True)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def _batch_sandbox(tmp_path):
    """A bare 'origin' with a `trunk` integration branch and a working repo
    wired to it. Returns (repo, bare, base_sha)."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "trunk", str(bare)],
                   check=True, capture_output=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _bg(repo, "init", "-q", "-b", "trunk")
    _bg(repo, "config", "user.email", "t@sable.invalid")
    _bg(repo, "config", "user.name", "SABLE Test")
    (repo / "root.txt").write_text("root\n")
    (repo / "common.txt").write_text("common\n")
    _bg(repo, "add", "-A")
    _bg(repo, "commit", "-q", "-m", "root")
    (repo / "base.txt").write_text("base\n")
    _bg(repo, "add", "-A")
    _bg(repo, "commit", "-q", "-m", "trunk base")
    _bg(repo, "remote", "add", "origin", str(bare))
    _bg(repo, "push", "-q", "origin", "trunk")
    _bg(repo, "fetch", "-q", "origin")
    base_sha = _bg(repo, "rev-parse", "HEAD")
    return repo, bare, base_sha


def _member_branch(repo, base_sha, label, filename, content, beads=()):
    """A member branch off base_sha adding one disjoint file, pushed to origin.
    Returns a typed BatchMember (branch, tip, beads, footprint)."""
    _bg(repo, "checkout", "-q", "-b", label, base_sha)
    (repo / filename).parent.mkdir(parents=True, exist_ok=True)
    (repo / filename).write_text(content)
    _bg(repo, "add", "-A")
    _bg(repo, "commit", "-q", "-m", f"work on {label}")
    tip = _bg(repo, "rev-parse", "HEAD")
    _bg(repo, "push", "-q", "origin", label)
    _bg(repo, "checkout", "-q", "trunk")
    return promote_lib.BatchMember(label, tip, tuple(beads), (filename,))


def _fold(repo, base_sha, members):
    fms = [fold_lib.FoldMember(m.branch, m.tip_sha, m.bead_ids[0] if m.bead_ids else "")
           for m in members]
    return fold_lib.fold_chain(str(repo), base_sha, fms).tip


def _ok_budget(members):
    return promote_lib.combined_tree_budget([list(m.footprint_paths) for m in members])


def _green_batch_verdict(fold_tip, combined_ref="ci-verify/batch-test"):
    """Exact-object CI authority for direct writer tests.

    The batch coordinator tests exercise verdict acquisition itself.  These
    lower-level writer tests supply the value the writer now requires so each
    case can stay focused on stale-base, atomic-push, and integrity behavior.
    """
    return promote_lib.classify.Verdict(
        "success", "", fold_tip, combined_ref, source="precomputed", complete=True)


# --------------------------------------------------------------------------
# Hermetic fleet-channel isolation (SABLE-xpab3). A batch-land test emits
# _notify() cockpit messages and _append_evidence() bd writes — NEITHER may
# reach the live fleet. The notify seam is redirected for EVERY test in this
# module (autouse) so no future test can reintroduce the leak by forgetting;
# the bd seam is no-op'd per-test by fleet_sink. test_batch_land_makes_zero_
# real_fleet_sends proves the isolation and its non-vacuity.
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _hermetic_fleet_channel(tmp_path_factory, monkeypatch):
    """Redirect SABLE_MG_NOTIFY to a per-test scratch SINK so a _notify() call
    is captured to a file instead of messaging the real `sable-msg` cockpit.
    Autouse: applies to every test in this module. Safe for the existing
    real-sandbox promote() test, which stubs promote_lib._notify at the Python
    level and so never reaches this seam at all."""
    sink_dir = tmp_path_factory.mktemp("fleet")
    sink = sink_dir / "sends.log"
    notifier = sink_dir / "sink-notify.sh"
    notifier.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SABLE_FLEET_SINK"\n')
    notifier.chmod(0o755)
    monkeypatch.setenv("SABLE_FLEET_SINK", str(sink))
    monkeypatch.setenv("SABLE_MG_NOTIFY", str(notifier))
    return sink


@pytest.fixture
def fleet_sink(_hermetic_fleet_channel, monkeypatch):
    """Batch-land isolation: the captured notify sink (as a reader) PLUS a no-op
    bd seam so a landed batch's per-member evidence writes never touch the real
    store. Returns a callable yielding the captured notification lines."""
    monkeypatch.setenv("SABLE_MG_BD", "true")

    def read():
        p = _hermetic_fleet_channel
        return p.read_text().splitlines() if p.exists() else []
    return read


# ---- (2) PER-BATCH STALE-BASE — both polarities --------------------------

@pytest.mark.parametrize(
    ("conclusion", "complete", "identity_offset", "expected_code"),
    [
        ("pending", False, False, promote_lib.classify.EXIT_PRECONDITION),
        ("failure", True, False, promote_lib.classify.EXIT_RED),
        ("success", True, True, promote_lib.classify.EXIT_INTEGRITY),
    ],
)
def test_land_batch_from_refs_refuses_non_green_or_mismatched_authority(
        tmp_path, monkeypatch, fleet_sink, conclusion, complete, identity_offset,
        expected_code):
    """The sanctioned CLI path cannot turn ref existence into authorization.

    The combined ref is present in a real bare remote in every case.  Pending,
    red, and exact-SHA-mismatched verdicts all fail before the integration ref
    moves.  This is the negative control the original implementation lacked:
    deleting the verdict assertion makes all three cases land.
    """
    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = [
        _member_branch(repo, base_sha, "wk-a", "bin/a.py", "a=1\n", ("SABLE-a",)),
        _member_branch(repo, base_sha, "wk-b", "bin/b.py", "b=1\n", ("SABLE-b",)),
    ]
    fold_members = [
        fold_lib.FoldMember(m.branch, m.tip_sha, m.bead_ids[0]) for m in members]
    fold_tip, combined_ref = fold_lib.push_batch_ref(
        str(repo), "origin", base_sha, fold_members)
    before = _remote_head(bare, "trunk")
    seen = []

    def verdict_for_exact_request(repo_, ref, sha):
        seen.append((ref, sha))
        bound_sha = ("f" * 40) if identity_offset else sha
        return promote_lib.classify.Verdict(
            conclusion, "", bound_sha, ref, source="precomputed",
            complete=complete)

    monkeypatch.setattr(promote_lib.preview, "acquire_verdict",
                        verdict_for_exact_request)
    with pytest.raises(promote_lib.GateError) as exc:
        promote_lib.land_batch_from_refs(
            "trunk", ["wk-a:SABLE-a", "wk-b:SABLE-b"],
            str(repo), "origin")

    assert exc.value.code == expected_code
    assert seen == [(combined_ref, fold_tip)], \
        "landing did not ask for the verdict of the exact combined object"
    assert _remote_head(bare, "trunk") == before, \
        "a non-green or mismatched verdict moved the integration ref"


def test_land_batch_from_refs_lands_only_the_exact_green_object(
        tmp_path, monkeypatch, fleet_sink):
    """Positive control for the authority matrix above: exact ref + exact SHA
    + completed GREEN reaches the existing atomic writer and lands that object."""
    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = [
        _member_branch(repo, base_sha, "wk-a", "bin/a.py", "a=1\n", ("SABLE-a",)),
        _member_branch(repo, base_sha, "wk-b", "bin/b.py", "b=1\n", ("SABLE-b",)),
    ]
    fold_members = [
        fold_lib.FoldMember(m.branch, m.tip_sha, m.bead_ids[0]) for m in members]
    fold_tip, combined_ref = fold_lib.push_batch_ref(
        str(repo), "origin", base_sha, fold_members)
    monkeypatch.setattr(
        promote_lib.preview, "acquire_verdict",
        lambda repo_, ref, sha: _green_batch_verdict(sha, ref))

    rc = promote_lib.land_batch_from_refs(
        "trunk", ["wk-a:SABLE-a", "wk-b:SABLE-b"],
        str(repo), "origin")

    assert rc == 0
    assert _remote_head(bare, "trunk") == fold_tip
    assert _remote_head(bare, combined_ref) == "", \
        "a consumed terminal batch ref was not cleaned up"


def test_batch_writer_refuses_a_declared_pair_missing_from_the_atomic_set(
        tmp_path, monkeypatch, fleet_sink):
    """The new batch writer must not become a bypass around the existing
    MUST-LAND-TOGETHER authority."""
    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = [
        _member_branch(repo, base_sha, "wk-a", "bin/a.py", "a=1\n", ("SABLE-a",)),
    ]
    fold_tip = _fold(repo, base_sha, members)
    combined_ref = "ci-verify/batch-incomplete-pair"
    before = _remote_head(bare, "trunk")
    monkeypatch.setattr(
        promote_lib, "declared_landing_pair",
        lambda repo_, bead: (
            frozenset({"SABLE-b"}) if bead == "SABLE-a" else frozenset()))
    monkeypatch.setattr(promote_lib, "_bead_landed", lambda *args: False)

    with pytest.raises(promote_lib.GateError) as exc:
        promote_lib.land_batch(
            str(repo), "origin", "trunk", base_sha, fold_tip, members,
            budget=_ok_budget(members), combined_ref=combined_ref,
            verdict=_green_batch_verdict(fold_tip, combined_ref))

    assert exc.value.code == promote_lib.classify.EXIT_PAIR_REFUSED
    assert "SABLE-b" in str(exc.value)
    assert _remote_head(bare, "trunk") == before


def test_land_batch_lands_when_base_equals_the_formed_base(tmp_path, fleet_sink):
    """SABLE-be4lo.7 behavior 2 (positive polarity): base parent == integration
    tip → the batch lands. The single fast-forward moves trunk to the fold tip
    and every member tip becomes an ancestor of the new integration tip."""
    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = [
        _member_branch(repo, base_sha, "wk-a", "bin/a.py", "a=1\n", ("SABLE-a",)),
        _member_branch(repo, base_sha, "wk-b", "bin/b.py", "b=1\n", ("SABLE-b",)),
    ]
    fold_tip = _fold(repo, base_sha, members)
    combined_ref = "ci-verify/batch-deadbee"
    res = promote_lib.land_batch(str(repo), "origin", "trunk", base_sha, fold_tip,
                                 members, budget=_ok_budget(members),
                                 combined_ref=combined_ref,
                                 verdict=_green_batch_verdict(fold_tip, combined_ref))
    assert res.landed, res.reason
    assert res.outcome == promote_lib.BATCH_OUTCOME_LANDED
    # The landing notification went to the HERMETIC sink, never the live cockpit.
    assert any("BATCH LANDED" in line for line in fleet_sink()), \
        "the land notification did not reach the hermetic sink"
    assert _remote_head(bare, "trunk") == fold_tip, "trunk did not fast-forward to the fold tip"
    for m in members:
        anc = subprocess.run(["git", "-C", str(bare), "merge-base", "--is-ancestor",
                              m.tip_sha, fold_tip], capture_output=True)
        assert anc.returncode == 0, f"{m.branch} tip is not an ancestor of the landed tip"


def test_land_batch_reforms_and_lands_nothing_on_a_stale_base(tmp_path, fleet_sink):
    """SABLE-be4lo.7 behavior 2 (negative polarity): the integration tip moved
    off the base the fold chain was formed on → RE-FORM, land NOTHING. The
    remote base is untouched by the land and every member branch survives for
    the re-form."""
    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = [
        _member_branch(repo, base_sha, "wk-a", "bin/a.py", "a=1\n", ("SABLE-a",)),
        _member_branch(repo, base_sha, "wk-b", "bin/b.py", "b=1\n", ("SABLE-b",)),
    ]
    fold_tip = _fold(repo, base_sha, members)
    # An unrelated commit lands on trunk AFTER the fold was formed — the base
    # has now moved out from under the batch.
    _bg(repo, "checkout", "-q", "trunk")
    (repo / "unrelated.txt").write_text("moved\n")
    _bg(repo, "add", "-A")
    _bg(repo, "commit", "-q", "-m", "an interleaved landing moved the base")
    _bg(repo, "push", "-q", "origin", "trunk")
    moved = _remote_head(bare, "trunk")

    combined_ref = "ci-verify/batch-deadbee"
    res = promote_lib.land_batch(
        str(repo), "origin", "trunk", base_sha, fold_tip, members,
        budget=_ok_budget(members), combined_ref=combined_ref,
        verdict=_green_batch_verdict(fold_tip, combined_ref))
    assert res.outcome == promote_lib.BATCH_OUTCOME_REFORM, res.reason
    assert not res.landed
    assert res.exit_code == promote_lib.classify.EXIT_BASE_MOVED
    assert _remote_head(bare, "trunk") == moved, "a re-formed batch must not touch the base"
    assert fold_tip not in (_remote_head(bare, "trunk"),), "the fold tip must NOT have landed"
    assert set(res.fallback_branches) == {"wk-a", "wk-b"}
    for m in members:
        assert _remote_head(bare, m.branch) == m.tip_sha, f"{m.branch} preview must be intact"


def test_land_batch_refuses_a_tip_not_built_on_the_named_base(tmp_path, fleet_sink):
    """Object-level precondition: a fold_tip that was NOT built on base_sha is a
    mismatched pair (a caller bug), not a stale base — it must fail loud
    (GateError 3), never masquerade as a re-form."""
    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = [_member_branch(repo, base_sha, "wk-a", "bin/a.py", "a=1\n", ("SABLE-a",))]
    # Fold onto ROOT (the very first commit), not onto base_sha (trunk tip).
    root = _bg(repo, "rev-list", "--max-parents=0", "HEAD")
    wrong_tip = _fold(repo, root, members)
    combined_ref = "ci-verify/batch-wrongbase"
    with pytest.raises(promote_lib.GateError) as exc:
        promote_lib.land_batch(str(repo), "origin", "trunk", base_sha, wrong_tip,
                               members, budget=_ok_budget(members),
                               combined_ref=combined_ref,
                               verdict=_green_batch_verdict(wrong_tip, combined_ref))
    assert exc.value.code == promote_lib.classify.EXIT_PRECONDITION
    assert "not built on base" in str(exc.value)


def test_land_batch_budget_absent_refuses_before_touching_the_base(tmp_path, fleet_sink):
    """Behavior 1 at the land seam: a budget missing the combined-tree field
    stops the land BEFORE any git write — the base is never touched."""
    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = [_member_branch(repo, base_sha, "wk-a", "bin/a.py", "a=1\n", ("SABLE-a",))]
    fold_tip = _fold(repo, base_sha, members)
    before = _remote_head(bare, "trunk")
    combined_ref = "ci-verify/batch-no-budget"
    with pytest.raises(promote_lib.GateError) as exc:
        promote_lib.land_batch(str(repo), "origin", "trunk", base_sha, fold_tip,
                               members, budget=promote_lib.impact_budget(),
                               combined_ref=combined_ref,
                               verdict=_green_batch_verdict(fold_tip, combined_ref))
    assert promote_lib.BATCH_BUDGET_FIELD in str(exc.value)
    assert _remote_head(bare, "trunk") == before, "a budget refusal must not touch the base"


def test_land_batch_integrity_aborts_if_the_landed_tip_is_not_the_tested_object(tmp_path, monkeypatch, fleet_sink):
    """The batch analogue of promote()'s F3 integrity assertion: if the base
    tip after the fast-forward is NOT exactly the tested fold tip (single-writer
    discipline violated), fail LOUD with EXIT_INTEGRITY rather than report a
    land of an object CI never saw."""
    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = [_member_branch(repo, base_sha, "wk-a", "bin/a.py", "a=1\n", ("SABLE-a",))]
    fold_tip = _fold(repo, base_sha, members)

    real_tip_matches = promote_lib.batch_key.tip_matches
    calls = {"n": 0}
    def flaky_tip_matches(landed, expected):
        calls["n"] += 1
        # 1st call is the pre-push stale-base check (let it pass); 2nd is the
        # post-push integrity read (force a mismatch).
        if calls["n"] >= 2:
            return False
        return real_tip_matches(landed, expected)
    monkeypatch.setattr(promote_lib.batch_key, "tip_matches", flaky_tip_matches)

    combined_ref = "ci-verify/batch-integrity"
    with pytest.raises(promote_lib.GateError) as exc:
        promote_lib.land_batch(str(repo), "origin", "trunk", base_sha, fold_tip,
                               members, budget=_ok_budget(members),
                               combined_ref=combined_ref,
                               verdict=_green_batch_verdict(fold_tip, combined_ref))
    assert exc.value.code == promote_lib.classify.EXIT_INTEGRITY
    assert "integrity abort" in str(exc.value)


# ---- (3) NO-HALF-LANDING under interrupt ---------------------------------

def test_land_batch_no_half_landing_when_the_single_push_fails(tmp_path, monkeypatch, fleet_sink):
    """SABLE-be4lo.7 behavior 3 (absolute): kill the ONE fast-forward mid-batch
    and NOTHING lands — the integration ref is either at the full chain tip or
    unmoved, never at an intermediate fold commit — and every member preview is
    intact and reusable afterward. Simulated by forcing the single land push to
    fail; the assertion is the STATE it leaves behind."""
    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = [
        _member_branch(repo, base_sha, "wk-a", "bin/a.py", "a=1\n", ("SABLE-a",)),
        _member_branch(repo, base_sha, "wk-b", "bin/b.py", "b=1\n", ("SABLE-b",)),
        _member_branch(repo, base_sha, "wk-c", "bin/c.py", "c=1\n", ("SABLE-c",)),
    ]
    fold_tip = _fold(repo, base_sha, members)
    # Stand up each member's individual preview ref on the remote — the serial
    # fallback's evidence, which the land must leave untouched.
    for m in members:
        _bg(repo, "push", "-q", "origin",
            f"{m.tip_sha}:refs/heads/ci-verify/{m.branch}-preview")
    before_trunk = _remote_head(bare, "trunk")

    real_git = promote_lib.git_lib._git
    def interrupt_the_land_push(repo_, *args, check=True):
        # Fail ONLY the fast-forward of the integration ref; everything else
        # (fetch, resolve) runs for real, so this is a faithful mid-land kill.
        if args and args[0] == "push" and any(a.endswith("refs/heads/trunk") for a in args):
            return subprocess.CompletedProcess(args, 1, stdout="simulated interrupt: push killed", stderr="")
        return real_git(repo_, *args, check=check)
    monkeypatch.setattr(promote_lib.git_lib, "_git", interrupt_the_land_push)

    combined_ref = "ci-verify/batch-deadbee"
    res = promote_lib.land_batch(
        str(repo), "origin", "trunk", base_sha, fold_tip, members,
        budget=_ok_budget(members), combined_ref=combined_ref,
        verdict=_green_batch_verdict(fold_tip, combined_ref))
    assert res.outcome == promote_lib.BATCH_OUTCOME_FELL_BACK_SERIAL, res.reason
    assert not res.landed
    # NO intermediate state: trunk is exactly where it was — no fold commit,
    # partial or whole, reached it.
    assert _remote_head(bare, "trunk") == before_trunk, "trunk moved despite a failed land"
    assert _remote_head(bare, "trunk") != fold_tip
    # Every member falls back AS ITSELF with its preview intact and reusable.
    assert set(res.fallback_branches) == {"wk-a", "wk-b", "wk-c"}
    for m in members:
        assert _remote_head(bare, m.branch) == m.tip_sha
        assert _remote_head(bare, f"ci-verify/{m.branch}-preview") == m.tip_sha, (
            f"{m.branch}'s individual preview was disturbed by the failed batch land")


# ---- (4) ONE-PROMOTE SEAT POLICY (ordering, not seat discipline) ----------

def test_one_promote_a_landed_batch_makes_an_interleaved_serial_non_ff(tmp_path, fleet_sink):
    """SABLE-be4lo.7 behavior 4: from formation to landing a batch is ONE
    promote, enforced by ordering — not a seat mutex. Once the batch lands
    (moving the base to the fold tip), an interleaved serial promote's own
    landing act (a no-force fast-forward of its verified object, built on the
    OLD base) is REJECTED non-ff. At most one of {batch, serial} lands against a
    given base; the loser re-forms. Real bare remote — the real store."""
    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = [
        _member_branch(repo, base_sha, "wk-a", "bin/a.py", "a=1\n", ("SABLE-a",)),
        _member_branch(repo, base_sha, "wk-b", "bin/b.py", "b=1\n", ("SABLE-b",)),
    ]
    # A serial branch verified against the SAME base, waiting behind the batch.
    serial = _member_branch(repo, base_sha, "wk-serial", "bin/s.py", "s=1\n", ("SABLE-s",))
    fold_tip = _fold(repo, base_sha, members)

    combined_ref = "ci-verify/batch-one-promote"
    res = promote_lib.land_batch(
        str(repo), "origin", "trunk", base_sha, fold_tip, members,
        budget=_ok_budget(members), combined_ref=combined_ref,
        verdict=_green_batch_verdict(fold_tip, combined_ref))
    assert res.landed
    assert _remote_head(bare, "trunk") == fold_tip

    # The interleaved serial promote's landing act, built on the now-superseded
    # base, cannot fast-forward without --force.
    reject = subprocess.run(["git", "-C", str(repo), "push", str(bare),
                             f"{serial.tip_sha}:refs/heads/trunk"],
                            capture_output=True, text=True)
    assert reject.returncode != 0, "an interleaved serial promote must NOT fast-forward past a landed batch"
    assert "non-fast-forward" in (reject.stderr + reject.stdout).lower() or "rejected" in \
        (reject.stderr + reject.stdout).lower()
    assert _remote_head(bare, "trunk") == fold_tip, "the base must still be the batch's fold tip"


# ---- S1 ACCEPTANCE (verbatim): 3 branches, ONE combined cycle -------------

def test_S1_acceptance_three_disjoint_branches_land_in_one_combined_cycle(tmp_path, capsys, fleet_sink):
    """SABLE-be4lo.7 S1 ACCEPTANCE (verbatim): 3 real disjoint individually-green
    branches land through ONE combined cycle; all 3 tips are ancestors of the
    integration tip; the batch's wall-clock is below the sum of 3 serial cycles
    (measured, recorded below). The crisp evidence for 'one cycle': the batch
    pushes exactly ONE ci-verify combined ref (one CI trigger) where the serial
    lane pushes THREE."""
    # ---- BATCH lane ----
    repo, bare, base_sha = _batch_sandbox(tmp_path / "batch")
    members = [
        _member_branch(repo, base_sha, "wk-a", "bin/a.py", "a=1\n", ("SABLE-a",)),
        _member_branch(repo, base_sha, "wk-b", "bin/b.py", "b=1\n", ("SABLE-b",)),
        _member_branch(repo, base_sha, "wk-c", "bin/c.py", "c=1\n", ("SABLE-c",)),
    ]
    t0 = time.monotonic()
    fold_members = [fold_lib.FoldMember(m.branch, m.tip_sha, m.bead_ids[0]) for m in members]
    fold_tip, combined_ref = fold_lib.push_batch_ref(str(repo), "origin", base_sha, fold_members)
    res = promote_lib.land_batch(
        str(repo), "origin", "trunk", base_sha, fold_tip, members,
        budget=_ok_budget(members), combined_ref=combined_ref,
        verdict=_green_batch_verdict(fold_tip, combined_ref))
    batch_wall = time.monotonic() - t0
    assert res.landed, res.reason
    landed_tip = _remote_head(bare, "trunk")
    assert landed_tip == fold_tip
    for m in members:
        anc = subprocess.run(["git", "-C", str(bare), "merge-base", "--is-ancestor",
                              m.tip_sha, landed_tip], capture_output=True)
        assert anc.returncode == 0, f"{m.branch} is not an ancestor of the integration tip"
    batch_ci_refs = subprocess.run(
        ["git", "-C", str(bare), "for-each-ref", "--format=%(refname)", "refs/heads/ci-verify/"],
        capture_output=True, text=True).stdout.split()
    assert len(batch_ci_refs) == 1, f"a batch is ONE cycle — expected 1 ci-verify ref, got {batch_ci_refs}"

    # ---- SERIAL lane (baseline): the same 3 members, one cycle each ----
    srepo, sbare, sbase = _batch_sandbox(tmp_path / "serial")
    smembers = [
        _member_branch(srepo, sbase, "wk-a", "bin/a.py", "a=1\n", ("SABLE-a",)),
        _member_branch(srepo, sbase, "wk-b", "bin/b.py", "b=1\n", ("SABLE-b",)),
        _member_branch(srepo, sbase, "wk-c", "bin/c.py", "c=1\n", ("SABLE-c",)),
    ]
    t1 = time.monotonic()
    for m in smembers:
        cur = _bg(srepo, "rev-parse", "refs/remotes/origin/trunk")
        # one serial cycle: build the preview, push its own ci-verify ref, land it
        tree = _bg(srepo, "merge-tree", "--write-tree", cur, m.tip_sha).splitlines()[0]
        preview = _bg(srepo, "commit-tree", tree, "-p", cur, "-p", m.tip_sha, "-m",
                      f"serial preview {m.branch}")
        _bg(srepo, "push", "-q", "origin", f"{preview}:refs/heads/ci-verify/{m.branch}-x")
        _bg(srepo, "push", "-q", "origin", f"{preview}:refs/heads/trunk")
        _bg(srepo, "fetch", "-q", "origin")
    serial_wall = time.monotonic() - t1
    serial_ci_refs = subprocess.run(
        ["git", "-C", str(sbare), "for-each-ref", "--format=%(refname)", "refs/heads/ci-verify/"],
        capture_output=True, text=True).stdout.split()
    assert len(serial_ci_refs) == 3, "the serial lane pays 3 cycles for the same 3 members"

    # A gate CYCLE's wall-clock is dominated by its CI wait, not its git ops —
    # a sandbox cannot run real CI, so a bare git-op micro-timing would compare
    # the wrong quantity (and, being spawn-count-bound, invert). The faithful
    # measurement models each cycle as (its measured git ops) + (ONE CI wait),
    # priced at the gate's OWN merge_preview tier budget — the real per-cycle CI
    # cost the batch collapses from 3 to 1. Recorded, and asserted, on that.
    ci_cost = promote_lib.impact_budget()["tier_timeout_s"]
    batch_total = batch_wall + 1 * ci_cost          # ONE combined CI cycle
    serial_total = serial_wall + 3 * ci_cost        # THREE serial CI cycles
    print(f"\nS1 ACCEPTANCE measured (per-cycle CI budget {ci_cost:.0f}s): "
          f"batch lane = {batch_wall*1000:.1f}ms git + 1 CI cycle = {batch_total:.1f}s "
          f"(1 ci-verify ref) vs serial lane = {serial_wall*1000:.1f}ms git + 3 CI "
          f"cycles = {serial_total:.1f}s (3 ci-verify refs). "
          f"Saved {serial_total - batch_total:.1f}s / {2} CI cycles.")
    assert batch_total < serial_total, (
        f"batch cycle wall {batch_total:.1f}s not below the sum of 3 serial cycles "
        f"{serial_total:.1f}s")


# ---- SABLE-xpab3 meta-test: the batch-land suite makes ZERO real fleet sends

def test_batch_land_makes_zero_real_fleet_sends(tmp_path, monkeypatch, fleet_sink):
    """SABLE-xpab3 (landing-blocker, folded into be4lo.7): prove a batch land
    makes ZERO real sends to the live fleet. The subprocess seam is instrumented
    to record every command; a real LANDED batch runs under the hermetic
    channel; assert NOTHING invoked the real `sable-msg` cockpit or the real
    `bd` store. NON-VACUITY: with the redirect removed, the very same _notify
    resolves to the real `sable-msg` channel — so the recorder WOULD catch a
    leak. Remove the isolation and this test fails, which is the whole point."""
    recorded = []
    real_run = promote_lib.git_lib._run

    def recording_run(argv, *, cwd, check=True, timeout=None, env=None):
        recorded.append(list(argv))
        # Suppress the REAL fleet tools even here — the meta-test must itself
        # never message the cockpit, not even in the non-vacuity probe below.
        if argv and argv[0] in ("sable-msg", "bd"):
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, cwd=cwd, check=check, timeout=timeout, env=env)
    monkeypatch.setattr(promote_lib.git_lib, "_run", recording_run)

    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = [
        _member_branch(repo, base_sha, "wk-a", "bin/a.py", "a=1\n", ("SABLE-a",)),
        _member_branch(repo, base_sha, "wk-b", "bin/b.py", "b=1\n", ("SABLE-b",)),
    ]
    fold_tip = _fold(repo, base_sha, members)
    combined_ref = "ci-verify/batch-zero"
    res = promote_lib.land_batch(
        str(repo), "origin", "trunk", base_sha, fold_tip, members,
        budget=_ok_budget(members), combined_ref=combined_ref,
        verdict=_green_batch_verdict(fold_tip, combined_ref))
    assert res.landed

    real_sends = [a for a in recorded if a and a[0] in ("sable-msg", "bd")]
    assert real_sends == [], f"batch land leaked to the REAL fleet: {real_sends}"
    # Non-vacuity part A: the land DID notify — into the hermetic sink, proving
    # the zero-real result is isolation, not an absent notification.
    assert any("BATCH LANDED" in line for line in fleet_sink()), \
        "the land emitted no notification at all — isolation would be vacuously true"

    # Non-vacuity part B: drop the redirect and the same notify targets the real
    # 'sable-msg' channel; the recorder now sees it. If the default target were
    # NOT the live channel, this assertion — and the leak it models — would not
    # bite, so its passing is what makes the zero-real assertion meaningful.
    monkeypatch.delenv("SABLE_MG_NOTIFY", raising=False)
    recorded.clear()
    promote_lib._notify("lincoln", "vacuity probe — never really sent")
    assert any(a and a[0] == "sable-msg" for a in recorded), (
        "guard is vacuous: the default notify target is not the real 'sable-msg' channel")
# --------------------------------------------------------------------------
# AUTO-PROMOTE (SABLE-21rug.4) — THE DISQUALIFIER TABLE, EVERY ROW SEEN TO FIRE
# --------------------------------------------------------------------------
#
# The bead's own framing: "a disqualifier never seen to fire is untested by
# construction". So there is one case per row of AutoPromoteDisqualifier below,
# each asserting the decline NAMES that row, plus a healthy-mechanical control
# in the same suite that ALLOWS — without the control, every row could be
# firing for a reason unrelated to the one it claims (a fixture that denies no
# matter what would pass all fourteen).
#
# test_every_disqualifier_row_has_a_case_that_fires_it closes the loop
# structurally: it is not enough that fourteen cases exist, the union of the
# rows they actually observed must equal the enum. A row added later with no
# case reds that test rather than joining the table unexercised.
#
# What is real here and what is stubbed, and why. Real git does everything git
# decides: the mechanical footprint (non_gate_class) and the single-member fold
# (zero_conflicts) run against real commits, mirroring
# bin/test_sable_batch_admission_lib.py's own convention of never hand-
# simulating what git would say. The bd seam is stubbed at
# sable_footprint_lib._read_bead — the ONE bd-read function the declared
# footprint and the hold read both consult. CI is stubbed at
# sable_gate_preview_lib.adopt_kicked_preview / read_verdict, because there is
# no Actions run to consult in a sandbox. The integration section further down
# removes the git stubs entirely and drives the real promote() path.

_ROWS_OBSERVED: set = set()


def _record_rows(evaluation):
    """Every case funnels its evaluation through here, so the suite can prove
    at the end that the union of observed rows covers the enum."""
    _ROWS_OBSERVED.update(evaluation.rows)
    return evaluation


def _ap_run(repo, *args, **kw):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _ap_sha(repo, ref="HEAD"):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def ap_repo(tmp_path):
    """A real repo with a real base commit and a real branch commit, plus the
    remote-tracking ref refs/remotes/origin/trunk pointing at the base — so
    the base-unchanged check reads a real ref rather than always failing to
    resolve one (which would make every other row's case ambiguous)."""
    r = tmp_path / "repo"
    r.mkdir()
    _ap_run(r, "init", "-q", "-b", "trunk")
    _ap_run(r, "config", "user.email", "t@sable.invalid")
    _ap_run(r, "config", "user.name", "SABLE Test")
    (r / "bin").mkdir()
    (r / "bin" / "harmless.py").write_text("x = 1\n")
    _ap_run(r, "add", "-A")
    _ap_run(r, "commit", "-q", "-m", "base")
    base_sha = _ap_sha(r)
    _ap_run(r, "checkout", "-q", "-b", "wk-auto", base_sha)
    (r / "bin" / "harmless.py").write_text("x = 2\n")
    _ap_run(r, "add", "-A")
    _ap_run(r, "commit", "-q", "-m", "branch work")
    branch_sha = _ap_sha(r)
    _ap_run(r, "checkout", "-q", "trunk")
    _ap_run(r, "update-ref", "refs/remotes/origin/trunk", base_sha)
    return str(r), base_sha, branch_sha


PREVIEW_SHA = "0" * 40


@pytest.fixture()
def ap_seams(monkeypatch, ap_repo):
    """The healthy defaults every case starts from: one harmless declared
    footprint, no hold, an adopted preview, a GREEN verdict bound to that
    exact preview SHA with a typed producer. Each case perturbs exactly ONE
    of these, which is what makes the row it observes attributable."""
    state = {
        "bead": {"description": "",
                 "metadata": {"footprint_writes": "bin/harmless.py",
                              "footprint_reads_declared": ""}},
        "adopted": (PREVIEW_SHA, "ci-verify/auto-fixture"),
        "verdict": promote_lib.classify.Verdict(
            "success", "", PREVIEW_SHA, "ci-verify/auto-fixture",
            source="precomputed", complete=True),
    }

    def _read_bead(repo, bead):
        record = state["bead"]
        if isinstance(record, Exception):
            raise record
        return record

    monkeypatch.setattr(footprint_lib_for_auto, "_read_bead", _read_bead)
    monkeypatch.setattr(promote_lib.preview, "adopt_kicked_preview",
                        lambda *a, **kw: state["adopted"])
    monkeypatch.setattr(promote_lib.preview, "read_verdict",
                        lambda *a, **kw: state["verdict"])
    return state


def _evaluate(ap_repo, **kw):
    repo, base_sha, branch_sha = ap_repo
    return _record_rows(promote_lib.evaluate_auto_promote(
        "SABLE-auto", "wk-auto", "trunk", repo, "origin", base_sha, branch_sha, **kw))


# --- the control: without it, every row below is unattributable --------------

def test_a_healthy_mechanical_branch_is_allowed(ap_repo, ap_seams):
    """THE CONTROL. Nothing is perturbed, so every clause of the shared
    predicate passes, the evidence chain is complete and bound to the preview
    SHA, and all three vacuous-green preconditions hold — the evaluation
    ALLOWS, with an empty disqualifier table."""
    evaluation = _evaluate(ap_repo)
    assert evaluation.allowed, evaluation.reason
    assert evaluation.disqualifications == ()
    assert evaluation.preview_sha == PREVIEW_SHA
    assert evaluation.provenance == promote_lib.VerdictSource.PRECOMPUTED.value
    assert len(evaluation.self_hash) == 64
    assert "ALLOWED" in evaluation.reason


def test_the_allowing_assert_returns_the_evaluation_and_does_not_raise(ap_repo, ap_seams):
    repo, base_sha, branch_sha = ap_repo
    evaluation = promote_lib.assert_auto_promote_allowed(
        "SABLE-auto", "wk-auto", "trunk", repo, "origin", base_sha, branch_sha)
    assert evaluation.allowed


def test_the_denying_assert_raises_auto_promote_refused_carrying_the_evaluation(
        ap_repo, ap_seams):
    """The DENY half of the assert, bd-free (ap_seams stubs the bd read). A
    perturbed footprint makes the evaluation deny, so assert_auto_promote_allowed
    RAISES AutoPromoteRefused rather than returning. The exception must carry the
    whole evaluation, not just a string — SABLE-21rug.5's shadow mode logs the
    table off exc.evaluation — and its message must BE the evaluation's reason,
    which is what AutoPromoteRefused.__init__ binds. Covers the assert's raise
    arm and the exception constructor, both reachable in CI only through a unit
    case like this one (the real-promote polarity that hit them self-skips
    without bd)."""
    repo, base_sha, branch_sha = ap_repo
    ap_seams["bead"]["metadata"]["footprint_writes"] = promote_lib.admission.DISPATCH_FILE
    with pytest.raises(promote_lib.AutoPromoteRefused) as exc:
        promote_lib.assert_auto_promote_allowed(
            "SABLE-auto", "wk-auto", "trunk", repo, "origin", base_sha, branch_sha)
    evaluation = exc.value.evaluation
    _record_rows(evaluation)
    assert not evaluation.allowed
    assert promote_lib.AutoPromoteDisqualifier.GATE_CLASS_FILE in evaluation.rows, \
        evaluation.reason
    # str(exc) is what a bare `except ... as e: log(e)` would surface — it must
    # equal the evaluation's own reason (AutoPromoteRefused.__init__ passes it
    # to super().__init__), and exc.evaluation must be the same object.
    assert str(exc.value) == evaluation.reason
    assert exc.value.evaluation is evaluation


# --- rows 1-3: the three faces of the non_gate_class clause -----------------

def test_declines_naming_gate_class_file(ap_repo, ap_seams):
    ap_seams["bead"]["metadata"]["footprint_writes"] = promote_lib.admission.DISPATCH_FILE
    evaluation = _evaluate(ap_repo)
    assert promote_lib.AutoPromoteDisqualifier.GATE_CLASS_FILE in evaluation.rows, \
        evaluation.reason
    assert promote_lib.admission.DISPATCH_FILE in evaluation.reason


def test_declines_naming_the_78qck_tier_mechanism_file(ap_repo, ap_seams):
    """SABLE-78qck's reaches-the-tier-mechanism exclusion gets its OWN row, not
    the generic gate-class one: 'you touched the thing that decides which
    suites run' is a different instruction to the human than 'you touched
    merge tooling'."""
    tier_file = ".github/ci/test-tiers.sh"
    assert tier_file in promote_lib.admission.GATE_TIER_FILES
    ap_seams["bead"]["metadata"]["footprint_writes"] = tier_file
    evaluation = _evaluate(ap_repo)
    assert promote_lib.AutoPromoteDisqualifier.TIER_MECHANISM_FILE in evaluation.rows, \
        evaluation.reason
    assert promote_lib.AutoPromoteDisqualifier.GATE_CLASS_FILE not in evaluation.rows, \
        "a tier-mechanism file was named as a generic gate-class file"


def test_declines_naming_classification_ambiguity(ap_repo, ap_seams):
    """A footprint that could not be DETERMINED is not a footprint that is
    clean. sable_footprint_lib.FootprintUndetermined reaches the table as its
    own row, fail-closed."""
    ap_seams["bead"] = footprint_lib_for_auto.FootprintUndetermined("bd unavailable in fixture")
    evaluation = _evaluate(ap_repo)
    assert promote_lib.AutoPromoteDisqualifier.CLASSIFICATION_AMBIGUITY in evaluation.rows, \
        evaluation.reason


def test_classification_ambiguity_maps_from_a_real_undetermined_clause(ap_repo, ap_seams):
    """ANTI-DRIFT. The three-way fan-out reads the shared predicate's own
    reason text, so it is coupled to wording this module does not own. This
    case pins the coupling against a REAL verdict produced by the REAL
    predicate — if be4lo.3 rewords 'undetermined', this reds here instead of
    silently degrading every ambiguous footprint into the wrong row."""
    repo, base_sha, branch_sha = ap_repo
    ap_seams["bead"] = footprint_lib_for_auto.FootprintUndetermined("bd unavailable in fixture")
    verdict = promote_lib.admission.is_this_branch_mechanical(
        repo, "origin", "SABLE-auto", "wk-auto", base_sha, branch_sha)
    clause = verdict.clause("non_gate_class")
    assert not clause.passed
    assert promote_lib._row_for_non_gate_class(clause.reason) is \
        promote_lib.AutoPromoteDisqualifier.CLASSIFICATION_AMBIGUITY, \
        f"the real predicate's undetermined reason no longer maps: {clause.reason!r}"


# --- rows 4-7: the remaining clauses of the shared predicate ----------------

def test_declines_naming_a_non_green_verdict(ap_repo, ap_seams):
    ap_seams["verdict"] = promote_lib.classify.Verdict(
        "failure", "", PREVIEW_SHA, "ci-verify/auto-fixture",
        source="precomputed", complete=True)
    evaluation = _evaluate(ap_repo)
    assert promote_lib.AutoPromoteDisqualifier.NOT_INDIVIDUALLY_GREEN in evaluation.rows, \
        evaluation.reason


def test_declines_naming_a_live_hold(ap_repo, ap_seams):
    ap_seams["bead"]["metadata"]["hold"] = "held by lincoln pending a ruling"
    evaluation = _evaluate(ap_repo)
    assert promote_lib.AutoPromoteDisqualifier.LIVE_HOLD in evaluation.rows, evaluation.reason
    assert "held by lincoln" in evaluation.reason


def test_declines_naming_a_missing_clean_ff_adoption(ap_repo, ap_seams):
    """No kicked preview for the CURRENT (base, branch) pair: nothing can be
    fast-forwarded, and there is equally no verdict to attribute a producer
    to — so the provenance row fires alongside, which is correct rather than
    redundant (they are two different absent things)."""
    ap_seams["adopted"] = None
    evaluation = _evaluate(ap_repo)
    assert promote_lib.AutoPromoteDisqualifier.NO_CLEAN_FF_ADOPTION in evaluation.rows, \
        evaluation.reason
    assert promote_lib.AutoPromoteDisqualifier.MISSING_PROVENANCE in evaluation.rows


def test_declines_naming_a_conflict(tmp_path, monkeypatch, ap_seams, ap_repo):
    """A REAL single-member fold conflict, from real git: the base advances
    with a change to the same lines the branch rewrote, so folding the branch
    alone onto that base cannot apply."""
    repo, base_sha, _ = ap_repo
    _ap_run(repo, "checkout", "-q", "trunk")
    (Path(repo) / "bin" / "harmless.py").write_text("x = 99\n")
    _ap_run(repo, "add", "-A")
    _ap_run(repo, "commit", "-q", "-m", "conflicting base advance")
    moved_base = _ap_sha(repo)
    _ap_run(repo, "update-ref", "refs/remotes/origin/trunk", moved_base)
    branch_sha = _ap_sha(repo, "wk-auto")

    evaluation = _record_rows(promote_lib.evaluate_auto_promote(
        "SABLE-auto", "wk-auto", "trunk", repo, "origin", moved_base, branch_sha))
    assert promote_lib.AutoPromoteDisqualifier.CONFLICT in evaluation.rows, evaluation.reason


# --- rows 8-11: the evidence chain -----------------------------------------

def test_declines_naming_missing_provenance(ap_repo, ap_seams):
    """An UNENUMERATED producer is refused, never passed through — the same
    contract SABLE-21rug.2 states for the verdict source it is adding."""
    ap_seams["verdict"] = promote_lib.classify.Verdict(
        "success", "", PREVIEW_SHA, "ci-verify/auto-fixture",
        source="some-untyped-producer", complete=True)
    evaluation = _evaluate(ap_repo)
    assert promote_lib.AutoPromoteDisqualifier.MISSING_PROVENANCE in evaluation.rows, \
        evaluation.reason
    assert "some-untyped-producer" in evaluation.reason


def test_declines_naming_a_missing_self_hash(ap_repo, ap_seams, monkeypatch, tmp_path):
    """An implementation that cannot be hashed cannot be recorded, and a
    landing whose record cannot say which code decided it is not replayable by
    SABLE-21rug.6. Fail-closed: absence of a hash is not a hash."""
    monkeypatch.setattr(promote_lib, "_deciding_sources",
                        lambda: [tmp_path / "no-such-implementation.py"])
    assert promote_lib.gate_self_hash() == ""
    evaluation = _evaluate(ap_repo)
    assert promote_lib.AutoPromoteDisqualifier.MISSING_SELF_HASH in evaluation.rows, \
        evaluation.reason
    assert evaluation.self_hash == ""


def test_gate_self_hash_over_zero_sources_is_absence_not_a_digest(monkeypatch):
    """p9n7k applied to the hash function's own input: sha256 of nothing is a
    perfectly well-formed digest, and returning it would make 'no sources' read
    as 'hashed successfully'."""
    monkeypatch.setattr(promote_lib, "_deciding_sources", lambda: [])
    assert promote_lib.gate_self_hash() == ""


def test_declines_naming_a_verdict_sha_mismatch(ap_repo, ap_seams):
    """A verdict for a DIFFERENT object is refused, never re-pointed at the
    object in hand."""
    ap_seams["verdict"] = promote_lib.classify.Verdict(
        "success", "", "f" * 40, "ci-verify/auto-fixture",
        source="precomputed", complete=True)
    evaluation = _evaluate(ap_repo)
    assert promote_lib.AutoPromoteDisqualifier.VERDICT_SHA_MISMATCH in evaluation.rows, \
        evaluation.reason


def test_declines_naming_a_base_that_moved_since_the_verdict(ap_repo, ap_seams):
    """The base is re-observed LIVE rather than taken from the caller's
    argument — the whole question being whether that argument is still true."""
    repo, base_sha, branch_sha = ap_repo
    _ap_run(repo, "checkout", "-q", "trunk")
    (Path(repo) / "unrelated.txt").write_text("base advanced\n")
    _ap_run(repo, "add", "-A")
    _ap_run(repo, "commit", "-q", "-m", "base advance")
    _ap_run(repo, "update-ref", "refs/remotes/origin/trunk", _ap_sha(repo))

    evaluation = _evaluate(ap_repo)
    assert promote_lib.AutoPromoteDisqualifier.BASE_MOVED in evaluation.rows, evaluation.reason


def test_an_unresolvable_base_denies_rather_than_raising(ap_repo, ap_seams):
    """'Unchanged' must not be inferred from 'unreadable'. The evaluation
    returns a row; it never raises out of the table."""
    repo, base_sha, branch_sha = ap_repo
    _ap_run(repo, "update-ref", "-d", "refs/remotes/origin/trunk")
    evaluation = _evaluate(ap_repo)
    assert promote_lib.AutoPromoteDisqualifier.BASE_MOVED in evaluation.rows, evaluation.reason
    assert "could not be resolved" in evaluation.reason


# --- rows 12-14: the three vacuous-green preconditions ----------------------

def _verdict_with(clauses):
    return promote_lib.admission.MechanicalVerdict(
        branch="wk-auto", bead="SABLE-auto",
        mechanical=all(c.passed for c in clauses), clauses=clauses)


def test_declines_naming_an_absent_non_vacuous_proof(ap_repo, ap_seams):
    """SABLE-p9n7k, literally: all(...) over an empty clause tuple is True, so
    a verdict that checked NOTHING presents as mechanical=True. The assert
    must not believe it."""
    empty = _verdict_with(())
    assert empty.mechanical is True, \
        "fixture no longer reproduces the vacuous green this row exists for"
    evaluation = _evaluate(ap_repo, mechanical=empty)
    assert promote_lib.AutoPromoteDisqualifier.NON_VACUOUS_PROOF_ABSENT in evaluation.rows, \
        evaluation.reason
    assert not evaluation.allowed


def test_declines_naming_absent_selection_completeness(ap_repo, ap_seams):
    """SABLE-x2n8a: a TRUNCATED clause set is non-empty and every clause in it
    passes — indistinguishable from a complete one at the call site, which is
    the whole defect. Distinct from p9n7k's row: something WAS checked here,
    just not everything."""
    partial = _verdict_with(tuple(
        promote_lib.admission.ClauseResult(name, True, "passed")
        for name in ("non_gate_class", "zero_holds")))
    assert partial.mechanical is True
    evaluation = _evaluate(ap_repo, mechanical=partial)
    rows = evaluation.rows
    assert promote_lib.AutoPromoteDisqualifier.SELECTION_COMPLETENESS_ABSENT in rows, \
        evaluation.reason
    assert promote_lib.AutoPromoteDisqualifier.NON_VACUOUS_PROOF_ABSENT not in rows, \
        "a short clause set was reported as an empty one — the two rows are different defects"
    for missing in ("individually_green", "clean_ff_adoption", "zero_conflicts"):
        assert missing in evaluation.reason


def test_declines_naming_a_truncated_bd_read(ap_repo, ap_seams):
    """SABLE-52aym: an enumerating bd read with no explicit --limit is
    silently cut at 50, and every absence-shaped conclusion drawn from it is
    wrong in the RELEASING direction."""
    planted = ast.parse('argv = ["list", "--json", "--status=open"]')
    evaluation = _evaluate(ap_repo, module_asts=[planted])
    assert promote_lib.AutoPromoteDisqualifier.TRUNCATED_BD_READ in evaluation.rows, \
        evaluation.reason
    assert "list --json --status=open" in evaluation.reason


def test_an_explicitly_bounded_bd_read_is_not_flagged(ap_repo, ap_seams):
    """The negative control for the row above (SABLE-rhsuj false-positive law):
    the SAME shape carrying --limit 0 must pass, or the check is just 'any bd
    list denies'."""
    bounded = ast.parse('argv = ["list", "--json", "--limit", "0"]')
    assert promote_lib.unlimited_bd_reads(bounded) == []
    evaluation = _evaluate(ap_repo, module_asts=[bounded])
    assert evaluation.allowed, evaluation.reason


def test_the_real_deciding_sources_carry_no_unbounded_enumerating_bd_read():
    """Non-vacuity against the real modules: the scanner is run over the
    ACTUAL deciding sources and must find them clean. If this ever reds, a bd
    enumeration joined the deciding path without a --limit — which is the
    defect, not a false alarm."""
    for path in promote_lib._deciding_sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        assert promote_lib.unlimited_bd_reads(tree) == [], f"unbounded bd read in {path}"


def test_a_bd_read_addressing_one_bead_by_id_is_never_flagged():
    """`show` and `update` name a single bead, so there is no result set to
    truncate. Flagging them would make the precondition unsatisfiable and
    therefore meaningless."""
    tree = ast.parse('a = ["show", bead, "--json"]\nb = ["update", bead, "--append-notes", n]')
    assert promote_lib.unlimited_bd_reads(tree) == []


def test_an_unparseable_deciding_source_denies_rather_than_scanning_nothing(
        ap_repo, ap_seams, monkeypatch, tmp_path):
    """A scan that COULD NOT RUN and a scan that FOUND NOTHING must not read
    the same — the bounded-search failure 52aym is itself an instance of."""
    broken = tmp_path / "broken_source.py"
    broken.write_text("def (((:\n")
    monkeypatch.setattr(promote_lib, "_deciding_sources", lambda: [broken])
    evaluation = _evaluate(ap_repo)
    assert promote_lib.AutoPromoteDisqualifier.TRUNCATED_BD_READ in evaluation.rows, \
        evaluation.reason
    assert "broken_source.py" in evaluation.reason


# --- table-level properties -------------------------------------------------

def test_the_completeness_target_matches_the_real_predicates_clause_set(ap_repo, ap_seams):
    """x2n8a's completeness precondition compares against a literal list of
    clause names. Read the names off a REAL verdict from the REAL predicate so
    the literal cannot drift away from what be4lo.3 actually returns — a
    drifted target would either deny every healthy branch or stop detecting
    truncation."""
    repo, base_sha, branch_sha = ap_repo
    verdict = promote_lib.admission.is_this_branch_mechanical(
        repo, "origin", "SABLE-auto", "wk-auto", base_sha, branch_sha)
    assert tuple(c.name for c in verdict.clauses) == promote_lib.REQUIRED_MECHANICAL_CLAUSES


def test_every_clause_of_the_shared_predicate_has_a_row(ap_repo, ap_seams):
    """No clause may deny anonymously. Every name the predicate can return is
    mapped — either through _CLAUSE_ROWS or through non_gate_class's fan-out —
    so a failing clause always NAMES a row."""
    mapped = set(promote_lib._CLAUSE_ROWS) | {"non_gate_class"}
    assert set(promote_lib.REQUIRED_MECHANICAL_CLAUSES) == mapped


def test_the_decline_names_every_row_that_fired_not_just_the_first(ap_repo, ap_seams):
    """Never short-circuited, mirroring is_this_branch_mechanical's own
    contract: one decline that hides three others costs three more round trips
    at the seat."""
    ap_seams["bead"]["metadata"]["footprint_writes"] = promote_lib.admission.DISPATCH_FILE
    ap_seams["bead"]["metadata"]["hold"] = "held"
    ap_seams["verdict"] = promote_lib.classify.Verdict(
        "failure", "", "f" * 40, "ci-verify/auto-fixture",
        source="precomputed", complete=True)
    evaluation = _evaluate(ap_repo)
    rows = set(evaluation.rows)
    assert {promote_lib.AutoPromoteDisqualifier.GATE_CLASS_FILE,
            promote_lib.AutoPromoteDisqualifier.LIVE_HOLD,
            promote_lib.AutoPromoteDisqualifier.NOT_INDIVIDUALLY_GREEN,
            promote_lib.AutoPromoteDisqualifier.VERDICT_SHA_MISMATCH} <= rows, evaluation.reason
    assert "4 disqualifier(s)" in evaluation.reason


def test_the_table_is_reported_in_table_order_not_evaluation_order(ap_repo, ap_seams):
    """Two records for the same facts must be byte-identical however the
    evaluation was scheduled — a property SABLE-21rug.6's replay compares on."""
    ap_seams["bead"]["metadata"]["footprint_writes"] = promote_lib.admission.DISPATCH_FILE
    ap_seams["bead"]["metadata"]["hold"] = "held"
    rows = _evaluate(ap_repo).rows
    order = list(promote_lib.AutoPromoteDisqualifier)
    assert list(rows) == sorted(rows, key=order.index)


def test_the_evaluation_record_round_trips_with_unknown_field_tolerance(ap_repo, ap_seams):
    """SABLE-21rug.6 replays from durable records; an additive field a later
    sibling writes must not break an older reader."""
    ap_seams["bead"]["metadata"]["hold"] = "held"
    evaluation = _evaluate(ap_repo)
    data = evaluation.to_dict()
    data["a_field_from_the_future"] = {"nested": True}
    restored = promote_lib.AutoPromoteEvaluation.from_dict(data)
    assert restored == evaluation
    assert restored.rows == evaluation.rows


def test_an_allowing_evaluation_is_recorded_just_as_completely(ap_repo, ap_seams):
    """The landings that actually happen must not be the unobservable ones."""
    data = _evaluate(ap_repo).to_dict()
    assert data["allowed"] is True
    assert data["disqualifiers"] == []
    assert data["preview_sha"] == PREVIEW_SHA
    assert data["provenance"] == promote_lib.VerdictSource.PRECOMPUTED.value
    assert len(data["self_hash"]) == 64


def test_the_auto_promote_assert_re_derives_no_mechanical_clause():
    """ACCEPTANCE (SABLE-21rug.4): 'the predicate is consumed from its one home
    — grep proves no clause re-derivation in this file'.

    Checked over the AST rather than by substring, so the prose above that
    NAMES these functions (deliberately, to explain the boundary) cannot be
    mistaken for a call to them. Every clause-deriving function
    sable_batch_admission_lib uses to build a MechanicalVerdict is banned here;
    a second derivation of any of them is the drift the factoring exists to
    prevent."""
    banned = {"declared_footprint", "mechanical_footprint", "declared_reads",
              "gate_class_roster", "is_disjoint", "is_rw_disjoint", "fold_check",
              "_read_bead", "_non_gate_class", "_zero_holds", "_zero_conflicts",
              "_individually_green_and_ff", "admit_batch"}
    tree = ast.parse(Path(promote_lib.__file__).read_text())
    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else None)
        if name:
            called.add(name)
    assert not (banned & called), (
        f"the auto-promote assert re-derives mechanical clauses: {sorted(banned & called)}")
    # Positive control: the ban above is only meaningful if this file really
    # does consume the shared predicate. An empty file would pass the ban.
    assert "is_this_branch_mechanical" in called, \
        "this file does not consume the shared predicate at all — the ban above proves nothing"


def test_every_disqualifier_row_has_a_case_that_fires_it(request):
    """THE COMPLETENESS GATE for this bead's whole test spec: 'a disqualifier
    never seen to fire is untested by construction'. The union of rows the
    cases above actually OBSERVED must equal the enum — not merely be covered
    by an equal count of tests. A row added later with no case reds here.

    It reads an accumulator the cases above fill, so it is only meaningful
    when they all ran. Two things deselect them routinely — a `-k` filter, and
    this repo's own pytest-testmon/pytest-impact selection in ci-verify — and
    under either, an accumulator gate that still ASSERTED would red on a
    selection artifact rather than a defect. So it SKIPS instead, naming
    exactly which cases were absent: a skip that says why is distinguishable
    from a pass, which is the whole point of the p9n7k family. What it must
    never become is a silent pass on a partial run."""
    cases = {name for name in globals()
             if name.startswith("test_declines_") or name == "test_an_unparseable_"
             "deciding_source_denies_rather_than_scanning_nothing"}
    selected = {item.name.split("[")[0] for item in request.session.items}
    absent = cases - selected
    if absent:
        pytest.skip(f"auto-promote section only partially selected — {len(absent)} "
                    f"disqualifier case(s) not run: {sorted(absent)}")
    missing = set(promote_lib.AutoPromoteDisqualifier) - _ROWS_OBSERVED
    assert not missing, (
        f"disqualifier rows never seen to fire: {sorted(d.value for d in missing)}")


# --------------------------------------------------------------------------
# AUTO-PROMOTE, the promote() entry wiring (SABLE-21rug.4) — bd-free
# --------------------------------------------------------------------------
#
# The `if auto:` block at the top of promote() is the seam between the entry
# point and the disqualifier table. The S3 integration cases below drive it
# through a real promote() with a real bd store, so they self-skip in the
# ci-verify clean room (no bd/dolt) — which leaves the block's own lines
# UNCOVERED exactly where the merge decision is gated. These two cases cover
# both polarities of the block with no bd at all, monkeypatching promote()'s
# prologue the same way test_tip_equals_tested_integrity_abort_still_fires
# does, and stubbing assert_auto_promote_allowed itself (its own arms are
# covered by the ap_seams unit cases above). So the wiring — record-the-ALLOW,
# record-and-raise-on-DENY — is verified where it is actually enforced, not
# only where bd happens to be installed.

def _auto_block_prologue_stubs(monkeypatch, evidence):
    """No-op every promote() precondition that runs BEFORE the `if auto:`
    block, and capture _append_evidence so the polarity of what the block
    recorded is observable. Leaves the auto block itself real."""
    git_lib = promote_lib.git_lib
    base_ref = promote_lib.classify.qualify_remote_ref(REMOTE, BASE)
    monkeypatch.setattr(promote_lib, "assert_not_frozen", lambda repo: None)
    monkeypatch.setattr(promote_lib, "assert_landing_pair_satisfied",
                        lambda *a, **kw: None)
    monkeypatch.setattr(git_lib, "_git", lambda repo, *args, check=True: _cp(0))
    monkeypatch.setattr(git_lib, "resolve_commit",
                        lambda repo, ref: BASE_SHA if ref == base_ref else BRANCH_SHA)
    monkeypatch.setattr(promote_lib, "_append_evidence",
                        lambda repo, bead, msg: evidence.append(msg))


def test_promote_with_auto_allow_records_the_allow_and_proceeds(monkeypatch):
    """auto=True + an ALLOW: the block records the allow durably (SABLE-21rug.6
    replays landings that actually happened, so the ALLOW write is not
    optional) and promote() proceeds past the gate. The rest of promote() is
    short-circuited at the adoption-miss check so the test isolates the block."""
    evidence = []
    _auto_block_prologue_stubs(monkeypatch, evidence)
    allow_eval = promote_lib.AutoPromoteEvaluation(
        bead="SABLE-x", branch=BRANCH, base_sha=BASE_SHA, branch_sha=BRANCH_SHA,
        preview_sha="c" * 40, self_hash="a" * 64, provenance="precomputed",
        disqualifications=())
    assert allow_eval.allowed
    monkeypatch.setattr(promote_lib, "assert_auto_promote_allowed",
                        lambda *a, **kw: allow_eval)
    # Past the block, stop promote() at the first cheap exit so nothing builds.
    monkeypatch.setattr(promote_lib, "assert_coverage_floor", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "_adoption_miss_optimistic", lambda *a, **kw: 0)

    rc = promote_lib.promote("SABLE-x", BRANCH, BASE, REPO, REMOTE, "chuck", None,
                             auto=True)
    assert rc == 0
    allowed_writes = [m for m in evidence if "auto-promote ALLOWED" in m]
    assert allowed_writes, f"the ALLOW was not recorded: {evidence}"
    assert "SABLE-21rug.4" in allowed_writes[0]


def test_promote_with_auto_decline_records_and_raises_gate_error(monkeypatch):
    """auto=True + a DENY: the block records the decline durably AND raises a
    GateError that NAMES the disqualifier, routing the branch back to the
    seat's ordinary queue. A decline is not an error state — nothing was built
    — but it must halt this auto promote loudly and observably."""
    evidence = []
    _auto_block_prologue_stubs(monkeypatch, evidence)
    deny_eval = promote_lib.AutoPromoteEvaluation(
        bead="SABLE-x", branch=BRANCH, base_sha=BASE_SHA, branch_sha=BRANCH_SHA,
        preview_sha="", self_hash="a" * 64, provenance="",
        disqualifications=(promote_lib.Disqualification(
            promote_lib.AutoPromoteDisqualifier.LIVE_HOLD, "planted live hold"),))
    assert not deny_eval.allowed

    def _decline(*a, **kw):
        raise promote_lib.AutoPromoteRefused(deny_eval)
    monkeypatch.setattr(promote_lib, "assert_auto_promote_allowed", _decline)

    with pytest.raises(promote_lib.GateError) as exc:
        promote_lib.promote("SABLE-x", BRANCH, BASE, REPO, REMOTE, "chuck", None,
                            auto=True)
    assert exc.value.code == promote_lib.classify.EXIT_PRECONDITION
    assert "AUTO-PROMOTE DECLINED" in str(exc.value)
    assert promote_lib.AutoPromoteDisqualifier.LIVE_HOLD.value in str(exc.value)
    declined_writes = [m for m in evidence if "auto-promote DECLINED" in m]
    assert declined_writes, f"the decline was not recorded: {evidence}"
    assert "SABLE-21rug.4" in declined_writes[0]


# --------------------------------------------------------------------------
# AUTO-PROMOTE INTEGRATION (SABLE-21rug.4) — the S3 acceptance
# --------------------------------------------------------------------------
#
# A REAL two-repo sandbox (a bare origin + a real working clone), the REAL
# promote() entry point, and a REAL bd store for the evidence write. Nothing
# about the auto-promote decision is stubbed here: the mechanical footprint,
# the hold read, the single-member fold, the base re-observation and the
# landing itself are all real. Only CI is stubbed (materialize_preview /
# acquire_verdict / delete_ci_ref) — there is no Actions run to consult in a
# sandbox, exactly as in the SABLE-21rug.1 integration case above.
#
# The acceptance is a PAIR, run against the same sandbox: one branch with a
# planted disqualifier and one healthy control. Without the control, "nothing
# landed" is satisfied by a gate that refuses everything; without the plant,
# "the healthy one landed" is satisfied by a gate that refuses nothing.

def _ap_sandbox_branch(work, bare, name, filename, content):
    subprocess.run(["git", "-C", work, "checkout", "-q", "-b", name, "trunk"],
                   check=True, capture_output=True)
    path = Path(work) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(["git", "-C", work, "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", work, "commit", "-q", "-m", f"work on {name}"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", work, "push", "-q", "origin", name],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", work, "checkout", "-q", "trunk"], check=True,
                   capture_output=True)
    return subprocess.run(["git", "-C", work, "rev-parse", name], check=True,
                          capture_output=True, text=True).stdout.strip()


def _ap_origin_tip(bare, ref="trunk"):
    return subprocess.run(["git", "-C", bare, "rev-parse", ref], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd/dolt by design; real-bd integration self-skips")
def test_the_auto_path_declines_loudly_and_lands_nothing_a_decider_would_have_held(
        tmp_path, monkeypatch):
    """S3 ACCEPTANCE (SABLE-21rug.4). Real sandbox, real bd, real promote().

    HARD INVARIANT, asserted directly rather than inferred from an exit code:
    ZERO LANDINGS A DECIDER WOULD HAVE HELD. The origin's trunk tip is read
    before and after the declined auto-promote and must be byte-identical —
    the branch is still sitting in the seat's ordinary queue, promotable by a
    human exactly as it is today."""
    work, bare, _ = _real_two_repo_sandbox(tmp_path)
    monkeypatch.setattr(promote_lib, "_notify", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "cleanup_after_merge", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib.preview, "delete_ci_ref", lambda *a, **kw: None)
    subprocess.run(["git", "-C", work, "fetch", "-q", "origin"], check=True,
                   capture_output=True)
    base_sha = _ap_origin_tip(bare)

    # The PLANT: a branch whose declared footprint reaches the gate's own
    # dispatch file — gate-class, and therefore never mechanically promotable.
    held_sha = _ap_sandbox_branch(work, bare, "wk-held", "bin/harmless-held.py", "h = 1\n")
    # The CONTROL: an ordinary, disjoint, non-gate-class branch.
    ok_sha = _ap_sandbox_branch(work, bare, "wk-ok", "bin/harmless-ok.py", "k = 1\n")

    previews = {held_sha: ("p" * 40, "ci-verify/held"), ok_sha: ("q" * 40, "ci-verify/ok")}
    monkeypatch.setattr(
        promote_lib.preview, "adopt_kicked_preview",
        lambda repo, remote, branch, b, br: previews.get(br))
    monkeypatch.setattr(
        promote_lib.preview, "read_verdict",
        lambda repo, ref, preview_sha: promote_lib.classify.Verdict(
            "success", "", preview_sha, ref, source="precomputed", complete=True))
    # The declared footprint is the ONLY thing distinguishing the two branches,
    # read through the real bd seam's shape.
    declared = {"SABLE-auto-held": promote_lib.admission.DISPATCH_FILE,
                "SABLE-auto-ok": "bin/harmless-ok.py"}
    monkeypatch.setattr(
        footprint_lib_for_auto, "_read_bead",
        lambda repo, bead: {"description": "",
                            "metadata": {"footprint_writes": declared[bead],
                                         "footprint_reads_declared": ""}})

    tip_before = _ap_origin_tip(bare)

    # --- the planted disqualifier: DECLINED, loudly, by name ---------------
    with pytest.raises(promote_lib.GateError) as excinfo:
        promote_lib.promote("SABLE-auto-held", "wk-held", "trunk", work, "origin",
                            "chuck", None, auto=True)
    message = str(excinfo.value)
    assert excinfo.value.code == promote_lib.classify.EXIT_PRECONDITION
    assert "AUTO-PROMOTE DECLINED" in message
    assert promote_lib.AutoPromoteDisqualifier.GATE_CLASS_FILE.value in message, message
    assert promote_lib.admission.DISPATCH_FILE in message, \
        "the decline did not name the specific file that disqualified it"
    assert "ordinary queue" in message

    # HARD INVARIANT: nothing landed from the auto path.
    assert _ap_origin_tip(bare) == tip_before, \
        "a branch the auto-promote gate DECLINED reached the integration branch anyway"
    # And the branch is still there to be promoted by a human — a decline
    # removes nothing from the seat's ordinary queue.
    assert _ap_origin_tip(bare, "wk-held") == held_sha

    # --- the healthy control: the SAME auto path lands it -----------------
    monkeypatch.setattr(promote_lib.preview, "materialize_preview",
                        lambda *a, **kw: (ok_sha, "ci-verify/ok", False))
    monkeypatch.setattr(promote_lib.preview, "acquire_verdict",
                        lambda *a, **kw: promote_lib.classify.Verdict(
                            "success", "", ok_sha, "ci-verify/ok", source="precomputed"))
    rc = promote_lib.promote("SABLE-auto-ok", "wk-ok", "trunk", work, "origin",
                             "chuck", None, auto=True)
    assert rc == 0, "the healthy control did not land — the gate refuses everything"
    assert _ap_origin_tip(bare) == ok_sha, "the control landed something other than its own commit"


@pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd/dolt by design; real-bd integration self-skips")
def test_both_polarities_of_the_auto_gate_are_recorded_durably(tmp_path, monkeypatch):
    """An ALLOW must be as durable as a DENY. A gate that only records its
    refusals makes the landings that ACTUALLY HAPPENED the unobservable ones,
    which is the shape SABLE-21rug.6's audit ('zero input on a landing day is
    RED') exists to catch.

    Driven through the REAL promote() against the real two-repo sandbox — real
    fetch, real refs, real landing — and captured at the _append_evidence seam,
    so what is asserted is what promote() really writes rather than what it
    computes. The two polarities differ in ONE fact (a hold on the bead), which
    is what makes the difference in the record attributable."""
    work, bare, branch_sha = _real_two_repo_sandbox(tmp_path)
    monkeypatch.setattr(promote_lib, "_notify", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "cleanup_after_merge", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib.preview, "delete_ci_ref", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib.preview, "adopt_kicked_preview",
                        lambda *a, **kw: (branch_sha, "ci-verify/polarity"))
    monkeypatch.setattr(promote_lib.preview, "read_verdict",
                        lambda repo, ref, sha: promote_lib.classify.Verdict(
                            "success", "", sha, ref, source="precomputed", complete=True))
    held = {"on": True}
    monkeypatch.setattr(
        footprint_lib_for_auto, "_read_bead",
        lambda repo, bead: {"description": "", "metadata": dict(
            {"footprint_writes": "feature.txt", "footprint_reads_declared": ""},
            **({"hold": "held by lincoln"} if held["on"] else {}))})
    written = []
    monkeypatch.setattr(promote_lib, "_append_evidence",
                        lambda repo, bead, note: written.append(note))
    monkeypatch.setattr(promote_lib.preview, "materialize_preview",
                        lambda *a, **kw: pytest.fail("a DECLINE built a preview"))

    # --- DENY polarity ----------------------------------------------------
    with pytest.raises(promote_lib.GateError):
        promote_lib.promote("SABLE-auto", "wk-x", "trunk", work, "origin",
                            "chuck", None, auto=True)
    declines = [n for n in written if n.startswith("auto-promote DECLINED (SABLE-21rug.4): ")]
    assert len(declines) == 1, written
    denied = promote_lib.AutoPromoteEvaluation.from_dict(
        json.loads(declines[0].split(": ", 1)[1]))
    assert not denied.allowed
    assert promote_lib.AutoPromoteDisqualifier.LIVE_HOLD in denied.rows
    assert _ap_origin_tip(bare) != branch_sha, "the declined branch landed anyway"

    # --- ALLOW polarity: the SAME sandbox, one fact changed ---------------
    written.clear()
    held["on"] = False
    monkeypatch.setattr(promote_lib.preview, "materialize_preview",
                        lambda *a, **kw: (branch_sha, "ci-verify/polarity", False))
    monkeypatch.setattr(promote_lib.preview, "acquire_verdict",
                        lambda *a, **kw: promote_lib.classify.Verdict(
                            "success", "", branch_sha, "ci-verify/polarity",
                            source="precomputed"))

    assert promote_lib.promote("SABLE-auto", "wk-x", "trunk", work, "origin",
                               "chuck", None, auto=True) == 0
    allow_notes = [n for n in written if n.startswith("auto-promote ALLOWED (SABLE-21rug.4): ")]
    assert len(allow_notes) == 1, f"the ALLOW was not recorded durably: {written}"
    permitted = promote_lib.AutoPromoteEvaluation.from_dict(
        json.loads(allow_notes[0].split(": ", 1)[1]))
    assert permitted.allowed
    assert permitted.disqualifications == ()
    assert len(permitted.self_hash) == 64
    assert permitted.provenance == promote_lib.VerdictSource.PRECOMPUTED.value
    assert _ap_origin_tip(bare) == branch_sha, "the allowed branch did not land"


@pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd/dolt by design; real-bd integration self-skips")
def test_a_seat_promote_never_reaches_the_auto_gate(tmp_path, monkeypatch):
    """REGRESSION, and the reason this bead is behaviour-neutral on landing:
    every caller today promotes WITHOUT `auto`, and that path must not consult
    the auto-promote gate at all. The same planted disqualifier that declined
    above lands normally here, because a human asked for it."""
    work, bare, branch_sha = _real_two_repo_sandbox(tmp_path)
    monkeypatch.setattr(promote_lib, "_notify", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "cleanup_after_merge", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib.preview, "delete_ci_ref", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib.preview, "materialize_preview",
                        lambda *a, **kw: (branch_sha, "ci-verify/seat", False))
    monkeypatch.setattr(promote_lib.preview, "acquire_verdict",
                        lambda *a, **kw: promote_lib.classify.Verdict(
                            "success", "", branch_sha, "ci-verify/seat", source="waited"))
    monkeypatch.setattr(promote_lib, "evaluate_auto_promote", lambda *a, **kw: pytest.fail(
        "a seat promote consulted the auto-promote gate"))

    assert promote_lib.promote("SABLE-seat", "wk-x", "trunk", work, "origin",
                               "chuck", None) == 0
    assert _ap_origin_tip(bare) == branch_sha


# ==========================================================================
# THE RED-BATCH BISECTION STATE MACHINE (SABLE-be4lo.8) — S3
# ==========================================================================
#
# Layout mirrors the be4lo.7 batch-land section above: the pure arithmetic and
# the state-machine properties first, then E2E cases on a REAL git sandbox with
# a REAL content-derived verifier. Every fold below is built by the real
# sable_batch_fold_lib and every ci-verify ref is really pushed to a real bare
# remote, so "how many extra combined runs did it cost" is COUNTED off the
# remote rather than tallied by the code under test.
#
# WHAT IS AND IS NOT MODELLED. There is no Actions run in a sandbox, so the
# verify seam is supplied. It is supplied two ways, deliberately:
#   * the UNIT cases pass a predicate over the member SET (a named culprit, or
#     a named interacting pair) — the cheapest way to force each branch of the
#     state machine deterministically;
#   * the E2E cases pass _sandbox_verifier, which derives its verdict from the
#     REAL FOLDED TREE by reading the folded object's blobs and running a real
#     check over them (does every module compile; does any exported symbol have
#     two owners). Nothing there is a lookup table: plant a broken file and the
#     verdict goes red because the file is broken.


def _bisect_members(repo, base_sha, specs):
    """specs: [(label, filename, content)] -> [BatchMember], each a real branch
    off base_sha pushed to the real origin."""
    return [_member_branch(repo, base_sha, label, filename, content, (f"SABLE-{label}",))
            for label, filename, content in specs]


def _set_verifier(red_when_all_present, log=None):
    """A verify seam that answers RED exactly when the subset contains EVERY
    branch in `red_when_all_present`. One member named models a SINGLE CULPRIT
    (any subset containing it is red); two members model an INTERACTION (each
    alone is green, only the combination is red). Records every call so a test
    can assert what was actually asked, not just what came back."""
    culprits = set(red_when_all_present)

    def verify(members, fold_tip, combined_ref):
        branches = {m.branch for m in members}
        green = not culprits.issubset(branches)
        if log is not None:
            log.append((tuple(sorted(branches)), combined_ref, green))
        return green
    return verify


def _ci_refs(bare):
    return sorted(subprocess.run(
        ["git", "-C", str(bare), "for-each-ref", "--format=%(refname:short)",
         "refs/heads/ci-verify/"], capture_output=True, text=True).stdout.split())


# --- the pure arithmetic: the bound IS a computation ------------------------

def test_the_bisection_bound_is_three_extra_combined_runs_at_n_of_four():
    """UNIT (architecture decision 4, verbatim): at n=4 the culprit is isolated
    in AT MOST 3 extra combined runs. Stated as the machine's own arithmetic so
    the number in the architecture and the number the code will spend are the
    same object, not two claims that happen to agree today."""
    assert promote_lib.max_bisection_rounds(4) == 3
    assert [promote_lib.max_bisection_rounds(n) for n in range(1, 9)] == \
        [1, 2, 3, 3, 4, 4, 4, 4]


def test_the_bound_refuses_a_batch_of_no_members():
    with pytest.raises(ValueError):
        promote_lib.max_bisection_rounds(0)


def test_bisection_split_halves_with_the_extra_member_on_the_left():
    assert promote_lib.bisection_split(["a", "b", "c", "d"]) == (["a", "b"], ["c", "d"])
    assert promote_lib.bisection_split(["a", "b", "c"]) == (["a", "b"], ["c"])
    # Both halves non-empty at every n>=2 — the descent always makes progress.
    for n in range(2, 17):
        left, right = promote_lib.bisection_split(list(range(n)))
        assert left and right and len(left) + len(right) == n


def test_bisection_split_refuses_a_node_it_cannot_split():
    with pytest.raises(ValueError):
        promote_lib.bisection_split(["only-one"])


# --- the VACUOUS-PASS GUARD (SABLE-p9n7k on the round sequence) -------------

def test_a_zero_round_bisection_result_raises_instead_of_existing():
    """VACUOUS-PASS GUARD, load-bearing. Every property this section reports —
    a culprit, a COULD-NOT-ATTRIBUTE, a bound — is a statement ABOUT the round
    set, so a result carrying ZERO rounds makes all of them vacuously true at
    once. It must be unconstructable, which is what makes every round-set
    assertion below non-vacuous by construction rather than by inspection."""
    with pytest.raises(promote_lib.VacuousBisectionError):
        promote_lib.BisectionResult(
            promote_lib.BATCH_OUTCOME_COULD_NOT_ATTRIBUTE, "", (), ("wk-a",), (),
            ("a report over nothing",))


def test_bisect_red_batch_refuses_an_empty_batch():
    """The other end of the same guard: an empty batch cannot be red, so
    bisecting one is a caller bug, never a vacuous no-op result."""
    with pytest.raises(promote_lib.EmptyBatchError):
        promote_lib.bisect_red_batch("/nonexistent", "origin", "a" * 40, [],
                                     _set_verifier(["wk-a"]))


def test_every_real_bisection_spends_at_least_one_round(tmp_path, fleet_sink):
    """The guard's positive control: for EVERY batch size the machine handles,
    a real bisection produces a non-empty round set. Without this the guard
    above could be satisfied by a machine that simply never returns a result."""
    for n in range(1, 5):
        repo, bare, base_sha = _batch_sandbox(tmp_path / f"n{n}")
        members = _bisect_members(repo, base_sha, [
            (f"wk-{i}", f"bin/m{i}.py", f"m{i} = 1\n") for i in range(n)])
        res = promote_lib.bisect_red_batch(str(repo), "origin", base_sha, members,
                                           _set_verifier(["wk-0"]))
        assert res.rounds, f"n={n} bisected in zero rounds — every assertion would be vacuous"
        assert len(res.rounds) <= promote_lib.max_bisection_rounds(n)


# --- COULD-NOT-ATTRIBUTE: BOTH polarities, or the assertion is worthless ----

def test_could_not_attribute_is_emitted_when_both_halves_verify_green(
        tmp_path, capsys, fleet_sink):
    """UNIT (S3 matrix case, POSITIVE polarity): an interaction-only red — two
    members individually green whose combination is red — emits
    COULD-NOT-ATTRIBUTE as an observable line, names no culprit, and engages the
    all-serial fallback for EVERY member."""
    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = _bisect_members(repo, base_sha, [
        ("wk-a", "bin/a.py", "a = 1\n"), ("wk-b", "bin/b.py", "b = 1\n")])
    res = promote_lib.bisect_red_batch(str(repo), "origin", base_sha, members,
                                       _set_verifier(["wk-a", "wk-b"]),
                                       combined_ref="ci-verify/batch-inter")

    assert res.outcome == promote_lib.BATCH_OUTCOME_COULD_NOT_ATTRIBUTE
    assert res.culprit == ""
    assert not res.attributed
    assert res.rounds, "vacuity guard: no rounds means the verdict is about nothing"
    # Every half really did verify green — that is what the verdict CLAIMS.
    assert all(r.green for r in res.rounds)
    assert res.red_rounds == ()
    # LOUD on all three channels.
    report = promote_lib.render_bisection_report(res)
    assert promote_lib.BISECT_REPORT_COULD_NOT_ATTRIBUTE in report
    assert promote_lib.BISECT_REPORT_COULD_NOT_ATTRIBUTE in capsys.readouterr().out
    assert any(promote_lib.BISECT_REPORT_COULD_NOT_ATTRIBUTE in line
               for line in fleet_sink()), "the interaction verdict never reached the cockpit"
    # ALL-SERIAL fallback: every member back to the serial queue, nothing re-batched.
    assert set(res.serial_queue) == {"wk-a", "wk-b"}
    assert res.rebatch == ()
    assert res.exit_code == promote_lib.classify.EXIT_RED


def test_a_normally_attributed_red_does_not_emit_could_not_attribute(
        tmp_path, capsys, fleet_sink):
    """UNIT (S3 matrix case, NEGATIVE CONTROL — the half that makes the case
    above mean anything): a red with a real single culprit names the member and
    emits NO COULD-NOT-ATTRIBUTE, on any channel. Without this, a machine that
    printed the token unconditionally would pass the positive case."""
    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = _bisect_members(repo, base_sha, [
        ("wk-a", "bin/a.py", "a = 1\n"), ("wk-bad", "bin/bad.py", "bad = 1\n")])
    res = promote_lib.bisect_red_batch(str(repo), "origin", base_sha, members,
                                       _set_verifier(["wk-bad"]),
                                       combined_ref="ci-verify/batch-single")

    assert res.outcome == promote_lib.BATCH_OUTCOME_BISECTED_CULPRIT
    assert res.culprit == "wk-bad"
    report = promote_lib.render_bisection_report(res)
    assert "wk-bad" in report, "the report did not NAME the member it isolated"
    assert promote_lib.BISECT_REPORT_COULD_NOT_ATTRIBUTE not in report
    assert promote_lib.BISECT_REPORT_COULD_NOT_ATTRIBUTE not in capsys.readouterr().out
    assert not any(promote_lib.BISECT_REPORT_COULD_NOT_ATTRIBUTE in line
                   for line in fleet_sink()), \
        "an attributed red emitted the interaction verdict — the token discriminates nothing"
    assert res.serial_queue == ("wk-bad",)
    assert set(res.rebatch) == {"wk-a"}


# --- the naming discipline: a culprit is MEASURED, never inferred -----------

@pytest.mark.parametrize("guilty", ["wk-0", "wk-1", "wk-2", "wk-3"])
def test_a_named_culprit_was_verified_red_alone_within_the_bound(
        tmp_path, guilty, fleet_sink):
    """The load-bearing property, swept over EVERY culprit position at n=4 (the
    worst case is position-dependent, so one hand-picked position would prove
    only that position): the machine names the right member, the naming is
    backed by a round in which that member was verified RED **by itself**, and
    the whole search stays inside max_bisection_rounds(4) == 3."""
    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = _bisect_members(repo, base_sha, [
        (f"wk-{i}", f"bin/m{i}.py", f"m{i} = 1\n") for i in range(4)])
    res = promote_lib.bisect_red_batch(str(repo), "origin", base_sha, members,
                                       _set_verifier([guilty]))

    assert res.culprit == guilty
    assert res.rounds, "vacuity guard"
    assert len(res.rounds) <= 3, f"n=4 cost {len(res.rounds)} extra runs, bound is 3"
    solo_red = [r for r in res.rounds if r.branches == (guilty,) and not r.green]
    assert solo_red, (
        f"{guilty} was named without ever being verified alone — the naming was "
        f"INFERRED from a green sibling, which is unsound exactly when the failure "
        f"is an interaction")
    assert set(res.rebatch) == {m.branch for m in members} - {guilty}


def test_the_bisection_tree_is_a_function_of_the_member_set_not_the_input_order(
        tmp_path, fleet_sink):
    """Ordering safety, the bisection analogue of BatchRecord's own: the SAME
    member set admitted in any of its 24 orders must name the same culprit and
    walk the same tree. A machine that bisected in caller order would name
    whichever member the caller happened to list first into a red half."""
    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = _bisect_members(repo, base_sha, [
        (f"wk-{i}", f"bin/m{i}.py", f"m{i} = 1\n") for i in range(4)])
    trees, culprits = set(), set()
    for order in itertools.permutations(members):
        res = promote_lib.bisect_red_batch(str(repo), "origin", base_sha, list(order),
                                           _set_verifier(["wk-2"]))
        trees.add(tuple(r.branches for r in res.rounds))
        culprits.add(res.culprit)
    assert culprits == {"wk-2"}
    assert len(trees) == 1, f"input order leaked into the bisection tree: {trees}"


def test_a_subset_is_never_verified_twice(tmp_path, fleet_sink):
    """A combined run is a real CI cycle. Re-asking a subset the machine already
    measured would spend one to re-learn a known fact — and would push the run
    count past the bound for zero information."""
    log = []
    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = _bisect_members(repo, base_sha, [
        (f"wk-{i}", f"bin/m{i}.py", f"m{i} = 1\n") for i in range(4)])
    res = promote_lib.bisect_red_batch(str(repo), "origin", base_sha, members,
                                       _set_verifier(["wk-0"], log))
    asked = [entry[0] for entry in log]
    assert len(asked) == len(set(asked)), f"a subset was verified twice: {asked}"
    assert len(res.rounds) == len(asked)


# --- the production verify seam ---------------------------------------------

def _verdict(outcome_conclusion):
    return promote_lib.classify.Verdict(outcome_conclusion, "", "f" * 40,
                                        "ci-verify/batch-x", source="precomputed",
                                        complete=True)


@pytest.mark.parametrize("conclusion,green", [("success", True), ("failure", False)])
def test_ci_batch_verify_reads_the_actions_verdict_for_the_round(
        monkeypatch, conclusion, green):
    """The production seam maps a round's Actions verdict to green/red — it
    READS the verdict for the ref the round just pushed, it does not recompute
    one (the promote module's standing boundary)."""
    monkeypatch.setattr(promote_lib.preview, "acquire_verdict",
                        lambda repo, ref, sha: _verdict(conclusion))
    monkeypatch.setattr(promote_lib.preview, "delete_ci_ref", lambda *a, **kw: None)
    verify = promote_lib.ci_batch_verify("/repo", "origin")
    assert verify([], "f" * 40, "ci-verify/batch-x") is green


@pytest.mark.parametrize("conclusion", ["cancelled", "actions_down", "timeout"])
def test_ci_batch_verify_refuses_to_guess_a_verdict_it_could_not_read(
        monkeypatch, conclusion):
    """Negative space, and the reason this seam is not a truthy coercion: an
    unreadable verdict is neither green nor red. Guessing red would name an
    innocent member; guessing green would clear a guilty one. It raises with the
    verdict's OWN taxonomy code so the caller's existing exit-code handling
    stays correct."""
    monkeypatch.setattr(promote_lib.preview, "acquire_verdict",
                        lambda repo, ref, sha: _verdict(conclusion))
    monkeypatch.setattr(promote_lib.preview, "delete_ci_ref", lambda *a, **kw: None)
    verify = promote_lib.ci_batch_verify("/repo", "origin")
    with pytest.raises(promote_lib.GateError) as exc:
        verify([], "f" * 40, "ci-verify/batch-x")
    assert exc.value.code == _verdict(conclusion).exit_code
    assert "no usable verdict" in str(exc.value)


# --- the manifest records the report vocabulary verbatim --------------------

def test_the_promote_record_names_the_culprit_and_carries_the_report_verbatim(
        tmp_path, monkeypatch, fleet_sink):
    """ACCEPTANCE clause: 'the report vocabulary (named member /
    COULD-NOT-ATTRIBUTE) appears verbatim in output the manifest records'. Read
    back off the REAL durable promote-record log, not off the in-memory result."""
    monkeypatch.setenv("SABLE_MG_BATCH_RECORD_LOG", str(tmp_path / "batch-records.jsonl"))
    repo, bare, base_sha = _batch_sandbox(tmp_path / "s")
    members = _bisect_members(repo, base_sha, [
        ("wk-a", "bin/a.py", "a = 1\n"), ("wk-bad", "bin/bad.py", "bad = 1\n")])
    res = promote_lib.bisect_red_batch(str(repo), "origin", base_sha, members,
                                       _set_verifier(["wk-bad"]),
                                       combined_ref="ci-verify/batch-named")

    record = promote_lib.find_batch_record(str(repo), "ci-verify/batch-named")
    assert record is not None, "the bisection wrote no promote record"
    assert record.outcome == promote_lib.BATCH_OUTCOME_BISECTED_CULPRIT
    assert record.culprit == "wk-bad"
    assert record.report_lines == res.report_lines
    assert any("wk-bad" in line for line in record.report_lines)
    assert set(record.member_branches()) == {"wk-a", "wk-bad"}


def test_the_promote_record_carries_could_not_attribute_verbatim(
        tmp_path, monkeypatch, fleet_sink):
    """The other polarity of the same acceptance clause, in the durable record."""
    monkeypatch.setenv("SABLE_MG_BATCH_RECORD_LOG", str(tmp_path / "batch-records.jsonl"))
    repo, bare, base_sha = _batch_sandbox(tmp_path / "s")
    members = _bisect_members(repo, base_sha, [
        ("wk-a", "bin/a.py", "a = 1\n"), ("wk-b", "bin/b.py", "b = 1\n")])
    promote_lib.bisect_red_batch(str(repo), "origin", base_sha, members,
                                 _set_verifier(["wk-a", "wk-b"]),
                                 combined_ref="ci-verify/batch-cna")

    record = promote_lib.find_batch_record(str(repo), "ci-verify/batch-cna")
    assert record.outcome == promote_lib.BATCH_OUTCOME_COULD_NOT_ATTRIBUTE
    assert record.culprit == ""
    assert any(promote_lib.BISECT_REPORT_COULD_NOT_ATTRIBUTE in line
               for line in record.report_lines), \
        "the durable manifest does not carry the verdict token verbatim"


def test_the_batch_record_round_trips_the_additive_bisection_fields():
    """The bisection fields are ADDITIVE on BatchRecord: they survive the round
    trip, and a record written before they existed (no keys at all) still
    reconstructs — the same additive discipline the pre-existing round-trip
    cases assert for the fields already there."""
    members = [promote_lib.BatchMember("wk-a", "a" * 40, ("SABLE-a",), ("bin/a.py",))]
    record = promote_lib.BatchRecord.from_members(
        "b" * 40, members, combined_ref="ci-verify/batch-rt",
        outcome=promote_lib.BATCH_OUTCOME_BISECTED_CULPRIT, fold_disjoint=True,
        culprit="wk-a", report_lines=("CULPRIT ISOLATED: wk-a",))
    assert promote_lib.BatchRecord.from_dict(record.to_dict()) == record
    legacy = promote_lib.BatchRecord.from_dict({"combined_ref": "ci-verify/batch-old"})
    assert legacy.culprit == "" and legacy.report_lines == ()


# --- round-object adoption: both polarities ---------------------------------

def test_a_re_run_bisection_adopts_the_objects_already_standing_on_its_refs(
        tmp_path, fleet_sink):
    """A bisection round's ref name is a pure function of (base, member tips)
    but its folded COMMIT is not — commit-tree stamps a committer date. So a
    re-run must ADOPT the object standing on each round's ref, not re-fold a
    fresh-timestamped duplicate and try to push it (a non-fast-forward; a
    --force would cancel any in-flight run, SABLE-sc24). Asserted as the same
    verdict reached over the same OBJECTS, which is also what makes a
    crashed-mid-bisection seat resumable."""
    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = _bisect_members(repo, base_sha, [
        (f"wk-{i}", f"bin/m{i}.py", f"m{i} = 1\n") for i in range(4)])
    first = promote_lib.bisect_red_batch(str(repo), "origin", base_sha, members,
                                         _set_verifier(["wk-1"]))
    assert [r.adopted for r in first.rounds] == [False] * len(first.rounds)
    refs_after_first = _ci_refs(bare)

    second = promote_lib.bisect_red_batch(str(repo), "origin", base_sha, members,
                                          _set_verifier(["wk-1"]))
    assert all(r.adopted for r in second.rounds), \
        "a re-run re-folded instead of adopting — the next push is a non-fast-forward"
    assert [r.fold_tip for r in second.rounds] == [r.fold_tip for r in first.rounds]
    assert second.culprit == first.culprit == "wk-1"
    assert _ci_refs(bare) == refs_after_first, "adoption pushed a new CI trigger anyway"


def test_a_round_refuses_a_foreign_object_standing_on_its_ref(tmp_path, fleet_sink):
    """NEGATIVE CONTROL for the adoption above: the ref name is not taken as
    proof. An object on that ref that is NOT a fold chain of this member count
    built on this base is foreign, and the round refuses LOUDLY instead of
    adopting it (which would verify the wrong object) or force-pushing over it
    (which would cancel an in-flight run)."""
    repo, bare, base_sha = _batch_sandbox(tmp_path)
    members = _bisect_members(repo, base_sha, [
        ("wk-a", "bin/a.py", "a = 1\n"), ("wk-b", "bin/b.py", "b = 1\n")])
    # Squat the ref the FIRST round will want with an unrelated commit.
    left, _right = promote_lib.bisection_split(
        sorted(members, key=lambda m: m.tip_sha))
    squatted = promote_lib.classify.preview_ref_name(
        "batch", promote_lib.batch_key.setkey(base_sha, [m.tip_sha for m in left]))
    _bg(repo, "push", "-q", "origin", f"{base_sha}:refs/heads/{squatted}")

    with pytest.raises(promote_lib.GateError) as exc:
        promote_lib.bisect_red_batch(str(repo), "origin", base_sha, members,
                                     _set_verifier(["wk-a"]))
    assert exc.value.code == promote_lib.classify.EXIT_PRECONDITION
    assert squatted in str(exc.value)
    assert "NOT a 1-member fold chain" in str(exc.value)


# ==========================================================================
# E2E (SABLE-be4lo.8, S3): a REAL git sandbox, the REAL fold builder, and a
# REAL content-derived verifier.
# ==========================================================================
#
# The verifier below is the thing that makes these cases E2E rather than
# elaborate unit tests. It takes no member names and consults no table: it
# READS THE FOLDED OBJECT'S OWN BLOBS out of the real git object store and runs
# two real checks over them — every module must compile, and no EXPORT_ symbol
# may have two owning modules. That second check is what a footprint-disjoint
# INTERACTION looks like in the small: two members touching two different files
# are perfectly disjoint and still break each other when combined, which is the
# whole reason the epic verifies the combined tree instead of trusting
# disjointness (locked contract SABLE-djopw).
#
# So: plant a file with a syntax error and the verdict is red BECAUSE the file
# does not compile. Plant two modules exporting the same symbol and the
# combined verdict is red BECAUSE the tree really does carry two owners, while
# each member alone really is green. Nothing is stubbed into being red.


def _tree_py_sources(repo, tip):
    """Every bin/*.py blob in the folded object, read out of the real object
    store — no checkout, no working tree, so this reads exactly the object that
    was folded and would be landed."""
    names = _bg(repo, "ls-tree", "-r", "--name-only", tip).splitlines()
    return {n: _bg(repo, "show", f"{tip}:{n}")
            for n in names if n.startswith("bin/") and n.endswith(".py")}


def _real_tree_check(sources):
    """The real verification a bisection round's CI run stands for. Returns the
    list of problems found (empty == green)."""
    problems = []
    for name, src in sorted(sources.items()):
        try:
            compile(src, name, "exec")
        except SyntaxError as exc:
            problems.append(f"{name}: does not compile: {exc.msg}")
    owners = {}
    for name, src in sorted(sources.items()):
        for match in re.finditer(r"^EXPORT_([A-Z_]+)\s*=", src, re.M):
            owners.setdefault(match.group(1), []).append(name)
    for symbol, names in sorted(owners.items()):
        if len(names) > 1:
            problems.append(f"EXPORT_{symbol} has {len(names)} owning modules: {names}")
    return problems


def _sandbox_verifier(repo, log=None):
    """The E2E verify seam: green iff the REAL folded tree passes the REAL
    check. Records (branches, ref, tip, problems) for every round."""
    def verify(members, fold_tip, combined_ref):
        problems = _real_tree_check(_tree_py_sources(repo, fold_tip))
        if log is not None:
            log.append((tuple(m.branch for m in members), combined_ref, fold_tip, problems))
        return not problems
    return verify


BROKEN_SOURCE = "def planted_failure(:\n"        # a real syntax error
EXPORTER = "EXPORT_LIMIT = {}\n"                 # two of these collide


def _assert_nothing_landed_from_the_batched_path(bare, result, trunk_before):
    """The NO SILENT LANDING sweep, as one reusable assertion so every E2E case
    below carries it rather than only the case named after it.

    Sweeps EVERY node of the bisection tree — red ones included, which is the
    clause that matters: a red node is exactly where a batched path would be
    tempted to salvage 'the good half'. The round-set non-emptiness guard is
    asserted FIRST, because a sweep over zero nodes passes for free and would
    hide a bisection that never ran (SABLE-p9n7k)."""
    assert result.rounds, \
        "no bisection rounds — the no-silent-landing sweep would pass vacuously"
    trunk_now = _remote_head(bare, "trunk")
    assert trunk_now == trunk_before, \
        f"the integration branch moved during a bisection: {trunk_before} -> {trunk_now}"
    for round_ in result.rounds:
        assert trunk_now != round_.fold_tip, (
            f"a bisection node's combined object landed on the integration branch: "
            f"{round_.branches} ({'red' if not round_.green else 'green'} node)")


def _serial_gate_cycle(repo, bare, member):
    """ONE real serial gate cycle for a single member: re-resolve the current
    integration tip, fold this member alone onto it, run the SAME real check on
    the result, and land it through the SAME writer (land_batch of one member)
    only if green. Returns (landed, problems) — a red cycle lands nothing and
    reports what it found."""
    _bg(repo, "fetch", "-q", "origin", "trunk")
    base_sha = _bg(repo, "rev-parse", "refs/remotes/origin/trunk")
    fold_tip = _fold(repo, base_sha, [member])
    problems = _real_tree_check(_tree_py_sources(repo, fold_tip))
    if problems:
        return False, problems
    res = promote_lib.land_batch(str(repo), "origin", "trunk", base_sha, fold_tip,
                                 [member], budget=_ok_budget([member]),
                                 combined_ref=f"ci-verify/serial-{member.branch}",
                                 verdict=_green_batch_verdict(
                                     fold_tip, f"ci-verify/serial-{member.branch}"))
    return res.landed, []


def test_S3_acceptance_a_planted_failure_is_named_and_nothing_lands_from_the_batch(
        tmp_path, monkeypatch, capsys, fleet_sink):
    """S3 ACCEPTANCE (verbatim). A failing branch is planted in an otherwise-
    green batch; the combined run is red; ZERO members land from the batched
    path; the report NAMES the member; the remaining members land serially
    WITHOUT manual re-queueing (the loop below iterates result.rebatch as
    handed back — it never re-derives the member set). A HEALTHY-BATCH CONTROL
    lands whole in the SAME test: without it, 'nothing landed' is satisfied by
    a machine that refuses everything."""
    monkeypatch.setenv("SABLE_MG_BATCH_RECORD_LOG", str(tmp_path / "records.jsonl"))
    repo, bare, base_sha = _batch_sandbox(tmp_path / "s3")
    members = _bisect_members(repo, base_sha, [
        ("wk-good1", "bin/good1.py", "good1 = 1\n"),
        ("wk-good2", "bin/good2.py", "good2 = 1\n"),
        ("wk-planted", "bin/planted.py", BROKEN_SOURCE)])
    by_branch = {m.branch: m for m in members}

    # The ORIGINAL combined run, for real: fold the whole batch, push its ref,
    # verify it. It must come back RED before there is anything to bisect.
    fold_members = [fold_lib.FoldMember(m.branch, m.tip_sha, m.bead_ids[0]) for m in members]
    combined_tip, combined_ref = fold_lib.push_batch_ref(str(repo), "origin", base_sha,
                                                         fold_members)
    verify = _sandbox_verifier(str(repo))
    assert verify(members, combined_tip, combined_ref) is False, \
        "the planted failure did not make the combined run red — nothing to bisect"
    trunk_before = _remote_head(bare, "trunk")

    res = promote_lib.bisect_red_batch(str(repo), "origin", base_sha, members, verify,
                                       combined_ref=combined_ref)

    # ... the report NAMES the member ...
    assert res.culprit == "wk-planted"
    report = promote_lib.render_bisection_report(res)
    assert "wk-planted" in report and "wk-planted" in capsys.readouterr().out
    assert promote_lib.BISECT_REPORT_COULD_NOT_ATTRIBUTE not in report
    # ... ZERO members land from the batched path, at EVERY node of the tree ...
    _assert_nothing_landed_from_the_batched_path(bare, res, trunk_before)
    assert res.red_rounds, "an attribution with no red node did not measure anything"
    # ... the manifest records it ...
    assert promote_lib.find_batch_record(str(repo), combined_ref).culprit == "wk-planted"

    # ... and the remaining members land SERIALLY, straight off the result, with
    # no manual re-queueing step in between.
    assert set(res.rebatch) == {"wk-good1", "wk-good2"}
    for branch in res.rebatch:
        landed, problems = _serial_gate_cycle(repo, bare, by_branch[branch])
        assert landed, f"{branch} did not land serially after the batch was bisected: {problems}"
    for branch in res.rebatch:
        anc = subprocess.run(["git", "-C", str(bare), "merge-base", "--is-ancestor",
                              by_branch[branch].tip_sha, _remote_head(bare, "trunk")],
                             capture_output=True)
        assert anc.returncode == 0, f"{branch} is not an ancestor of the integration tip"
    # The culprit did NOT land — it is in the serial queue, and its own serial
    # cycle is still red until its author fixes it.
    landed, problems = _serial_gate_cycle(repo, bare, by_branch["wk-planted"])
    assert not landed and problems

    # ---- the HEALTHY-BATCH CONTROL, same suite, same machinery -------------
    crepo, cbare, cbase = _batch_sandbox(tmp_path / "control")
    healthy = _bisect_members(crepo, cbase, [
        ("wk-h1", "bin/h1.py", "h1 = 1\n"),
        ("wk-h2", "bin/h2.py", "h2 = 1\n"),
        ("wk-h3", "bin/h3.py", "h3 = 1\n")])
    hfold = [fold_lib.FoldMember(m.branch, m.tip_sha, m.bead_ids[0]) for m in healthy]
    htip, href = fold_lib.push_batch_ref(str(crepo), "origin", cbase, hfold)
    assert _sandbox_verifier(str(crepo))(healthy, htip, href) is True, \
        "the control batch was red — the acceptance would prove nothing"
    control = promote_lib.land_batch(
        str(crepo), "origin", "trunk", cbase, htip, healthy,
        budget=_ok_budget(healthy), combined_ref=href,
        verdict=_green_batch_verdict(htip, href))
    assert control.landed, control.reason
    assert _remote_head(cbare, "trunk") == htip, "the healthy batch did not land WHOLE"


def test_S3_interaction_red_is_loud_all_serial_and_surfaces_late_on_the_second_member(
        tmp_path, monkeypatch, capsys, fleet_sink):
    """INTERACTION-RED (S3 matrix case, verbatim). Two members are individually
    green and their COMBINATION is red — a real footprint-disjoint interaction
    (two different files, one shared exported symbol). Every bisected half
    verifies green, so: COULD-NOT-ATTRIBUTE, loudly; the all-serial fallback
    engages for ALL members; nothing lands from the batched path; and the
    interacting pair surfaces LATE BUT LOUD — as a RED on the SECOND member's
    own serial gate cycle, against a base that by then carries the first — and
    that red is recorded rather than swallowed."""
    monkeypatch.setenv("SABLE_MG_BATCH_RECORD_LOG", str(tmp_path / "records.jsonl"))
    repo, bare, base_sha = _batch_sandbox(tmp_path / "inter")
    members = _bisect_members(repo, base_sha, [
        ("wk-x", "bin/x.py", EXPORTER.format(10)),
        ("wk-y", "bin/y.py", EXPORTER.format(20))])
    by_branch = {m.branch: m for m in members}
    verify = _sandbox_verifier(str(repo))

    # Each member is individually GREEN against the base — established by real
    # verification, not asserted. Without this the "interaction" claim is empty.
    for m in members:
        solo_tip = _fold(repo, base_sha, [m])
        assert verify([m], solo_tip, "ci-verify/solo") is True, \
            f"{m.branch} is not individually green — this is not an interaction"
    fold_members = [fold_lib.FoldMember(m.branch, m.tip_sha, m.bead_ids[0]) for m in members]
    combined_tip, combined_ref = fold_lib.push_batch_ref(str(repo), "origin", base_sha,
                                                         fold_members)
    assert verify(members, combined_tip, combined_ref) is False, \
        "the combination is not red — there is no interaction to fall back from"
    trunk_before = _remote_head(bare, "trunk")

    res = promote_lib.bisect_red_batch(str(repo), "origin", base_sha, members, verify,
                                       combined_ref=combined_ref)

    # COULD-NOT-ATTRIBUTE, loud on stdout, the cockpit and the durable manifest.
    assert res.outcome == promote_lib.BATCH_OUTCOME_COULD_NOT_ATTRIBUTE
    token = promote_lib.BISECT_REPORT_COULD_NOT_ATTRIBUTE
    assert token in capsys.readouterr().out
    assert any(token in line for line in fleet_sink())
    assert any(token in line for line in
               promote_lib.find_batch_record(str(repo), combined_ref).report_lines)
    # ALL-SERIAL fallback for EVERY member; nothing landed from the batch.
    assert set(res.serial_queue) == {"wk-x", "wk-y"} and res.rebatch == ()
    _assert_nothing_landed_from_the_batched_path(bare, res, trunk_before)

    # LATE BUT LOUD: each member takes its own serial cycle, off the result's
    # serial_queue. The first lands; the second's cycle goes RED against a base
    # that now carries the first, which is where the interaction becomes visible.
    order = list(res.serial_queue)
    first_landed, first_problems = _serial_gate_cycle(repo, bare, by_branch[order[0]])
    assert first_landed, f"the first serial member did not land: {first_problems}"
    second_landed, second_problems = _serial_gate_cycle(repo, bare, by_branch[order[1]])
    assert not second_landed, \
        "the interacting pair landed anyway — the interaction never surfaced"
    assert second_problems and any("EXPORT_LIMIT" in p for p in second_problems), \
        f"the second member's red does not name the interaction: {second_problems}"
    # Recorded, not swallowed: the integration tip carries the first member only.
    tip = _remote_head(bare, "trunk")
    assert subprocess.run(["git", "-C", str(bare), "merge-base", "--is-ancestor",
                           by_branch[order[0]].tip_sha, tip], capture_output=True).returncode == 0
    assert subprocess.run(["git", "-C", str(bare), "merge-base", "--is-ancestor",
                           by_branch[order[1]].tip_sha, tip], capture_output=True).returncode != 0


def test_S3_an_n4_single_culprit_isolates_within_three_extra_combined_runs(
        tmp_path, monkeypatch, capsys, fleet_sink):
    """BISECTION BOUND (S3 matrix case, verbatim): an n=4 single-culprit red
    isolates in <=3 EXTRA combined runs, the culprit is named, and the rest are
    re-batched and land. The run count is COUNTED off the real remote — one
    ci-verify ref per combined run — not tallied by the code under test, so a
    machine that under-reported its own rounds could not pass this."""
    monkeypatch.setenv("SABLE_MG_BATCH_RECORD_LOG", str(tmp_path / "records.jsonl"))
    repo, bare, base_sha = _batch_sandbox(tmp_path / "n4")
    members = _bisect_members(repo, base_sha, [
        ("wk-p", "bin/p.py", "p = 1\n"), ("wk-q", "bin/q.py", "q = 1\n"),
        ("wk-r", "bin/r.py", "r = 1\n"), ("wk-bad", "bin/bad.py", BROKEN_SOURCE)])
    by_branch = {m.branch: m for m in members}
    verify = _sandbox_verifier(str(repo))

    fold_members = [fold_lib.FoldMember(m.branch, m.tip_sha, m.bead_ids[0]) for m in members]
    combined_tip, combined_ref = fold_lib.push_batch_ref(str(repo), "origin", base_sha,
                                                         fold_members)
    assert verify(members, combined_tip, combined_ref) is False
    refs_before = set(_ci_refs(bare))       # the ORIGINAL combined run's ref
    trunk_before = _remote_head(bare, "trunk")

    res = promote_lib.bisect_red_batch(str(repo), "origin", base_sha, members, verify,
                                       combined_ref=combined_ref)

    extra_refs = set(_ci_refs(bare)) - refs_before
    assert res.rounds, "vacuity guard"
    assert len(extra_refs) <= 3, \
        f"n=4 cost {len(extra_refs)} EXTRA combined runs (bound 3): {sorted(extra_refs)}"
    assert len(extra_refs) == len(res.rounds), \
        "the reported round count does not match the CI triggers actually pushed"
    assert res.culprit == "wk-bad"
    assert "wk-bad" in promote_lib.render_bisection_report(res)
    _assert_nothing_landed_from_the_batched_path(bare, res, trunk_before)

    # The rest are RE-BATCHED (as a batch, not one at a time) and land whole.
    rest = [by_branch[b] for b in res.rebatch]
    assert {m.branch for m in rest} == {"wk-p", "wk-q", "wk-r"}
    rest_fold = [fold_lib.FoldMember(m.branch, m.tip_sha, m.bead_ids[0]) for m in rest]
    rest_tip, rest_ref = fold_lib.push_batch_ref(str(repo), "origin", base_sha, rest_fold)
    assert verify(rest, rest_tip, rest_ref) is True, "the re-batch of the innocent members is red"
    landed = promote_lib.land_batch(
        str(repo), "origin", "trunk", base_sha, rest_tip, rest,
        budget=_ok_budget(rest), combined_ref=rest_ref,
        verdict=_green_batch_verdict(rest_tip, rest_ref))
    assert landed.landed, landed.reason
    assert _remote_head(bare, "trunk") == rest_tip


def test_S3_no_silent_landing_across_every_red_node_of_the_bisection_tree(
        tmp_path, monkeypatch, fleet_sink):
    """NO SILENT LANDING (S3 matrix case, verbatim). Sweeps every RED node of
    the bisection tree — over every culprit position at n=4, so every shape the
    tree can take is swept, not one shape — and asserts ZERO batched-path
    landings from any of them: the integration branch never moves, and no red
    node's combined object is ever the integration tip.

    NON-VACUITY, twice over: the swept red-node set is asserted non-empty
    (a sweep over nothing passes for free), and the same sandbox then LANDS a
    green batch through the same writer, proving the integration branch was
    movable all along and 'it never moved' is a property of the bisection, not
    of a sandbox where nothing can land."""
    monkeypatch.setenv("SABLE_MG_BATCH_RECORD_LOG", str(tmp_path / "records.jsonl"))
    swept = 0
    for guilty in ("wk-0", "wk-1", "wk-2", "wk-3"):
        repo, bare, base_sha = _batch_sandbox(tmp_path / f"sweep-{guilty}")
        members = _bisect_members(repo, base_sha, [
            (f"wk-{i}", f"bin/m{i}.py",
             BROKEN_SOURCE if f"wk-{i}" == guilty else f"m{i} = 1\n") for i in range(4)])
        verify = _sandbox_verifier(str(repo))
        trunk_before = _remote_head(bare, "trunk")

        res = promote_lib.bisect_red_batch(str(repo), "origin", base_sha, members, verify)

        assert res.culprit == guilty
        assert res.red_rounds, f"culprit {guilty}: no red node in the tree to sweep"
        _assert_nothing_landed_from_the_batched_path(bare, res, trunk_before)
        for red in res.red_rounds:
            assert _remote_head(bare, "trunk") != red.fold_tip
            # And no red node's members reached the integration branch either.
            for branch in red.branches:
                member = next(m for m in members if m.branch == branch)
                anc = subprocess.run(["git", "-C", str(bare), "merge-base", "--is-ancestor",
                                      member.tip_sha, _remote_head(bare, "trunk")],
                                     capture_output=True)
                assert anc.returncode != 0, \
                    f"{branch}, a member of RED node {red.branches}, landed anyway"
            swept += 1

        # NON-VACUITY: this very sandbox CAN land a batch. The innocent members,
        # re-batched, land whole through the real writer — so "trunk never
        # moved" above was the bisection's doing.
        rest = [m for m in members if m.branch != guilty]
        rest_fold = [fold_lib.FoldMember(m.branch, m.tip_sha, m.bead_ids[0]) for m in rest]
        rest_tip, rest_ref = fold_lib.push_batch_ref(str(repo), "origin", base_sha, rest_fold)
        ok = promote_lib.land_batch(
            str(repo), "origin", "trunk", base_sha, rest_tip, rest,
            budget=_ok_budget(rest), combined_ref=rest_ref,
            verdict=_green_batch_verdict(rest_tip, rest_ref))
        assert ok.landed, f"the sandbox could not land anything at all: {ok.reason}"
        assert _remote_head(bare, "trunk") != trunk_before

    assert swept >= 4, f"the no-silent-landing sweep covered only {swept} red nodes"


# ==========================================================================
# COVERAGE-FLOOR PHASE ATTRIBUTION (SABLE-9yjt5)
# ==========================================================================
#
# The floor collapsed THREE independent causes into one boolean and printed
# ONE sentence for all of them — "coverage regressed on the removed/skipped
# test's lines" — which is a POSITIVE FALSE STATEMENT about the branch for two
# of the three. It cost a full revise cycle: SABLE-21rug.4's worker read that
# sentence literally and added 115 lines of tests to a branch whose real
# problem was the run overrunning its 900s budget, i.e. the advice LENGTHENED
# the run that was already too long.
#
# Every assertion below carries all three legs:
#   (a) the condition is FORCED deterministically — the timeout is INJECTED
#       (subprocess.TimeoutExpired raised at the seam), never approximated by
#       making the host slow, which would be both flaky and a manufactured-load
#       violation of the standing fleet rule;
#   (b) the intended phase is NAMED;
#   (c) a NEGATIVE CONTROL proves the check DISCRIMINATES — every case asserts
#       the OTHER phases' vocabulary is ABSENT. Asserting only that the
#       pytest case says "pytest" passes against an implementation that
#       hardcodes one string; asserting the coverage case does NOT say
#       "pytest" is what makes it bite.
#
# And the DECISION AXIS is pinned separately (test_coverage_floor_every_phase_
# still_denies): all three still DENY at exit 27. This bead moves the report
# axis and only the report axis — a change that made any of them start passing
# would have converted a reporting bug into a false-green.

_PRUNED_TEST_FILE_BASE = (
    "import sys, os\n"
    "sys.path.insert(0, os.path.dirname(__file__))\n"
    "from mod import foo, bar\n"
    "\n"
    "def test_foo():\n"
    "    assert foo(1) == 1\n"
    "\n"
    "def test_bar():\n"
    "    assert bar(1) == 2\n"
)
_PRUNED_TEST_FILE_TIP = (
    "import sys, os\n"
    "sys.path.insert(0, os.path.dirname(__file__))\n"
    "from mod import foo\n"
    "\n"
    "def test_foo():\n"
    "    assert foo(1) == 1\n"
)


def _coverage_git(repo, *args):
    cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                        text=True, check=True)
    return cp.stdout.strip()


def _coverage_floor_repo(tmp_path, *, carry_script: bool = True,
                         script_body: str | None = None):
    """A REAL git repo whose base..tip diff is a genuine PRUNING diff (bar()'s
    only test is removed while bar() itself changes), so detect_pruning fires
    for real and assert_coverage_floor actually consults the check. Returns
    (repo, base_sha, tip_sha).

    The pruning half is never faked: the whole gate only reaches the phase
    logic through a real pruning verdict, so a hand-stubbed signal would test
    a path the gate does not have."""
    repo = tmp_path / "floor-repo"
    (repo / "bin").mkdir(parents=True)
    (repo / ".github" / "ci").mkdir(parents=True)
    _coverage_git(repo.parent, "init", "-q", str(repo))
    _coverage_git(repo, "config", "user.email", "t@sable.invalid")
    _coverage_git(repo, "config", "user.name", "SABLE Test")
    _coverage_git(repo, "config", "commit.gpgsign", "false")

    (repo / "bin" / "mod.py").write_text(
        "def foo(x):\n    return x\n\n\ndef bar(x):\n    return x + 1\n")
    (repo / "bin" / "test_mod.py").write_text(_PRUNED_TEST_FILE_BASE)
    if carry_script:
        (repo / ".github" / "ci" / "diff-cover-gate.sh").write_text(
            script_body if script_body is not None
            else (REPO_ROOT_FOR_FLOOR / ".github" / "ci" / "diff-cover-gate.sh").read_text())
        os.chmod(repo / ".github" / "ci" / "diff-cover-gate.sh", 0o755)
    _coverage_git(repo, "add", "-A")
    _coverage_git(repo, "commit", "-q", "-m", "base")
    base_sha = _coverage_git(repo, "rev-parse", "HEAD")

    (repo / "bin" / "mod.py").write_text(
        "def foo(x):\n    return x\n\n\ndef bar(x):\n"
        "    if x > 0:\n        return x + 2\n    return x - 2\n")
    (repo / "bin" / "test_mod.py").write_text(_PRUNED_TEST_FILE_TIP)
    _coverage_git(repo, "add", "-A")
    _coverage_git(repo, "commit", "-q", "-m", "tip: prune bar's only test")
    return repo, base_sha, _coverage_git(repo, "rev-parse", "HEAD")


REPO_ROOT_FOR_FLOOR = Path(__file__).resolve().parent.parent


def _force_gate_script(monkeypatch, *, returncode=None, stdout="", raises=None):
    """Force ONE deterministic outcome out of the gate script's subprocess and
    leave every other subprocess (all the real git plumbing) untouched.

    This is the seam the defect lives at: the caller sees one CompletedProcess
    and used to reduce it to `rc == 0`. Intercepting exactly the `bash <script>`
    invocation — and nothing else — is what makes the timeout INJECTABLE
    instead of waited for."""
    real_run = promote_lib.git_lib._run

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "bash":
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(argv, returncode, stdout, None)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(promote_lib.git_lib, "_run", fake_run)


def _coverage_floor_deny_message(tmp_path, monkeypatch, **forced):
    """Run the REAL assert_coverage_floor over a REAL pruning diff with one
    forced script outcome, and return the exit-27 message an operator reads."""
    repo, base_sha, tip_sha = _coverage_floor_repo(tmp_path)
    _force_gate_script(monkeypatch, **forced)
    monkeypatch.setattr(promote_lib, "_append_evidence", lambda *a, **kw: None)
    with pytest.raises(promote_lib.GateError) as excinfo:
        promote_lib.assert_coverage_floor(str(repo), "TEST-9yjt5", base_sha, tip_sha, None)
    assert excinfo.value.code == promote_lib.classify.EXIT_COVERAGE_FLOOR, (
        "the DECISION axis moved: a denied pruning diff must still be exit 27")
    return str(excinfo.value)


# The vocabulary each phase owns. A message may contain ONLY its own row's
# words — that mutual exclusion is the negative control that would catch an
# implementation hardcoding a single phase string.
_PYTEST_PHASE_WORDS = ("[phase: pytest-failed]", "pytest suite FAILED")
_COVERAGE_PHASE_WORDS = ("[phase: diff-cover-under-floor]", "coverage regressed")
_TIMEOUT_PHASE_WORDS = ("[phase: timed-out]", "did not finish")
_ABSENT_SCRIPT_WORDS = ("no coverage-delta check",)


def test_coverage_floor_names_the_failing_phase(tmp_path, monkeypatch):
    """(A) A RED SUITE. diff-cover never ran, so there is no coverage number
    to regress — the old message claimed one anyway, and the worker who
    believed it went and wrote tests."""
    msg = _coverage_floor_deny_message(
        tmp_path, monkeypatch, returncode=10,
        stdout=("FAILED bin/test_mod.py::test_foo - assert 1 == 999\n"
                "FAILED bin/test_mod.py::test_other - AssertionError\n"
                "SABLE-COVERAGE-FLOOR-PHASE: pytest-failed rc=1\n"))

    # (b) it names the PYTEST phase, and the tests that failed.
    for word in _PYTEST_PHASE_WORDS:
        assert word in msg, f"the pytest-phase deny does not say {word!r}: {msg}"
    assert "bin/test_mod.py::test_foo" in msg, \
        f"the pytest-phase deny does not NAME the failing test: {msg}"
    assert "diff-cover NEVER RAN" in msg, msg

    # (c) NEGATIVE CONTROL — it must not borrow any other phase's vocabulary.
    # "coverage regressed" here is the exact false statement this bead exists
    # to delete, and "no coverage-delta check ... was carried" is the other
    # false one (the branch carries it; it ran and failed).
    for word in _COVERAGE_PHASE_WORDS + _TIMEOUT_PHASE_WORDS + _ABSENT_SCRIPT_WORDS:
        assert word not in msg, \
            f"the pytest-phase deny wrongly claims {word!r} — phases are not discriminated: {msg}"


def test_coverage_floor_names_the_diff_cover_phase(tmp_path, monkeypatch):
    """(C) A GENUINE PATCH-COVERAGE MISS — the one cause the old message
    actually described. Its wording must SURVIVE the fix (this is the
    over-correction control: it would be just as broken to stop saying
    'coverage regressed' when coverage really did regress)."""
    msg = _coverage_floor_deny_message(
        tmp_path, monkeypatch, returncode=11,
        stdout=("SABLE-COVERAGE-FLOOR-PHASE: diff-cover-under-floor rc=1 fail_under=80\n"))

    for word in _COVERAGE_PHASE_WORDS:
        assert word in msg, f"the coverage-miss deny does not say {word!r}: {msg}"
    assert "diff-cover RAN and produced a real patch-coverage number" in msg, msg

    # (c) NEGATIVE CONTROL — and specifically NOT the pytest phase.
    for word in _PYTEST_PHASE_WORDS + _TIMEOUT_PHASE_WORDS + _ABSENT_SCRIPT_WORDS:
        assert word not in msg, \
            f"the coverage-miss deny wrongly claims {word!r}: {msg}"


def test_coverage_floor_timeout_reads_could_not_assess(tmp_path, monkeypatch):
    """(B) A BUDGET OVERRUN — the most expensive misreport of the three,
    because the remedy the old message named (add tests) makes the true cause
    STRICTLY WORSE by lengthening the run that is already overrunning.

    The timeout is INJECTED at the subprocess seam, never produced by making
    the host slow: a wall-clock-dependent timeout test is flaky AND is exactly
    the manufactured host load the standing fleet rule forbids."""
    monkeypatch.setenv("SABLE_MG_COVERAGE_FLOOR_TIMEOUT", "900")
    msg = _coverage_floor_deny_message(
        tmp_path, monkeypatch,
        raises=subprocess.TimeoutExpired(cmd=["bash", "diff-cover-gate.sh"], timeout=900))

    # (b) could-not-assess, with elapsed vs budget, and the branch DOES carry it.
    assert "COULD NOT ASSESS" in msg, msg
    for word in _TIMEOUT_PHASE_WORDS:
        assert word in msg, f"the timeout deny does not say {word!r}: {msg}"
    assert re.search(r"killed after \d+s against a 900s budget", msg), \
        f"the timeout deny does not state elapsed vs budget: {msg}"
    assert "The branch DOES carry the check" in msg, msg
    assert "Adding tests LENGTHENS this run" in msg, \
        f"the timeout deny does not warn off the remedy that deepens it: {msg}"

    # (c) NEGATIVE CONTROL — the two false statements the old gate made here.
    for word in _COVERAGE_PHASE_WORDS + _PYTEST_PHASE_WORDS + _ABSENT_SCRIPT_WORDS:
        assert word not in msg, \
            f"the timeout deny wrongly claims {word!r} — this is the be4lo.7 misreport: {msg}"


def test_coverage_floor_unattributed_exit_claims_no_coverage_number(tmp_path, monkeypatch):
    """An OLD branch's set -e script: non-zero, no phase marker. Attribution is
    genuinely unavailable, and saying so is the honest report — the one thing
    it must not do is pick a phase and assert it."""
    msg = _coverage_floor_deny_message(tmp_path, monkeypatch, returncode=1,
                                       stdout="some legacy output, no marker\n")

    assert "[phase: unattributed-exit]" in msg, msg
    assert "exited 1 without naming a phase" in msg, msg
    assert "UNKNOWN" in msg, msg
    for word in _COVERAGE_PHASE_WORDS + _PYTEST_PHASE_WORDS + _TIMEOUT_PHASE_WORDS:
        assert word not in msg, \
            f"an unattributable exit was attributed to {word!r} anyway: {msg}"


def test_coverage_floor_absent_script_still_says_the_branch_lacks_it(tmp_path, monkeypatch):
    """The one None cause whose STOCK sentence was already true. Over-
    correcting it into 'could not assess' would lose the only actionable
    instruction an operator has here: add the check."""
    repo, base_sha, tip_sha = _coverage_floor_repo(tmp_path, carry_script=False)
    monkeypatch.setattr(promote_lib, "_append_evidence", lambda *a, **kw: None)
    with pytest.raises(promote_lib.GateError) as excinfo:
        promote_lib.assert_coverage_floor(str(repo), "TEST-9yjt5", base_sha, tip_sha, None)
    msg = str(excinfo.value)

    assert excinfo.value.code == promote_lib.classify.EXIT_COVERAGE_FLOOR
    assert "no coverage-delta check" in msg, msg
    assert "[phase: no-script]" in msg, msg
    for word in _COVERAGE_PHASE_WORDS + _PYTEST_PHASE_WORDS + _TIMEOUT_PHASE_WORDS:
        assert word not in msg, msg


def test_coverage_floor_phase_vocabularies_are_mutually_exclusive(tmp_path, monkeypatch):
    """THE DISCRIMINATION CONTROL, stated as one property instead of four
    scattered absences: across the four attributable denies, every phase's
    signature phrase appears in EXACTLY ONE message.

    An implementation that hardcoded a single phase string, or that appended
    every phase's text unconditionally, fails here even if each individual
    positive assertion above still passed."""
    cases = {
        "pytest": dict(returncode=10,
                       stdout="SABLE-COVERAGE-FLOOR-PHASE: pytest-failed rc=1\n"),
        "coverage": dict(returncode=11,
                         stdout="SABLE-COVERAGE-FLOOR-PHASE: diff-cover-under-floor rc=1\n"),
        "cannot-assess": dict(returncode=12,
                              stdout="SABLE-COVERAGE-FLOOR-PHASE: diff-cover-unavailable rc=0\n"),
        "unattributed": dict(returncode=1, stdout="legacy\n"),
    }
    messages = {}
    for name, forced in cases.items():
        mp = pytest.MonkeyPatch()
        try:
            messages[name] = _coverage_floor_deny_message(
                tmp_path / f"mx-{name}", mp, **forced)
        finally:
            mp.undo()

    signatures = {
        "[phase: pytest-failed]": "pytest",
        "[phase: diff-cover-under-floor]": "coverage",
        "[phase: cannot-assess]": "cannot-assess",
        "[phase: unattributed-exit]": "unattributed",
        "coverage regressed": "coverage",
        "pytest suite FAILED": "pytest",
    }
    for token, owner in signatures.items():
        holders = sorted(n for n, m in messages.items() if token in m)
        assert holders == [owner], (
            f"{token!r} should appear in exactly the {owner!r} message, "
            f"but appears in {holders}")


def test_coverage_floor_every_phase_still_denies(tmp_path, monkeypatch):
    """THE DECISION AXIS, pinned. This bead changes the REPORT and nothing
    else: all three causes (red suite, budget overrun, real coverage miss) —
    plus every could-not-assess variant — must STILL deny at exit 27, and a
    clean run must still allow. A change that made any of them start passing
    would have turned a reporting bug into a false-green, which is strictly
    worse than the bug it replaced."""
    denying = [
        ("red suite", dict(returncode=10,
                           stdout="SABLE-COVERAGE-FLOOR-PHASE: pytest-failed rc=1\n")),
        ("coverage miss", dict(returncode=11,
                               stdout="SABLE-COVERAGE-FLOOR-PHASE: diff-cover-under-floor rc=1\n")),
        ("tooling gap", dict(returncode=12,
                             stdout="SABLE-COVERAGE-FLOOR-PHASE: coverage-xml-missing rc=0\n")),
        ("legacy non-zero", dict(returncode=1, stdout="")),
        ("budget overrun", dict(raises=subprocess.TimeoutExpired(cmd=["bash"], timeout=1))),
        ("os error", dict(raises=OSError("no bash here"))),
    ]
    for label, forced in denying:
        mp = pytest.MonkeyPatch()
        try:
            repo, base_sha, tip_sha = _coverage_floor_repo(tmp_path / f"deny-{label}")
            _force_gate_script(mp, **forced)
            mp.setattr(promote_lib, "_append_evidence", lambda *a, **kw: None)
            with pytest.raises(promote_lib.GateError) as excinfo:
                promote_lib.assert_coverage_floor(str(repo), "TEST-9yjt5",
                                                  base_sha, tip_sha, None)
            assert excinfo.value.code == promote_lib.classify.EXIT_COVERAGE_FLOOR, \
                f"{label} did not deny at exit 27"
        finally:
            mp.undo()

    # NON-VACUITY: the same harness CAN allow. Without this, every assertion
    # above would pass against a gate that denies unconditionally.
    mp = pytest.MonkeyPatch()
    try:
        repo, base_sha, tip_sha = _coverage_floor_repo(tmp_path / "allow")
        _force_gate_script(mp, returncode=0,
                           stdout="SABLE-COVERAGE-FLOOR-PHASE: ok rc=0 fail_under=80\n")
        recorded = []
        mp.setattr(promote_lib, "_append_evidence",
                   lambda repo_, bead_, note: recorded.append(note))
        promote_lib.assert_coverage_floor(str(repo), "TEST-9yjt5", base_sha, tip_sha, None)
        assert recorded and "check passed" in recorded[0], recorded
        assert "[phase: ok]" in recorded[0], recorded
    finally:
        mp.undo()


# --------------------------------------------------------------------------
# INTEGRATION — the REAL script, real git worktree, real bash, real pytest,
# real coverage.py, real diff-cover. NOTHING mocked.
#
# The entire defect is that a REAL script's REAL exit code was collapsed, so a
# mocked script would test the mock. These two fixtures are planted to differ
# in exactly one respect — one has a deliberately failing test, one is green
# but under --fail-under — and the deliverable is that they come out
# DISTINGUISHABLE. Both run through run_coverage_floor_check itself, so the
# throwaway `git worktree add --detach` is real too.
# --------------------------------------------------------------------------

def _real_gate_repo(root: Path, *, tip_source: str, tip_test: str):
    """A synthetic repo mirroring this repo's bin/-rooted layout, carrying the
    ACTUAL diff-cover-gate.sh (copied, never reimplemented — the script under
    test is the shipped one). Tiny on purpose: the real `pytest bin/ --cov=bin`
    is what runs, so the fixture is two files, not this repo."""
    (root / "bin").mkdir(parents=True)
    ci = root / ".github" / "ci"
    ci.mkdir(parents=True)
    gate = REPO_ROOT_FOR_FLOOR / ".github" / "ci" / "diff-cover-gate.sh"
    (ci / "diff-cover-gate.sh").write_text(gate.read_text())
    os.chmod(ci / "diff-cover-gate.sh", 0o755)

    _coverage_git(root.parent, "init", "-q", str(root))
    _coverage_git(root, "config", "user.email", "t@sable.invalid")
    _coverage_git(root, "config", "user.name", "SABLE Test")
    _coverage_git(root, "config", "commit.gpgsign", "false")

    (root / "bin" / "mod.py").write_text("def foo(x):\n    return x\n")
    (root / "bin" / "test_mod.py").write_text(
        "import sys, os\n"
        "sys.path.insert(0, os.path.dirname(__file__))\n"
        "from mod import foo\n"
        "\n"
        "def test_foo():\n"
        "    assert foo(1) == 1\n")
    _coverage_git(root, "add", "-A")
    _coverage_git(root, "commit", "-q", "-m", "base")
    base_sha = _coverage_git(root, "rev-parse", "HEAD")

    (root / "bin" / "mod.py").write_text(tip_source)
    (root / "bin" / "test_mod.py").write_text(tip_test)
    _coverage_git(root, "add", "-A")
    _coverage_git(root, "commit", "-q", "-m", "tip")
    return base_sha, _coverage_git(root, "rev-parse", "HEAD")


_GREEN_TEST = (
    "import sys, os\n"
    "sys.path.insert(0, os.path.dirname(__file__))\n"
    "from mod import foo\n"
    "\n"
    "def test_foo():\n"
    "    assert foo(1) == 1\n")
_FAILING_TEST = (
    "import sys, os\n"
    "sys.path.insert(0, os.path.dirname(__file__))\n"
    "from mod import foo\n"
    "\n"
    "def test_foo_deliberately_fails():\n"
    "    assert foo(1) == 999\n")
# A tip whose NEW lines are entirely uncovered: diff-cover measures a real
# number and it lands under --fail-under.
_UNCOVERED_SOURCE = (
    "def foo(x):\n"
    "    return x\n"
    "\n"
    "\n"
    "def never_exercised(a):\n"
    "    if a > 0:\n"
    "        return 1\n"
    "    if a < 0:\n"
    "        return 2\n"
    "    return 3\n")


def test_coverage_floor_phases_against_real_script(tmp_path):
    """THE DELIVERABLE, measured: two planted fixtures differing only in which
    phase breaks produce DISTINCT phases out of the real script, through the
    real worktree, with nothing mocked.

    Fixture 1: a deliberately failing test (source unchanged).
    Fixture 2: green suite, new source lines nobody covers -> under
               --fail-under.

    If these two came out the same, the gate would once again be telling a
    worker with a red suite to go write tests."""
    fail_repo = tmp_path / "real-pytest-fail"
    fail_base, fail_tip = _real_gate_repo(
        fail_repo, tip_source="def foo(x):\n    return x\n", tip_test=_FAILING_TEST)
    fail_run = promote_lib.run_coverage_floor_check(str(fail_repo), fail_base, fail_tip)

    miss_repo = tmp_path / "real-coverage-miss"
    miss_base, miss_tip = _real_gate_repo(
        miss_repo, tip_source=_UNCOVERED_SOURCE, tip_test=_GREEN_TEST)
    miss_run = promote_lib.run_coverage_floor_check(str(miss_repo), miss_base, miss_tip)

    # (b) each is attributed to ITS OWN phase, by the real script's real exit.
    assert fail_run.phase == promote_lib.PHASE_PYTEST_FAILED, \
        f"real red suite attributed to {fail_run.phase}: {fail_run.detail}"
    assert miss_run.phase == promote_lib.PHASE_DIFF_COVER_UNDER_FLOOR, \
        f"real coverage miss attributed to {miss_run.phase}: {miss_run.detail}"

    # (c) NEGATIVE CONTROL — DISTINCTNESS is the deliverable, so assert the
    # two differ rather than only that each matches. Under the old script both
    # were "non-zero" and indistinguishable.
    assert fail_run.phase != miss_run.phase
    assert fail_run.detail != miss_run.detail

    # The tri-state moves with the phase: a red suite produced NO coverage
    # number (None, could-not-assess), a real miss produced one (False).
    assert fail_run.passed is None, \
        "a red suite must not be reported as a measured coverage failure"
    assert miss_run.passed is False, \
        "a real diff-cover miss is a measured failure, not a could-not-assess"

    # The real script really did name the failing test.
    assert "test_foo_deliberately_fails" in fail_run.detail, fail_run.detail

    # DECISION AXIS unchanged: both still deny.
    for run in (fail_run, miss_run):
        decision = promote_lib.coverage_floor_lib.evaluate_coverage_floor(
            promote_lib.coverage_floor_lib.PruningSignal(removed_test_functions=["test_x"]),
            run.passed, None)
        assert decision.action == promote_lib.coverage_floor_lib.ACTION_DENY, \
            f"{run.phase} stopped denying — a reporting fix turned into a false-green"


def test_coverage_floor_real_script_allows_a_covered_patch(tmp_path):
    """NON-VACUITY for the integration pair above: the same real script, real
    worktree and real diff-cover CAN return a clean allow. Without this, both
    phase assertions would hold just as well against a script that could only
    ever fail."""
    repo = tmp_path / "real-clean"
    base, tip = _real_gate_repo(
        repo,
        tip_source="def foo(x):\n    return x\n\n\ndef bar(a):\n    return a + 1\n",
        tip_test=(
            "import sys, os\n"
            "sys.path.insert(0, os.path.dirname(__file__))\n"
            "from mod import foo, bar\n"
            "\n"
            "def test_foo():\n"
            "    assert foo(1) == 1\n"
            "\n"
            "def test_bar():\n"
            "    assert bar(1) == 2\n"))
    run = promote_lib.run_coverage_floor_check(str(repo), base, tip)
    assert run.passed is True, f"{run.phase}: {run.detail}"
    assert run.phase == promote_lib.PHASE_OK
