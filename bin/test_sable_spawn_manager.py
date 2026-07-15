#!/usr/bin/env python3
"""Unit tests for bin/sable-spawn-manager (SABLE-dqhn.2).

Pure-logic layer: role validation, idempotent-skip planning from an existing
pane listing, and window-not-split command construction (the Lincoln window
must never be disturbed: new-window with -d). tmux behavior is covered by
test_sable_spawn_manager_integration.py.
"""
import importlib.util
import os
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-spawn-manager"
_LOADER = SourceFileLoader(
    "sable_spawn_manager", str(Path(__file__).resolve().parent / "sable-spawn-manager")
)
_SPEC = importlib.util.spec_from_loader("sable_spawn_manager", _LOADER)
sm = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(sm)

# sable-spawn-manager's own top-level `sys.path.insert` (executed above via
# exec_module) already put bin/ on sys.path, so this resolves cleanly.
import sable_pane_lib as spl  # noqa: E402


def test_validate_roles_accepts_managers():
    assert sm.validate_roles(["optimus", "chuck"]) == ["optimus", "chuck"]


def test_validate_roles_rejects_unknown():
    with pytest.raises(ValueError) as e:
        sm.validate_roles(["optimus", "lincoln"])
    assert "lincoln" in str(e.value) and "optimus" in str(e.value)


def test_parse_existing_roles():
    out = "%0 lincoln\n%1 optimus\n%2 \n"
    assert sm.parse_existing_roles(out) == {"lincoln", "optimus"}


def test_plan_spawns_skips_existing():
    to_spawn, skipped = sm.plan_spawns(["optimus", "tarzan"], {"lincoln", "optimus"})
    assert to_spawn == ["tarzan"]
    assert skipped == ["optimus"]


def test_window_args_are_detached_named_windows():
    args = sm.window_args("sable", "optimus", "bash")
    assert "new-window" in args
    assert "-d" in args                      # never steal the Lincoln window
    assert "-n" in args and "optimus" in args
    i = args.index("-t")
    assert args[i + 1] == "sable"
    assert "split-window" not in args


# --- SABLE-tz7h.1: producer spawn contract (roles, --deliverable, --model,
# @sable_class stamping, bounded kick) -----------------------------------

def test_validate_roles_accepts_producer():
    assert sm.validate_roles(["victor"]) == ["victor"]


def test_validate_roles_rejects_unknown_still_lists_producer_as_valid():
    with pytest.raises(ValueError) as e:
        sm.validate_roles(["lincoln"])
    assert "victor" in str(e.value)          # producer role is now valid too


def test_sable_class_producer_vs_manager():
    assert sm.sable_class("victor") == "producer"
    assert sm.sable_class("optimus") == "manager"
    assert sm.sable_class("tarzan") == "manager"
    assert sm.sable_class("chuck") == "manager"


def test_missing_deliverable_error_required_for_producer():
    err = sm.missing_deliverable_error(["victor"], None)
    assert err is not None
    assert "victor" in err and "--deliverable" in err


def test_missing_deliverable_error_absent_when_provided():
    assert sm.missing_deliverable_error(["victor"], "/tmp/report.md") is None


def test_missing_deliverable_error_not_triggered_for_managers():
    assert sm.missing_deliverable_error(["optimus", "tarzan"], None) is None


def test_window_args_producer_sets_agent_role_producer():
    args = sm.window_args("sable", "victor", "bash")
    assert "CLAUDE_AGENT_ROLE=producer" in args
    assert "CLAUDE_AGENT_ROLE=manager" not in args


def test_window_args_manager_still_sets_agent_role_manager_regression():
    """Managers keep CLAUDE_AGENT_ROLE=manager, byte-identical to before the
    producer branch existed."""
    args = sm.window_args("sable", "optimus", "bash")
    assert "CLAUDE_AGENT_ROLE=manager" in args
    assert "CLAUDE_AGENT_ROLE=producer" not in args


