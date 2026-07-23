#!/usr/bin/env python3
"""Unit tests for bin/sable-spawn-worker (SABLE-bldh.3).

Pure-function coverage: model resolution from label + override, worktree/window
naming, dispatch-prompt assembly, bead JSON parsing.
"""
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader(
    "sable_spawn_worker", str(Path(__file__).resolve().parent / "sable-spawn-worker")
)
_SPEC = importlib.util.spec_from_loader("sable_spawn_worker", _LOADER)
ssw = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(ssw)


# --- model ladder resolution ------------------------------------------------

def test_resolve_model_default_sonnet():
    assert ssw.resolve_model([], None) == ("sonnet", None, "default")
    assert ssw.resolve_model(["scope:foo"], None) == ("sonnet", None, "default")


def test_resolve_model_from_label():
    assert ssw.resolve_model(["model:haiku"], None) == ("haiku", None, "label")
    assert ssw.resolve_model(["x", "model:opus", "y"], None) == ("opus", None, "label")


def test_resolve_model_override_wins_and_carries_reason():
    model, reason, source = ssw.resolve_model(["model:sonnet"], "opus:auth path now")
    assert model == "opus"
    assert reason == "auth path now"
    assert source == "override"


def test_resolve_model_override_without_reason():
    model, reason, source = ssw.resolve_model([], "haiku")
    assert model == "haiku"
    assert reason is None
    assert source == "override"


# --- SABLE-mn1da: the "ladder" is a flat default, and says so ----------------

def test_resolve_model_does_not_infer_difficulty_from_the_bead():
    """The whole defect: no bead signal — type, priority, description size, an
    unresolved ruling — moves the model. Only an override or a model: label
    does. Asserted directly so a future 'smart' inference cannot be added
    without deciding, explicitly, to break this."""
    heavy = ["type:bug", "priority:0", "security", "ruling:unresolved"]
    assert ssw.resolve_model(heavy, None) == (ssw.DEFAULT_MODEL, None, "default")


def test_model_announcement_default_names_model_and_says_it_is_the_default():
    ann = ssw.model_announcement("sonnet", None, "default")
    assert "sonnet" in ann
    assert "DEFAULT" in ann
    # it must state WHY (neither signal was present), not just that it defaulted
    assert "--model" in ann and "model: label" in ann


def test_model_announcement_reasoned_override_is_byte_identical_to_the_old_wording():
    """The path that already worked must gain no noise (SABLE-mn1da test spec):
    this is exactly the string sable-spawn-worker printed before the fix."""
    assert ssw.model_announcement("opus", "auth path now", "override") == \
        "model opus, override: auth path now"


def test_model_announcement_bare_override_says_no_reason_was_given():
    ann = ssw.model_announcement("haiku", None, "override")
    assert ann.startswith("model haiku, override:")
    assert "no reason given" in ann


def test_model_announcement_label_names_the_label_as_the_source():
    ann = ssw.model_announcement("haiku", None, "label")
    assert "haiku" in ann and "model:haiku" in ann
    assert "DEFAULT" not in ann


# --- SABLE-qw9jv: provenance comes from what LAUNCHED -----------------------

def test_launched_model_reads_the_default_worker_command():
    cmd = ssw.worker_command("haiku", None)
    assert ssw.launched_model(cmd) == "haiku"


def test_launched_model_survives_the_lifecycle_wrapper():
    """The stamp is taken from the FINAL string handed to tmux, so it must
    still be readable after with_lifecycle_flags wraps it."""
    wrapped = ssw.with_lifecycle_flags(ssw.worker_command("opus", None))
    assert ssw.launched_model(wrapped) == "opus"


def test_launched_model_stops_at_shell_punctuation():
    """Caught by the integration test, not by inspection: with_lifecycle_flags
    joins the worker command to the done-flag write with `;`, so a command
    ending in the model token ('claude --model opus') parsed as 'opus;' and
    that value went straight into the bead. The stamp must be the model, not
    the model plus whatever shell syntax followed it."""
    wrapped = ssw.with_lifecycle_flags(ssw.worker_command("x", "claude --model opus"))
    assert ssw.launched_model(wrapped) == "opus"
    assert ssw.launched_model("claude --model haiku && echo hi") == "haiku"


def test_launched_model_accepts_equals_form():
    assert ssw.launched_model("claude --model=opus --permission-mode x") == "opus"


def test_launched_model_is_none_when_the_command_names_no_model():
    """A full SABLE_WORKER_CMD override replaces the command verbatim. When it
    pins no model, the honest answer is 'unknown' — NOT the model the
    dispatcher asked for. Recording the request here is the intent-vs-execution
    error the bead exists to kill."""
    assert ssw.launched_model(ssw.worker_command("opus", "bash --noprofile --norc")) is None
    assert ssw.launched_model("") is None


def test_launched_model_reports_the_override_model_not_the_requested_one():
    """SABLE_WORKER_CMD naming a DIFFERENT model than the one resolved: the
    launch wins."""
    cmd = ssw.worker_command("sonnet", "claude --model opus --permission-mode x")
    assert ssw.launched_model(cmd) == "opus"


# --- naming -----------------------------------------------------------------

def test_worktree_name_from_bead():
    assert ssw.worktree_name("SABLE-bldh.3", None) == "wk-sable-bldh-3"


def test_worktree_name_from_scope():
    assert ssw.worktree_name("SABLE-bldh.3", "msg-helper") == "wk-msg-helper"


def test_worktree_name_idempotent_on_already_prefixed_scope():
    """SABLE-v2k3: a scope that already starts with wk- must NOT be
    double-prefixed into wk-wk-*."""
    assert ssw.worktree_name("SABLE-jxcg", "wk-claim-hook-sandbox") == "wk-claim-hook-sandbox"
    assert ssw.worktree_name("SABLE-jxcg", "claim-hook-sandbox") == "wk-claim-hook-sandbox"


def test_worktree_name_idempotent_on_already_prefixed_bead_id():
    assert ssw.worktree_name("wk-foo", None) == "wk-foo"


def test_window_name():
    assert ssw.window_name("SABLE-bldh.3") == "worker-sable-bldh-3"


def test_resolve_worktree_path_is_sibling_of_repo():
    # SABLE-bldh.11: the worktree must be the repo's SIBLING (parent dir), and the
    # value handed to `bd worktree create` must equal the value handed to tmux -c.
    assert (ssw.resolve_worktree_path("/home/u/dev/SABLE", "wk-parity")
            == "/home/u/dev/wk-parity")
    assert (ssw.resolve_worktree_path("/a/b/c/REPO", "wk-x")
            == "/a/b/c/wk-x")


# --- bead JSON parsing ------------------------------------------------------

def test_parse_bead_takes_first_of_list():
    raw = '[{"id":"X-1","title":"T","description":"D","labels":["model:haiku"]}]'
    b = ssw.parse_bead(raw)
    assert b["id"] == "X-1"
    assert b["title"] == "T"
    assert ssw.bead_labels(b) == ["model:haiku"]


def test_bead_labels_handles_null():
    assert ssw.bead_labels({"labels": None}) == []
    assert ssw.bead_labels({}) == []


# --- model-check enforcement (re-homed governance, SABLE-bldh.6) -------------

def test_label_model_extracts():
    assert ssw.label_model(["x", "model:opus"]) == "opus"
    assert ssw.label_model(["x"]) is None


def test_model_check_blocks_silent_override():
    err = ssw.model_check(["model:sonnet"], "opus")
    assert err is not None and "opus" in err and "sonnet" in err


def test_model_check_allows_override_with_reason():
    assert ssw.model_check(["model:sonnet"], "opus:auth path now") is None


def test_model_check_allows_matching_override():
    assert ssw.model_check(["model:sonnet"], "sonnet") is None


def test_model_check_allows_when_no_label_or_no_override():
    assert ssw.model_check([], "opus") is None
    assert ssw.model_check(["model:sonnet"], None) is None


# --- duplicate-dispatch / overlap / preempt (re-homed governance, SABLE-bldh.8) --

def test_parse_bead_list_valid_array():
    assert ssw.parse_bead_list('[{"id":"X-1"}]') == [{"id": "X-1"}]


def test_parse_bead_list_fails_open_on_malformed():
    assert ssw.parse_bead_list("not json") == []
    assert ssw.parse_bead_list('{"id":"X-1"}') == []  # object, not array


def test_already_in_progress_check_blocks_second_spawn_with_pane_evidence():
    err = ssw.already_in_progress_check(
        {"id": "X-1", "status": "in_progress", "assignee": "tarzan"},
        pane_evidence=True, worktree_evidence=False)
    assert err is not None and "X-1" in err and "IN_PROGRESS" in err and "tarzan" in err


def test_already_in_progress_check_allows_open_bead():
    assert ssw.already_in_progress_check(
        {"id": "X-1", "status": "open"}, True, True) is None


def test_already_in_progress_check_handles_missing_assignee():
    err = ssw.already_in_progress_check(
        {"id": "X-1", "status": "in_progress"},
        pane_evidence=False, worktree_evidence=True)
    assert err is not None and "unassigned" in err


# --- SABLE-676c: claim-then-hold must NOT false-positive as a duplicate -------
#
# A manager claims a bead (-> IN_PROGRESS) to mark lane ownership during a
# coordination hold, then spawns the FIRST worker when the hold lifts (the
# documented claim-THEN-spawn protocol). With NO worker pane and NO worktree yet,
# the old any-in_progress guard wrongly refused that first dispatch as a
# duplicate. IN_PROGRESS is a duplicate ONLY when dispatch evidence exists.

def test_already_in_progress_check_allows_bare_claim_no_evidence():
    # the core fix: in_progress + no pane + no worktree = claim-then-hold -> PASS
    assert ssw.already_in_progress_check(
        {"id": "X-1", "status": "in_progress", "assignee": "optimus"},
        pane_evidence=False, worktree_evidence=False) is None


def test_already_in_progress_check_blocks_on_worktree_evidence_alone():
    err = ssw.already_in_progress_check(
        {"id": "X-1", "status": "in_progress"},
        pane_evidence=False, worktree_evidence=True)
    assert err is not None and "IN_PROGRESS" in err and "worktree" in err


def test_already_in_progress_check_names_both_evidence_signals():
    err = ssw.already_in_progress_check(
        {"id": "X-1", "status": "in_progress"},
        pane_evidence=True, worktree_evidence=True)
    assert err is not None and "pane" in err and "worktree" in err


# --- dispatch-evidence detection (SABLE-676c) --------------------------------

def test_bead_pane_tagged_true_on_running_pane_for_bead():
    listing = "SABLE-1\trunning\nSABLE-676c\trunning\n"
    assert ssw.bead_pane_tagged(listing, "SABLE-676c") is True


def test_bead_pane_tagged_false_on_done_pane():
    # SABLE-qq6r: a stale/done pane is not live dispatch evidence.
    assert ssw.bead_pane_tagged("SABLE-676c\tdone\n", "SABLE-676c") is False


def test_bead_pane_tagged_false_on_non_running_status():
    # design: match a RUNNING pane — a blank/transient status is NOT evidence, so
    # a bare claim never false-positives on an untagged pane.
    assert ssw.bead_pane_tagged("SABLE-676c\t\n", "SABLE-676c") is False


def test_bead_pane_tagged_false_when_bead_absent():
    assert ssw.bead_pane_tagged("SABLE-1\trunning\n", "SABLE-676c") is False


def test_bead_pane_tagged_empty_listing():
    assert ssw.bead_pane_tagged("", "SABLE-676c") is False


def test_prospective_worktree_path_derives_sibling_when_no_override():
    p = ssw.prospective_worktree_path(
        "SABLE-676c", "claim-guard", None, "/home/u/dev/SABLE")
    assert p == "/home/u/dev/wk-claim-guard"


def test_prospective_worktree_path_derives_from_bead_when_no_scope():
    p = ssw.prospective_worktree_path("SABLE-676c", None, None, "/home/u/dev/SABLE")
    assert p == "/home/u/dev/wk-sable-676c"


