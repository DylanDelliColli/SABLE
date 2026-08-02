#!/usr/bin/env python3
"""Unit tests for bin/sable-spawn-manager (SABLE-dqhn.2).

Pure-logic layer: role validation, idempotent-skip planning from an existing
pane listing, and window-not-split command construction (the Lincoln window
must never be disturbed: new-window with -d). tmux behavior is covered by
test_sable_spawn_manager_integration.py.
"""
import importlib.util
import inspect
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


def _manager_pane(
    *,
    pane_id="%1",
    role="optimus",
    provider="claude",
    pane_dead=False,
    status="running",
    boot_epoch="100-aaaaaaaaaaaa",
    kicked_epoch="100-aaaaaaaaaaaa",
):
    return sm.ExistingPane(
        pane_id=pane_id,
        role=role,
        provider=provider,
        pane_dead=pane_dead,
        status=status,
        boot_epoch=boot_epoch,
        kicked_epoch=kicked_epoch,
    )


def test_parse_existing_panes_preserves_only_authoritative_liveness_fields():
    out = (
        "%0\tlincoln\t\t0\trunning\t100-a\t100-a\n"
        "%1\toptimus\tcodex\t1\tdone\t200-b\t100-a\n"
        "%2\t\t\t0\t\t\t\n"
    )

    panes = sm.parse_existing_panes(out)

    assert panes == {
        "lincoln": sm.ExistingPane("%0", "lincoln", "claude", False,
                                   "running", "100-a", "100-a"),
        "optimus": sm.ExistingPane("%1", "optimus", "codex", True,
                                   "done", "200-b", "100-a"),
    }


def test_parse_existing_panes_refuses_duplicate_role_owners():
    out = (
        "%1\toptimus\tclaude\t0\trunning\t100-a\t100-a\n"
        "%2\toptimus\tclaude\t1\tdone\t100-a\t100-a\n"
    )
    with pytest.raises(ValueError, match=r"duplicate.*optimus.*%1.*%2"):
        sm.parse_existing_panes(out)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "ALIVE_AND_SESSIONED"),
        ({"boot_epoch": "200-bbbbbbbbbbbb"}, "RESTARTED_SINCE"),
        ({"pane_dead": True}, "DEAD"),
        ({"status": "done"}, "DEAD"),
    ],
)
def test_manager_liveness_trichotomy(overrides, expected):
    pane = _manager_pane(**overrides)
    assert sm.classify_manager_pane(pane).name == expected


def test_process_alive_correct_environ_old_pid_shape_without_agent_epoch_is_unknown():
    """The measured 28-hour-old process had every process-derived ALIVE hint.

    None is an agent SessionStart event.  Even a lifecycle-running pane must
    fail closed when the agent-written epoch is absent.
    """
    pane = _manager_pane(boot_epoch="", kicked_epoch="")
    assert sm.classify_manager_pane(pane).name == "UNKNOWN"


def test_pre_migration_epoch_present_but_status_and_reference_absent_is_unknown():
    pane = _manager_pane(
        status="", boot_epoch="100-aaaaaaaaaaaa", kicked_epoch=""
    )
    assert sm.classify_manager_pane(pane).name == "UNKNOWN"
    assert sm.plan_spawns(["optimus"], {"optimus": pane}) == {
        "optimus": sm.SpawnAction.REFUSE_UNKNOWN,
    }


def test_idle_does_not_collapse_working_and_cleared_manager_states():
    """Both measured panes are idle; only the boot/reference relation differs."""
    working_between_turns = _manager_pane()
    cleared_and_idle = _manager_pane(boot_epoch="200-bbbbbbbbbbbb")

    assert sm.classify_manager_pane(working_between_turns).name == "ALIVE_AND_SESSIONED"
    assert sm.classify_manager_pane(cleared_and_idle).name == "RESTARTED_SINCE"
    assert tuple(inspect.signature(sm.classify_manager_pane).parameters) == ("pane",)


def test_scrollback_is_not_consulted_for_liveness(monkeypatch):
    monkeypatch.setattr(
        sm,
        "capture_pane",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("scrollback consulted")
        ),
    )
    assert sm.classify_manager_pane(_manager_pane()).name == "ALIVE_AND_SESSIONED"


