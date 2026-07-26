#!/usr/bin/env python3
"""Unit tests for the shadowed-role-card warning in
hooks/multi-manager/session-role-anchor.sh (SABLE-thx70).

session-role-anchor.sh resolves a manager's role card PROJECT-FIRST
($PWD/.claude/sable/roles/<name>.md) then falls back to the user-level
install (~/.claude/sable/roles/<name>.md). That precedence is a supported
feature; the defect was that a stale project-local copy could silently
outrank a freshly-edited user-level one forever, with no event — six days of
role-card edits went dark fleet-wide before anyone noticed (measured on
optimus.md: six bead ids present ONLY in the user-level copy were never
read).

These tests construct BOTH candidate paths with differing content in a
tmp_path fixture (the live tree can't reproduce this any more — the
project-local shadow copies were already removed) and invoke the REAL hook
script as a subprocess, asserting: (1) the warning fires and names both
paths when they exist and differ, (2) precedence is unchanged — the
project-local copy still wins the injected identity, and (3) three negative
controls stay silent — only one candidate present (either side), and both
present but byte-identical — since a warning firing on every ordinary boot
is noise that would get reverted.
"""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "multi-manager" / "session-role-anchor.sh"

# SABLE-j3bi: this suite may itself run inside a live SABLE manager/worker
# pane, whose real CLAUDE_AGENT_NAME / SABLE_WORKER_PANE / CLAUDE_AGENT_ROLE
# would otherwise leak into the subprocess env and short-circuit the hook
# (the worker-pane or non-manager early-exits) before it ever reaches the
# resolver under test.
_IDENTITY_VARS = ("SABLE_WORKER_PANE", "CLAUDE_AGENT_NAME", "CLAUDE_AGENT_ROLE", "SABLE_BEAD")

SS_INPUT = '{"hook_event_name":"SessionStart"}'


def _run_hook(cwd, home, name="cockpit", role="manager"):
    env = {k: v for k, v in os.environ.items() if k not in _IDENTITY_VARS}
    env["HOME"] = str(home)
    env["CLAUDE_AGENT_NAME"] = name
    env["CLAUDE_AGENT_ROLE"] = role
    proc = subprocess.run(
        ["bash", str(HOOK)],
        input=SS_INPUT,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    return proc.stdout, proc.stderr


def _role_paths(cwd: Path, home: Path, name="cockpit"):
    project = cwd / ".claude" / "sable" / "roles" / f"{name}.md"
    user = home / ".claude" / "sable" / "roles" / f"{name}.md"
    project.parent.mkdir(parents=True, exist_ok=True)
    user.parent.mkdir(parents=True, exist_ok=True)
    return project, user


def _make_dirs(tmp_path):
    cwd = tmp_path / "proj"
    home = tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    return cwd, home


def test_shadowed_role_card_is_reported(tmp_path):
    cwd, home = _make_dirs(tmp_path)
    project, user = _role_paths(cwd, home)
    project.write_text("PROJECT_VERSION\n")
    user.write_text("USER_VERSION\n")

    stdout, stderr = _run_hook(cwd, home)

    assert "PROJECT_VERSION" in stdout  # precedence unchanged: project-local still wins
    assert "SABLE-ROLE-CARD-SHADOWED" in stderr
    assert str(project) in stderr
    assert str(user) in stderr


def test_negative_control_only_project_local_no_warning(tmp_path):
    cwd, home = _make_dirs(tmp_path)
    project, _user = _role_paths(cwd, home)
    project.write_text("PROJECT_VERSION\n")

    stdout, stderr = _run_hook(cwd, home)

    assert "PROJECT_VERSION" in stdout
    assert "SABLE-ROLE-CARD-SHADOWED" not in stderr


def test_negative_control_only_user_level_no_warning(tmp_path):
    cwd, home = _make_dirs(tmp_path)
    _project, user = _role_paths(cwd, home)
    user.write_text("USER_VERSION\n")

    stdout, stderr = _run_hook(cwd, home)

    assert "USER_VERSION" in stdout
    assert "SABLE-ROLE-CARD-SHADOWED" not in stderr


def test_negative_control_identical_content_no_warning(tmp_path):
    cwd, home = _make_dirs(tmp_path)
    project, user = _role_paths(cwd, home)
    project.write_text("SAME_VERSION\n")
    user.write_text("SAME_VERSION\n")

    stdout, stderr = _run_hook(cwd, home)

    assert "SAME_VERSION" in stdout
    assert "SABLE-ROLE-CARD-SHADOWED" not in stderr


def test_no_role_anywhere_no_warning(tmp_path):
    cwd, home = _make_dirs(tmp_path)

    stdout, stderr = _run_hook(cwd, home)

    assert stdout == ""
    assert "SABLE-ROLE-CARD-SHADOWED" not in stderr


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