def test_prospective_worktree_path_empty_on_explicit_override():
    # an explicit --worktree is an intentional reuse / revision re-spawn, never a
    # duplicate — worktree-evidence must not fire on it.
    assert ssw.prospective_worktree_path(
        "SABLE-676c", None, "/some/existing/wt", "/home/u/dev/SABLE") == ""


def test_prospective_worktree_path_empty_without_toplevel():
    assert ssw.prospective_worktree_path("SABLE-676c", "x", None, "") == ""


def test_worktree_dispatch_exists_true_for_real_dir(tmp_path):
    assert ssw.worktree_dispatch_exists(str(tmp_path)) is True


def test_worktree_dispatch_exists_false_for_missing(tmp_path):
    assert ssw.worktree_dispatch_exists(str(tmp_path / "nope")) is False


def test_worktree_dispatch_exists_false_for_empty_path():
    assert ssw.worktree_dispatch_exists("") is False


def test_extract_wip_claims_parses_comma_list():
    text = "some notes\nWIP-CLAIMS: a/b.py, c/d.py\nmore text"
    assert ssw.extract_wip_claims(text) == {"a/b.py", "c/d.py"}


def test_extract_wip_claims_empty_when_absent():
    assert ssw.extract_wip_claims("no claims here") == set()


def test_bead_claimed_files_reads_notes_and_description():
    bead = {"notes": "WIP-CLAIMS: x.py", "description": "WIP-CLAIMS: y.py"}
    assert ssw.bead_claimed_files(bead) == {"x.py", "y.py"}


def test_bead_claimed_files_reads_wip_claims_metadata():
    bead = {"metadata": {"wip_claims": "a.py, b.py"}}
    assert ssw.bead_claimed_files(bead) == {"a.py", "b.py"}


def test_bead_claimed_files_reads_file_footprint_section():
    # SABLE-jd5fj.6: declared footprint section, including an extension-less
    # entry with a trailing parenthetical annotation stripped to its path token.
    bead = {"description": "Story text.\n\n## File footprint\n"
                           "hooks/foo.sh, bin/sable-spawn-worker (constraint surfacing)"}
    assert ssw.bead_claimed_files(bead) == {"hooks/foo.sh", "bin/sable-spawn-worker"}


# --- SABLE-47try: could-not-assess vs declares-nothing -----------------------
# The old `if not my_files: return OverlapVerdict("none")` collapsed two facts
# into the releasing verdict. These assert the distinction now exists at the
# parse layer (where the information has to come from) and at the call site.


def test_read_bead_footprint_reports_empty_footprint_section_as_unreadable():
    bead = {"description": "Story text.\n\n## File footprint\n\n## Next section\nx"}
    read = ssw.read_bead_footprint(bead)
    assert read.files == frozenset()
    assert read.could_not_assess is True
    assert "## File footprint" in read.unreadable_sources[0]


def test_extract_footprint_section_present_but_empty_yields_no_paths():
    # SABLE-qm9ky's named spec. The old regex's trailing \s* consumed the blank
    # line, so (.+?) matched INTO the following heading and part.split()[0]
    # yielded the literal token '##' — a DECLARED-EMPTY footprint section
    # producing a claimed file named '##'.
    desc = "Story.\n\n## File footprint\n\n## Test spec\nsomething"
    assert ssw.extract_footprint_section(desc) == set()
    assert "##" not in ssw.extract_footprint_section(desc)
    # Negative controls, same test: heading genuinely absent still returns the
    # absent form, and a two-real-path section still returns exactly those two.
    assert ssw.extract_footprint_section("no heading here") == set()
    assert ssw.extract_footprint_section(
        "S.\n\n## File footprint\nbin/a.py, bin/b.py\n\n## Next\nx"
    ) == {"bin/a.py", "bin/b.py"}
    # The SECOND caller qm9ky warned about: extract_footprint_section's set
    # signature is deliberately unchanged, so bead_claimed_files' union is
    # undisturbed. The (declared, entries) shape lives on read_footprint_section
    # alongside it rather than replacing it.
    assert ssw.bead_claimed_files({"description": desc}) == set()


def test_read_bead_footprint_absent_sources_is_not_could_not_assess():
    # The load-bearing negative control at the parse layer: a bead that names
    # no footprint source at all DECLARES NOTHING. That is a real answer.
    read = ssw.read_bead_footprint({"id": "X-1", "description": "just prose"})
    assert read.files == frozenset()
    assert read.unreadable_sources == ()
    assert read.could_not_assess is False


def test_read_bead_footprint_reports_unreadable_per_source_not_as_a_boolean():
    # Unparseable in one source, silent in the other two, and READABLE in a
    # third — the union means the check is still answerable, so this is a
    # warning-grade condition, not could-not-assess.
    bead = {"notes": "WIP-CLAIMS: real.py",
            "description": "Story.\n\n## File footprint\n   \n"}
    read = ssw.read_bead_footprint(bead)
    assert read.files == frozenset({"real.py"})
    assert read.could_not_assess is False
    assert any("File footprint" in s for s in read.unreadable_sources)


def test_read_bead_footprint_reports_unparseable_wip_claims_metadata():
    read = ssw.read_bead_footprint({"metadata": {"wip_claims": " , , "}})
    assert read.could_not_assess is True
    assert read.unreadable_sources == ("wip_claims metadata",)


def test_read_bead_footprint_blank_wip_claims_metadata_is_not_a_failed_read():
    # pre-dispatch-claim.sh may simply not have fired yet — an absent/blank
    # claim value is "nothing claimed", not a footprint that failed to parse.
    read = ssw.read_bead_footprint({"metadata": {"wip_claims": ""}})
    assert read.unreadable_sources == ()
    assert read.could_not_assess is False


def test_overlap_check_malformed_footprint_is_could_not_assess_not_none():
    # THE BEAD. A dispatching bead whose footprint section is present but
    # malformed must NOT return the same verdict as a completed clean check.
    # Plant-and-fail control: restoring the bare `return OverlapVerdict("none")`
    # at the top of overlap_check turns this assertion RED (observed).
    bead = {"id": "X-1", "description": "Story.\n\n## File footprint\n \n"}
    other = {"id": "Y-1", "notes": "WIP-CLAIMS: shared.py", "assignee": "tarzan"}
    verdict = ssw.overlap_check("X-1", bead, [other])
    assert verdict.decision == "could-not-assess"
    assert verdict.decision != "none"
    assert "X-1" in verdict.message and "could not be read" in verdict.message


def test_overlap_check_bead_declaring_no_footprint_still_dispatches():
    # LOAD-BEARING NEGATIVE CONTROL (prove-the-gate-can-release): many beads
    # legitimately declare no footprint. If this ever goes red, the fix has
    # become a gate that can never release and must be reverted.
    bead = {"id": "X-1", "description": "just prose, no footprint declared"}
    other = {"id": "Y-1", "notes": "WIP-CLAIMS: shared.py", "assignee": "tarzan"}
    verdict = ssw.overlap_check("X-1", bead, [other])
    assert verdict.decision == "none"


def test_overlap_check_wellformed_overlap_still_denies():
    # The working path is undisturbed by the fix.
    bead = {"id": "X-1", "description": "Story.\n\n## File footprint\nshared.py"}
    other = {"id": "Y-1", "notes": "WIP-CLAIMS: shared.py", "assignee": "tarzan"}
    verdict = ssw.overlap_check("X-1", bead, [other])
    assert verdict.decision == "deny"
    assert "shared.py" in verdict.message


def test_overlap_check_warns_when_an_in_progress_bead_footprint_is_unreadable():
    # Same defect on the other side: an unreadable in-progress footprint drops
    # out of the comparison silently. It warns (naming the bead) and does NOT
    # deny — denying would let one malformed bead block every dispatch.
    bead = {"id": "X-1", "notes": "WIP-CLAIMS: mine.py"}
    other = {"id": "Y-1", "description": "S.\n\n## File footprint\n \n"}
    verdict = ssw.overlap_check("X-1", bead, [other])
    assert verdict.decision == "none"
    assert any("Y-1" in w and "NOT have been detected" in w for w in verdict.warnings)


def test_overlap_check_partial_read_still_checks_but_warns():
    bead = {"id": "X-1",
            "notes": "WIP-CLAIMS: shared.py",
            "description": "S.\n\n## File footprint\n \n"}
    other = {"id": "Y-1", "notes": "WIP-CLAIMS: shared.py", "assignee": "tarzan"}
    verdict = ssw.overlap_check("X-1", bead, [other])
    assert verdict.decision == "deny"
    assert any("possibly-incomplete" in w for w in verdict.warnings)


def test_bead_claimed_files_still_returns_a_plain_set_for_legacy_callers():
    assert ssw.bead_claimed_files({"notes": "WIP-CLAIMS: a.py"}) == {"a.py"}


def test_extract_serialize_with_parses_comma_list():
    text = "notes\nSerialize-with: SABLE-a, SABLE-b\nmore"
    assert ssw.extract_serialize_with(text) == {"SABLE-a", "SABLE-b"}


def test_extract_serialize_with_empty_when_absent():
    assert ssw.extract_serialize_with("no serialize line here") == set()


def test_overlap_check_denies_on_shared_file_with_other_bead():
    # SABLE-jd5fj.6: overlap is now a SCHEDULING CONSTRAINT, not advisory.
    bead = {"id": "X-1", "notes": "WIP-CLAIMS: shared.py"}
    other = {"id": "Y-1", "notes": "WIP-CLAIMS: shared.py", "assignee": "tarzan"}
    verdict = ssw.overlap_check("X-1", bead, [other])
    assert verdict.decision == "deny"
    assert "Y-1" in verdict.message and "shared.py" in verdict.message and "tarzan" in verdict.message


def test_overlap_check_ignores_self_in_progress_list():
    # already_in_progress_check owns the same-bead case; overlap_check must not
    # double-flag itself if it happens to appear in the in-progress list.
    bead = {"id": "X-1", "notes": "WIP-CLAIMS: shared.py"}
    assert ssw.overlap_check("X-1", bead, [bead]).decision == "none"


def test_overlap_check_none_when_no_shared_files():
    bead = {"id": "X-1", "notes": "WIP-CLAIMS: a.py"}
    other = {"id": "Y-1", "notes": "WIP-CLAIMS: b.py"}
    assert ssw.overlap_check("X-1", bead, [other]).decision == "none"


def test_overlap_check_none_when_bead_has_no_claims():
    verdict = ssw.overlap_check("X-1", {"id": "X-1"}, [{"id": "Y-1", "notes": "WIP-CLAIMS: a.py"}])
    assert verdict.decision == "none"


def test_overlap_check_allows_with_matching_serialize_with():
    bead = {"id": "X-1", "notes": "WIP-CLAIMS: shared.py\nSerialize-with: Y-1"}
    other = {"id": "Y-1", "notes": "WIP-CLAIMS: shared.py", "assignee": "tarzan"}
    verdict = ssw.overlap_check("X-1", bead, [other])
    assert verdict.decision == "allow"
    assert verdict.tagged_ids == ("Y-1",)


def test_overlap_check_denies_when_serialize_with_names_unrelated_bead():
    # Naming a DIFFERENT bead does not launder the actual overlap.
    bead = {"id": "X-1", "notes": "WIP-CLAIMS: shared.py\nSerialize-with: Z-9"}
    other = {"id": "Y-1", "notes": "WIP-CLAIMS: shared.py", "assignee": "tarzan"}
    verdict = ssw.overlap_check("X-1", bead, [other])
    assert verdict.decision == "deny"


def test_preempt_check_blocks_on_p0_in_inbox():
    inbox = [{"id": "C-1", "title": "urgent coord", "priority": 0}]
    err = ssw.preempt_check("optimus", inbox)
    assert err is not None and "optimus" in err and "C-1" in err


def test_preempt_check_allows_when_no_p0():
    inbox = [{"id": "C-1", "title": "low pri", "priority": 2}]
    assert ssw.preempt_check("optimus", inbox) is None


def test_preempt_check_allows_when_empty_inbox():
    assert ssw.preempt_check("optimus", []) is None


