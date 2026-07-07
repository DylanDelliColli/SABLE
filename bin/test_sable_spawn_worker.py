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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