def test_plan_spawns_consumes_all_three_liveness_arms_distinctly():
    existing = {
        "optimus": _manager_pane(role="optimus"),
        "tarzan": _manager_pane(
            pane_id="%2", role="tarzan", boot_epoch="200-bbbbbbbbbbbb"
        ),
        "chuck": _manager_pane(pane_id="%3", role="chuck", pane_dead=True),
    }

    decisions = sm.plan_spawns(["optimus", "tarzan", "chuck"], existing)

    assert decisions == {
        "optimus": sm.SpawnAction.SKIP_ALIVE,
        "tarzan": sm.SpawnAction.REKICK_RESTARTED,
        "chuck": sm.SpawnAction.RESPAWN_DEAD,
    }
    assert len(set(decisions.values())) == 3


def test_plan_spawns_absent_role_creates_new_pane():
    assert sm.plan_spawns(["tarzan"], {}) == {
        "tarzan": sm.SpawnAction.SPAWN_NEW,
    }


def test_plan_spawns_adopts_running_epoch_without_reference_but_never_kicks_it():
    pane = _manager_pane(kicked_epoch="")
    assert sm.plan_spawns(["optimus"], {"optimus": pane}) == {
        "optimus": sm.SpawnAction.ADOPT_REFERENCE,
    }


def test_plan_spawns_refuses_missing_agent_epoch_instead_of_skipping_role_tag():
    pane = _manager_pane(boot_epoch="", kicked_epoch="")
    assert sm.plan_spawns(["optimus"], {"optimus": pane}) == {
        "optimus": sm.SpawnAction.REFUSE_UNKNOWN,
    }


@pytest.mark.parametrize(
    ("status", "dead", "expected"),
    [
        ("running", False, "SKIP_ALIVE"),
        ("done", False, "RESPAWN_DEAD"),
        ("running", True, "RESPAWN_DEAD"),
        ("", False, "REFUSE_UNKNOWN"),
    ],
)
def test_producer_planning_uses_lifecycle_without_boot_epoch(status, dead, expected):
    pane = _manager_pane(
        role="victor", status=status, pane_dead=dead,
        boot_epoch="", kicked_epoch="",
    )
    decision = sm.plan_spawns(["victor"], {"victor": pane})["victor"]
    assert decision.name == expected


def test_stable_epoch_kick_retries_when_session_restarts_during_delivery():
    reads = iter(("100-a", "200-b", "200-b", "200-b"))
    delivered = []
    recorded = []

    ok, epoch = sm.stabilize_manager_kick(
        ready=lambda: True,
        read_epoch=lambda: next(reads),
        deliver=lambda observed: delivered.append(observed) or True,
        record=lambda observed: recorded.append(observed) or True,
        attempts=2,
    )

    assert ok is True
    assert epoch == "200-b"
    assert delivered == ["100-a", "200-b"]
    assert recorded == ["200-b"]


def test_stable_epoch_kick_never_delivers_without_agent_epoch():
    delivered = []
    ok, reason = sm.stabilize_manager_kick(
        ready=lambda: True,
        read_epoch=lambda: "",
        deliver=lambda observed: delivered.append(observed) or True,
        record=lambda _observed: True,
        attempts=2,
    )
    assert ok is False
    assert "epoch" in reason.lower()
    assert delivered == []


def test_lifecycle_wrapper_is_one_shared_object_for_workers_and_managers():
    assert sm.with_lifecycle_flags is spl.with_lifecycle_flags
    wrapped = sm.with_lifecycle_flags("bash -c 'exit 7'")
    assert wrapped.startswith(
        'tmux set-option -p -t "$TMUX_PANE" @sable_status running;'
    )
    assert wrapped.endswith(
        '; tmux set-option -p -t "$TMUX_PANE" @sable_status done'
    )


def test_retained_pane_provider_mismatch_is_refused():
    mismatches = sm.provider_mismatches(
        ["optimus", "tarzan"],
        {"optimus": "claude", "tarzan": "codex"},
        {"optimus": "codex", "tarzan": "codex"},
    )
    assert mismatches == [("optimus", "claude", "codex")]


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