def test_preempt_check_allows_when_no_lane():
    inbox = [{"id": "C-1", "title": "urgent coord", "priority": 0}]
    assert ssw.preempt_check("", inbox) is None


# --- SABLE-m40k: idempotent claim skip ---------------------------------------
#
# SABLE-676c's already_in_progress_check GUARD lets a claim-then-hold bead
# (in_progress, no pane/worktree evidence) through to dispatch. But the claim
# CALL right after it was still unconditional, and real `bd --claim` is only
# idempotent against ITS OWN actor identity, never a SABLE lane name, so it
# errored "already claimed by <lane>" and aborted the spawn. The fix skips the
# redundant claim call when the bead's assignee already IS the dispatching
# lane — however it got there.

def test_bead_already_claimed_by_lane_true_on_self_claim_or_reassignment():
    # covers both the lane's own prior claim-then-hold AND a manager
    # REASSIGNMENT by a different actor (SABLE-m40k design note) — the check
    # only looks at the resulting assignee, not who set it.
    assert ssw.bead_already_claimed_by_lane(
        {"id": "X-1", "assignee": "optimus"}, "optimus") is True


def test_bead_already_claimed_by_lane_false_for_different_lane():
    assert ssw.bead_already_claimed_by_lane(
        {"id": "X-1", "assignee": "tarzan"}, "optimus") is False


def test_bead_already_claimed_by_lane_false_when_unassigned():
    assert ssw.bead_already_claimed_by_lane(
        {"id": "X-1", "assignee": None}, "optimus") is False
    assert ssw.bead_already_claimed_by_lane({"id": "X-1"}, "optimus") is False


def test_bead_already_claimed_by_lane_false_when_lane_empty():
    # an empty/unresolvable lane never matches, even if assignee is also falsy
    # -- guards against treating an unassigned bead as "already mine".
    assert ssw.bead_already_claimed_by_lane(
        {"id": "X-1", "assignee": None}, "") is False
    assert ssw.bead_already_claimed_by_lane(
        {"id": "X-1", "assignee": "optimus"}, "") is False


# --- dispatch throttle: worker cap + live count (SABLE-mmdt) ------------------
#
# 3 managers x ~5 concurrent workers + Docker froze the WSL host (2026-07-07);
# nothing anywhere bounded spawn concurrency. sable-spawn-worker must refuse a
# spawn once SABLE_MAX_WORKERS live worker panes exist, with a message naming
# BOTH the cap and the live count (operator observability is part of acceptance).

def test_worker_cap_default_is_8(monkeypatch):
    monkeypatch.delenv("SABLE_MAX_WORKERS", raising=False)
    assert ssw.worker_cap() == 8


def test_worker_cap_env_override(monkeypatch):
    monkeypatch.setenv("SABLE_MAX_WORKERS", "2")
    assert ssw.worker_cap() == 2


def test_worker_cap_zero_pauses_spawning(monkeypatch):
    # explicit emergency stop: cap 0 refuses every spawn
    monkeypatch.setenv("SABLE_MAX_WORKERS", "0")
    assert ssw.worker_cap() == 0
    assert ssw.capacity_check(0, 0) is not None


def test_worker_cap_invalid_falls_back_to_default(monkeypatch):
    # a garbled knob must keep the throttle at its default, never lift it
    for bad in ("many", "", "  ", "-3", "2.5"):
        monkeypatch.setenv("SABLE_MAX_WORKERS", bad)
        assert ssw.worker_cap() == 8, bad


def test_count_live_workers_counts_running_workers_only():
    listing = ("worker\trunning\tworker\n"     # class-stamped worker: live
               "worker\tdone\tworker\n"        # done: not live (reap frees the slot)
               "worker\t\t\n"                  # legacy classless worker, no status yet: live
               "optimus\trunning\tmanager\n"   # manager loop: never counted
               "victor\trunning\tproducer\n"   # bounded producer: not a worker
               "\t\t\n")                       # role-less pane: skipped
    assert ssw.count_live_workers(listing) == 2


def test_count_live_workers_empty_listing():
    assert ssw.count_live_workers("") == 0


# --- SABLE-6xtx: the capacity counter has the SAME tag-only blindness
# sable-worker-status's classifier had -- a pane whose @sable_role/@sable_class
# tags are absent/lost is invisible to count_live_workers too (cls == "" and
# role doesn't start with "worker" -> is_worker is False). The window-name
# fallback (worker-<bead>, sable-spawn-worker's own window_name()) recognizes
# it as a worker pane, exactly like sable-worker-status's classifier fallback;
# its @sable_status tag (or the "not done" default) still decides whether it
# occupies a capacity slot -- see sable-worker-status's
# WINDOW_NAME_WORKER_PREFIX comment for why a live-process-based (pane_current_
# command) done override was tried here too and reverted (it broke this
# module's own bash-fixture-based integration tests). ---

def test_count_live_workers_window_name_fallback_recognizes_untagged_worker():
    # tags absent entirely, but window name alone is enough to count this as
    # a live worker -- consistent with the pre-existing legacy role-prefix path
    listing = "\trunning\t\tworker-sable-x\n"
    assert ssw.count_live_workers(listing) == 1


def test_count_live_workers_window_name_fallback_excludes_done_pane():
    # the fallback path still respects @sable_status=done exactly like the
    # tag-classified path -- a done fallback-classified pane frees its slot
    listing = "\tdone\t\tworker-sable-y\n"
    assert ssw.count_live_workers(listing) == 0


def test_count_live_workers_still_ignores_non_worker_window_names():
    # empty tags AND a non-worker-prefixed window name -- still skipped
    listing = "\trunning\t\tlincoln\n"
    assert ssw.count_live_workers(listing) == 0


def test_capacity_check_allows_under_cap():
    assert ssw.capacity_check(3, 4) is None


def test_capacity_check_refuses_at_cap_naming_cap_and_count():
    err = ssw.capacity_check(5, 4)
    assert err is not None
    assert "5" in err and "4" in err          # live count AND cap are both named
    assert "SABLE_MAX_WORKERS" in err          # the knob is named for the operator


# --- host-resource guard (SABLE-mmdt) -----------------------------------------
#
# The 2026-07-13 occurrence: load ~46 on 14 cores flaked the analytics container.
# Refuse to ADD a worker when 1-min load/core is already at/over the threshold.

def test_host_guard_allows_under_threshold():
    assert ssw.host_guard(3.0, 14, 2.0) is None


def test_host_guard_refuses_when_load_per_core_at_threshold():
    err = ssw.host_guard(46.0, 14, 2.0)       # the observed freeze shape
    assert err is not None
    assert "46.0" in err and "14" in err       # load and cores are named
    assert "SABLE_MAX_LOAD_PER_CORE" in err


def test_host_guard_disabled_by_nonpositive_threshold():
    assert ssw.host_guard(999.0, 1, 0.0) is None
    assert ssw.host_guard(999.0, 1, -1.0) is None


def test_host_guard_survives_zero_cores():
    # os.cpu_count() can return None/0 in odd containers; never ZeroDivisionError
    assert ssw.host_guard(10.0, 0, 2.0) is not None


def test_load_threshold_default_env_and_invalid(monkeypatch):
    monkeypatch.delenv("SABLE_MAX_LOAD_PER_CORE", raising=False)
    assert ssw.load_threshold() == 2.0
    monkeypatch.setenv("SABLE_MAX_LOAD_PER_CORE", "1.5")
    assert ssw.load_threshold() == 1.5
    monkeypatch.setenv("SABLE_MAX_LOAD_PER_CORE", "junk")
    assert ssw.load_threshold() == 2.0


# --- dispatch prompt assembly -----------------------------------------------

def test_assemble_dispatch_prompt_has_load_bearing_slots():
    p = ssw.assemble_dispatch_prompt(
        bead_id="X-1", title="Do the thing", description="full desc here",
        worktree="/wt/wk-x", branch="wk-x", model="haiku",
    )
    assert "X-1" in p
    assert "/wt/wk-x" in p
    assert "haiku" in p
    assert "full desc here" in p
    # warm-pane self-push contract markers
    assert "git push" in p
    assert "git -C" in p  # explicitly warns against it
    assert "@sable_status" in p  # done-signal instruction


def test_dispatch_prompt_done_flag_targets_own_pane():
    """market-brief-package-uj22: without -t, tmux resolves the target pane from
    the client's active pane (the operator's focus), not the invoking worker's
    own pane, so the bare '-p' form silently flags a manager pane done instead
    and starves the worker's own reap."""
    p = ssw.assemble_dispatch_prompt(
        bead_id="X-1", title="Do the thing", description="full desc here",
        worktree="/wt/wk-x", branch="wk-x", model="haiku",
    )
    assert 'tmux set-option -p -t "$TMUX_PANE" @sable_status done' in p
    assert "tmux set-option -p @sable_status done" not in p


def test_dispatch_prompt_has_no_unresolvable_templates_reference():
    """SABLE-zlu8: the worker's CWD is a project worktree, not the SABLE repo,
    so a relative 'templates/worker-dispatch.md' citation resolves nowhere —
    every fresh worker burned 1-3min on a `find /` hunting it. The inline
    contract is self-sufficient; the prompt must cite either an absolute
    existing path or no path at all."""
    p = ssw.assemble_dispatch_prompt(
        bead_id="X-1", title="Do the thing", description="full desc here",
        worktree="/wt/wk-x", branch="wk-x", model="haiku",
    )
    for line in p.splitlines():
        if "templates/" not in line:
            continue
        for token in line.split():
            if "templates/" in token and not token.startswith("/"):
                pytest.fail(f"unresolvable relative templates/ reference: {line!r}")


def test_dispatch_prompt_verifies_close_landed_before_flagging_done():
    """SABLE-u0c6: a worker that reports 'bead closed' without checking bd
    close's exit code or the bead's real status can mis-report a
    TDD-gate-denied close as success, stranding the bead in_progress with a
    pushed branch (observed live: m2tv). The contract must tell the worker to
    (a) check the close's exit code and (b) re-verify via `bd show --json`
    that status is closed BEFORE flagging done — and that guard must appear
    between the close instruction and the done-flag instruction, not after."""
    p = ssw.assemble_dispatch_prompt(
        bead_id="X-1", title="Do the thing", description="full desc here",
        worktree="/wt/wk-x", branch="wk-x", model="haiku",
    )
    assert "exit code" in p
    assert "bd show" in p
    assert "closed" in p
    close_idx = p.index("bd close X-1")
    show_idx = p.index("bd show")
    done_idx = p.index('@sable_status done')
    assert close_idx < show_idx < done_idx


def test_read_instruction_is_single_line():
    instr = ssw.read_instruction("/abs/dispatch/X-1.md")
    assert "/abs/dispatch/X-1.md" in instr
    assert "\n" not in instr


# --- bundle dispatch (SABLE-q13h) --------------------------------------------

def test_parse_bundle_ids_splits_dedupes_and_drops_lead():
    assert ssw.parse_bundle_ids("Y-2, Z-3,Y-2", "X-1") == ["Y-2", "Z-3"]
    assert ssw.parse_bundle_ids("X-1, Y-2", "X-1") == ["Y-2"]
    assert ssw.parse_bundle_ids("  ,  ", "X-1") == []


def test_parse_bundle_ids_empty_or_none():
    assert ssw.parse_bundle_ids(None, "X-1") == []
    assert ssw.parse_bundle_ids("", "X-1") == []


def test_bundle_ready_for_done_all_closed():
    assert ssw.bundle_ready_for_done(
        ["Y-2", "Z-3"], {"Y-2": "closed", "Z-3": "closed"}) is None


def test_bundle_ready_for_done_refuses_when_sibling_open():
    """TEST SPEC (SABLE-q13h): the done-flag helper refuses when a listed
    bead is still open."""
    err = ssw.bundle_ready_for_done(
        ["Y-2", "Z-3"], {"Y-2": "in_progress", "Z-3": "closed"})
    assert err is not None
    assert "Y-2" in err
    assert "Z-3" not in err


