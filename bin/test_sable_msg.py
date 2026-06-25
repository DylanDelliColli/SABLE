#!/usr/bin/env python3
"""Unit tests for bin/sable-msg (loaded by path — the CLI has no .py extension).

Covers the Fresh-Agent-Test spec items for SABLE-bldh.1: header formatting,
arg parsing, --interrupt Escape-first sequence, registry (role->pane) lookup,
and the missing-role error.
"""
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

# Load the no-extension CLI as a module (needs an explicit source loader since
# there is no .py suffix for importlib to infer one from).
_LOADER = SourceFileLoader("sable_msg", str(Path(__file__).resolve().parent / "sable-msg"))
_SPEC = importlib.util.spec_from_loader("sable_msg", _LOADER)
sable_msg = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(sable_msg)


# --- header / message formatting -------------------------------------------

def test_format_message_basic():
    msg = sable_msg.format_message("optimus", "lincoln", "API epic is urgent")
    assert msg == "⟦SABLE-MSG⟧ from=optimus to=lincoln :: API epic is urgent"


def test_format_message_collapses_newlines_and_runs():
    msg = sable_msg.format_message("lincoln", "optimus", "drop auth\n\n  do API   now")
    # newlines/extra spaces collapse to single spaces -> single-line, single turn
    assert msg == "⟦SABLE-MSG⟧ from=lincoln to=optimus :: drop auth do API now"
    assert "\n" not in msg


def test_header_glyph_present():
    assert sable_msg.HEADER == "⟦SABLE-MSG⟧"


# --- registry parsing (tmux list-panes output) ------------------------------

def test_parse_panes_basic():
    out = "%1 lincoln\n%2 optimus\n%3 tarzan\n"
    assert sable_msg.parse_panes(out) == {
        "lincoln": "%1",
        "optimus": "%2",
        "tarzan": "%3",
    }


def test_parse_panes_skips_roleless_and_blank():
    # panes with no @sable_role set emit just the pane id (no second field)
    out = "%1 lincoln\n%2 \n%3\n\n%4 optimus\n"
    assert sable_msg.parse_panes(out) == {"lincoln": "%1", "optimus": "%4"}


def test_parse_panes_first_wins_on_duplicate_role():
    out = "%1 optimus\n%2 optimus\n"
    assert sable_msg.parse_panes(out)["optimus"] == "%1"


def test_lookup_pane_found_and_missing():
    fake_out = "%1 lincoln\n%2 optimus\n"
    runner = lambda args: fake_out
    assert sable_msg.lookup_pane("optimus", runner) == "%2"
    assert sable_msg.lookup_pane("ghost", runner) is None


# --- tmux base / socket isolation -------------------------------------------

def test_tmux_base_default():
    assert sable_msg.tmux_base(None) == ["tmux"]


def test_tmux_base_socket():
    assert sable_msg.tmux_base("sable-test") == ["tmux", "-L", "sable-test"]


# --- send-keys command construction (the interrupt sequence) ----------------

def test_build_commands_no_interrupt():
    cmds = sable_msg.build_sendkeys_commands("%2", "hello", interrupt=False)
    assert cmds == [
        ["tmux", "send-keys", "-t", "%2", "-l", "hello"],
        ["tmux", "send-keys", "-t", "%2", "Enter"],
    ]


def test_build_commands_interrupt_sends_escape_first():
    cmds = sable_msg.build_sendkeys_commands("%2", "hello", interrupt=True)
    assert cmds[0] == ["tmux", "send-keys", "-t", "%2", "Escape"]
    assert cmds[-1] == ["tmux", "send-keys", "-t", "%2", "Enter"]
    assert ["tmux", "send-keys", "-t", "%2", "-l", "hello"] in cmds


def test_build_commands_socket_threads_through():
    cmds = sable_msg.build_sendkeys_commands("%2", "hi", interrupt=False, socket="sk")
    assert all(c[:3] == ["tmux", "-L", "sk"] for c in cmds)


# --- arg parsing ------------------------------------------------------------

def test_parse_args_requires_role_and_body():
    with pytest.raises(SystemExit):
        sable_msg.parse_args([])
    with pytest.raises(SystemExit):
        sable_msg.parse_args(["optimus"])  # body missing


def test_parse_args_from_default_and_interrupt():
    ns = sable_msg.parse_args(["optimus", "hi there", "--from", "lincoln"])
    assert ns.to_role == "optimus"
    assert ns.body == "hi there"
    assert ns.frm == "lincoln"
    assert ns.interrupt is False
    ns2 = sable_msg.parse_args(["lincoln", "stop", "--interrupt"])
    assert ns2.interrupt is True


# --- main: missing role is a hard error -------------------------------------

def test_main_missing_role_errors(monkeypatch, capsys):
    monkeypatch.setattr(sable_msg, "lookup_pane", lambda role, run=None, socket=None: None)
    rc = sable_msg.main(["ghost", "hello", "--from", "lincoln"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "ghost" in err


def test_main_happy_path_runs_commands(monkeypatch):
    monkeypatch.setattr(sable_msg, "lookup_pane", lambda role, run=None, socket=None: "%2")
    sent = []
    monkeypatch.setattr(sable_msg, "run_tmux", lambda args: sent.append(args))
    rc = sable_msg.main(["optimus", "ship it", "--from", "lincoln"])
    assert rc == 0
    # last command submits the turn
    assert sent[-1][-1] == "Enter"
    assert any("-l" in c for c in sent)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