def test_window_args_dual_stamps_provider_neutral_claude_identity():
    args = sm.window_args("sable", "optimus", "bash")
    assert args == [
        "new-window", "-d", "-t", "sable", "-n", "optimus",
        "-P", "-F", "#{pane_id}",
        "-e", "SABLE_PROVIDER=claude",
        "-e", "SABLE_AGENT_NAME=optimus",
        "-e", "SABLE_AGENT_ROLE=manager",
        "-e", "CLAUDE_AGENT_NAME=optimus",
        "-e", "CLAUDE_AGENT_ROLE=manager",
        "bash",
    ]


def test_window_args_codex_neutralizes_then_removes_claude_identity_aliases():
    args = sm.window_args("sable", "tarzan", "bash", provider="codex")
    assert "SABLE_PROVIDER=codex" in args
    assert "SABLE_AGENT_NAME=tarzan" in args
    assert "SABLE_AGENT_ROLE=manager" in args
    assert "CLAUDE_AGENT_NAME=" in args
    assert "CLAUDE_AGENT_ROLE=" in args
    assert args[-1].startswith("exec env -u CLAUDE_AGENT_NAME -u CLAUDE_AGENT_ROLE ")
    assert "bash -c" in args[-1]


def test_respawn_args_codex_carries_both_scrub_layers_too():
    args = sm.respawn_args("%7", "chuck", "bash", provider="codex")
    assert args[:4] == ["respawn-pane", "-k", "-t", "%7"]
    assert "CLAUDE_AGENT_NAME=" in args
    assert "CLAUDE_AGENT_ROLE=" in args
    assert args[-1].startswith("exec env -u CLAUDE_AGENT_NAME -u CLAUDE_AGENT_ROLE ")


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


def _mock_codex_spawn_dependencies(monkeypatch, tmp_path):
    monkeypatch.setattr(sm, "fleet_boundary_refusal", lambda: None)
    monkeypatch.setattr(sm, "resolve_session", lambda socket=None: "sable")
    monkeypatch.setattr(sm, "read_execution_authority", lambda base=None: {})
    monkeypatch.setattr(sm, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(sm, "execution_provider", lambda role, base=None: "codex")
    monkeypatch.setattr(sm, "wait_for_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(sm, "deliver_text", lambda *args, **kwargs: True)
    monkeypatch.setattr(sm, "register_instance", lambda *args, **kwargs: "base")

    def fake_run(argv, **kwargs):
        if "new-window" in argv:
            stdout = "%9\n"
        elif "show-options" in argv and "@sable_boot_epoch" in argv:
            stdout = "test-epoch\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(sm.subprocess, "run", fake_run)


def test_codex_spawn_refuses_missing_hook_graph_with_exact_remedy(
    tmp_path, monkeypatch, capsys
):
    _mock_codex_spawn_dependencies(monkeypatch, tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))

    rc = sm.main(["optimus"])

    assert rc != 0
    assert (
        "sable-orchestration-install --user --merge-settings"
        in capsys.readouterr().err
    )


def test_codex_spawn_proceeds_with_installed_hook_graph(
    tmp_path, monkeypatch
):
    _mock_codex_spawn_dependencies(monkeypatch, tmp_path)
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "hooks.json").write_text('{"SessionStart": [{}]}')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    role = tmp_path / ".claude" / "sable" / "roles" / "optimus.md"
    role.parent.mkdir(parents=True)
    role.write_text("# Optimus")

    assert sm.main(["optimus"]) == 0


# --- SABLE-gbd: managers are ALWAYS Opus, the ladder is workers-only -------

def test_manager_command_always_pins_opus(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_PANE_CMD", raising=False)
    monkeypatch.delenv("SABLE_WORKER_PERMISSION", raising=False)
    assert sm.manager_command() == "claude --model opus --permission-mode bypassPermissions"


def test_manager_command_launches_deep_interactive_codex(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_PANE_CMD", raising=False)
    command = sm.manager_command("codex")
    assert command.startswith("codex --no-alt-screen")
    assert "--model gpt-5.6-sol" in command
    assert 'model_reasoning_effort="high"' in command


def test_codex_manager_can_create_sibling_worker_worktrees(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_PANE_CMD", raising=False)
    command = sm.manager_command("codex", "/work/projects/repo")
    assert "--cd /work/projects/repo" in command
    assert "--add-dir /work/projects" in command


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