def test_bundle_ready_for_done_treats_unknown_status_as_not_closed():
    """Fail-safe: a bead missing from the status map (e.g. a lookup that
    failed) must not silently pass the gate."""
    err = ssw.bundle_ready_for_done(["Y-2"], {})
    assert err is not None
    assert "Y-2" in err


def test_assemble_dispatch_prompt_renders_all_bundled_bead_descriptions():
    """TEST SPEC (SABLE-q13h): the dispatch prompt for a bundle must contain
    every bundled bead id + description, not just the lead's."""
    p = ssw.assemble_dispatch_prompt(
        bead_id="X-1", title="Lead thing", description="lead desc",
        worktree="/wt/wk-x", branch="wk-x", model="sonnet",
        bundle=[
            {"id": "Y-2", "title": "Sibling one", "description": "sibling one desc"},
            {"id": "Z-3", "title": "Sibling two", "description": "sibling two desc"},
        ],
    )
    assert "X-1" in p and "lead desc" in p
    assert "Y-2" in p and "sibling one desc" in p
    assert "Z-3" in p and "sibling two desc" in p
    # bundle-ownership contract present and names every bead
    assert "Bundle contract" in p
    assert "regardless of who claimed" in p or "regardless of claim state" in p
    # done-flag gate line and close line both enumerate the full bundle
    assert "bd close X-1" in p
    assert "every other bundled bead listed above" in p


def test_assemble_dispatch_prompt_without_bundle_has_no_bundle_section():
    p = ssw.assemble_dispatch_prompt(
        bead_id="X-1", title="Do the thing", description="full desc here",
        worktree="/wt/wk-x", branch="wk-x", model="haiku",
    )
    assert "Bundle contract" not in p
    assert "Bundled bead" not in p
    assert "bd close X-1" in p


# --- plant-and-fail verdict requirement (SABLE-4jogz) ------------------------

_PLANT_AND_FAIL_VALUES = (
    "NOT TRIGGERED",
    "TRIGGERED AND CLEARED",
    "TRIGGERED AND DEMONSTRATED",
)


def test_assemble_dispatch_prompt_carries_plant_and_fail_verdict_normal():
    """SABLE-4jogz: every dispatched worker must receive the plant-and-fail
    verdict requirement without the dispatching manager having to remember it
    — pinned here to the literal three legal values, not a generic 'verdict'
    word, so the test can't pass on unrelated prompt text."""
    p = ssw.assemble_dispatch_prompt(
        bead_id="X-1", title="Do the thing", description="full desc here",
        worktree="/wt/wk-x", branch="wk-x", model="sonnet",
    )
    for value in _PLANT_AND_FAIL_VALUES:
        assert value in p, f"missing legal value {value!r}"
    assert "close_reason" in p
    # the requirement must precede the close instruction it decorates
    assert p.index(_PLANT_AND_FAIL_VALUES[0]) < p.index("bd close X-1")


def test_assemble_dispatch_prompt_carries_plant_and_fail_verdict_bundle():
    """Same requirement, bundle dispatch path (SABLE-q13h) — the bundle
    rendering must not crowd out the verdict requirement."""
    p = ssw.assemble_dispatch_prompt(
        bead_id="X-1", title="Lead thing", description="lead desc",
        worktree="/wt/wk-x", branch="wk-x", model="sonnet",
        bundle=[{"id": "Y-2", "title": "Sibling", "description": "sibling desc"}],
    )
    for value in _PLANT_AND_FAIL_VALUES:
        assert value in p, f"missing legal value {value!r}"


def test_assemble_dispatch_prompt_is_the_sole_render_path_for_respawn():
    """SABLE-4jogz test spec: the requirement must reach EVERY dispatch path,
    including --respawn. --respawn carries no separate prompt-render function
    — it flows through the exact same assemble_dispatch_prompt call as a
    normal dispatch (verified structurally: exactly one call site in the
    source), so the normal-path coverage above already covers --respawn. This
    test pins that structural fact so a future refactor that splits respawn
    onto its own render path cannot silently drop the requirement without
    also breaking this assertion."""
    src = Path(ssw.__file__).read_text()
    assert src.count("assemble_dispatch_prompt(") == 2  # def + the one call site


def test_claim_bundle_beads_claims_each_unclaimed_sibling(monkeypatch):
    """Claim-all-up-front (SABLE-q13h DESIGN): every bundled sibling gets the
    same `bd update --claim` the lead bead does, so none of them looks
    separately-owned."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(ssw.subprocess, "run", fake_run)
    bundle = [
        {"id": "Y-2", "assignee": None},
        {"id": "Z-3", "assignee": None},
    ]
    ssw.claim_bundle_beads(bundle, lane="optimus")
    assert calls == [
        ["bd", "update", "Y-2", "--claim"],
        ["bd", "update", "Z-3", "--claim"],
    ]


def test_claim_bundle_beads_skips_sibling_already_claimed_by_lane(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(ssw.subprocess, "run", fake_run)
    bundle = [{"id": "Y-2", "assignee": "optimus"}]
    ssw.claim_bundle_beads(bundle, lane="optimus")
    assert calls == []


def test_claim_bundle_beads_warns_but_does_not_raise_on_failure(monkeypatch, capsys):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="already claimed")

    monkeypatch.setattr(ssw.subprocess, "run", fake_run)
    bundle = [{"id": "Y-2", "assignee": None}]
    ssw.claim_bundle_beads(bundle, lane="optimus")  # must not raise
    err = capsys.readouterr().err
    assert "Y-2" in err and "already claimed" in err


# --- tag_branch_metadata (SABLE-i5739) --------------------------------------

def test_tag_branch_metadata_writes_sandboxed_set_metadata(monkeypatch):
    """Dispatch-time write the reconciliation floor's structured resolution
    depends on: `bd update <id> --sandbox --set-metadata branch=<branch>`."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(ssw.subprocess, "run", fake_run)
    ssw.tag_branch_metadata("SABLE-x1", "wk-my-slug")
    assert calls == [
        ["bd", "update", "SABLE-x1", "--sandbox", "--set-metadata", "branch=wk-my-slug"],
    ]


def test_tag_branch_metadata_swallows_bd_failure(monkeypatch):
    # a missed tag degrades resolution to the legacy prose fallback — it must
    # never raise and block dispatch.
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="db locked")

    monkeypatch.setattr(ssw.subprocess, "run", fake_run)
    ssw.tag_branch_metadata("SABLE-x1", "wk-my-slug")  # must not raise


# --- tag_footprint_metadata (SABLE-jd5fj.10) --------------------------------

