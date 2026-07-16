#!/usr/bin/env python3
"""Unit tests for bin/sable-relink (SABLE-to8m).

sable-relink re-registers the calling pane's identity tags to AGREE with its
authoritative process identity (CLAUDE_AGENT_NAME). It is authority-bound: it can
only stamp the role the process env backs, never forge a new one. Pinned here:
  - the process identity is the authority (a requested role that disagrees is
    refused; an absent identity is a hard error);
  - the tmux ops rewrite @sable_role and, by default, CLEAR @sable_status so a
    re-registered cockpit/interactive pane sheds a prior worker's done flag.
"""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader(
    "sable_relink", str(Path(__file__).resolve().parent / "sable-relink"))
_SPEC = importlib.util.spec_from_loader("sable_relink", _LOADER)
rl = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(rl)


# --- resolve_relink_role: the process env is the authority ------------------

def test_role_defaults_to_process_identity():
    assert rl.resolve_relink_role(None, "lincoln") == "lincoln"


def test_requested_role_matching_identity_is_accepted():
    assert rl.resolve_relink_role("optimus", "optimus") == "optimus"


def test_requested_role_disagreeing_with_identity_is_refused():
    with pytest.raises(rl.RelinkError) as e:
        rl.resolve_relink_role("lincoln", "optimus")
    assert "optimus" in str(e.value)  # names the real (authoritative) identity


def test_absent_identity_is_a_hard_error():
    with pytest.raises(rl.RelinkError):
        rl.resolve_relink_role("lincoln", None)
    with pytest.raises(rl.RelinkError):
        rl.resolve_relink_role(None, None)


# --- relink_ops: the tmux writes it performs -------------------------------

def test_relink_ops_sets_role_and_clears_status_by_default():
    ops = rl.relink_ops("%3", "lincoln")
    assert ops[0] == ["set-option", "-p", "-t", "%3", "@sable_role", "lincoln"]
    # @sable_status is UNSET (-u) so a resumed cockpit sheds the stale done flag
    assert ["set-option", "-p", "-u", "-t", "%3", "@sable_status"] in ops
    assert not any("@sable_bead" in op for op in ops)


def test_relink_ops_sets_bead_and_explicit_status():
    ops = rl.relink_ops("%3", "worker", bead="SABLE-x1", status="running")
    assert ["set-option", "-p", "-t", "%3", "@sable_bead", "SABLE-x1"] in ops
    assert ["set-option", "-p", "-t", "%3", "@sable_status", "running"] in ops
    assert not any(op[:3] == ["set-option", "-p", "-u"] for op in ops)


# --- main() guards ----------------------------------------------------------

def test_main_refuses_outside_tmux(monkeypatch, capsys):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert rl.main([]) == 2
    assert "not inside a tmux pane" in capsys.readouterr().err


def test_main_refuses_when_process_has_no_identity(monkeypatch, capsys):
    monkeypatch.setenv("TMUX_PANE", "%5")
    monkeypatch.delenv("CLAUDE_AGENT_NAME", raising=False)
    assert rl.main([]) == 1
    assert "no authoritative identity" in capsys.readouterr().err


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
