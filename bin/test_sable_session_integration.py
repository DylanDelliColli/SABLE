#!/usr/bin/env python3
"""Integration tests for the session resolver + bin/sable-session CLI against a
REAL tmux server on an isolated socket (SABLE-e1e3.1).

Proves the multi-repo contract end-to-end: two repos resolve to two distinct
sessions, the collision guard trips on a hijacked name, and the legacy 'sable'
session is claimed only by the repo whose panes actually live in it.
"""
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-session"
HAVE_TMUX = shutil.which("tmux") is not None
pytestmark = pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")


@pytest.fixture()
def sock():
    s = f"sable-sr-{uuid.uuid4().hex[:8]}"
    yield s
    subprocess.run(["tmux", "-L", s, "kill-server"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _tmux(s, *args):
    return subprocess.run(["tmux", "-L", s, *args],
                          capture_output=True, text=True, check=True)


def make_repo(tmp_path, name):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def run_cli(cwd, sock, extra_env=None):
    env = {**os.environ, "SABLE_TMUX_SOCKET": sock}
    env.pop("SABLE_TMUX_SESSION", None)
    # These tests exercise CWD-derivation and must not depend on whichever
    # real pane happens to be running pytest (SABLE-ssd8: resolve_session now
    # prefers the calling pane's actual session, and pytest's own ambient
    # $TMUX_PANE could coincidentally collide with a pane id on this freshly
    # created isolated socket, since ids restart from %0 per server).
    env.pop("TMUX_PANE", None)
    env.pop("TMUX", None)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(BIN)], cwd=cwd,
                          capture_output=True, text=True, env=env)


def test_two_repos_two_sessions(tmp_path, sock):
    a = make_repo(tmp_path, "alpha")
    b = make_repo(tmp_path, "beta")
    ra, rb = run_cli(a, sock), run_cli(b, sock)
    assert ra.returncode == 0 and rb.returncode == 0, ra.stderr + rb.stderr
    assert ra.stdout.strip() == "sable-alpha"
    assert rb.stdout.strip() == "sable-beta"


def test_env_override_wins(tmp_path, sock):
    a = make_repo(tmp_path, "alpha")
    r = run_cli(a, sock, {"SABLE_TMUX_SESSION": "sable"})
    assert r.returncode == 0
    assert r.stdout.strip() == "sable"


def test_collision_guard_trips(tmp_path, sock):
    a = make_repo(tmp_path, "alpha")
    _tmux(sock, "new-session", "-d", "-s", "sable-alpha", "-x", "80", "-y", "24",
          "bash", "--noprofile", "--norc")
    _tmux(sock, "set-option", "-t", "sable-alpha", "@sable_repo", "/somewhere/else/alpha")
    r = run_cli(a, sock)
    assert r.returncode != 0
    assert "SABLE_TMUX_SESSION" in r.stderr


def test_owned_derived_session_resolves(tmp_path, sock):
    a = make_repo(tmp_path, "alpha")
    _tmux(sock, "new-session", "-d", "-s", "sable-alpha", "-x", "80", "-y", "24",
          "bash", "--noprofile", "--norc")
    _tmux(sock, "set-option", "-t", "sable-alpha", "@sable_repo",
          str(a.resolve()))
    r = run_cli(a, sock)
    assert r.returncode == 0
    assert r.stdout.strip() == "sable-alpha"


def test_legacy_session_claimed_by_its_own_repo_only(tmp_path, sock):
    a = make_repo(tmp_path, "alpha")
    b = make_repo(tmp_path, "beta")
    # legacy fleet: session literally named 'sable', pane cwd'd inside repo a
    _tmux(sock, "new-session", "-d", "-s", "sable", "-x", "80", "-y", "24",
          "-c", str(a), "bash", "--noprofile", "--norc")
    ra, rb = run_cli(a, sock), run_cli(b, sock)
    assert ra.stdout.strip() == "sable"        # a's fleet stays addressable
    assert rb.stdout.strip() == "sable-beta"   # b never touches a's fleet


def test_unrelated_prefix_session_does_not_spuriously_claim_legacy_name(tmp_path, sock):
    # SABLE-hvwk: tmux resolves a bare has-session -t target with prefix/
    # fnmatch semantics when no exact match exists, and LEGACY_SESSION
    # ('sable') is a prefix of every per-repo derived name (sable-<repo>).
    # Reproduce the exact hazard the bead describes: a DIFFERENT repo's
    # derived fleet session exists (no session literally named 'sable'), and
    # -- the coincidence that masked this in practice -- its pane cwd happens
    # to sit inside THIS repo's root too. Before the fix, has-session -t sable
    # prefix-matches 'sable-somefleet', _panes_under_root then also passes on
    # the coincidental cwd match, and resolve_session wrongly returns the
    # legacy name instead of gamma's own derived session.
    g = make_repo(tmp_path, "gamma")
    _tmux(sock, "new-session", "-d", "-s", "sable-somefleet", "-x", "80", "-y", "24",
          "-c", str(g), "bash", "--noprofile", "--norc")
    r = run_cli(g, sock)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "sable-gamma"