def test_tag_footprint_metadata_writes_both_fields_when_both_sections_present(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(ssw.subprocess, "run", fake_run)
    description = ("Story.\n\n## File footprint\nbin/one.py, bin/two.py\n\n"
                   "## File reads\nhooks/read.sh\n")
    ssw.tag_footprint_metadata("SABLE-x1", description)
    assert calls == [
        ["bd", "update", "SABLE-x1", "--sandbox", "--set-metadata",
         "footprint_writes=bin/one.py,bin/two.py"],
        ["bd", "update", "SABLE-x1", "--sandbox", "--set-metadata",
         "footprint_reads_declared=hooks/read.sh"],
    ]


def test_tag_footprint_metadata_omits_both_keys_when_no_sections_at_all(monkeypatch):
    """THE WRITER-SIDE TRAP, negative direction: a bead with no '## File
    footprint' and no '## File reads' section must get NEITHER key stamped --
    not stamped with an empty list. ABSENT is the correct encoding of
    "nothing was supplied", and the reader distinguishes it from
    present-and-empty by key membership alone."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(ssw.subprocess, "run", fake_run)
    ssw.tag_footprint_metadata("SABLE-x1", "just a story, no sections at all")
    assert calls == [], (
        "no section declared -> no metadata write at all; a stamped empty "
        "list here would silently convert UNDECLARED into DECLARED-EMPTY")


def test_tag_footprint_metadata_writes_present_and_empty_reads_when_explicitly_declared_empty(monkeypatch):
    """The negative control's counterpart: when the planner DID declare an
    explicit (even empty) '## File reads' section, the key MUST be present
    with an empty value -- this is the "declared, found nothing" state and it
    is a real answer, not an omission."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(ssw.subprocess, "run", fake_run)
    description = "Story, no footprint section.\n\n## File reads\nnone\n"
    ssw.tag_footprint_metadata("SABLE-x1", description)
    assert calls == [
        ["bd", "update", "SABLE-x1", "--sandbox", "--set-metadata",
         "footprint_reads_declared="],
    ], "an explicitly-declared-empty reads section must still stamp the key, with an empty value"


def test_tag_footprint_metadata_swallows_bd_failure(monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="db locked")

    monkeypatch.setattr(ssw.subprocess, "run", fake_run)
    description = "Story.\n\n## File footprint\nbin/one.py\n\n## File reads\nnone\n"
    ssw.tag_footprint_metadata("SABLE-x1", description)  # must not raise


# --- worker command ---------------------------------------------------------

def test_worker_command_default_pins_model_and_auto_approves(monkeypatch):
    # SABLE-bldh.12: a hands-off worker must auto-approve writes AND bash, so the
    # default carries a bypass permission posture (configurable).
    monkeypatch.delenv("SABLE_WORKER_PERMISSION", raising=False)
    assert ssw.worker_command("haiku", None) == (
        "claude --model haiku --permission-mode bypassPermissions"
    )


def test_worker_command_permission_env_override(monkeypatch):
    monkeypatch.setenv("SABLE_WORKER_PERMISSION", "--permission-mode acceptEdits")
    assert ssw.worker_command("sonnet", None) == (
        "claude --model sonnet --permission-mode acceptEdits"
    )


def test_worker_command_override_used_verbatim():
    assert ssw.worker_command("haiku", "bash --norc") == "bash --norc"


# --- deterministic done-flag (SABLE-5v9n) ------------------------------------

def test_with_lifecycle_flags_sets_running_before_and_done_after():
    wrapped = ssw.with_lifecycle_flags("claude --model haiku")
    assert wrapped == (
        'tmux set-option -p -t "$TMUX_PANE" @sable_status running; '
        'claude --model haiku; '
        'tmux set-option -p -t "$TMUX_PANE" @sable_status done'
    )


def test_with_lifecycle_flags_uses_semicolon_not_and_and():
    """The done flip must fire even when the worker command exits non-zero or
    crashes -- `&&` would skip it exactly when a worker dies mid-task, which
    is precisely the case a deterministic reaper signal must cover."""
    wrapped = ssw.with_lifecycle_flags("bash -c 'exit 1'")
    assert "&&" not in wrapped
    assert wrapped.endswith('; tmux set-option -p -t "$TMUX_PANE" @sable_status done')


def test_with_lifecycle_flags_running_is_the_first_command():
    """Setting `running` from INSIDE the pane's own script (as the very first
    action, before the worker command even starts) instead of via a separate
    manager-side set-option call after window creation is what makes the
    done-flip race-free: a fast-exiting worker can't have its done write
    clobbered by a slower, external 'running' write racing in afterward."""
    wrapped = ssw.with_lifecycle_flags("claude --model haiku")
    assert wrapped.startswith('tmux set-option -p -t "$TMUX_PANE" @sable_status running;')


def test_with_lifecycle_flags_wraps_override_too():
    """SABLE_WORKER_CMD overrides (the test stand-in mechanism) must also be
    wrapped -- the whole point is a lifecycle flag that no longer depends on
    which command is actually running in the pane."""
    wrapped = ssw.with_lifecycle_flags(ssw.worker_command("haiku", "bash --noprofile --norc"))
    assert wrapped == (
        'tmux set-option -p -t "$TMUX_PANE" @sable_status running; '
        'bash --noprofile --norc; '
        'tmux set-option -p -t "$TMUX_PANE" @sable_status done'
    )


# --- lane identity (SABLE-bldh.13) ------------------------------------------

def test_resolve_lane_prefers_explicit_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT_NAME", "lincoln")
    assert ssw.resolve_lane("optimus") == "optimus"


def test_resolve_lane_falls_back_to_invoking_manager_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT_NAME", "tarzan")
    assert ssw.resolve_lane(None) == "tarzan"


def test_resolve_lane_empty_when_no_identity(monkeypatch):
    monkeypatch.delenv("CLAUDE_AGENT_NAME", raising=False)
    assert ssw.resolve_lane(None) == ""


def test_worker_env_args_stamps_manager_identity():
    # post-push-merge-notify fires only for MANAGER identities, so the worker must
    # carry the lane manager's name + manager role for the for-chuck handoff to
    # fire AND be attributed correctly. SABLE-38zi: it ALSO carries the
    # SABLE_WORKER_PANE marker so the SessionStart role-anchor refuses to load
    # that manager's role-card into the worker (identity bleed -> re-dispatch).
    assert ssw.worker_env_args("optimus") == [
        "-e", "CLAUDE_AGENT_NAME=optimus", "-e", "CLAUDE_AGENT_ROLE=manager",
        "-e", "SABLE_WORKER_PANE=1",
    ]


def test_worker_env_args_marks_worker_pane_even_without_lane():
    # SABLE-38zi: the worker marker is ALWAYS stamped, independent of whether a
    # lane manager identity is resolvable — a lane-less worker pane must still be
    # recognizable as a worker (role-anchor stand-down + re-dispatch guard).
    assert ssw.worker_env_args("") == ["-e", "SABLE_WORKER_PANE=1"]


def test_worker_env_args_always_contains_worker_marker():
    for lane in ("", "optimus", "tarzan"):
        assert "-e" in ssw.worker_env_args(lane)
        assert "SABLE_WORKER_PANE=1" in ssw.worker_env_args(lane)


# --- SABLE-dcw2: the worker pane must ALSO be stamped @sable_lane=<owning
# manager> so sable-worker-status can attribute it to one lane instead of every
# manager's sweep seeing every pane. worker_pane_tags is the pure tag list. ---

def test_worker_pane_tags_stamps_lane_when_resolvable():
    assert ssw.worker_pane_tags("SABLE-x", "/repo", "optimus") == [
        ("@sable_role", "worker"), ("@sable_bead", "SABLE-x"),
        ("@sable_repo", "/repo"), ("@sable_lane", "optimus"),
    ]


def test_worker_pane_tags_omits_lane_when_empty():
    # a lane-less dispatch leaves the pane unattributed (no @sable_lane), exactly
    # as an empty @sable_repo is omitted — sable-worker-status then shows it only
    # under --all, never silently folding it into some manager's lane
    assert ssw.worker_pane_tags("SABLE-x", "/repo", "") == [
        ("@sable_role", "worker"), ("@sable_bead", "SABLE-x"),
        ("@sable_repo", "/repo"),
    ]


def test_worker_pane_tags_omits_repo_when_empty_but_keeps_lane():
    assert ssw.worker_pane_tags("SABLE-x", "", "tarzan") == [
        ("@sable_role", "worker"), ("@sable_bead", "SABLE-x"),
        ("@sable_lane", "tarzan"),
    ]


def test_worker_pane_tags_never_stamps_status():
    # @sable_status is owned by the pane's own with_lifecycle_flags script
    # (SABLE-5v9n); a manager-side stamp here would race the done-flip
    keys = [k for k, _ in ssw.worker_pane_tags("SABLE-x", "/repo", "optimus")]
    assert "@sable_status" not in keys


# --- SABLE-38zi: a worker pane can NOT re-dispatch --------------------------
#
# OBSERVED 2026-07-14: a spawned worker booted as its lane MANAGER (tarzan
# identity bled in via CLAUDE_AGENT_NAME) and ran sable-spawn-worker ITSELF,
# spawning a SECOND worker pane for the SAME bead — one dispatch silently became
# two live panes, defeating the SABLE_MAX_WORKERS cap. The pane carries
# SABLE_WORKER_PANE=1 (worker_env_args); sable-spawn-worker hard-refuses when it
# sees that marker in its own env, so a worker can never re-dispatch even if a
# manager role-card somehow bled in.

def test_worker_pane_guard_blocks_when_marker_set(monkeypatch):
    monkeypatch.setenv("SABLE_WORKER_PANE", "1")
    err = ssw.worker_pane_guard()
    assert err is not None
    assert "worker" in err.lower()
    assert "SABLE_MAX_WORKERS" in err  # names the cap the re-dispatch defeats


def test_worker_pane_guard_allows_when_marker_absent(monkeypatch):
    monkeypatch.delenv("SABLE_WORKER_PANE", raising=False)
    assert ssw.worker_pane_guard() is None


def test_worker_pane_guard_allows_when_marker_empty(monkeypatch):
    # an empty marker is not a worker pane (only a truthy value marks one)
    monkeypatch.setenv("SABLE_WORKER_PANE", "")
    assert ssw.worker_pane_guard() is None


def test_main_refuses_dispatch_from_worker_pane(monkeypatch):
    # end-to-end: the guard fires FIRST in main() — before any bead fetch / tmux
    # / worktree side effect — so a refused re-dispatch leaves nothing claimed
    # and no grandchild pane. A distinct exit code (9) separates it from the
    # throttle/governance refusals (5-8). Robust regardless of tmux/bd state
    # because the guard short-circuits before touching either.
    monkeypatch.setenv("SABLE_WORKER_PANE", "1")
    assert ssw.main(["SABLE-anything", "--skip-governance"]) == 9


# --- worker window spawn argv (SABLE-zgbt) -----------------------------------

def test_new_window_args_spawns_detached_in_background():
    # SABLE-zgbt: without -d tmux makes every fresh worker window the session's
    # CURRENT window, yanking each attached client's view on every dispatch.
    args = ssw.new_window_args("sable", "worker-sable-x", "/wt/wk-x",
                               ["-e", "CLAUDE_AGENT_NAME=optimus",
                                "-e", "CLAUDE_AGENT_ROLE=manager"],
                               "claude --model haiku")
    assert args[0] == "new-window"
    assert "-d" in args
    # the detached spawn must not disturb pane-id capture, targeting, or delivery
    assert args[args.index("-t") + 1] == "sable"
    assert args[args.index("-n") + 1] == "worker-sable-x"
    assert args[args.index("-c") + 1] == "/wt/wk-x"
    assert "-P" in args and "#{pane_id}" in args
    assert "CLAUDE_AGENT_NAME=optimus" in args
    assert args[-1] == "claude --model haiku"


# --- dispatch readiness + submission (SABLE-91m3) ---------------------------

def test_pane_ready_true_on_empty_prompt():
    cap = "splash\n\n❯ \n  ddc@host:~/wt\n  bypass permissions on"
    assert ssw.pane_ready(cap) is True


def test_pane_ready_false_while_booting():
    cap = "╭─ Claude Code ─╮\n│ Welcome back │\n╰──────────────╯"
    assert ssw.pane_ready(cap) is False


def test_dispatch_landed_false_when_still_in_input_box():
    # the instruction is sitting unsubmitted in the input box (the dropped-Enter
    # race) -> NOT landed.
    cap = "❯ Read /x/SABLE-2cao.1.md in full and execute it.\n  ddc@host:~/wt"
    assert ssw.dispatch_landed(cap, "SABLE-2cao.1") is False


def test_dispatch_landed_true_when_submitted():
    # the instruction moved out of the input box (now empty) into the turn above.
    cap = ("❯ Read /x/SABLE-2cao.1.md in full and execute it.\n"
           "● Reading the dispatch...\n✻ Crystallizing…\n❯ \n  ddc@host:~/wt")
    assert ssw.dispatch_landed(cap, "SABLE-2cao.1") is True


def test_dispatch_landed_false_when_absent():
    cap = "❯ \n  ddc@host:~/wt"
    assert ssw.dispatch_landed(cap, "SABLE-2cao.1") is False


# --- wrapped-composer + control-char box detection (SABLE-1umr / SABLE-zaum) -

def test_dispatch_landed_false_when_wrapped_across_composer_lines():
    # SABLE-1umr root cause: a framed message longer than the pane width WRAPS;
    # continuation lines carry no prompt glyph, so a last-glyph-line-only box
    # check sees just the first segment and false-positives "landed" while the
    # full message is still sitting unsubmitted in the composer.
    snippet = ("⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap all lanes at 4 "
               "workers and hold pushes until chuck drains the merge queue")
    cap = ("● earlier turn output\n"
           "❯ ⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap all lanes at 4\n"
           "workers and hold pushes until chuck drains the merge queue\n"
           "  ddc@host:~/wt")
    assert ssw.dispatch_landed(cap, snippet) is False


def test_dispatch_landed_true_when_submitted_message_wrapped_midword():
    # Inverse false NEGATIVE: after a real submit, a transcript wrap that
    # splits mid-word must still match (whitespace-insensitive comparison),
    # or a landed message is reported undelivered.
    snippet = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: enforce workercaps now"
    cap = ("⟦SABLE-MSG⟧ from=lincoln to=optimus :: enforce workerca\n"
           "ps now\n"
           "● thinking…\n"
           "❯ \n  ddc@host:~/wt")
    assert ssw.dispatch_landed(cap, snippet) is True


def test_dispatch_landed_false_on_control_char_prefixed_box_line():
    # SABLE-zaum: a leading control byte on the prompt line (e.g. an echoed
    # Escape) must not make box-detection conclude "no box -> already
    # submitted" while the text sits unsubmitted.
    cap = "\x1b❯ Read /x/SABLE-2cao.1.md in full and execute it."
    assert ssw.dispatch_landed(cap, "SABLE-2cao.1") is False


def test_pane_ready_true_with_control_char_prefix():
    # Same corruption on an EMPTY prompt line must not stall readiness forever.
    cap = "splash\n\x1b❯ \n  ddc@host:~/wt"
    assert ssw.pane_ready(cap) is True


# --- startup gate clearing (SABLE-91m3 / bldh.12) ---------------------------

BYPASS_WARNING = (
    "  WARNING: Claude Code running in Bypass Permissions mode\n"
    "  By proceeding, you accept all responsibility.\n"
    "  ❯ 1. No, exit\n    2. Yes, I accept\n  Enter to confirm")

TRUST_DIALOG = (
    "  Is this a project you trust?\n"
    "  ❯ 1. Yes, I trust this folder\n    2. No, exit\n  Enter to confirm")


def test_accept_startup_gate_bypass_returns_accept_key():
    # default is '1. No, exit' -> must actively pick '2. Yes, I accept'
    assert ssw.accept_startup_gate(BYPASS_WARNING) == "2"


def test_accept_startup_gate_trust_returns_yes_key():
    assert ssw.accept_startup_gate(TRUST_DIALOG) == "1"


def test_accept_startup_gate_none_when_ready():
    assert ssw.accept_startup_gate("❯ \n  ddc@host:~/wt\n  bypass permissions on") is None


def test_pane_ready_false_on_bypass_warning():
    # the warning's prompt line is '❯ 1. No, exit', not an empty box -> not ready
    assert ssw.pane_ready(BYPASS_WARNING) is False


# --- dialog/selector posture classifier (SABLE-m94k) -------------------------
#
# 73t4 dispatch (2026-07-07): a worker pane came up on an Enter-to-select
# dialog accept_startup_gate does NOT recognize (only the two known startup
# gates above), so wait_for_ready polled to timeout and its False return was
# discarded — the dispatch text got typed straight into the dialog. This
# classifier lets the caller tell "unrecognized dialog, never type into it"
# apart from "still booting, keep waiting".

UNKNOWN_SELECT_DIALOG = (
    "  ? Which package manager would you like to use?\n"
    "  > 1. npm\n"
    "    2. yarn\n"
    "    3. pnpm\n"
    "  (Use arrow keys, Enter to select)")


def test_dialog_posture_true_for_unrecognized_select_menu():
    assert ssw.dialog_posture(UNKNOWN_SELECT_DIALOG) is True


def test_dialog_posture_true_for_known_bypass_gate():
    # the classifier is deliberately broader than accept_startup_gate: it also
    # flags gates that DO happen to be recognized/dismissable.
    assert ssw.dialog_posture(BYPASS_WARNING) is True


def test_dialog_posture_true_for_known_trust_gate():
    assert ssw.dialog_posture(TRUST_DIALOG) is True


def test_dialog_posture_false_on_empty_composer_prompt():
    cap = "splash\n\n❯ \n  ddc@host:~/wt\n  bypass permissions on"
    assert ssw.dialog_posture(cap) is False


def test_dialog_posture_false_while_booting():
    # not-ready (no empty prompt yet) is NOT the same as a stuck dialog — a
    # splash screen has neither a numbered menu nor a keypress affordance.
    cap = "╭─ Claude Code ─╮\n│ Welcome back │\n╰──────────────╯"
    assert ssw.dialog_posture(cap) is False


def test_dialog_posture_false_on_single_incidental_numbered_line():
    # conservative: ONE numbered-looking line (e.g. scrollback content) must
    # not false-positive a legit spawn — a real menu always has 2+ options.
    cap = "❯ \n  1. only one line here, not a menu\n  ddc@host:~/wt"
    assert ssw.dialog_posture(cap) is False


# --- refresh: base-ref fallback (re-homed pre-dispatch-refresh, SABLE-bldh.8) -

import subprocess  # noqa: E402


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture()
def worktree_with_origin(tmp_path):
    """A tiny local repo with a remote named 'origin' (also local), so
    resolve_base_ref's rev-parse checks resolve without any network access."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    (work / "f.txt").write_text("1")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "origin", "main")
    _git(work, "fetch", "origin")
    return work


def test_resolve_base_ref_returns_desired_when_it_exists(worktree_with_origin):
    assert ssw.resolve_base_ref(str(worktree_with_origin), "origin/main") == "origin/main"


def test_resolve_base_ref_falls_back_to_origin_main_when_desired_missing(worktree_with_origin):
    assert (ssw.resolve_base_ref(str(worktree_with_origin), "origin/no-such-branch")
            == "origin/main")


def test_resolve_base_ref_falls_back_to_desired_when_nothing_resolves(tmp_path):
    lone = tmp_path / "lone"
    lone.mkdir()
    _git(lone, "init", "-b", "main")
    _git(lone, "config", "user.email", "t@example.com")
    _git(lone, "config", "user.name", "T")
    (lone / "f.txt").write_text("1")
    _git(lone, "add", "f.txt")
    _git(lone, "commit", "-m", "init")
    # no origin remote, no upstream configured -> nothing resolves
    assert ssw.resolve_base_ref(str(lone), "origin/main") == "origin/main"


# --- refresh: per-repo integration-branch base resolution (SABLE-ybdm) -------
#
# resolve_base_ref hardcoded an origin/main fallback and never consulted the
# repo's OWN integration branch, so a reused worktree on a non-main integration
# repo (tmux-only here) was silently rebased onto DIVERGED origin/main at spawn
# time (same corruption class as SABLE-4amz at push time). The fix ports a Python
# mirror of lib-identity.sh's sable_resolve_integration_branch + defaults the
# refresh base to origin/<INT> when published.

def _rev(cwd, ref="HEAD"):
    return subprocess.run(["git", "-C", str(cwd), "rev-parse", ref],
                          capture_output=True, text=True).stdout.strip()


def _bare_repo(tmp_path, name="r"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "seed.txt").write_text("seed")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-m", "seed")
    return repo


def test_resolve_integration_branch_prefers_git_config(worktree_with_origin):
    _git(worktree_with_origin, "config", "sable.integrationBranch", "tmux-only")
    assert ssw.resolve_integration_branch(str(worktree_with_origin)) == "tmux-only"


def test_resolve_integration_branch_reads_dot_sable_file(tmp_path):
    repo = _bare_repo(tmp_path)
    (repo / ".sable").write_text("# comment\nintegrationBranch=dev\n")
    assert ssw.resolve_integration_branch(str(repo)) == "dev"


def test_resolve_integration_branch_git_config_wins_over_dot_sable(tmp_path):
    repo = _bare_repo(tmp_path)
    (repo / ".sable").write_text("integrationBranch=from-file\n")
    _git(repo, "config", "sable.integrationBranch", "from-config")
    assert ssw.resolve_integration_branch(str(repo)) == "from-config"


def test_resolve_integration_branch_env_fallback_strips_origin(monkeypatch, tmp_path):
    repo = _bare_repo(tmp_path)
    monkeypatch.delenv("SABLE_INTEGRATION_BRANCH", raising=False)
    monkeypatch.setenv("SABLE_BASE_BRANCH", "origin/tmux-only")
    assert ssw.resolve_integration_branch(str(repo)) == "tmux-only"


def test_resolve_integration_branch_defaults_to_main(monkeypatch, tmp_path):
    repo = _bare_repo(tmp_path)
    monkeypatch.delenv("SABLE_INTEGRATION_BRANCH", raising=False)
    monkeypatch.delenv("SABLE_BASE_BRANCH", raising=False)
    assert ssw.resolve_integration_branch(str(repo)) == "main"


@pytest.fixture()
def worktree_with_published_integration(tmp_path):
    """Repo whose integration branch 'tmux-only' is PUBLISHED at origin/tmux-only,
    with origin/main DIVERGED from it (each carries commits the other does not) —
    the exact SABLE-ybdm shape. Returns the primary checkout path."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    _git(work, "config", "sable.integrationBranch", "tmux-only")
    (work / "root.txt").write_text("root")
    _git(work, "add", "root.txt")
    _git(work, "commit", "-m", "root")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "origin", "main")
    # integration branch diverges from main
    _git(work, "checkout", "-b", "tmux-only")
    (work / "int.txt").write_text("integration-only")
    _git(work, "add", "int.txt")
    _git(work, "commit", "-m", "integration lineage commit")
    _git(work, "push", "origin", "tmux-only")
    # advance origin/main independently so it is DIVERGED from tmux-only
    _git(work, "checkout", "main")
    (work / "main.txt").write_text("main-only")
    _git(work, "add", "main.txt")
    _git(work, "commit", "-m", "main-only commit")
    _git(work, "push", "origin", "main")
    _git(work, "fetch", "origin")
    return work


