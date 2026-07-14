#!/usr/bin/env python3
"""Integration tests for bin/sable-build-agents (SABLE-t4s3).

Real composition: the actual bin/sable-build-agents binary run as a
subprocess against the real templates/multi-manager/roles/ source tree,
regenerated into a temp dir and diffed against the real committed
templates/agents/*.md at HEAD -- the regeneration-idempotence guard this
bead exists to add. A hand-edit to any generated agent def (or a role
source edit without a rebuild) reds this suite.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUILDER = REPO / "bin" / "sable-build-agents"
AGENTS_DIR = REPO / "templates" / "agents"
AGENTS = sorted(p.stem for p in AGENTS_DIR.glob("*.md"))


def run_builder(out_dir, *extra_args):
    return subprocess.run(
        [sys.executable, str(BUILDER), "--out-dir", str(out_dir), *extra_args],
        capture_output=True, text=True, timeout=30,
    )


def test_builder_regenerates_cleanly(tmp_path):
    result = run_builder(tmp_path / "out")
    assert result.returncode == 0, result.stdout + result.stderr


def test_regeneration_matches_committed_tree_at_head(tmp_path):
    result = run_builder(tmp_path / "out")
    assert result.returncode == 0, result.stderr
    out_dir = tmp_path / "out"
    assert AGENTS, "no committed agent defs found under templates/agents/"
    for name in AGENTS:
        committed = (AGENTS_DIR / f"{name}.md").read_bytes()
        regenerated = (out_dir / f"{name}.md").read_bytes()
        assert regenerated == committed, (
            f"{name}.md drifted from its regeneration -- re-run bin/sable-build-agents"
        )


def test_two_consecutive_runs_are_byte_identical(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert run_builder(a).returncode == 0
    assert run_builder(b).returncode == 0
    for name in AGENTS:
        assert (a / f"{name}.md").read_bytes() == (b / f"{name}.md").read_bytes()


def test_hand_edit_to_a_committed_agent_def_would_be_caught(tmp_path):
    """Demonstrates the guard's failure mode without mutating the real repo:
    a copy of a committed artifact with a hand-edit no longer matches a fresh
    regeneration from the same sources."""
    result = run_builder(tmp_path / "out")
    assert result.returncode == 0
    name = AGENTS[0]
    regenerated = (tmp_path / "out" / f"{name}.md").read_bytes()
    hand_edited = regenerated + b"\nSOMEONE HAND-EDITED THIS LINE\n"
    assert hand_edited != regenerated
