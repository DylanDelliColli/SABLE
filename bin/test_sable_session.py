#!/usr/bin/env python3
"""Unit tests for the per-repo tmux session resolver (SABLE-e1e3.1, SABLE-ssd8).

Covers: name sanitization, derivation from the repo root, resolve_session
precedence — env override, the calling pane's actual tmux session (SABLE-ssd8),
derived-session ownership check (collision), the transitional legacy-'sable'
heuristic (pane cwd under the caller's root), and the outside-a-repo fallback.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_pane_lib as lib  # noqa: E402


class FakeTmux:
    """Canned tmux runner: knows which sessions exist, their @sable_repo
    session option, and their panes' current paths."""

    def __init__(self, sessions=None, repos=None, pane_paths=None, pane_sessions=None):
        self.sessions = sessions or set()
        self.repos = repos or {}
        self.pane_paths = pane_paths or {}
        self.pane_sessions = pane_sessions or {}
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        args = cmd[cmd.index("tmux") + 1:] if "tmux" in cmd else cmd
        if "-L" in args:
            i = args.index("-L")
            args = args[:i] + args[i + 2:]
        verb = args[0]
        target = args[args.index("-t") + 1] if "-t" in args else None
        if verb == "has-session":
            rc = 0 if target in self.sessions else 1
            return subprocess.CompletedProcess(cmd, rc, "", "")
        if verb == "show-options":
            val = self.repos.get(target)
            if val is None:
                return subprocess.CompletedProcess(cmd, 1, "", "")
            return subprocess.CompletedProcess(cmd, 0, val + "\n", "")
        if verb == "list-panes":
            paths = self.pane_paths.get(target, [])
            return subprocess.CompletedProcess(cmd, 0, "\n".join(paths) + "\n", "")
        if verb == "display-message":
            val = self.pane_sessions.get(target)
            if val is None:
                return subprocess.CompletedProcess(cmd, 1, "", "")
            return subprocess.CompletedProcess(cmd, 0, val + "\n", "")
        raise AssertionError(f"unexpected tmux verb {verb}")


# --- sanitize / derive -------------------------------------------------------

def test_sanitize_replaces_tmux_hostile_chars():
    assert lib.sanitize_session_name("my.repo:v2") == "my-repo-v2"


def test_sanitize_strips_leading_dashes_and_collapses():
    assert lib.sanitize_session_name("--weird..name") == "weird-name"


def test_sanitize_empty_falls_back():
    assert lib.sanitize_session_name("...") == "repo"


def test_derived_session_uses_basename():
    assert lib.derived_session("/home/x/dev/SABLE") == "sable-SABLE"


# --- resolve_session precedence ---------------------------------------------

ROOT = "/home/x/dev/repoA"
OTHER = "/home/x/dev/repoB"


def resolve(fake, root=ROOT, pane_session=None, **kw):
    # pane_session=None by default: these precedence tests are about the
    # CWD-derivation path (pre-SABLE-ssd8) and must not depend on whether
    # THIS process happens to be running inside a real tmux pane. The
    # pane-session-wins behavior itself is covered separately below.
    return lib.resolve_session(base=root, run=fake, _root=root,
                               _pane_session=pane_session, **kw)


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("SABLE_TMUX_SESSION", "custom")
    fake = FakeTmux()
    assert resolve(fake) == "custom"
    assert fake.calls == []  # no tmux consulted


def test_env_override_wins_even_over_pane_session(monkeypatch):
    monkeypatch.setenv("SABLE_TMUX_SESSION", "custom")
    fake = FakeTmux()
    assert resolve(fake, pane_session="sable-some-other-fleet") == "custom"
    assert fake.calls == []  # short-circuited before any tmux/pane lookup