def test_resolve_refresh_base_defaults_to_published_integration_branch(
        monkeypatch, worktree_with_published_integration):
    # SABLE-ybdm: SABLE_BASE_BRANCH unset -> a reused worktree on a non-main
    # integration repo must resolve origin/<INT>, NOT the diverged origin/main.
    monkeypatch.delenv("SABLE_BASE_BRANCH", raising=False)
    monkeypatch.delenv("SABLE_INTEGRATION_BRANCH", raising=False)
    assert (ssw.resolve_refresh_base(
        str(worktree_with_published_integration), None) == "origin/tmux-only")


def test_resolve_refresh_base_explicit_base_still_wins(
        worktree_with_published_integration):
    # An explicit base (SABLE_BASE_BRANCH) still wins over the default (4amz shape).
    assert (ssw.resolve_refresh_base(
        str(worktree_with_published_integration), "origin/main") == "origin/main")


def test_resolve_refresh_base_falls_back_to_origin_main_without_integration(
        monkeypatch, worktree_with_origin):
    # No sable.integrationBranch, no origin/<INT> published -> origin/main.
    monkeypatch.delenv("SABLE_BASE_BRANCH", raising=False)
    monkeypatch.delenv("SABLE_INTEGRATION_BRANCH", raising=False)
    assert ssw.resolve_refresh_base(str(worktree_with_origin), None) == "origin/main"


def test_refresh_worktree_rebases_onto_integration_not_diverged_main(
        monkeypatch, worktree_with_published_integration):
    # SABLE-ybdm integration test: a reused LINKED worktree cut from
    # origin/tmux-only must NOT be re-parented onto diverged origin/main — its
    # HEAD must be unchanged (rebase onto its own integration base is a no-op).
    work = worktree_with_published_integration
    _git(work, "branch", "wk-x", "origin/tmux-only")
    wt = work.parent / "wk-x"
    _git(work, "worktree", "add", str(wt), "wk-x")
    head_before = _rev(wt)
    monkeypatch.delenv("SABLE_BASE_BRANCH", raising=False)
    monkeypatch.delenv("SABLE_INTEGRATION_BRANCH", raising=False)
    warning = ssw.refresh_worktree(str(wt), None)
    assert warning is None, warning
    assert _rev(wt) == head_before  # no lineage rewrite
    # still an descendant of the integration branch, never re-parented onto main
    assert subprocess.run(
        ["git", "-C", str(wt), "merge-base", "--is-ancestor",
         "origin/tmux-only", "HEAD"], capture_output=True).returncode == 0
    assert subprocess.run(
        ["git", "-C", str(wt), "merge-base", "--is-ancestor",
         "origin/main", "HEAD"], capture_output=True).returncode != 0


# --- refresh: primary-checkout + invalid-path stand-down (SABLE-4byx) ---------
#
# refresh_worktree must STAND DOWN (warn, no fetch/rebase) when the target is the
# primary checkout (git-dir == git-common-dir) — the market-brief-package-o45j
# shared-tree rebase race — and refuse an empty/nonexistent worktree path rather
# than operate on a cwd fallback.

def test_refresh_worktree_stands_down_on_primary_checkout(
        monkeypatch, worktree_with_origin):
    work = worktree_with_origin  # a primary checkout (git init)
    # make origin/main AHEAD of local main so a rebase WOULD move HEAD
    _git(work, "commit", "--allow-empty", "-m", "c2")
    _git(work, "push", "origin", "main")
    _git(work, "reset", "--hard", "HEAD~1")
    _git(work, "fetch", "origin")
    head_before = _rev(work)
    behind = subprocess.run(
        ["git", "-C", str(work), "rev-list", "--count", "HEAD..origin/main"],
        capture_output=True, text=True).stdout.strip()
    assert behind == "1"  # sanity: a rebase would advance HEAD
    monkeypatch.delenv("SABLE_BASE_BRANCH", raising=False)
    warning = ssw.refresh_worktree(str(work), None)
    assert warning is not None and "primary" in warning.lower()
    assert _rev(work) == head_before  # NO rebase ran


def test_refresh_worktree_still_refreshes_linked_worktree(
        monkeypatch, worktree_with_origin):
    work = worktree_with_origin
    _git(work, "commit", "--allow-empty", "-m", "c2")
    _git(work, "push", "origin", "main")
    _git(work, "reset", "--hard", "HEAD~1")
    _git(work, "fetch", "origin")
    _git(work, "branch", "wk-y", "HEAD")
    wt = work.parent / "wk-y"
    _git(work, "worktree", "add", str(wt), "wk-y")
    head_before = _rev(wt)
    monkeypatch.delenv("SABLE_BASE_BRANCH", raising=False)
    monkeypatch.delenv("SABLE_INTEGRATION_BRANCH", raising=False)
    warning = ssw.refresh_worktree(str(wt), None)
    assert warning is None, warning
    assert _rev(wt) != head_before               # a real rebase ran
    assert _rev(wt) == _rev(wt, "origin/main")   # advanced onto origin/main


def test_refresh_worktree_refuses_missing_worktree_path(tmp_path):
    warning = ssw.refresh_worktree(str(tmp_path / "does-not-exist"), None)
    assert warning is not None and "no valid worktree" in warning.lower()


def test_refresh_worktree_refuses_empty_worktree_path():
    warning = ssw.refresh_worktree("", None)
    assert warning is not None and "no valid worktree" in warning.lower()


# --- SABLE-3eax: --respawn (REVISE / push-only close-out into an existing
# worktree) -------------------------------------------------------------------
#
# The manager REVISE pattern re-spawns a worker into the SAME worktree to finish
# a closed bead's landing. Three walls hit the governance the bldh.8 re-home
# moved into this helper: (1) an unconditional `bd update --claim` traceback-
# crashes on a CLOSED bead; (2) a reaped worker strands its worktree tree-claim,
# blocking the next spawn until TTL/force-release; (3) the duplicate-dispatch
# guard refused a reused worktree outright, with only the far-too-blunt
# --skip-governance to bypass it. --respawn is the first-class path: reopen a
# closed bead, release a stranded stale claim, and pass the duplicate guard when
# no LIVE pane carries the tag — while keeping model-check active.

