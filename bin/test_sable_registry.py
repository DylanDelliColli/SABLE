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


# --- authoritative pane identity: env WINS over the mutable @sable_role tag ---
# (SABLE-to8m). The @sable_role / @sable_bead pane options are mutable global
# tmux state with no owner; the AUTHORITY is the CLAUDE_AGENT_NAME env of the
# process actually running in the pane (tmux #{pane_pid} -> /proc/PID/environ).
# These drive the functions with an injected tmux runner + a fake /proc root so
# they need no real tmux server.
import sable_pane_lib as spl  # noqa: E402


class _CP:
    """A subprocess.CompletedProcess stand-in for the injected tmux runner."""
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _fake_tmux(pid, role_tag):
    """A run(cmd)->_CP that answers #{pane_pid} with `pid` and the @sable_role
    show-options with `role_tag` (None => unset). Mirrors the two tmux calls the
    identity helpers make."""
    def run(cmd):
        if "#{pane_pid}" in cmd:
            return _CP(stdout=f"{pid}\n" if pid is not None else "", returncode=0)
        if "@sable_role" in cmd:
            return _CP(stdout=f"{role_tag}\n" if role_tag else "",
                       returncode=0 if role_tag else 1)
        return _CP(returncode=1)
    return run


def _proc_with_identity(tmp_path, pid, identity):
    """A fake /proc root whose <pid>/environ carries CLAUDE_AGENT_NAME=identity
    (or no such var when identity is None)."""
    d = tmp_path / str(pid)
    d.mkdir()
    entries = [b"PATH=/usr/bin"]
    if identity is not None:
        entries.insert(0, b"CLAUDE_AGENT_NAME=" + identity.encode())
    (d / "environ").write_bytes(b"\x00".join(entries) + b"\x00")
    return str(tmp_path)


def test_env_identity_wins_over_poisoned_role_tag(tmp_path):
    # The bead's core: @sable_role=lincoln is poisoned onto a pane whose process
    # is really optimus. Authority resolves to the ENV, not the tag.
    proc = _proc_with_identity(tmp_path, 4242, "optimus")
    run = _fake_tmux(4242, role_tag="lincoln")
    assert spl.pane_process_identity(["tmux"], "%1", run=run, proc_root=proc) == "optimus"
    assert spl.resolve_pane_identity(["tmux"], "%1", run=run, proc_root=proc) == "optimus"
    # ...and the disagreement is flagged as a poisoned tag for the claimed role
    assert spl.tag_is_poisoned(["tmux"], "%1", "lincoln", run=run, proc_root=proc) is True


def test_agreeing_tag_is_not_poisoned(tmp_path):
    proc = _proc_with_identity(tmp_path, 7, "optimus")
    run = _fake_tmux(7, role_tag="optimus")
    assert spl.tag_is_poisoned(["tmux"], "%2", "optimus", run=run, proc_root=proc) is False


def test_no_process_identity_falls_back_to_tag(tmp_path):
    # A pane SABLE did not spawn (no CLAUDE_AGENT_NAME in its environ): the tag
    # is the only signal, so resolution falls back to it and nothing is treated
    # as poisoned (fail-open — delivery/reaping unchanged for such panes).
    proc = _proc_with_identity(tmp_path, 99, identity=None)
    run = _fake_tmux(99, role_tag="optimus")
    assert spl.pane_process_identity(["tmux"], "%3", run=run, proc_root=proc) is None
    assert spl.resolve_pane_identity(["tmux"], "%3", run=run, proc_root=proc) == "optimus"
    assert spl.tag_is_poisoned(["tmux"], "%3", "lincoln", run=run, proc_root=proc) is False


def test_pane_pid_fails_open_when_tmux_errors():
    def boom(cmd):
        raise FileNotFoundError("tmux not installed")
    assert spl.pane_pid(["tmux"], "%9", run=boom) is None
    assert spl.pane_process_identity(["tmux"], "%9", run=boom) is None


# --- pane_is_live_nonworker_agent (SABLE-k8o5): spare a live agent we did not
# spawn as a worker, keyed on the SABLE_WORKER_PANE spawn marker rather than the
# CLAUDE_AGENT_NAME (a worker inherits its OWNING MANAGER's lane name) ----------

def _proc_with_env(tmp_path, pid, env):
    """A fake /proc root whose <pid>/environ carries the given env dict."""
    d = tmp_path / str(pid)
    d.mkdir()
    entries = [f"{k}={v}".encode() for k, v in {"PATH": "/usr/bin", **env}.items()]
    (d / "environ").write_bytes(b"\x00".join(entries) + b"\x00")
    return str(tmp_path)


def test_live_nonworker_agent_true_for_resumed_manager(tmp_path):
    # An interactive claude (CLAUDE_AGENT_NAME set) with NO worker marker: a
    # resumed manager/cockpit occupying a stale-done window -> spare it.
    proc = _proc_with_env(tmp_path, 100, {"CLAUDE_AGENT_NAME": "optimus"})
    run = _fake_tmux(100, role_tag=None)
    assert spl.pane_is_live_nonworker_agent(["tmux"], "%1", run=run, proc_root=proc) is True


def test_live_nonworker_agent_false_for_spawned_worker(tmp_path):
    # A genuine done worker: SAME lane identity as its manager, but carries the
    # SABLE_WORKER_PANE=1 spawn marker -> NOT spared (reaped).
    proc = _proc_with_env(tmp_path, 101,
                          {"CLAUDE_AGENT_NAME": "optimus", "SABLE_WORKER_PANE": "1"})
    run = _fake_tmux(101, role_tag=None)
    assert spl.pane_is_live_nonworker_agent(["tmux"], "%1", run=run, proc_root=proc) is False


def test_live_nonworker_agent_false_for_bare_shell(tmp_path):
    # No CLAUDE_AGENT_NAME at all (a pane SABLE did not spawn) -> no authority to
    # spare, fail-open (reaped on the tag as before).
    proc = _proc_with_env(tmp_path, 102, {})
    run = _fake_tmux(102, role_tag=None)
    assert spl.pane_is_live_nonworker_agent(["tmux"], "%1", run=run, proc_root=proc) is False


def test_live_nonworker_agent_false_when_pane_gone(tmp_path):
    # tmux can't resolve the pid (vanished pane) -> fail-open (not spared).
    def boom(cmd):
        raise FileNotFoundError("tmux not installed")
    assert spl.pane_is_live_nonworker_agent(["tmux"], "%9", run=boom) is False


def test_live_nonworker_agent_false_for_empty_worker_marker(tmp_path):
    # Belt-and-braces: an EMPTY SABLE_WORKER_PANE (tmux `-e VAR=` override used in
    # the hermetic integration tests) is falsy -> still counts as a non-worker.
    proc = _proc_with_env(tmp_path, 103,
                          {"CLAUDE_AGENT_NAME": "optimus", "SABLE_WORKER_PANE": ""})
    run = _fake_tmux(103, role_tag=None)
    assert spl.pane_is_live_nonworker_agent(["tmux"], "%1", run=run, proc_root=proc) is True


if __name__ == "__main__":
    import sys
    import pytest as _p
    sys.exit(_p.main([__file__, "-q"]))
