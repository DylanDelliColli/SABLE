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


def test_already_in_progress_check_blocks_second_spawn():
    err = ssw.already_in_progress_check(
        {"id": "X-1", "status": "in_progress", "assignee": "tarzan"})
    assert err is not None and "X-1" in err and "IN_PROGRESS" in err and "tarzan" in err


def test_already_in_progress_check_allows_open_bead():
    assert ssw.already_in_progress_check({"id": "X-1", "status": "open"}) is None


def test_already_in_progress_check_handles_missing_assignee():
    err = ssw.already_in_progress_check({"id": "X-1", "status": "in_progress"})
    assert err is not None and "unassigned" in err


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
    # fire AND be attributed correctly.
    assert ssw.worker_env_args("optimus") == [
        "-e", "CLAUDE_AGENT_NAME=optimus", "-e", "CLAUDE_AGENT_ROLE=manager",
    ]


def test_worker_env_args_empty_when_no_lane():
    assert ssw.worker_env_args("") == []


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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