# (a) reopen: a respawn targets a bead that was CLOSED and must be reopened to
# in_progress before the claim/close flow, or the claim traceback-crashes.

def test_needs_reopen_true_for_closed_bead():
    assert ssw.needs_reopen({"id": "X-1", "status": "closed"}) is True


def test_needs_reopen_false_for_in_progress_or_open():
    assert ssw.needs_reopen({"id": "X-1", "status": "in_progress"}) is False
    assert ssw.needs_reopen({"id": "X-1", "status": "open"}) is False
    assert ssw.needs_reopen({"id": "X-1"}) is False


# (c) duplicate guard under respawn: a reused worktree is EXPECTED (never a
# duplicate signal), but a LIVE worker pane still carrying the bead tag must
# STILL block (two workers racing the same push).

def test_respawn_ignores_worktree_evidence():
    # worktree-evidence alone (a prior dispatch's tree, deliberately reused) must
    # NOT block a respawn — the whole point is to re-enter that same worktree.
    assert ssw.already_in_progress_check(
        {"id": "X-1", "status": "in_progress", "assignee": "tarzan"},
        pane_evidence=False, worktree_evidence=True, respawn=True) is None


def test_respawn_still_blocks_on_live_pane_evidence():
    # the safety-critical case: a LIVE worker pane tagged with the bead means a
    # worker is already running it — refuse a second respawn even in respawn mode.
    err = ssw.already_in_progress_check(
        {"id": "X-1", "status": "in_progress", "assignee": "tarzan"},
        pane_evidence=True, worktree_evidence=False, respawn=True)
    assert err is not None and "X-1" in err and "IN_PROGRESS" in err


def test_respawn_default_off_preserves_worktree_evidence_block():
    # regression guard: without respawn=True the worktree-evidence block still
    # fires exactly as before (SABLE-676c behavior is unchanged for fresh spawns).
    err = ssw.already_in_progress_check(
        {"id": "X-1", "status": "in_progress"},
        pane_evidence=False, worktree_evidence=True)
    assert err is not None and "worktree" in err


# (b) stale tree-claim release: a reaped worker's claim (holder has no live pane)
# must be released before the fresh worker runs git, or tree-claim.sh denies its
# index-mutating git ops until the claim TTL-expires.

