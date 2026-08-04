#!/usr/bin/env python3
"""Integration tests for bin/sable-spawn-manager against a REAL tmux server.

Isolated socket. Seeds a lincoln-only session (the sable-launch shape), then
proves: spawning a manager creates a DETACHED role-tagged window (window 0
stays active), a second spawn of the same role skips idempotently, --all
stands up all three autonomous roles, and a missing session errors pointing
at sable-launch.
"""
import hashlib
import json
import os
import signal
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

import sable_provider_lib as provider_lib
from sable_pane_lib import with_lifecycle_flags

BIN = Path(__file__).resolve().parent / "sable-spawn-manager"
HAVE_TMUX = shutil.which("tmux") is not None
pytestmark = pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")

SESSION = "ssm"


def _authority():
    proof = {
        "version": 1,
        "kind": "break-glass",
        "approved_by": "test-operator",
        "approved_at": "2026-07-28T00:00:00+00:00",
        "reason": "synthetic execution authority for spawn integration",
        "base": {"ref": "HEAD", "sha": "a" * 40},
        "failed_checks": ["test fixture"],
    }
    proof["receipt_id"] = hashlib.sha256(
        json.dumps(
            proof, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()
    return proof


def _write_execution_state(path, providers=None):
    state = {"mode": "execution", "handoff": _authority()}
    if providers:
        state["providers"] = providers
    path.write_text(json.dumps(state))


@pytest.fixture()
def sock():
    s = f"sable-sm-{uuid.uuid4().hex[:8]}"
    yield s
    subprocess.run(["tmux", "-L", s, "kill-server"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture(autouse=True)
def execution_mode_state(tmp_path, monkeypatch):
    """Launch tests must not inherit the checkout's live planning/execution mode."""
    state = tmp_path / "mode-state.json"
    _write_execution_state(state)
    monkeypatch.setenv("SABLE_MODE_STATE", str(state))


def _tmux(s, *args, check=True):
    return subprocess.run(["tmux", "-L", s, *args],
                          capture_output=True, text=True, check=check)


def _run(s, *args, env_overrides=None):
    fake_tui = (
        "bash --noprofile --norc -c '"
        "tmux set-option -p -t \"$TMUX_PANE\" @sable_boot_epoch \"test-$BASHPID\"; "
        "tmux set-option -p -t \"$TMUX_PANE\" @sable_test_tui_pid \"$BASHPID\"; "
        "while true; do printf \"❯ \"; "
        "IFS= read -r line || break; printf \"%s\\n\" \"$line\"; done'"
    )
    env = {**os.environ, "SABLE_TMUX_SOCKET": s,
           "SABLE_TMUX_SESSION": SESSION,
           "SABLE_TMUX_PANE_CMD": fake_tui,
           "SABLE_DISPATCH_READY_TIMEOUT": "2",
           "SABLE_DISPATCH_SUBMIT_TRIES": "1",
           "SABLE_DISPATCH_POLL_INTERVAL": "0.1"}
    env.update(env_overrides or {})
    return subprocess.run(
        ["python3", str(BIN), *args], capture_output=True, text=True, env=env
    )


def _seed_lincoln(s):
    _tmux(s, "new-session", "-d", "-s", SESSION, "-x", "180", "-y", "50", "bash")
    pane = _tmux(s, "list-panes", "-t", SESSION, "-F", "#{pane_id}").stdout.strip()
    _tmux(s, "set-option", "-p", "-t", pane, "@sable_role", "lincoln")


def _roles(s):
    out = _tmux(s, "list-panes", "-s", "-t", SESSION, "-F", "#{@sable_role}").stdout
    return sorted(r for r in out.split() if r)


def _pane_for_role(s, role):
    out = _tmux(s, "list-panes", "-s", "-t", SESSION,
               "-F", "#{pane_id} #{@sable_role}").stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == role:
            return parts[0]
    return None


def _pane_option(s, pane, name):
    return _tmux(s, "show-options", "-p", "-v", "-t", pane, name, check=False).stdout.strip()


def _pane_process_environ(s, pane):
    pid = _tmux(s, "display-message", "-p", "-t", pane, "#{pane_pid}").stdout.strip()
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    return {
        key.decode(): value.decode()
        for entry in raw.split(b"\0")
        if entry and b"=" in entry
        for key, value in [entry.split(b"=", 1)]
    }


def _wait_pane(s, pane, *, status=None, dead=None, timeout=3.0):
    deadline = time.monotonic() + timeout
    observed = None
    while time.monotonic() < deadline:
        out = _tmux(
            s,
            "display-message",
            "-p",
            "-t",
            pane,
            "#{pane_dead}\t#{@sable_status}",
            check=False,
        )
        if out.returncode == 0:
            fields = out.stdout.rstrip("\n").split("\t")
            observed = (fields[0] == "1", fields[1] if len(fields) > 1 else "")
            if (status is None or observed[1] == status) and (
                dead is None or observed[0] is dead
            ):
                return observed
        time.sleep(0.05)
    raise AssertionError(
        f"pane {pane} never reached status={status!r} dead={dead!r}; "
        f"last={observed!r}"
    )


def test_spawn_creates_detached_role_window(sock):
    _seed_lincoln(sock)
    r = _run(sock, "optimus")
    assert r.returncode == 0, r.stderr
    assert "optimus" in _roles(sock)
    active = _tmux(sock, "display-message", "-t", SESSION, "-p",
                   "#{window_index}").stdout.strip()
    assert active == "0"        # the Lincoln window was not stolen
    names = _tmux(sock, "list-windows", "-t", SESSION,
                  "-F", "#{window_name}").stdout
    assert "optimus" in names

    pane = _pane_for_role(sock, "optimus")
    assert pane is not None
    _wait_pane(sock, pane, status="running", dead=False)
    epoch = _pane_option(sock, pane, "@sable_boot_epoch")
    assert epoch
    assert _pane_option(sock, pane, "@sable_kicked_epoch") == epoch


def test_codex_spawn_removes_inherited_claude_identity_from_pane_pid(
    sock, tmp_path
):
    """The real pane PID is the authority sable_pane_lib reads.  Polluting the
    tmux session and the invoking parent reproduces the live lincoln leak; a
    successful spawn must retain SABLE identity while both Claude keys vanish.
    """
    state = Path(os.environ["SABLE_MODE_STATE"])
    _write_execution_state(state, {"optimus": "codex"})
    home = tmp_path / "home"
    codex_home = home / ".codex"
    role = home / ".claude" / "sable" / "roles" / "optimus.md"
    role.parent.mkdir(parents=True)
    role.write_text("# Optimus")
    codex_home.mkdir(parents=True)
    (codex_home / "hooks.json").write_text('{"SessionStart": [{}]}')
    _seed_lincoln(sock)
    _tmux(sock, "set-environment", "-t", SESSION, "CLAUDE_AGENT_NAME", "lincoln")
    _tmux(sock, "set-environment", "-t", SESSION, "CLAUDE_AGENT_ROLE", "cockpit")
    assert "lincoln" in _tmux(
        sock, "show-environment", "-t", SESSION, "CLAUDE_AGENT_NAME"
    ).stdout
    codex_tui = (
        "bash --noprofile --norc -c '"
        "tmux set-option -p -t \"$TMUX_PANE\" @sable_boot_epoch \"test-$BASHPID\"; "
        "tmux set-option -p -t \"$TMUX_PANE\" @sable_test_tui_pid \"$BASHPID\"; "
        "while true; do printf \"› \"; "
        "IFS= read -r line || break; printf \"%s\\n\" \"$line\"; done'"
    )

    spawned = _run(
        sock,
        "optimus",
        env_overrides={
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "CLAUDE_AGENT_NAME": "lincoln",
            "CLAUDE_AGENT_ROLE": "cockpit",
            "SABLE_TMUX_PANE_CMD": codex_tui,
            "SABLE_AGENTS_YAML": str(
                Path(__file__).resolve().parent.parent
                / "templates" / "multi-manager" / "agents.yaml"
            ),
            "SABLE_DISPATCH_DIR": str(tmp_path / "dispatch"),
        },
    )

    assert spawned.returncode == 0, spawned.stderr
    pane = _pane_for_role(sock, "optimus")
    assert pane is not None
    environ = _pane_process_environ(sock, pane)
    assert environ["SABLE_PROVIDER"] == "codex"
    assert environ["SABLE_AGENT_NAME"] == "optimus"
    assert environ["SABLE_AGENT_ROLE"] == "manager"
    assert "CLAUDE_AGENT_NAME" not in environ
    assert "CLAUDE_AGENT_ROLE" not in environ


def test_codex_scrub_wrapper_preserves_running_to_done_lifecycle(sock):
    _seed_lincoln(sock)
    _tmux(sock, "set-option", "-g", "remain-on-exit", "on")
    observe_running = (
        'if [ "$(tmux show-options -p -v -t "$TMUX_PANE" '
        '@sable_status)" = running ]; then '
        'tmux set-option -p -t "$TMUX_PANE" @sable_saw_running yes; fi'
    )
    lifecycle = with_lifecycle_flags(observe_running)
    command = provider_lib.provider_pane_command("codex", lifecycle)
    env_args = [
        part
        for key, value in provider_lib.pane_environment(
            "codex", "optimus", "manager"
        ).items()
        for part in ("-e", f"{key}={value}")
    ]
    pane = _tmux(
        sock,
        "new-window", "-d", "-t", SESSION, "-n", "lifecycle",
        "-P", "-F", "#{pane_id}", *env_args, command,
    ).stdout.strip()

    _wait_pane(sock, pane, status="done", dead=True)
    assert _pane_option(sock, pane, "@sable_saw_running") == "yes"
    assert _pane_option(sock, pane, "@sable_status") == "done"


def test_manager_refuses_statusless_execution_before_creating_pane(sock):
    Path(os.environ["SABLE_MODE_STATE"]).write_text('{"mode":"execution"}')
    _seed_lincoln(sock)

    r = _run(sock, "optimus")

    assert r.returncode == 5
    assert "handoff" in r.stderr.lower()
    assert "optimus" not in _roles(sock)


def test_second_spawn_skips_idempotently(sock):
    _seed_lincoln(sock)
    _run(sock, "tarzan")
    before = _roles(sock)
    r = _run(sock, "tarzan")
    assert r.returncode == 0
    assert _roles(sock) == before
    assert "skip" in (r.stderr + r.stdout).lower()


def test_restarted_epoch_rekicks_same_pane_and_advances_reference(sock):
    _seed_lincoln(sock)
    first = _run(sock, "tarzan")
    assert first.returncode == 0, first.stderr
    pane = _pane_for_role(sock, "tarzan")
    assert pane is not None
    old_ref = _pane_option(sock, pane, "@sable_kicked_epoch")
    assert old_ref

    _tmux(sock, "set-option", "-p", "-t", pane, "@sable_boot_epoch", "new-session")
    restarted = _run(sock, "tarzan")

    assert restarted.returncode == 0, restarted.stderr
    assert _pane_for_role(sock, "tarzan") == pane
    assert _pane_option(sock, pane, "@sable_kicked_epoch") == "new-session"
    assert "re-kick" in (restarted.stdout + restarted.stderr).lower()


def test_done_manager_respawns_in_same_pane_with_fresh_lifecycle(sock):
    _seed_lincoln(sock)
    first = _run(sock, "optimus")
    assert first.returncode == 0, first.stderr
    pane = _pane_for_role(sock, "optimus")
    assert pane is not None
    old_epoch = _pane_option(sock, pane, "@sable_boot_epoch")
    window = _tmux(sock, "display-message", "-p", "-t", pane,
                   "#{window_id}").stdout.strip()
    _tmux(sock, "set-option", "-w", "-t", window, "remain-on-exit", "on")

    tui_pid = int(_pane_option(sock, pane, "@sable_test_tui_pid"))
    os.kill(tui_pid, signal.SIGTERM)
    _wait_pane(sock, pane, status="done", dead=True)

    respawned = _run(sock, "optimus")

    assert respawned.returncode == 0, respawned.stderr
    assert _pane_for_role(sock, "optimus") == pane
    _wait_pane(sock, pane, status="running", dead=False)
    new_epoch = _pane_option(sock, pane, "@sable_boot_epoch")
    assert new_epoch and new_epoch != old_epoch
    assert _pane_option(sock, pane, "@sable_kicked_epoch") == new_epoch
    assert "respawn" in (respawned.stdout + respawned.stderr).lower()


def test_reference_less_running_manager_is_adopted_without_typing(sock):
    _seed_lincoln(sock)
    pane = _tmux(
        sock,
        "new-window",
        "-d",
        "-t",
        SESSION,
        "-n",
        "optimus",
        "-P",
        "-F",
        "#{pane_id}",
        "bash --noprofile --norc",
    ).stdout.strip()
    for option, value in (
        ("@sable_role", "optimus"),
        ("@sable_provider", "claude"),
        ("@sable_class", "manager"),
        ("@sable_status", "running"),
        ("@sable_boot_epoch", "adopt-me"),
    ):
        _tmux(sock, "set-option", "-p", "-t", pane, option, value)

    adopted = _run(sock, "optimus")

    assert adopted.returncode == 0, adopted.stderr
    assert _pane_for_role(sock, "optimus") == pane
    assert _pane_option(sock, pane, "@sable_kicked_epoch") == "adopt-me"
    assert "adopted" in (adopted.stdout + adopted.stderr).lower()
    assert "SABLE-AUTOSTART" not in _tmux(
        sock, "capture-pane", "-p", "-t", pane
    ).stdout


def test_bare_tagged_manager_without_sessionstart_epoch_refuses_unknown(sock):
    _seed_lincoln(sock)
    pane = _tmux(
        sock,
        "new-window",
        "-d",
        "-t",
        SESSION,
        "-n",
        "optimus",
        "-P",
        "-F",
        "#{pane_id}",
        "bash --noprofile --norc",
    ).stdout.strip()
    for option, value in (
        ("@sable_role", "optimus"),
        ("@sable_provider", "claude"),
        ("@sable_class", "manager"),
        ("@sable_status", "running"),
    ):
        _tmux(sock, "set-option", "-p", "-t", pane, option, value)
    before = _tmux(sock, "list-panes", "-s", "-t", SESSION,
                    "-F", "#{pane_id}").stdout.split()

    refused = _run(sock, "optimus", "tarzan")

    after = _tmux(sock, "list-panes", "-s", "-t", SESSION,
                   "-F", "#{pane_id}").stdout.split()
    assert refused.returncode != 0
    assert "unknown" in (refused.stdout + refused.stderr).lower()
    assert after == before
    assert _pane_for_role(sock, "optimus") == pane
    assert _pane_for_role(sock, "tarzan") is None


def test_all_spawns_three_roles(sock):
    _seed_lincoln(sock)
    r = _run(sock, "--all")
    assert r.returncode == 0, r.stderr
    assert {"chuck", "optimus", "tarzan"} <= set(_roles(sock))


def test_unknown_dialog_refuses_manager_kick_without_typing(sock, tmp_path):
    """A fresh manager pane parked on an unknown selector must remain untouched.

    The stand-in records the first byte it receives. Discarding wait_for_ready's
    False return types the autostart kick into that read and creates the file
    even if Enter is never submitted.
    """
    rec = tmp_path / "typed-into-dialog.txt"
    script = tmp_path / "fake-dialog.sh"
    script.write_text(
        "echo '  ? Which workspace should be opened?'\n"
        "echo '  > 1. primary'\n"
        "echo '    2. recovery'\n"
        "echo '  (Use arrow keys, Enter to select)'\n"
        "IFS= read -r -n 1 byte\n"
        f'printf "%s" "$byte" > "{rec}"\n'
        "sleep 2\n"
    )
    _seed_lincoln(sock)
    env = {
        **os.environ,
        "SABLE_TMUX_SOCKET": sock,
        "SABLE_TMUX_SESSION": SESSION,
        "SABLE_TMUX_PANE_CMD": f"bash --noprofile --norc {script}",
        "SABLE_DISPATCH_READY_TIMEOUT": "0.6",
        "SABLE_DISPATCH_POLL_INTERVAL": "0.1",
        "SABLE_DISPATCH_SUBMIT_TRIES": "1",
    }
    r = subprocess.run(
        ["python3", str(BIN), "optimus"],
        capture_output=True, text=True, env=env,
    )

    assert r.returncode == 10, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "role=optimus" in r.stderr
    assert "provider=claude" in r.stderr
    assert "interactive dialog/selector" in r.stderr
    assert "NOT typed" in r.stderr
    assert "failed pane removed" in r.stderr
    time.sleep(0.3)
    # Assert on CONTENT, not existence. The fake pane records with
    # `read -r -n 1 byte; printf "%s" "$byte" > rec`, and the `>` redirect
    # creates the file the moment printf runs — which happens as soon as read
    # RETURNS FOR ANY REASON, including EOF when the gate removes the failed
    # pane. So an EMPTY file is evidence of the CORRECT outcome (read returned
    # nothing typed), and the old `not rec.exists()` check only passed when the
    # pane happened to die before printf ran. That is a race, and it went red
    # in CI once suite timing shifted. Empty iff nothing was typed: a real kick
    # would have given read its first byte and printf would have written it.
    # The sibling tests in this file already assert on content this way.
    typed = rec.read_text() if rec.exists() else ""
    assert typed == "", (
        f"manager kick landed in the unknown dialog: {typed!r}"
    )
    assert "optimus" not in _roles(sock)

    retry = _run(sock, "optimus")
    assert retry.returncode == 0, retry.stderr
    assert "optimus" in _roles(sock)
    assert "already running" not in retry.stderr


def test_known_startup_gate_is_accepted_then_manager_is_kicked(sock, tmp_path):
    """The fail-closed branch must preserve explicitly recognized selectors."""
    accepted = tmp_path / "accepted-key.txt"
    kicked = tmp_path / "manager-kick.txt"
    script = tmp_path / "fake-known-gate.sh"
    script.write_text(
        "tmux set-option -p -t \"$TMUX_PANE\" @sable_boot_epoch \"test-$BASHPID\"\n"
        "echo 'WARNING: Claude Code running in Bypass Permissions mode'\n"
        "echo '  1. No, exit'\n"
        "echo '  2. Yes, I accept'\n"
        "echo '  Enter to confirm'\n"
        "IFS= read -r key\n"
        f'printf "%s" "$key" > "{accepted}"\n'
        "printf '❯ '\n"
        "IFS= read -r line\n"
        f'printf "%s" "$line" > "{kicked}"\n'
        "printf '\\n%s\\n❯ ' \"$line\"\n"
        "IFS= read -r _hold\n"
    )
    _seed_lincoln(sock)
    env = {
        **os.environ,
        "SABLE_TMUX_SOCKET": sock,
        "SABLE_TMUX_SESSION": SESSION,
        "SABLE_TMUX_PANE_CMD": f"bash --noprofile --norc {script}",
        "SABLE_DISPATCH_READY_TIMEOUT": "2",
        "SABLE_DISPATCH_POLL_INTERVAL": "0.1",
        "SABLE_DISPATCH_SUBMIT_TRIES": "2",
    }
    r = subprocess.run(
        ["python3", str(BIN), "optimus"],
        capture_output=True, text=True, env=env,
    )

    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    for _ in range(20):
        if accepted.exists() and kicked.exists():
            break
        time.sleep(0.1)
    assert accepted.read_text() == "2"
    assert "SABLE-AUTOSTART" in kicked.read_text()


def test_unverified_manager_kick_fails_closed_and_retry_converges(sock, tmp_path):
    """A pane that exits on the first kick byte makes delivery unverifiable."""
    first_byte = tmp_path / "first-byte.txt"
    script = tmp_path / "exit-during-kick.sh"
    script.write_text(
        "tmux set-option -p -t \"$TMUX_PANE\" @sable_boot_epoch \"test-$BASHPID\"\n"
        "printf '❯ '\n"
        "IFS= read -r -n 1 byte\n"
        f'printf "%s" "$byte" > "{first_byte}"\n'
    )
    _seed_lincoln(sock)
    env = {
        **os.environ,
        "SABLE_TMUX_SOCKET": sock,
        "SABLE_TMUX_SESSION": SESSION,
        "SABLE_TMUX_PANE_CMD": f"bash --noprofile --norc {script}",
        "SABLE_DISPATCH_READY_TIMEOUT": "2",
        "SABLE_DISPATCH_POLL_INTERVAL": "0.1",
        "SABLE_DISPATCH_SUBMIT_TRIES": "1",
    }
    r = subprocess.run(
        ["python3", str(BIN), "optimus"],
        capture_output=True, text=True, env=env,
    )

    assert first_byte.read_text() == "[", "delivery-failure leg was not exercised"
    assert r.returncode == 11, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "delivery could not be verified" in r.stderr
    assert "role=optimus" in r.stderr
    assert "optimus" not in _roles(sock)

    retry = _run(sock, "optimus")
    assert retry.returncode == 0, retry.stderr
    assert "optimus" in _roles(sock)


def test_provider_boot_failure_removes_manager_pane(sock, tmp_path):
    """A missing Codex role card fails after pane creation but before typing."""
    state = Path(os.environ["SABLE_MODE_STATE"])
    _write_execution_state(state, {"optimus": "codex"})
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    codex_home = empty_home / ".codex"
    codex_home.mkdir()
    (codex_home / "hooks.json").write_text('{"SessionStart": [{}]}')
    first_byte = tmp_path / "first-byte.txt"
    script = tmp_path / "codex-ready.sh"
    script.write_text(
        "printf '› '\n"
        "IFS= read -r -n 1 byte\n"
        f'[ -z "$byte" ] || printf "%s" "$byte" > "{first_byte}"\n'
        "sleep 2\n"
    )
    _seed_lincoln(sock)
    env = {
        **os.environ,
        "HOME": str(empty_home),
        "CODEX_HOME": str(codex_home),
        # Keep fleet-boundary resolution project-local so this test reaches
        # the intended post-pane provider-role-card failure. Boundary refusal
        # itself has dedicated rc=6 coverage in test_sable_spawn_manager.py.
        "SABLE_AGENTS_YAML": str(
            Path(__file__).resolve().parent.parent
            / "templates" / "multi-manager" / "agents.yaml"
        ),
        "SABLE_DISPATCH_DIR": str(tmp_path / "dispatch"),
        "SABLE_TMUX_SOCKET": sock,
        "SABLE_TMUX_SESSION": SESSION,
        "SABLE_TMUX_PANE_CMD": f"bash --noprofile --norc {script}",
        "SABLE_DISPATCH_READY_TIMEOUT": "2",
        "SABLE_DISPATCH_POLL_INTERVAL": "0.1",
        "SABLE_DISPATCH_SUBMIT_TRIES": "1",
    }
    r = subprocess.run(
        ["python3", str(BIN), "optimus"],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )

    assert r.returncode == 5, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "no installed role card" in r.stderr
    assert "role=optimus" in r.stderr
    assert "provider=codex" in r.stderr
    assert "failed pane removed" in r.stderr
    assert not first_byte.exists(), "provider failure typed into the pane"
    assert "optimus" not in _roles(sock)

    _write_execution_state(state)
    retry = _run(sock, "optimus")
    assert retry.returncode == 0, retry.stderr
    assert "optimus" in _roles(sock)


def test_codex_hook_graph_preflight_refuses_then_spawn_converges(sock, tmp_path):
    state = Path(os.environ["SABLE_MODE_STATE"])
    _write_execution_state(state, {"optimus": "codex"})
    home = tmp_path / "home"
    home.mkdir()
    codex_home = home / ".codex"
    role = home / ".claude" / "sable" / "roles" / "optimus.md"
    role.parent.mkdir(parents=True)
    role.write_text("# Optimus")
    _seed_lincoln(sock)
    env = {
        **os.environ,
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "SABLE_AGENTS_YAML": str(
            Path(__file__).resolve().parent.parent
            / "templates" / "multi-manager" / "agents.yaml"
        ),
        "SABLE_DISPATCH_DIR": str(tmp_path / "dispatch"),
        "SABLE_TMUX_SOCKET": sock,
        "SABLE_TMUX_SESSION": SESSION,
        "SABLE_TMUX_PANE_CMD": (
            "bash --noprofile --norc -c '"
            "tmux set-option -p -t \"$TMUX_PANE\" @sable_boot_epoch \"test-$BASHPID\"; "
            "while true; do printf \"› \"; "
            "IFS= read -r line || break; printf \"%s\\n\" \"$line\"; done'"
        ),
        "SABLE_DISPATCH_READY_TIMEOUT": "2",
        "SABLE_DISPATCH_SUBMIT_TRIES": "1",
        "SABLE_DISPATCH_POLL_INTERVAL": "0.1",
    }

    refused = subprocess.run(
        ["python3", str(BIN), "optimus"],
        capture_output=True, text=True, env=env,
    )

    assert refused.returncode == 5
    assert "sable-orchestration-install --user --merge-settings" in refused.stderr
    assert "optimus" not in _roles(sock)

    codex_home.mkdir()
    (codex_home / "hooks.json").write_text('{"SessionStart": [{}]}')
    accepted = subprocess.run(
        ["python3", str(BIN), "optimus"],
        capture_output=True, text=True, env=env,
    )

    assert accepted.returncode == 0, accepted.stderr
    assert "optimus" in _roles(sock)


# --- SABLE-tz7h.1: producer spawn contract -------------------------------

def test_producer_spawn_tags_class_and_deliverable(sock, tmp_path):
    _seed_lincoln(sock)
    deliverable = tmp_path / "victor-report.md"
    r = _run(sock, "victor", "--deliverable", str(deliverable))
    assert r.returncode == 0, r.stderr
    assert "victor" in _roles(sock)

    pane = _pane_for_role(sock, "victor")
    assert pane is not None
    assert _pane_option(sock, pane, "@sable_class") == "producer"
    assert _pane_option(sock, pane, "@sable_deliverable") == str(deliverable)

    # window 0 (Lincoln) is never disturbed by a producer spawn either
    active = _tmux(sock, "display-message", "-t", SESSION, "-p",
                   "#{window_index}").stdout.strip()
    assert active == "0"


def test_producer_spawn_remains_available_in_planning(sock, tmp_path):
    Path(os.environ["SABLE_MODE_STATE"]).write_text(
        '{"mode":"planning","tier":"full","substage":"research"}'
    )
    _seed_lincoln(sock)
    deliverable = tmp_path / "victor-report.md"

    r = _run(sock, "victor", "--deliverable", str(deliverable))

    assert r.returncode == 0, r.stderr
    assert "victor" in _roles(sock)


def test_producer_spawn_requires_deliverable(sock):
    _seed_lincoln(sock)
    r = _run(sock, "victor")
    assert r.returncode == 2
    assert "--deliverable" in r.stderr
    assert "victor" not in _roles(sock)


def test_all_spawns_tag_manager_class(sock):
    _seed_lincoln(sock)
    r = _run(sock, "--all")
    assert r.returncode == 0, r.stderr
    for role in ("optimus", "tarzan", "chuck"):
        pane = _pane_for_role(sock, role)
        assert pane is not None
        assert _pane_option(sock, pane, "@sable_class") == "manager"


def test_no_session_points_at_launch(sock):
    r = _run(sock, "optimus")
    assert r.returncode == 1
    assert "sable-launch" in r.stderr


def test_manager_spawn_pins_real_claude_command_to_opus(sock, tmp_path):
    """SABLE-gbd: a real spawn (no SABLE_TMUX_PANE_CMD override) must launch
    the pane's `claude` process with --model opus. A real `claude` binary
    would actually start a live, autonomous session and burn API calls the
    moment deliver_text() types the autostart kick into it (proven the hard
    way: an earlier draft of this test genuinely launched one). So PATH is
    pointed at a harmless stub for JUST this test — everything else (the
    tmux server, sable-spawn-manager's own subprocess call, the argv
    construction, the real `new-window`) is real.

    The stub must be resolvable at the point `new-window` is actually
    invoked: tmux resolves that command using the environment of the CLIENT
    process making the `new-window` call — here, sable-spawn-manager's own
    `subprocess.run`, which inherits from the sable-spawn-manager process
    itself — NOT the environment the target session was originally created
    with. So the PATH override goes on the `python3 sable-spawn-manager`
    subprocess env, not the tmux server's.
    """
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir()
    log_path = tmp_path / "claude-args.log"
    stub = stub_dir / "claude"
    # The log path is baked into the script text (not passed via env/-e):
    # a new-window env var wouldn't reliably reach the spawned process even
    # though PATH-based command resolution does (see class docstring).
    stub.write_text(
        "#!/bin/sh\n"
        "tmux set-option -p -t \"$TMUX_PANE\" @sable_boot_epoch \"test-$$\"\n"
        f'printf "%s\\n" "$*" >> "{log_path}"\n'
        "while true; do printf '❯ '; IFS= read -r line || break; "
        "printf '%s\\n' \"$line\"; done\n"
    )
    stub.chmod(0o755)

    _seed_lincoln(sock)

    r = subprocess.run(["python3", str(BIN), "optimus"], capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                          "SABLE_TMUX_SOCKET": sock,
                          "SABLE_TMUX_SESSION": SESSION,
                          "SABLE_DISPATCH_READY_TIMEOUT": "2",
                          "SABLE_DISPATCH_SUBMIT_TRIES": "1",
                          "SABLE_DISPATCH_POLL_INTERVAL": "0.1"})
    assert r.returncode == 0, r.stderr
    assert "optimus" in _roles(sock)
    # The stub's interpreter (sh), not the real claude binary, must be what
    # actually ran — the strongest confirmation the stub was really hit.
    assert "sh" in _tmux(sock, "list-panes", "-a", "-F",
                         "#{@sable_role} #{pane_current_command}").stdout

    for _ in range(20):
        if log_path.exists() and log_path.read_text().strip():
            break
        time.sleep(0.1)
    assert log_path.exists(), "stub claude binary was never invoked"
    logged = log_path.read_text().strip()
    assert "--model opus" in logged, logged


# --- 73t4: instance registry CLI (register / self-reg refusal / prune) ------

def _reg_env(sock, yaml, **extra):
    return {**os.environ, "SABLE_TMUX_SOCKET": sock, "SABLE_TMUX_SESSION": SESSION,
            "SABLE_AGENTS_YAML": str(yaml), **extra}


def _spawn_manager(sock, *args, env):
    return subprocess.run(["python3", str(BIN), *args], capture_output=True, text=True, env=env)


def test_register_instance_cli_writes_entry(sock, tmp_path):
    yaml = tmp_path / "agents.yaml"
    yaml.write_text("agents:\n  optimus:\n    type: epic_manager\n"
                    "  tarzan:\n    type: one_off_manager\n")
    r = _spawn_manager(sock, "--register-instance", "tarzan-2", env=_reg_env(sock, yaml))
    assert r.returncode == 0, r.stderr
    txt = yaml.read_text()
    assert "  tarzan-2:" in txt and "instance_of: tarzan" in txt
    # idempotent second call
    r2 = _spawn_manager(sock, "--register-instance", "tarzan-2", env=_reg_env(sock, yaml))
    assert r2.returncode == 0 and "exists" in r2.stderr
    assert yaml.read_text().count("  tarzan-2:") == 1


def test_register_instance_cli_refuses_self(sock, tmp_path):
    """A session whose own identity == the name it tries to register is refused
    (constraint 2: no self-elevation)."""
    yaml = tmp_path / "agents.yaml"
    yaml.write_text("agents:\n  tarzan:\n    type: one_off_manager\n")
    r = _spawn_manager(sock, "--register-instance", "tarzan-2",
                       env=_reg_env(sock, yaml, SABLE_REGISTER_ACTOR="tarzan-2"))
    assert r.returncode == 3
    assert "refused-self" in r.stderr
    assert "tarzan-2" not in yaml.read_text()


def test_prune_instances_cli_keeps_live_prunes_dead(sock, tmp_path):
    yaml = tmp_path / "agents.yaml"
    yaml.write_text("agents:\n  optimus:\n    type: epic_manager\n"
                    "  tarzan:\n    type: one_off_manager\n")
    env = _reg_env(sock, yaml)
    _spawn_manager(sock, "--register-instance", "tarzan-2", env=env)
    _spawn_manager(sock, "--register-instance", "optimus-3", env=env)
    # a live tarzan-2 pane; optimus-3 has none
    _seed_lincoln(sock)
    pane = _tmux(sock, "new-window", "-d", "-P", "-F", "#{pane_id}",
                 "-t", SESSION, "bash").stdout.strip()
    _tmux(sock, "set-option", "-p", "-t", pane, "@sable_role", "tarzan-2")
    r = _spawn_manager(sock, "--prune-instances", env=env)
    assert r.returncode == 0, r.stderr
    txt = yaml.read_text()
    assert "  tarzan-2:" in txt        # live instance kept
    assert "  optimus-3:" not in txt   # dead instance pruned
    assert "  tarzan:" in txt and "  optimus:" in txt   # base entries untouched


if __name__ == "__main__":
    import sys
    import pytest as _p
    sys.exit(_p.main([__file__, "-q"]))