def test_producer_command_pins_model_tier(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_PANE_CMD", raising=False)
    monkeypatch.setenv("SABLE_WORKER_PERMISSION", "--permission-mode bypassPermissions")
    assert sm.producer_command("sonnet") == "claude --model sonnet --permission-mode bypassPermissions"


def test_producer_command_honors_pane_cmd_stand_in(monkeypatch):
    monkeypatch.setenv("SABLE_TMUX_PANE_CMD", "bash")
    assert sm.producer_command("sonnet") == "bash"


def test_bounded_producer_kick_contains_deliverable_and_never_loop():
    msg = spl.kick_message("victor", deliverable="/tmp/victor-report.md")
    assert "/tmp/victor-report.md" in msg
    assert "never loop" in msg.lower()
    # must NOT contain manager-loop phrasing (bounded producer, not a manager)
    assert "bd ready" not in msg
    assert "Drain your lane" not in msg
    assert "sable-spawn-worker" not in msg


def test_manager_kick_text_byte_identical_regression():
    """kick_message(role) with no deliverable is a byte-exact snapshot, not a
    paraphrase, so any accidental rewording of the manager kick trips this.
    SABLE-nmmh deliberately reworded the lane-manager kick to event-driven
    'end your turn when idle' phrasing (dropping the 'pause briefly and loop'
    foreground-wait mandate that deafened the msg channel, SABLE-kkgt); this
    assertion was consciously updated to the new wording as part of that change.
    Chuck's kick is untouched."""
    expected_lane_managers = (
        "[SABLE-AUTOSTART] Operator: begin your operating loop now and run it "
        "autonomously — do not wait for further input. Drain your lane from "
        "`bd ready`: verify each ready bead, claim it, and `sable-spawn-worker "
        "<id> --scope <name>`; review the results and reap done panes. You are "
        "EVENT-DRIVEN: when nothing is actionable, end your turn — a new "
        "⟦SABLE-MSG⟧ turn or a worker-landing notification wakes you; never "
        "foreground-sleep to hold the pane. Stand down when a wake finds the "
        "pool and your inbox empty with no workers in flight."
    )
    expected_chuck = (
        "[SABLE-AUTOSTART] Operator: begin your operating loop now and run it "
        "autonomously — do not wait for further input. You are event-driven: "
        "each ⟦SABLE-MSG⟧ PR-ready message from a manager is a merge request — "
        "review and merge it, then report back. Also drain any existing "
        "for-chuck beads and run a stranded-recovery sweep now, then idle "
        "waiting for messages."
    )
    assert spl.kick_message("optimus") == expected_lane_managers
    assert spl.kick_message("tarzan") == expected_lane_managers
    assert spl.kick_message("chuck") == expected_chuck


# --- SABLE-gbd: managers are ALWAYS Opus, the ladder is workers-only -------

def test_manager_command_always_pins_opus(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_PANE_CMD", raising=False)
    monkeypatch.delenv("SABLE_WORKER_PERMISSION", raising=False)
    assert sm.manager_command() == "claude --model opus --permission-mode bypassPermissions"


def test_manager_command_pins_opus_regardless_of_permission_override(monkeypatch):
    # SABLE-gbd: managers are ALWAYS Opus — no env or bead-style model label
    # can steer the manager's own model. Only SABLE_TMUX_PANE_CMD (a full
    # command override reserved for tests) is exempt.
    monkeypatch.delenv("SABLE_TMUX_PANE_CMD", raising=False)
    monkeypatch.setenv("SABLE_WORKER_PERMISSION", "--permission-mode acceptEdits")
    monkeypatch.setenv("SABLE_WORKER_MODEL", "haiku")   # decoy: not a real knob, must be ignored
    cmd = sm.manager_command()
    assert cmd.startswith("claude --model opus ")
    assert "haiku" not in cmd


def test_manager_command_pane_cmd_override_bypasses_opus_pin(monkeypatch):
    # The one intentional exemption: tests stand in a fake pane command.
    monkeypatch.setenv("SABLE_TMUX_PANE_CMD", "bash")
    assert sm.manager_command() == "bash"


# --- SABLE-59t6.4: v1 fleet boundary — project-only install refuses ---------
# The whole tool is fleet tooling: under a project-only install (registry
# resolves to a PROJECT agents.yaml, no global install, no SABLE_AGENTS_YAML
# override) every operation refuses with the exact remedy. The decision lives in
# the 59t6.1 resolver (fleet_project_only); the tests here pin both the decision
# function and its wiring into the CLI. Fixture is a real git repo shipping its
# own project registry + an isolated HOME with no global registry.

def _git_repo_with_project_registry(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    reg = repo / ".claude" / "sable" / "agents.yaml"
    reg.parent.mkdir(parents=True)
    reg.write_text("agents:\n  optimus:\n    type: epic_manager\n")
    return repo


def test_fleet_boundary_remedy_is_the_exact_contract_string():
    assert sm.FLEET_PROJECT_ONLY_REMEDY == (
        "fleet requires the global install in v1, or export SABLE_AGENTS_YAML "
        "and SABLE_DISPATCH_DIR in the shell that creates the tmux session"
    )


def test_fleet_boundary_refuses_under_project_only_resolution(tmp_path, monkeypatch):
    repo = _git_repo_with_project_registry(tmp_path)
    home = tmp_path / "home"
    home.mkdir()                                     # no ~/.claude/sable/agents.yaml
    monkeypatch.delenv("SABLE_AGENTS_YAML", raising=False)
    monkeypatch.setenv("HOME", str(home))
    assert sm.fleet_boundary_refusal(str(repo)) == sm.FLEET_PROJECT_ONLY_REMEDY


def test_fleet_boundary_allows_when_global_install_present(tmp_path, monkeypatch):
    """A global install (its registry present at ~/.claude/sable/agents.yaml)
    lifts the boundary even when a project registry is ALSO present."""
    repo = _git_repo_with_project_registry(tmp_path)
    home = tmp_path / "home"
    (home / ".claude" / "sable").mkdir(parents=True)
    (home / ".claude" / "sable" / "agents.yaml").write_text("agents: {}\n")
    monkeypatch.delenv("SABLE_AGENTS_YAML", raising=False)
    monkeypatch.setenv("HOME", str(home))
    assert sm.fleet_boundary_refusal(str(repo)) is None


def test_fleet_boundary_allows_with_agents_yaml_override(tmp_path, monkeypatch):
    """Exporting SABLE_AGENTS_YAML (the escape hatch named in the remedy) flips
    the scope to 'override' and lifts the boundary, global install or not."""
    repo = _git_repo_with_project_registry(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SABLE_AGENTS_YAML", str(tmp_path / "explicit.yaml"))
    monkeypatch.setenv("HOME", str(home))
    assert sm.fleet_boundary_refusal(str(repo)) is None


def test_fleet_boundary_allows_when_no_project_registry(tmp_path, monkeypatch):
    """A git repo that ships NO project registry resolves to 'global' scope, so
    the boundary never fires — it is scoped strictly to project-only installs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("SABLE_AGENTS_YAML", raising=False)
    monkeypatch.setenv("HOME", str(home))
    assert sm.fleet_boundary_refusal(str(repo)) is None


def test_cli_refuses_every_op_under_project_only(tmp_path):
    """Integration: the actual CLI refuses a manager spawn under project-only,
    on stderr with the verbatim remedy and a non-zero (fleet-boundary) exit,
    BEFORE any tmux/session interaction."""
    repo = _git_repo_with_project_registry(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("SABLE_AGENTS_YAML", "SABLE_REGISTRY")}
    env["HOME"] = str(home)
    r = subprocess.run(["python3", str(BIN), "optimus"], cwd=str(repo),
                       capture_output=True, text=True, env=env)
    assert r.returncode == sm.FLEET_BOUNDARY_EXIT
    assert sm.FLEET_PROJECT_ONLY_REMEDY in r.stderr


if __name__ == "__main__":
    import sys
    import pytest as _p
    sys.exit(_p.main([__file__, "-q"]))