def _linked_worktree(tmp_path):
    """A real repo + a linked worktree, so tree_claim_file resolves the
    per-worktree git-dir (`.git/worktrees/<name>`) exactly like tree-claim.sh."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    (work / "f.txt").write_text("1")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "wk-x", "HEAD")
    wt = tmp_path / "wk-x"
    _git(work, "worktree", "add", str(wt), "wk-x")
    return work, wt


def test_tree_claim_file_resolves_per_worktree_gitdir(tmp_path):
    _work, wt = _linked_worktree(tmp_path)
    cf = ssw.tree_claim_file(str(wt))
    assert cf is not None
    assert cf.endswith("/sable-tree-claim")
    # the linked worktree's git-dir is under .git/worktrees/<name>, NOT the
    # shared common dir — each worktree gets its OWN claim file.
    assert "/worktrees/wk-x/" in cf


def test_tree_claim_file_none_outside_git_repo(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert ssw.tree_claim_file(str(d)) is None


def test_release_stale_tree_claim_removes_when_holder_dead(tmp_path):
    _work, wt = _linked_worktree(tmp_path)
    cf = ssw.tree_claim_file(str(wt))
    Path(cf).write_text("dead-session-uuid 1600000000 tarzan\n")
    msg = ssw.release_stale_tree_claim(str(wt), holder_pane_live=False)
    assert msg is not None and "released" in msg.lower()
    assert not Path(cf).exists()


def test_release_stale_tree_claim_preserves_when_holder_live(tmp_path):
    # a LIVE holder's claim must NEVER be released (respawn is refused elsewhere
    # when a live pane exists; this is the belt-and-suspenders guard).
    _work, wt = _linked_worktree(tmp_path)
    cf = ssw.tree_claim_file(str(wt))
    Path(cf).write_text("live-session-uuid 1600000000 tarzan\n")
    assert ssw.release_stale_tree_claim(str(wt), holder_pane_live=True) is None
    assert Path(cf).exists()


def test_release_stale_tree_claim_noop_without_claim_file(tmp_path):
    _work, wt = _linked_worktree(tmp_path)
    assert ssw.release_stale_tree_claim(str(wt), holder_pane_live=False) is None


def test_release_stale_tree_claim_noop_outside_git_repo(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert ssw.release_stale_tree_claim(str(d), holder_pane_live=False) is None


def test_main_respawn_requires_worktree():
    # --respawn re-enters a SPECIFIC existing worktree; without --worktree it is a
    # usage error (argparse exits 2) — refused before any bead fetch / tmux side
    # effect. This is the guard that keeps respawn from silently creating a fresh
    # tree that has nothing to revise.
    with pytest.raises(SystemExit) as exc:
        ssw.main(["SABLE-x", "--respawn"])
    assert exc.value.code == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# --- fixture linking into worktrees (SABLE-ji4h) ----------------------------
# A git worktree materialises TRACKED files only, so gitignored integration
# fixtures vanish from every worker tree and the suite silently all-skips.
# These cover the PLANNER, which holds every decision; link_fixture_paths only
# executes the plan.

def _plan(tmp_path, rels, make_src=True, make_dst=False):
    top = tmp_path / "repo"
    wt = tmp_path / "wk-thing"
    (top / "sub").mkdir(parents=True)
    wt.mkdir()
    if make_src:
        for r in rels:
            (top / r).mkdir(parents=True, exist_ok=True)
    if make_dst:
        for r in rels:
            (wt / r).mkdir(parents=True, exist_ok=True)
    return ssw.plan_fixture_links(str(top), str(wt), rels), top, wt


def test_plan_links_existing_source_absent_destination(tmp_path):
    plan, top, wt = _plan(tmp_path, ["fixtures"])
    assert [p[1] for p in plan] == ["link"]
    assert plan[0][2] == str(top / "fixtures")
    assert plan[0][3] == str(wt / "fixtures")


def test_plan_skips_when_source_absent(tmp_path):
    plan, _, _ = _plan(tmp_path, ["nope"], make_src=False)
    assert [p[1] for p in plan] == ["skip-missing-source"]


def test_plan_skips_when_destination_already_present(tmp_path):
    plan, _, _ = _plan(tmp_path, ["fixtures"], make_dst=True)
    assert [p[1] for p in plan] == ["skip-exists"]


def test_plan_rejects_path_escaping_the_repo(tmp_path):
    plan, _, _ = _plan(tmp_path, ["../outside"], make_src=False)
    assert [p[1] for p in plan] == ["skip-escapes-repo"], (
        "a traversal must be reported as an escape, not as a missing source"
    )


def test_plan_handles_multiple_paths_independently(tmp_path):
    top = tmp_path / "repo"
    wt = tmp_path / "wk-thing"
    top.mkdir()
    wt.mkdir()
    (top / "have").mkdir()
    plan = ssw.plan_fixture_links(str(top), str(wt), ["have", "missing"])
    assert [p[1] for p in plan] == ["link", "skip-missing-source"]


def test_plan_is_empty_for_repos_that_opted_out(tmp_path):
    top = tmp_path / "repo"
    wt = tmp_path / "wk-thing"
    top.mkdir()
    wt.mkdir()
    assert ssw.plan_fixture_links(str(top), str(wt), []) == []


def test_link_creates_a_symlink_not_a_copy(tmp_path):
    top = tmp_path / "repo"
    wt = tmp_path / "wk-thing"
    (top / "fixtures").mkdir(parents=True)
    (top / "fixtures" / "book.xlsx").write_text("payload")
    wt.mkdir()
    plan = ssw.plan_fixture_links(str(top), str(wt), ["fixtures"])
    assert plan[0][1] == "link"
    import os as _os
    _os.symlink(plan[0][2], plan[0][3])
    dst = wt / "fixtures"
    assert dst.is_symlink(), "must be a link; copying gitignored customer data is not permitted"
    assert (dst / "book.xlsx").read_text() == "payload"


def test_configured_fixture_paths_empty_when_key_unset(tmp_path):
    import subprocess as _sp
    _sp.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert ssw.configured_fixture_paths(str(tmp_path)) == []


def test_configured_fixture_paths_reads_all_values(tmp_path):
    import subprocess as _sp
    _sp.run(["git", "init", "-q", str(tmp_path)], check=True)
    for v in ("fixtures", "data/samples"):
        _sp.run(["git", "-C", str(tmp_path), "config", "--add",
                 ssw.FIXTURE_PATH_CONFIG_KEY, v], check=True)
    assert ssw.configured_fixture_paths(str(tmp_path)) == ["fixtures", "data/samples"]


# SABLE-6sdpx: `git config --get-all` exits 1 SPECIFICALLY for "key not
# found" (the legitimate-empty case pinned above) — every OTHER nonzero exit
# is a real failure (git could not even evaluate the key) that must not be
# folded into the same silent-empty return. Both directions covered: a
# genuinely broken config warns loudly on stderr; an unset key (already
# proven above) stays silent.

def test_configured_fixture_paths_warns_loudly_on_real_git_failure(tmp_path, capsys):
    import subprocess as _sp
    _sp.run(["git", "init", "-q", str(tmp_path)], check=True)
    # a malformed local config makes `git config --get-all` fail with a git
    # error (exit 128), NOT the "key unset" exit 1 — this is the real-failure
    # case, distinct from the legitimate-empty case above.
    cfg = tmp_path / ".git" / "config"
    with cfg.open("a") as f:
        f.write("[bad syntax here no equals\n")

    result = ssw.configured_fixture_paths(str(tmp_path))
    assert result == [], "a broken config must fail safe (no fixtures), never raise"
    err = capsys.readouterr().err
    assert "WARNING" in err and "git config" in err, (
        "a real git failure (not just an unset key) must warn loudly, or a "
        "repo that DID opt in via sable.fixturePath silently runs workers "
        "without their fixtures with zero signal anywhere"
    )


def test_configured_fixture_paths_unset_key_stays_silent(tmp_path, capsys):
    import subprocess as _sp
    _sp.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert ssw.configured_fixture_paths(str(tmp_path)) == []
    assert capsys.readouterr().err == "", "an unset key is not a failure, must not warn"


# --- unmerged-blocker advisory (SABLE-d5iku) --------------------------------
# The check that `bd ready` cannot make: a blocker's STATUS closing releases its
# dependent, but a BASE-PREMISE bead needs the blocker's CODE on the branch it
# forks from. These cover the two helpers standing between the governance chain
# and bin/sable-dep-check. Every case is paired with its complement — a guard
# that only ever fires has traded a false-go for a false-block.

def _fake_dep_check(tmp_path, stdout="", exit_code=0, sleep=0.0, stderr=""):
    """A stand-in for bin/sable-dep-check. The CHECKER's own behaviour is
    covered by bin/test_sable_dep_check.py and, against real git + real bd, by
    hooks/test/test-dep-merge-state.sh; what is under test here is the wiring
    around it — resolution, arguments, and the fail-open contract."""
    script = tmp_path / "sable-dep-check"
    body = "#!/usr/bin/env bash\n"
    if sleep:
        body += f"sleep {sleep}\n"
    body += f'printf "%s" "$*" > "{tmp_path}/argv"\n'
    if stdout:
        body += f"printf '%s\\n' {stdout!r}\n"
    if stderr:
        body += f"printf '%s\\n' {stderr!r} >&2\n"
    body += f"exit {exit_code}\n"
    script.write_text(body)
    script.chmod(0o755)
    return script


def test_resolve_dep_check_bin_prefers_explicit_override(tmp_path, monkeypatch):
    script = _fake_dep_check(tmp_path)
    monkeypatch.setenv("SABLE_DEP_CHECK_BIN", str(script))
    assert ssw.resolve_dep_check_bin() == str(script)


def test_resolve_dep_check_bin_override_that_is_not_executable_is_not_used(
        tmp_path, monkeypatch):
    """A typo'd override must degrade to None (advisory skipped), never to a
    path that will fail to exec on every dispatch."""
    dud = tmp_path / "not-there"
    monkeypatch.setenv("SABLE_DEP_CHECK_BIN", str(dud))
    assert ssw.resolve_dep_check_bin() is None


def test_resolve_dep_check_bin_falls_back_to_path(tmp_path, monkeypatch):
    _fake_dep_check(tmp_path)
    monkeypatch.delenv("SABLE_DEP_CHECK_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert ssw.resolve_dep_check_bin() == str(tmp_path / "sable-dep-check")


def test_resolve_dep_check_bin_falls_back_to_repo_sibling(monkeypatch):
    """A dev checkout whose bins are not installed still gets the guard: the
    sibling of this very program is the last resort."""
    monkeypatch.delenv("SABLE_DEP_CHECK_BIN", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent-dir-for-this-test")
    resolved = ssw.resolve_dep_check_bin()
    assert resolved is not None
    assert resolved.endswith("/sable-dep-check")


def test_dep_merge_advisory_returns_the_warning_text(tmp_path, monkeypatch):
    _fake_dep_check(tmp_path, stdout="UNMERGED-BLOCKER WARNING: SABLE-dep", exit_code=3)
    monkeypatch.setenv("SABLE_DEP_CHECK_BIN", str(tmp_path / "sable-dep-check"))
    out = ssw.dep_merge_advisory(["SABLE-dep"], str(tmp_path))
    assert out is not None and "UNMERGED-BLOCKER WARNING" in out


def test_dep_merge_advisory_silent_when_checker_prints_nothing(tmp_path, monkeypatch):
    """--format=hook prints NOTHING when clean, so emptiness is the whole
    decision — a clean dispatch must produce no line at all."""
    _fake_dep_check(tmp_path, stdout="", exit_code=0)
    monkeypatch.setenv("SABLE_DEP_CHECK_BIN", str(tmp_path / "sable-dep-check"))
    assert ssw.dep_merge_advisory(["SABLE-dep"], str(tmp_path)) is None


def test_dep_merge_advisory_reads_text_not_exit_code_for_a_warning(tmp_path, monkeypatch):
    """Text is still what makes a WARNING: present with a 0 exit still warns
    (the exit code is not required to recognize a finding), and a nonzero exit
    with no text is NEVER turned into a fabricated WARNING — the caller never
    invents warning text the checker did not itself produce. SABLE-wezu1's
    revise replaced the original exit-4-only denylist with an allowlist of the
    single legal silent case (exit 0, no text), so bare exit 3 (which real
    sable-dep-check never actually produces; findings always carry text) is no
    longer silent either — it is COULD NOT ASSESS, same as any other
    unexplained empty-text nonzero exit, never a fabricated WARNING and never
    the clean-path None."""
    monkeypatch.setenv("SABLE_DEP_CHECK_BIN", str(tmp_path / "sable-dep-check"))
    _fake_dep_check(tmp_path, stdout="UNMERGED-BLOCKER WARNING: x", exit_code=0)
    assert ssw.dep_merge_advisory(["SABLE-a"], str(tmp_path)) is not None
    _fake_dep_check(tmp_path, stdout="", exit_code=3)
    out = ssw.dep_merge_advisory(["SABLE-a"], str(tmp_path))
    assert out is not None and "COULD NOT ASSESS" in out
    assert "UNMERGED-BLOCKER WARNING" not in out


def test_dep_merge_advisory_passes_hook_format_repo_and_every_bead(tmp_path, monkeypatch):
    """The bundle siblings travel too: a --bundle sibling can be the
    base-premise bead just as easily as the lead."""
    _fake_dep_check(tmp_path, stdout="warn")
    monkeypatch.setenv("SABLE_DEP_CHECK_BIN", str(tmp_path / "sable-dep-check"))
    ssw.dep_merge_advisory(["SABLE-lead", "SABLE-sib"], "/some/repo")
    argv = (tmp_path / "argv").read_text()
    assert "--format=hook" in argv
    assert "--repo /some/repo" in argv
    assert "SABLE-lead" in argv and "SABLE-sib" in argv


def test_dep_merge_advisory_disabled_by_env(tmp_path, monkeypatch):
    _fake_dep_check(tmp_path, stdout="UNMERGED-BLOCKER WARNING: x")
    monkeypatch.setenv("SABLE_DEP_CHECK_BIN", str(tmp_path / "sable-dep-check"))
    monkeypatch.setenv("SABLE_DEP_MERGE_GUARD", "0")
    assert ssw.dep_merge_advisory(["SABLE-dep"], str(tmp_path)) is None
    # ...and the complement: the default (unset) is ON. A guard that ships
    # off-by-default protects nothing.
    monkeypatch.delenv("SABLE_DEP_MERGE_GUARD")
    assert ssw.dep_merge_advisory(["SABLE-dep"], str(tmp_path)) is not None


def test_dep_merge_advisory_fails_open_when_checker_is_absent(monkeypatch):
    monkeypatch.setenv("SABLE_DEP_CHECK_BIN", "/nonexistent/sable-dep-check")
    assert ssw.dep_merge_advisory(["SABLE-dep"], "/tmp") is None


def test_dep_merge_advisory_fails_open_on_no_beads(tmp_path, monkeypatch):
    _fake_dep_check(tmp_path, stdout="warn")
    monkeypatch.setenv("SABLE_DEP_CHECK_BIN", str(tmp_path / "sable-dep-check"))
    assert ssw.dep_merge_advisory([], str(tmp_path)) is None
    assert ssw.dep_merge_advisory(["", None], str(tmp_path)) is None


def test_dep_merge_advisory_reports_could_not_assess_on_timeout(tmp_path, monkeypatch):
    """SABLE-wezu1: a slow/contended bd must cost the DISPATCH never — the
    fail-open decision is unchanged and this call still returns in time to let
    the manager proceed — but it must NOT cost the report by going silent. The
    old behaviour (asserted `is None` here, indistinguishable from a healthy
    clean run) was the exact defect: a bd hiccup and 'no unmerged blocker'
    read identically on the dispatch path. Now the outer timeout itself is a
    REPORTED could-not-assess outcome, distinct from both the clean silence
    and a genuine warning."""
    _fake_dep_check(tmp_path, stdout="UNMERGED-BLOCKER WARNING: x", sleep=3)
    monkeypatch.setenv("SABLE_DEP_CHECK_BIN", str(tmp_path / "sable-dep-check"))
    monkeypatch.setenv("SABLE_DEP_CHECK_TIMEOUT", "0.3")
    out = ssw.dep_merge_advisory(["SABLE-dep"], str(tmp_path))
    assert out is not None, "a timed-out check must report, not collapse into the clean-path None"
    assert "COULD NOT ASSESS" in out
    assert "UNMERGED-BLOCKER WARNING" not in out, \
        "the checker's own (unread, killed-before-finishing) text must not leak through as a real finding"


def test_dep_merge_advisory_reports_could_not_assess_on_bare_exit_unknown(tmp_path, monkeypatch):
    """SABLE-wezu1's other reproduced door: sable-dep-check's own exit 4
    (EXIT_UNKNOWN) always carries text in production (render_unknown_block),
    but the caller must not TRUST that as an invariant — if it ever exits 4
    with nothing on stdout, that is still COULD NOT ASSESS, not the same
    silence a healthy exit-0-empty-stdout run produces. This is the bead's
    named unit spec: 'stub sable-dep-check to exit 4 with empty hook text'."""
    _fake_dep_check(tmp_path, stdout="", exit_code=4)
    monkeypatch.setenv("SABLE_DEP_CHECK_BIN", str(tmp_path / "sable-dep-check"))
    out = ssw.dep_merge_advisory(["SABLE-dep"], str(tmp_path))
    assert out is not None, "exit 4 must never collapse into the clean-path None (SABLE-wezu1)"
    assert "COULD NOT ASSESS" in out
    assert "UNMERGED-BLOCKER WARNING" not in out


def test_dep_merge_advisory_reports_could_not_assess_on_unexpected_crash(
        tmp_path, monkeypatch):
    """SABLE-wezu1's revise: the residual door. The first pass denylisted
    exit 4 specifically, so an UNEXPECTED nonzero exit — e.g. exit 1 from a
    crashed checker, empty stdout, traceback on stderr — fell through to
    `return None`, reading identically to a healthy clean run: a crashed
    checker and a genuinely clean one produced the same report. Enumerating
    the single legal silent case (exit 0, no text) instead of the known-bad
    ones closes this door too, without a manager needing to name every exit
    code a future regression might produce."""
    _fake_dep_check(tmp_path, stdout="", exit_code=1,
                     stderr="Traceback (most recent call last):\nRuntimeError: bd locked")
    monkeypatch.setenv("SABLE_DEP_CHECK_BIN", str(tmp_path / "sable-dep-check"))
    out = ssw.dep_merge_advisory(["SABLE-dep"], str(tmp_path))
    assert out is not None, \
        "an unexpected crash must never collapse into the clean-path None"
    assert "COULD NOT ASSESS" in out
    assert "UNMERGED-BLOCKER WARNING" not in out


def test_dep_merge_advisory_relays_trimmed_stderr_on_could_not_assess(
        tmp_path, monkeypatch):
    """stderr is captured (capture_output=True) via subprocess.run but was
    previously discarded on every could-not-assess leg. On a crash it holds
    the whole diagnostic; without it a manager seeing COULD NOT ASSESS has to
    reproduce the crash by hand just to learn why."""
    _fake_dep_check(tmp_path, stdout="", exit_code=1,
                     stderr="Traceback (most recent call last):\nRuntimeError: bd locked")
    monkeypatch.setenv("SABLE_DEP_CHECK_BIN", str(tmp_path / "sable-dep-check"))
    out = ssw.dep_merge_advisory(["SABLE-dep"], str(tmp_path))
    assert out is not None
    assert "RuntimeError: bd locked" in out


def test_dep_merge_advisory_still_silent_on_healthy_exit_zero(tmp_path, monkeypatch):
    """Regression complement to the two tests above: SABLE-wezu1 must not make
    the clean path noisy. This is the property the fleet-wide activation
    depends on (bin/sable-spawn-worker is a live symlink into the shared
    checkout — every manager and worker starts running this code the moment
    the fix lands, with no staged rollout), so it is asserted here explicitly
    rather than only inferred from the exit-0/empty-stdout branch reading
    'return None'."""
    _fake_dep_check(tmp_path, stdout="", exit_code=0)
    monkeypatch.setenv("SABLE_DEP_CHECK_BIN", str(tmp_path / "sable-dep-check"))
    assert ssw.dep_merge_advisory(["SABLE-dep"], str(tmp_path)) is None


def test_dep_merge_advisory_reports_could_not_assess_when_checker_errors_to_run(
        tmp_path, monkeypatch):
    """The OSError/SubprocessError leg (distinct from TimeoutExpired above) —
    e.g. the resolved binary vanishes or loses its exec bit between resolution
    and exec. Also a bd-hiccup-shaped failure of the OUTER call, not something
    sable-dep-check itself got to report."""
    script = tmp_path / "sable-dep-check"
    script.write_text("#!/usr/bin/env bash\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv("SABLE_DEP_CHECK_BIN", str(script))

    real_run = ssw.subprocess.run

    def _boom(*args, **kwargs):
        raise OSError("simulated exec failure")

    monkeypatch.setattr(ssw.subprocess, "run", _boom)
    out = ssw.dep_merge_advisory(["SABLE-dep"], str(tmp_path))
    monkeypatch.setattr(ssw.subprocess, "run", real_run)
    assert out is not None
    assert "COULD NOT ASSESS" in out


def test_dep_merge_advisory_unparseable_timeout_keeps_the_default(tmp_path, monkeypatch):
    _fake_dep_check(tmp_path, stdout="warn")
    monkeypatch.setenv("SABLE_DEP_CHECK_BIN", str(tmp_path / "sable-dep-check"))
    monkeypatch.setenv("SABLE_DEP_CHECK_TIMEOUT", "not-a-number")
    assert ssw.dep_merge_advisory(["SABLE-dep"], str(tmp_path)) is not None
