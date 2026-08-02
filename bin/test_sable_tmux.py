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
    assert args == [
        "-e", "SABLE_PROVIDER=claude",
        "-e", "SABLE_AGENT_NAME=optimus",
        "-e", "SABLE_AGENT_ROLE=manager",
        "-e", "CLAUDE_AGENT_NAME=optimus",
        "-e", "CLAUDE_AGENT_ROLE=manager",
    ]


def test_codex_pane_env_neutralizes_legacy_aliases_at_tmux_boundary():
    args = st.pane_env_args("tarzan", provider="codex")
    assert "SABLE_PROVIDER=codex" in args
    assert "SABLE_AGENT_NAME=tarzan" in args
    assert "SABLE_AGENT_ROLE=manager" in args
    assert "CLAUDE_AGENT_NAME=" in args
    assert "CLAUDE_AGENT_ROLE=" in args


def test_main_refuses_corrupt_execution_provider_state(tmp_path, monkeypatch, capsys):
    state = tmp_path / "mode.json"
    state.write_text("{broken")
    monkeypatch.setenv("SABLE_MODE_STATE", str(state))
    assert st.main(["--roles", "optimus"]) == 5
    assert "refusing invalid provider state" in capsys.readouterr().err


def test_pane_command_plain_when_not_autostart(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_PANE_CMD", raising=False)
    assert st.pane_command("optimus", False) == "claude"
    assert st.pane_command("lincoln", True) == "claude"  # lincoln is never bypass/kicked


def test_pane_command_autostart_bypass_for_autonomous(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_PANE_CMD", raising=False)
    monkeypatch.delenv("SABLE_WORKER_PERMISSION", raising=False)
    assert st.pane_command("optimus", True) == "claude --permission-mode bypassPermissions"
    assert st.pane_command("chuck", True) == "claude --permission-mode bypassPermissions"


def test_pane_command_override_wins(monkeypatch):
    monkeypatch.setenv("SABLE_TMUX_PANE_CMD", "bash --norc")
    assert st.pane_command("optimus", True) == "bash --norc"


def test_codex_pane_command_scrubs_override_but_claude_stays_byte_exact(monkeypatch):
    override = "[[ -n \"$BASH_VERSION\" ]] && printf ready"
    monkeypatch.setenv("SABLE_TMUX_PANE_CMD", override)
    assert st.pane_command("optimus", True, provider="claude") == override
    codex = st.pane_command("optimus", True, provider="codex")
    assert codex.startswith(
        "exec env -u CLAUDE_AGENT_NAME -u CLAUDE_AGENT_ROLE bash -c "
    )
    assert override in codex


def test_autonomous_roles_excludes_lincoln():
    assert st.AUTONOMOUS_ROLES == {"optimus", "tarzan", "chuck"}
    assert "lincoln" not in st.AUTONOMOUS_ROLES


def test_kick_message_is_tagged_and_role_specific():
    assert st.KICK_TAG in st.kick_message("optimus")
    assert "sable-spawn-worker" in st.kick_message("optimus")
    assert "sable-spawn-worker" in st.kick_message("tarzan")
    assert "merge" in st.kick_message("chuck").lower()


def test_tmux_base():
    assert st.tmux_base(None) == ["tmux"]
    assert st.tmux_base("sk") == ["tmux", "-L", "sk"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
