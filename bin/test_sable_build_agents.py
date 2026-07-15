#!/usr/bin/env python3
"""Unit tests for bin/sable-build-agents (SABLE-t4s3).

Pure logic against synthetic role-file fixtures under tmp_path, with the
module's ROLES/AGENTS globals monkeypatched -- no dependency on the real
templates/multi-manager/roles/ tree (that's the integration variant, which
regenerates the real repo and diffs against templates/agents/ at HEAD).
Covers the regeneration-idempotence guard this bead exists to add: the
generator must be deterministic (same input -> byte-identical output across
runs) so that a diff against committed output actually means something, and a
hand-edit to a generated file must be detectable by that diff.
"""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader(
    "sable_build_agents", str(Path(__file__).resolve().parent / "sable-build-agents")
)
_SPEC = importlib.util.spec_from_loader("sable_build_agents", _LOADER)
build_agents = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(build_agents)


def make_roles(tmp_path, roles):
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    for name, body in roles.items():
        (roles_dir / f"{name}.md").write_text(body)
    return roles_dir


@pytest.fixture()
def synthetic_agents(tmp_path, monkeypatch):
    """Points the module at a small synthetic role tree instead of the real repo."""
    roles_dir = make_roles(tmp_path, {
        "alpha": "# alpha role\nbody text A\n",
        "beta": "# beta role\nbody text B\n",
    })
    agents = {"alpha": "does alpha things", "beta": "does beta things"}
    monkeypatch.setattr(build_agents, "ROLES", roles_dir)
    monkeypatch.setattr(build_agents, "AGENTS", agents)
    return roles_dir, agents


# --- build() basic behavior ---------------------------------------------------

def test_build_writes_one_file_per_agent(tmp_path, synthetic_agents):
    out_dir = tmp_path / "out"
    rc = build_agents.build(out_dir)
    assert rc == 0
    assert sorted(p.name for p in out_dir.iterdir()) == ["alpha.md", "beta.md"]


def test_build_generated_file_carries_frontmatter_and_marker(tmp_path, synthetic_agents):
    out_dir = tmp_path / "out"
    build_agents.build(out_dir)
    text = (out_dir / "alpha.md").read_text()
    assert text.startswith("---\nname: alpha\ndescription: does alpha things\n---\n")
    assert "GENERATED from templates/multi-manager/roles/alpha.md by bin/sable-build-agents" in text
    assert text.rstrip("\n").endswith("body text A")


def test_build_missing_role_file_errors(tmp_path, monkeypatch, capsys):
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()  # empty -- no role files
    monkeypatch.setattr(build_agents, "ROLES", roles_dir)
    monkeypatch.setattr(build_agents, "AGENTS", {"ghost": "a role with no source file"})
    rc = build_agents.build(tmp_path / "out")
    assert rc == 1
    assert "missing role file" in capsys.readouterr().err


# --- regeneration-idempotence guard --------------------------------------------

def test_build_is_idempotent_across_two_runs(tmp_path, synthetic_agents):
    out_a, out_b = tmp_path / "out_a", tmp_path / "out_b"
    assert build_agents.build(out_a) == 0
    assert build_agents.build(out_b) == 0
    for name in ("alpha", "beta"):
        assert (out_a / f"{name}.md").read_bytes() == (out_b / f"{name}.md").read_bytes()


def test_build_output_changes_only_when_role_source_changes(tmp_path, synthetic_agents):
    roles_dir, _ = synthetic_agents
    out_a = tmp_path / "out_a"
    build_agents.build(out_a)
    baseline = (out_a / "alpha.md").read_bytes()

    # regenerating again with unchanged sources reproduces the same bytes
    out_b = tmp_path / "out_b"
    build_agents.build(out_b)
    assert (out_b / "alpha.md").read_bytes() == baseline

    # editing the role SOURCE changes the generated output (expected drift)
    (roles_dir / "alpha.md").write_text("# alpha role\nbody text A -- revised\n")
    out_c = tmp_path / "out_c"
    build_agents.build(out_c)
    assert (out_c / "alpha.md").read_bytes() != baseline


def test_hand_edit_to_generated_artifact_is_detected_by_regeneration(tmp_path, synthetic_agents):
    """Models the regeneration-idempotence guard itself: a committed artifact
    that was hand-edited (drifted from what the generator would produce) is
    caught by regenerating from the same sources and diffing."""
    committed = tmp_path / "committed"
    build_agents.build(committed)
    hand_edited = (committed / "alpha.md").read_text() + "\nSOMEONE HAND-EDITED THIS LINE\n"
    (committed / "alpha.md").write_text(hand_edited)

    fresh = tmp_path / "fresh"
    build_agents.build(fresh)

    assert (committed / "alpha.md").read_bytes() != (fresh / "alpha.md").read_bytes()
