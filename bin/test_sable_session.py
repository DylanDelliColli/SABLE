#!/usr/bin/env python3
"""Unit tests for the per-repo tmux session resolver (SABLE-e1e3.1).

Covers: name sanitization, derivation from the repo root, and resolve_session
precedence — env override, derived-session ownership check (collision), the
transitional legacy-'sable' heuristic (pane cwd under the caller's root), and
the outside-a-repo fallback.
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

    def __init__(self, sessions=None, repos=None, pane_paths=None):
        self.sessions = sessions or set()
        self.repos = repos or {}
        self.pane_paths = pane_paths or {}
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


def resolve(fake, root=ROOT, **kw):
    return lib.resolve_session(base=root, run=fake, _root=root, **kw)


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("SABLE_TMUX_SESSION", "custom")
    fake = FakeTmux()
    assert resolve(fake) == "custom"
    assert fake.calls == []  # no tmux consulted


def test_outside_repo_falls_back_to_legacy_name(monkeypatch):
    monkeypatch.delenv("SABLE_TMUX_SESSION", raising=False)
    fake = FakeTmux()
    assert lib.resolve_session(base="/nowhere", run=fake, _root=None) == "sable"


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


# --- repo_root ---------------------------------------------------------------

def test_repo_root_in_git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert lib.repo_root(str(tmp_path)) == str(tmp_path.resolve())


def test_repo_root_outside_git_repo(tmp_path):
    assert lib.repo_root(str(tmp_path)) is None
