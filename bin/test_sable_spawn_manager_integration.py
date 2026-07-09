#!/usr/bin/env python3
"""Integration tests for bin/sable-spawn-manager against a REAL tmux server.

Isolated socket. Seeds a lincoln-only session (the sable-launch shape), then
proves: spawning a manager creates a DETACHED role-tagged window (window 0
stays active), a second spawn of the same role skips idempotently, --all
stands up all three autonomous roles, and a missing session errors pointing
at sable-launch.
"""
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-spawn-manager"
HAVE_TMUX = shutil.which("tmux") is not None
pytestmark = pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")

SESSION = "ssm"


@pytest.fixture()
def sock():
    s = f"sable-sm-{uuid.uuid4().hex[:8]}"
    yield s
    subprocess.run(["tmux", "-L", s, "kill-server"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _tmux(s, *args, check=True):
    return subprocess.run(["tmux", "-L", s, *args],
                          capture_output=True, text=True, check=check)


def _run(s, *args):
    return subprocess.run(["python3", str(BIN), *args], capture_output=True, text=True,
                          env={**os.environ, "SABLE_TMUX_SOCKET": s,
                             "SABLE_TMUX_SESSION": SESSION,
                             "SABLE_TMUX_PANE_CMD": "bash",
                             "SABLE_DISPATCH_READY_TIMEOUT": "0",
                             "SABLE_DISPATCH_SUBMIT_TRIES": "1",
                             "SABLE_DISPATCH_POLL_INTERVAL": "0.1"})


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


def test_second_spawn_skips_idempotently(sock):
    _seed_lincoln(sock)
    _run(sock, "tarzan")
    before = _roles(sock)
    r = _run(sock, "tarzan")
    assert r.returncode == 0
    assert _roles(sock) == before
    assert "skip" in (r.stderr + r.stdout).lower()


def test_all_spawns_three_roles(sock):
    _seed_lincoln(sock)
    r = _run(sock, "--all")
    assert r.returncode == 0, r.stderr
    assert {"chuck", "optimus", "tarzan"} <= set(_roles(sock))


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
