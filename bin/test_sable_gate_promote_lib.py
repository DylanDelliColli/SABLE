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
import shutil
import subprocess
import sys
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
