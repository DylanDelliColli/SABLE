#!/usr/bin/env python3
"""Unit tests for the instance-registry writer in bin/sable-spawn-manager
(market-brief-package-73t4, Lincoln ruling 2026-07-07: mechanism (b) EXPLICIT
INSTANCE REGISTRATION).

The spawn/respawn tooling writes an instance's OWN agents.yaml entry (mirroring
its base manager's type) so lib-identity's UNCHANGED exact-match lookup resolves
it as a manager. Pinned here:
  - privilege never derives from the name pattern: base_manager_type only mirrors
    a REGISTERED manager base; a registered non-manager base or an absent base
    yields nothing;
  - a running session may never register ITSELF (actor == name refused);
  - the writer is confined to the spawn tooling (no hook / other bin tool calls
    it);
  - stale instance entries are pruned on reap/respawn; base entries never touched.
"""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent / "sable-spawn-manager"
_LOADER = SourceFileLoader("sable_spawn_manager", str(_BIN))
_SPEC = importlib.util.spec_from_loader("sable_spawn_manager", _LOADER)
sm = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(sm)

BASE_YAML = """\
agents:
  optimus:
    type: epic_manager
  tarzan:
    type: one_off_manager
  chuck:
    type: integrator
  sherlock:
    type: auditor
"""


@pytest.fixture()
def reg(tmp_path):
    p = tmp_path / "agents.yaml"
    p.write_text(BASE_YAML)
    return p


# --- strip_instance_suffix / base_manager_type -----------------------------

def test_strip_instance_suffix():
    assert sm.strip_instance_suffix("tarzan-2") == "tarzan"
    assert sm.strip_instance_suffix("optimus-30") == "optimus"
    assert sm.strip_instance_suffix("tarzan") == "tarzan"            # not an instance
    assert sm.strip_instance_suffix("tarzan-abc") == "tarzan-abc"    # non-numeric suffix
    assert sm.strip_instance_suffix("general-purpose") == "general-purpose"


def test_base_manager_type_only_for_manager_instances():
    assert sm.base_manager_type(BASE_YAML, "tarzan-2") == "one_off_manager"
    assert sm.base_manager_type(BASE_YAML, "optimus-3") == "epic_manager"
    # base is a registered NON-manager -> None (no elevation via a non-manager base)
    assert sm.base_manager_type(BASE_YAML, "sherlock-2") is None
    # base absent -> None
    assert sm.base_manager_type(BASE_YAML, "megatron-2") is None
    # not an instance -> None
    assert sm.base_manager_type(BASE_YAML, "tarzan") is None
    # non-numeric suffix is not an instance -> None
    assert sm.base_manager_type(BASE_YAML, "tarzan-abc") is None


# --- register_instance (the spawn-time writer) -----------------------------

def test_register_writes_instance_entry_resolvable_by_lookup(reg):
    assert sm.register_instance("tarzan-2", yaml_path=str(reg)) == "registered"
    parsed = sm.parse_registry(reg.read_text())
    assert parsed["tarzan-2"]["type"] == "one_off_manager"
    assert parsed["tarzan-2"]["instance_of"] == "tarzan"
    # base entries left intact
    assert parsed["tarzan"]["type"] == "one_off_manager"


def test_register_is_idempotent(reg):
    assert sm.register_instance("tarzan-2", yaml_path=str(reg)) == "registered"
    assert sm.register_instance("tarzan-2", yaml_path=str(reg)) == "exists"
    assert reg.read_text().count("  tarzan-2:\n") == 1     # exactly one entry


def test_register_refuses_self(reg):
    """A running session cannot register ITSELF (self-elevation, constraint 2)."""
    before = reg.read_text()
    assert sm.register_instance("tarzan-2", actor="tarzan-2", yaml_path=str(reg)) == "refused-self"
    assert reg.read_text() == before                       # no write occurred


def test_register_allows_a_different_actor(reg):
    """The spawner (a DIFFERENT identity, e.g. lincoln) may register the instance."""
    assert sm.register_instance("tarzan-2", actor="lincoln", yaml_path=str(reg)) == "registered"
    assert "tarzan-2" in sm.parse_registry(reg.read_text())


def test_register_noop_for_non_instance(reg):
    before = reg.read_text()
    assert sm.register_instance("tarzan", yaml_path=str(reg)) == "noop-not-instance"
    assert reg.read_text() == before


def test_register_noop_when_base_not_manager(reg):
    before = reg.read_text()
    assert sm.register_instance("sherlock-2", yaml_path=str(reg)) == "noop-base-not-manager"
    assert reg.read_text() == before


def test_register_noop_when_base_absent(reg):
    before = reg.read_text()
    assert sm.register_instance("megatron-2", yaml_path=str(reg)) == "noop-base-not-manager"
    assert reg.read_text() == before


# --- prune_stale_instances (reap / respawn path) ---------------------------

def test_prune_removes_only_dead_instances(reg):
    sm.register_instance("tarzan-2", yaml_path=str(reg))
    sm.register_instance("optimus-3", yaml_path=str(reg))
    # tarzan-2 is live, optimus-3 is not
    pruned = sm.prune_stale_instances({"tarzan-2", "lincoln"}, yaml_path=str(reg))
    assert pruned == ["optimus-3"]
    parsed = sm.parse_registry(reg.read_text())
    assert "tarzan-2" in parsed
    assert "optimus-3" not in parsed
    # base entries survive
    assert parsed["tarzan"]["type"] == "one_off_manager"
    assert parsed["optimus"]["type"] == "epic_manager"


def test_prune_never_touches_base_entries(reg):
    # no instances registered -> nothing pruned even with an empty live set
    assert sm.prune_stale_instances(set(), yaml_path=str(reg)) == []
    assert sm.parse_registry(reg.read_text()).keys() >= {"optimus", "tarzan", "chuck", "sherlock"}


def test_prune_all_when_no_live_panes(reg):
    sm.register_instance("tarzan-2", yaml_path=str(reg))
    sm.register_instance("optimus-3", yaml_path=str(reg))
    pruned = sm.prune_stale_instances(set(), yaml_path=str(reg))
    assert sorted(pruned) == ["optimus-3", "tarzan-2"]
    parsed = sm.parse_registry(reg.read_text())
    assert "tarzan-2" not in parsed and "optimus-3" not in parsed
    assert {"optimus", "tarzan", "chuck", "sherlock"} <= set(parsed)


# --- confinement: the writer is spawn-tooling only, never a hook -----------

def test_registration_writer_confined_to_spawn_tooling():
    """Constraint 2: the writer must not be reachable from a running session's
    tool calls. No multi-manager hook and no other bin/sable-* tool may CALL the
    Python writer, so nothing auto-registers (or auto-elevates) an identity. The
    pre-push deny message merely *quotes* the `--register-instance` CLI as advice
    to a human — that is the dash form and is not a call to `register_instance(`."""
    root = Path(__file__).resolve().parent.parent           # SABLE repo root
    callers = []
    candidates = list((root / "hooks").rglob("*.sh"))
    candidates += [p for p in (root / "bin").glob("sable-*") if p.name != "sable-spawn-manager"]
    for f in candidates:
        if "register_instance(" in f.read_text():
            callers.append(str(f.relative_to(root)))
    assert callers == [], f"registration writer called outside spawn tooling: {callers}"


if __name__ == "__main__":
    import sys
    import pytest as _p
    sys.exit(_p.main([__file__, "-q"]))
