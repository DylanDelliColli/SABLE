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
    assert ssw.resolve_model([], None) == ("sonnet", None)
    assert ssw.resolve_model(["scope:foo"], None) == ("sonnet", None)


def test_resolve_model_from_label():
    assert ssw.resolve_model(["model:haiku"], None) == ("haiku", None)
    assert ssw.resolve_model(["x", "model:opus", "y"], None) == ("opus", None)


def test_resolve_model_override_wins_and_carries_reason():
    model, reason = ssw.resolve_model(["model:sonnet"], "opus:auth path now")
    assert model == "opus"
    assert reason == "auth path now"


def test_resolve_model_override_without_reason():
    model, reason = ssw.resolve_model([], "haiku")
    assert model == "haiku"
    assert reason is None


# --- naming -----------------------------------------------------------------

def test_worktree_name_from_bead():
    assert ssw.worktree_name("SABLE-bldh.3", None) == "wk-sable-bldh-3"


def test_worktree_name_from_scope():
    assert ssw.worktree_name("SABLE-bldh.3", "msg-helper") == "wk-msg-helper"


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


def test_overlap_check_warns_on_shared_file_with_other_bead():
    bead = {"id": "X-1", "notes": "WIP-CLAIMS: shared.py"}
    other = {"id": "Y-1", "notes": "WIP-CLAIMS: shared.py", "assignee": "tarzan"}
    warning = ssw.overlap_check("X-1", bead, [other])
    assert warning is not None
    assert "Y-1" in warning and "shared.py" in warning and "tarzan" in warning


def test_overlap_check_ignores_self_in_progress_list():
    # already_in_progress_check owns the same-bead case; overlap_check must not
    # double-flag itself if it happens to appear in the in-progress list.
    bead = {"id": "X-1", "notes": "WIP-CLAIMS: shared.py"}
    assert ssw.overlap_check("X-1", bead, [bead]) is None


def test_overlap_check_none_when_no_shared_files():
    bead = {"id": "X-1", "notes": "WIP-CLAIMS: a.py"}
    other = {"id": "Y-1", "notes": "WIP-CLAIMS: b.py"}
    assert ssw.overlap_check("X-1", bead, [other]) is None


def test_overlap_check_none_when_bead_has_no_claims():
    assert ssw.overlap_check("X-1", {"id": "X-1"}, [{"id": "Y-1", "notes": "WIP-CLAIMS: a.py"}]) is None


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

def test_worker_cap_default_is_4(monkeypatch):
    monkeypatch.delenv("SABLE_MAX_WORKERS", raising=False)
    assert ssw.worker_cap() == 4


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
        assert ssw.worker_cap() == 4, bad


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


def test_read_instruction_is_single_line():
    instr = ssw.read_instruction("/abs/dispatch/X-1.md")
    assert "/abs/dispatch/X-1.md" in instr
    assert "\n" not in instr


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
