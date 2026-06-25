#!/usr/bin/env python3
"""Unit tests for bin/sable-tmux (SABLE-bldh.2) — pure helpers."""
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader("sable_tmux", str(Path(__file__).resolve().parent / "sable-tmux"))
_SPEC = importlib.util.spec_from_loader("sable_tmux", _LOADER)
st = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(st)


def test_default_roles():
    assert st.parse_roles(None) == ["lincoln", "optimus", "tarzan", "chuck"]


def test_parse_roles_csv():
    assert st.parse_roles("lincoln,optimus") == ["lincoln", "optimus"]
    assert st.parse_roles(" lincoln , chuck ") == ["lincoln", "chuck"]


def test_pane_env_args_sets_identity_and_role():
    args = st.pane_env_args("optimus")
    assert args == ["-e", "CLAUDE_AGENT_NAME=optimus", "-e", "CLAUDE_AGENT_ROLE=manager"]


def test_pane_command_default(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_PANE_CMD", raising=False)
    assert st.pane_command() == "claude"


def test_pane_command_override(monkeypatch):
    monkeypatch.setenv("SABLE_TMUX_PANE_CMD", "bash --norc")
    assert st.pane_command() == "bash --norc"


def test_tmux_base():
    assert st.tmux_base(None) == ["tmux"]
    assert st.tmux_base("sk") == ["tmux", "-L", "sk"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
