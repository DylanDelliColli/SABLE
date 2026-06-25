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

def test_worker_command_default_pins_model():
    assert ssw.worker_command("haiku", None) == "claude --model haiku"


def test_worker_command_override_used_verbatim():
    assert ssw.worker_command("haiku", "bash --norc") == "bash --norc"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