def test_outside_repo_falls_back_to_legacy_name(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_SESSION", raising=False)
    fake = FakeTmux()
    assert lib.resolve_session(base="/nowhere", run=fake, _root=None,
                               _pane_session=None) == "sable"


def test_derived_exists_and_owned_by_us(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_SESSION", raising=False)
    fake = FakeTmux(sessions={"sable-repoA"}, repos={"sable-repoA": ROOT})
    assert resolve(fake) == "sable-repoA"


def test_derived_exists_unowned_is_reused(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_SESSION", raising=False)
    fake = FakeTmux(sessions={"sable-repoA"})  # no @sable_repo recorded
    assert resolve(fake) == "sable-repoA"


def test_derived_exists_owned_elsewhere_collides(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_SESSION", raising=False)
    fake = FakeTmux(sessions={"sable-repoA"}, repos={"sable-repoA": OTHER})
    with pytest.raises(lib.SessionCollision) as e:
        resolve(fake)
    assert "SABLE_TMUX_SESSION" in str(e.value)


def test_legacy_claimed_by_pane_cwd(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_SESSION", raising=False)
    fake = FakeTmux(sessions={"sable"},
                    pane_paths={"sable": [OTHER, ROOT + "/sub"]})
    assert resolve(fake) == "sable"


def test_legacy_owned_by_other_repo_is_ignored(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_SESSION", raising=False)
    fake = FakeTmux(sessions={"sable"}, pane_paths={"sable": [OTHER]})
    assert resolve(fake) == "sable-repoA"


def test_no_sessions_at_all_returns_derived(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_SESSION", raising=False)
    fake = FakeTmux()
    assert resolve(fake) == "sable-repoA"


def test_prefix_match_requires_boundary(monkeypatch):
    # /home/x/dev/repoAxx must NOT claim repoA's legacy fallback
    monkeypatch.delenv("SABLE_TMUX_SESSION", raising=False)
    fake = FakeTmux(sessions={"sable"}, pane_paths={"sable": [ROOT + "xx"]})
    assert resolve(fake) == "sable-repoA"


# --- calling_pane_session (SABLE-ssd8) ---------------------------------------

def test_calling_pane_session_outside_tmux_returns_none(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    fake = FakeTmux()
    assert lib.calling_pane_session(["tmux"], run=fake) is None
    assert fake.calls == []  # no point asking tmux with no pane to ask about


def test_calling_pane_session_inside_tmux_queries_display_message(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%7")
    fake = FakeTmux(pane_sessions={"%7": "sable-market-brief-package"})
    assert lib.calling_pane_session(["tmux"], run=fake) == "sable-market-brief-package"


def test_calling_pane_session_dead_pane_returns_none(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%404")
    fake = FakeTmux()  # no entry for %404 -> display-message fails
    assert lib.calling_pane_session(["tmux"], run=fake) is None


# --- resolve_session: pane-session precedence (SABLE-ssd8) -------------------
# Reproduces the live bug: a worker's actual tmux session (where tarzan lives)
# differs from the session CWD-derivation would compute for its worktree repo.
# The calling pane's real session must win even when the CWD-derived session
# ALSO validly exists — otherwise a worker could silently talk to the wrong
# fleet's stale pane instead of failing over or finding the right one.

def test_pane_session_wins_over_a_validly_existing_derived_session(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_SESSION", raising=False)
    fake = FakeTmux(sessions={"sable-repoA"}, repos={"sable-repoA": ROOT})
    assert resolve(fake, pane_session="sable-market-brief-package") == \
        "sable-market-brief-package"
    # the derived-session machinery (has-session/show-options) was never
    # consulted -- pane-session short-circuits before it
    assert fake.calls == []


def test_no_pane_session_falls_back_to_cwd_derivation(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_SESSION", raising=False)
    fake = FakeTmux(sessions={"sable-repoA"}, repos={"sable-repoA": ROOT})
    assert resolve(fake, pane_session=None) == "sable-repoA"


def test_pane_session_auto_wires_calling_pane_session_into_resolve(monkeypatch):
    # End-to-end: TMUX_PANE set for real, resolve_session's default "auto"
    # seam must consult calling_pane_session itself (not just accept an
    # injected value) and let it win over a validly-existing derived session.
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.delenv("SABLE_TMUX_SESSION", raising=False)
    fake = FakeTmux(sessions={"sable-repoA"}, repos={"sable-repoA": ROOT},
                    pane_sessions={"%7": "sable-market-brief-package"})
    assert lib.resolve_session(base=ROOT, run=fake, _root=ROOT) == \
        "sable-market-brief-package"


# --- repo_root ---------------------------------------------------------------

def test_repo_root_in_git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert lib.repo_root(str(tmp_path)) == str(tmp_path.resolve())


def test_repo_root_outside_git_repo(tmp_path):
    assert lib.repo_root(str(tmp_path)) is None
