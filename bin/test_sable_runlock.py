#!/usr/bin/env python3
"""Unit tests for bin/sable_runlock_lib.py — the suite-run registry behind
hot-swap clearance (SABLE-pk15w / SABLE-4qlcf).

THREE STATES ARE DEMONSTRATED, NOT TWO. Two states is how the current defect
exists: a probe that answers only clear/not-clear cannot distinguish "nothing is
running" from "something is running that never registered", so an unenumerated
runner reads CLEAR. Every state assertion below therefore names the exact state,
never merely "not clear".

  clear                 quiet host — THE NEGATIVE CONTROL. Load-bearing: a gate
                        that can never release is indistinguishable from correct
                        caution and gets reverted within a day.
  busy                  a registered runner is holding.
  unregistered-runner   a suite is executing that no registration covers.
  stale                 a dead registration — fail-closed, needs explicit reap.
  could-not-assess      asserted NOT-EQUAL to clear, because "reports nothing at
                        all" also "does not report clear".

PLANT-AND-FAIL (SABLE-5lli.7) — this file carries the pk15w half; the 4qlcf
half (remove a real runner's registration, watch the integration case go red)
lives in test_sable_runlock_integration.py. Two separate demonstrations on
purpose: one demonstration covering both halves would be the very
one-boolean-for-N-claims defect these beads exist to remove.

The process table is INJECTED in every unit case. This suite runs under a real
pytest — which registers itself, and would otherwise be seen by the real probe.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "sable_runlock_lib_under_test", BIN / "sable_runlock_lib.py")
rl = importlib.util.module_from_spec(_spec)
# Registered BEFORE exec: @dataclass resolves annotations via
# sys.modules[cls.__module__], which is None for an unregistered module.
sys.modules["sable_runlock_lib_under_test"] = rl
_spec.loader.exec_module(rl)

CLI = BIN / "sable-run-registry"


@pytest.fixture()
def reg(tmp_path, monkeypatch):
    """An isolated registry dir. Also becomes the repo-less base, so nothing
    here can touch the real repo's registry."""
    d = tmp_path / "registry"
    d.mkdir()
    monkeypatch.setenv(rl.ENV_REGISTRY_DIR, str(d))
    return d


def _proc(pid, ppid, args, cwd="/nowhere", uid=None):
    # cwd=None means "unreadable" — the deliberate unattributable case; the
    # default is a real-but-unrelated path, not None.
    return rl.ProcInfo(pid=pid, ppid=ppid, uid=os.getuid() if uid is None else uid,
                       args=args, cwd=cwd)


def _roots(monkeypatch, *roots):
    monkeypatch.setattr(rl, "worktree_roots", lambda base=None: list(roots))


# --- the negative control: a quiet host MUST clear ---------------------------


def test_quiet_host_returns_clear(reg, monkeypatch):
    """PROVE-THE-GATE-CAN-RELEASE. Without this, an implementation that always
    says not-clear passes every safety assertion in this file and is useless."""
    _roots(monkeypatch, "/repo")
    c = rl.clearance(base="/repo", table=[_proc(10, 1, "sleep 30", cwd="/repo")])
    assert c.state == rl.CLEAR
    assert c.is_clear is True
    assert c.exit_code == 0


# --- registered runner -------------------------------------------------------


def test_registered_runner_reads_busy(reg, monkeypatch):
    _roots(monkeypatch, "/repo")
    rl.register("some-runner --run", pid=4242)
    monkeypatch.setattr(rl, "pid_alive", lambda pid: pid == 4242)
    c = rl.clearance(base="/repo", table=[_proc(4242, 1, "some-runner --run", cwd="/repo")])
    assert c.state == rl.BUSY
    assert c.exit_code == 1
    assert any("some-runner --run" in r for r in c.reasons)


def test_registration_covers_the_suites_it_execs_as_children(reg, monkeypatch):
    """The shell half: the runner registers ITS OWN pid, and the suites it execs
    are descendants. Those children must NOT read as unregistered runners."""
    _roots(monkeypatch, "/repo")
    rl.register("shell-run-set.sh --run", pid=500)
    monkeypatch.setattr(rl, "pid_alive", lambda pid: pid == 500)
    table = [
        _proc(500, 1, "bash /repo/.github/ci/shell-run-set.sh --run", cwd="/repo"),
        _proc(501, 500, "bash /repo/hooks/test/test-dep-merge-state.sh", cwd="/repo"),
    ]
    c = rl.clearance(base="/repo", table=table)
    assert c.state == rl.BUSY
    assert c.unregistered == []


def test_registration_covers_the_wrapper_shell_that_launched_it(reg, monkeypatch):
    """The wrapper case, found live on 2026-07-22: a registered `pytest bin/` is
    a CHILD of the `bash -c ...` that launched it, and that wrapper's command
    line contains "pytest" too. Ancestor-only coverage flagged the wrapper as an
    unregistered runner — a false alarm on a correctly registered run."""
    _roots(monkeypatch, "/repo")
    rl.register("pytest bin/", pid=900)
    monkeypatch.setattr(rl, "pid_alive", lambda pid: pid == 900)
    table = [
        _proc(800, 1, "/bin/bash -c eval 'python -m pytest bin/ -q'", cwd="/repo"),
        _proc(900, 800, "python -m pytest bin/ -q", cwd="/repo"),
    ]
    c = rl.clearance(base="/repo", table=table)
    assert c.state == rl.BUSY
    assert c.unregistered == []


# --- THE bead: an UNENUMERATED runner ---------------------------------------


def test_unenumerated_runner_is_not_clear_and_is_its_own_state(reg, monkeypatch):
    """SABLE-pk15w's exact case. The runner's NAME appears in no list anywhere in
    this codebase — it is the "third runner class" nobody has met. It executes
    one of this repo's suites, and it did not register.

    Two assertions, and the second is the one the bead is actually about:
      1. it is NOT clear (a ps probe naming known runners would have said clear);
      2. it is its OWN state — not folded into busy — so "something is running
         that never registered" is distinguishable from "something is running".
    """
    _roots(monkeypatch, "/repo")
    table = [_proc(7777, 1,
                   "bash /repo/hooks/test/test-dep-merge-state.sh",
                   cwd="/repo")]
    c = rl.clearance(base="/repo", table=table)
    assert c.state == rl.UNREGISTERED
    assert c.state != rl.CLEAR
    assert c.state != rl.BUSY
    assert c.exit_code == 3
    assert c.unregistered and c.unregistered[0]["pid"] == 7777


def test_unenumerated_runner_caught_by_suite_shape_not_runner_name(reg, monkeypatch):
    """The probe must key on the SUITE, not on a hand-list of runner names — a
    runner invented tomorrow, with a name in no list, is still seen because the
    thing it executes is one of this repo's suite files."""
    _roots(monkeypatch, "/repo")
    for args in (
        "cadence-snapshot-runner --tier full_snapshot /repo/hooks/test/test-role.sh",
        "python3 /repo/bin/test_sable_spawn_worker.py",
        "some-future-harness pytest /repo/bin",
    ):
        c = rl.clearance(base="/repo", table=[_proc(31337, 1, args, cwd="/repo")])
        assert c.state == rl.UNREGISTERED, args


def test_plant_and_fail_pk15w_half_removing_the_interlock_restores_the_false_clear(
        reg, monkeypatch):
    """PLANT-AND-FAIL for the pk15w half. Neuter the interlock — make the probe
    blind, which is precisely what a runner-name enumeration is for an
    unenumerated runner — and the SAME process table starts reading CLEAR.

    That is the defect, reproduced on demand: no error, no warning, a clearance
    indistinguishable from a correct one, failing in the RELEASING direction."""
    _roots(monkeypatch, "/repo")
    table = [_proc(7777, 1, "bash /repo/hooks/test/test-dep-merge-state.sh", cwd="/repo")]

    assert rl.clearance(base="/repo", table=table).state == rl.UNREGISTERED

    monkeypatch.setattr(rl, "SUITE_PATTERNS", ())      # the enumeration misses it
    planted = rl.clearance(base="/repo", table=table)
    assert planted.state == rl.CLEAR                    # <- the bug, on demand
    assert planted.exit_code == 0


# --- other repos / other users must not block clearance ----------------------


def test_another_repos_suite_run_does_not_block_this_repo(reg, monkeypatch):
    """Scoping half of the negative control. The invariant is "any worktree of
    THIS repo" — a suite running in an unrelated checkout must still clear, or
    the gate never releases on a multi-repo host.

    "/other" is CONFIDENTLY FOREIGN (a real, different repo elsewhere on the
    host) rather than genuinely unidentifiable, so the discriminator is told
    that directly — this unit suite injects the process table, not the
    filesystem, and a hand-built cwd has no real .git for the real resolver to
    find (see test_a_real_foreign_suite_process_is_never_clear in the
    integration suite for that check exercised for real)."""
    _roots(monkeypatch, "/repo")
    monkeypatch.setattr(rl, "_cwd_resolves_to_a_git_repo", lambda cwd: cwd == "/other")
    table = [_proc(4141, 1, "bash /other/hooks/test/test-role.sh", cwd="/other")]
    assert rl.clearance(base="/repo", table=table).state == rl.CLEAR


def test_sibling_worktree_of_this_repo_DOES_block(reg, monkeypatch):
    """...and the other half: every linked worktree shares one registry and one
    answer, because a hot-swap hits all of them at once."""
    _roots(monkeypatch, "/repo", "/repo-wk-feature")
    table = [_proc(4242, 1, "bash hooks/test/test-role.sh", cwd="/repo-wk-feature")]
    assert rl.clearance(base="/repo", table=table).state == rl.UNREGISTERED


def test_other_users_processes_are_out_of_scope(reg, monkeypatch):
    _roots(monkeypatch, "/repo")
    table = [_proc(5151, 1, "bash /repo/hooks/test/test-role.sh", cwd="/repo",
                   uid=os.getuid() + 1)]
    assert rl.clearance(base="/repo", table=table).state == rl.CLEAR


# --- stale: fail-closed, never a silent timeout ------------------------------


def test_stale_entry_reads_stale_not_clear(reg, monkeypatch):
    _roots(monkeypatch, "/repo")
    rl.register("crashed-runner", pid=999999)
    monkeypatch.setattr(rl, "pid_alive", lambda pid: False)
    c = rl.clearance(base="/repo", table=[])
    assert c.state == rl.STALE
    assert c.state != rl.CLEAR
    assert c.exit_code == 4
    assert "reap" in c.render()


def test_stale_entry_is_not_silently_timed_out(reg, monkeypatch):
    """The entry is old enough that any timeout policy would have released it.
    It must still read stale: the releasing direction is the dangerous one, so
    stale is cleared by an operator, not by the clock."""
    _roots(monkeypatch, "/repo")
    token = rl.register("crashed-runner", pid=999999)
    p = reg / (token + ".json")
    data = json.loads(p.read_text())
    data["started_at"] = 0.0            # 1970 — older than any conceivable TTL
    p.write_text(json.dumps(data))
    monkeypatch.setattr(rl, "pid_alive", lambda pid: False)
    assert rl.clearance(base="/repo", table=[]).state == rl.STALE
    assert p.exists()                    # and nothing auto-removed it


def test_reap_clears_a_stale_entry_and_clearance_returns(reg, monkeypatch):
    """Explicit operator action releases — and the gate then CLEARS. The
    can-release control applied to the stale path specifically."""
    _roots(monkeypatch, "/repo")
    token = rl.register("crashed-runner", pid=999999)
    monkeypatch.setattr(rl, "pid_alive", lambda pid: False)
    assert rl.clearance(base="/repo", table=[]).state == rl.STALE
    rl.release(token)
    assert rl.clearance(base="/repo", table=[]).state == rl.CLEAR


# --- could-not-assess: distinct from clear -----------------------------------


def test_unparseable_entry_is_could_not_assess_NOT_clear(reg, monkeypatch):
    """THE LOAD-BEARING INEQUALITY (SABLE-4qlcf): a test that only checks "does
    not report clear" passes on an implementation that reports nothing at all."""
    _roots(monkeypatch, "/repo")
    (reg / "garbage.json").write_text("{not json")
    c = rl.clearance(base="/repo", table=[])
    assert c.state == rl.COULD_NOT_ASSESS
    assert c.state != rl.CLEAR
    assert c.is_clear is False
    assert c.exit_code == 2
    assert any("unparseable" in r for r in c.reasons)


def test_entry_missing_pid_is_could_not_assess(reg, monkeypatch):
    _roots(monkeypatch, "/repo")
    (reg / "half.json").write_text(json.dumps({"runner": "x"}))
    assert rl.clearance(base="/repo", table=[]).state == rl.COULD_NOT_ASSESS


def test_unresolvable_registry_is_could_not_assess(tmp_path, monkeypatch):
    """No override and not in a git work tree: registration is impossible, so
    clearance must not claim clear. The two halves fail closed together."""
    monkeypatch.delenv(rl.ENV_REGISTRY_DIR, raising=False)
    monkeypatch.setattr(rl, "git_common_dir", lambda base=None: None)
    c = rl.clearance(base=str(tmp_path), table=[])
    assert c.state == rl.COULD_NOT_ASSESS
    assert c.state != rl.CLEAR
    with pytest.raises(rl.RunlockError):
        rl.register("x", base=str(tmp_path))


def test_unwritable_registry_is_could_not_assess_not_clear(reg, monkeypatch):
    """An empty registry only means "no runs" if a runner COULD have registered.
    An unwritable dir reads empty forever — a false CLEAR of the releasing kind."""
    _roots(monkeypatch, "/repo")
    monkeypatch.setattr(os, "access", lambda p, mode: False)
    c = rl.clearance(base="/repo", table=[])
    assert c.state == rl.COULD_NOT_ASSESS
    assert c.state != rl.CLEAR


def test_unattributable_same_uid_candidate_is_could_not_assess(reg, monkeypatch):
    """"I saw something suite-shaped and could not tell whose" is a THIRD claim,
    not a quiet clear."""
    _roots(monkeypatch, "/repo")
    monkeypatch.setattr(rl, "pid_alive", lambda pid: True)
    monkeypatch.setattr(rl, "proc_cwd", lambda pid: None)   # /proc/<pid>/cwd unreadable
    table = [_proc(6161, 1, "bash hooks/test/test-role.sh", cwd=None)]
    c = rl.clearance(base="/repo", table=table)
    assert c.state == rl.COULD_NOT_ASSESS
    assert c.state != rl.CLEAR


def test_readable_cwd_outside_roots_is_unattributable_not_dropped(monkeypatch):
    """A candidate that IS suite-shaped, IS same-uid, and has a PERFECTLY
    READABLE cwd that simply is not one of `roots` must never be silently
    discarded (SABLE-skrdj) — it must surface in `unattributable`, not vanish
    from both return lists as it did before this fix.

    Controls, all in the same table: an ATTRIBUTED row (cwd inside roots)
    still lands in `attributed`; a non-suite-shaped row is still ignored
    entirely; a row whose pid has since exited is still dropped (a race, not
    a gap — matches the existing tolerance for the cwd-unreadable case)."""
    # The unit suite injects the process table, not the filesystem — tell the
    # real-repo discriminator there is none here, so the defect case falls
    # through to GENUINELY UNKNOWN rather than CONFIDENTLY FOREIGN.
    monkeypatch.setattr(rl, "_cwd_resolves_to_a_git_repo", lambda cwd: False)
    monkeypatch.setattr(rl, "pid_alive", lambda pid: pid != 400)
    table = [
        _proc(100, 1, "bash /repo/hooks/test/test-role.sh", cwd="/repo"),
        _proc(200, 1, "bash /elsewhere/hooks/test/test-role.sh", cwd="/elsewhere"),
        _proc(300, 1, "vim some_file.py", cwd="/elsewhere"),
        _proc(400, 1, "bash /elsewhere/hooks/test/test-role.sh", cwd="/elsewhere"),
    ]
    attributed, unattributable = rl.scan_processes(["/repo"], table=table,
                                                    self_pid=999999)
    assert {c.pid for c in attributed} == {100}
    assert {c.pid for c in unattributable} == {200}


def test_unattributable_candidate_makes_clearance_not_clear(reg, monkeypatch):
    """Under a clearance gate, could-not-assess must resolve NOT CLEAR, and the
    rendered reason must name pid, ppid, cwd and argv — a partial report is
    not a report (SABLE-skrdj acceptance criteria)."""
    _roots(monkeypatch, "/repo")
    monkeypatch.setattr(rl, "_cwd_resolves_to_a_git_repo", lambda cwd: False)
    monkeypatch.setattr(rl, "pid_alive", lambda pid: True)
    table = [_proc(9191, 42, "bash /elsewhere/hooks/test/test-role.sh",
                   cwd="/elsewhere")]
    c = rl.clearance(base="/repo", table=table)
    assert c.state == rl.COULD_NOT_ASSESS
    assert c.state != rl.CLEAR
    reason = c.render()
    assert "9191" in reason                          # pid
    assert "42" in reason                             # ppid
    assert "/elsewhere" in reason                      # cwd
    assert "hooks/test/test-role.sh" in reason          # argv


def test_probe_failure_is_could_not_assess(reg, monkeypatch):
    _roots(monkeypatch, "/repo")

    def boom(debug=None):
        raise rl.ProbeError("ps unavailable")

    monkeypatch.setattr(rl, "read_process_table", boom)
    c = rl.clearance(base="/repo")
    assert c.state == rl.COULD_NOT_ASSESS
    assert c.state != rl.CLEAR


def test_no_worktree_roots_is_could_not_assess(reg, monkeypatch):
    """The probe with nothing to attribute candidates to has not looked. It must
    not answer as if it had — "clear because I could not see" is the whole
    defect."""
    _roots(monkeypatch)
    assert rl.clearance(base="/repo", table=[]).state == rl.COULD_NOT_ASSESS


# --- five distinct states, one assertion ------------------------------------


def test_all_five_states_are_distinct(reg, monkeypatch):
    """Guards against a future refactor collapsing two outcomes into one — which
    is exactly how the original two-state probe was born."""
    states = {rl.CLEAR, rl.BUSY, rl.STALE, rl.UNREGISTERED, rl.COULD_NOT_ASSESS}
    assert len(states) == 5
    assert len({rl.EXIT_CODES[s] for s in states}) == 5
    assert rl.EXIT_CODES[rl.CLEAR] == 0
    assert all(rl.EXIT_CODES[s] != 0 for s in states - {rl.CLEAR})


# --- register / release / read ------------------------------------------------


def test_register_records_the_runner_and_release_removes_it(reg):
    token = rl.register("shell-run-set.sh --run", pid=1234)
    entries, errors = rl.read_entries()
    assert errors == []
    assert len(entries) == 1
    assert entries[0].pid == 1234
    assert entries[0].runner == "shell-run-set.sh --run"
    assert rl.release(token) is True
    assert rl.read_entries()[0] == []


def test_release_is_idempotent(reg):
    token = rl.register("x", pid=1)
    assert rl.release(token) is True
    assert rl.release(token) is False


def test_registry_is_shared_across_worktrees_of_one_repo(tmp_path, monkeypatch):
    """The "in ANY worktree" half of the invariant, against real git: a linked
    worktree resolves to the SAME registry as the main checkout, so one answer
    covers every worktree a hot-swap would hit."""
    monkeypatch.delenv(rl.ENV_REGISTRY_DIR, raising=False)
    main = tmp_path / "main"
    main.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
           "GIT_CONFIG_SYSTEM": "/dev/null"}
    run = lambda *a, **kw: subprocess.run(a, cwd=str(main), env=env, check=True,
                                          capture_output=True, text=True)
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (main / "f").write_text("x")
    run("git", "add", "f")
    run("git", "commit", "-qm", "init")
    wk = tmp_path / "wk"
    run("git", "worktree", "add", "-q", "-b", "feature", str(wk))
    assert rl.registry_dir(str(main)) == rl.registry_dir(str(wk))


# --- the static coverage audit ----------------------------------------------


def test_audit_is_clean_on_this_repo():
    """The interlock's coverage as a TESTED property rather than a remembered
    one — the whole point of inverting the question. A new suite runner that
    forgets to register fails HERE, at commit time, instead of silently
    producing a false CLEAR during the next hot-swap window."""
    res = rl.audit(str(BIN.parent))
    assert res["ok"], "\n".join(res["violations"])
    assert res["covered"], "audit found no suite-executing paths at all — the "
    "detector has gone blind, which would make this gate vacuous"


def test_audit_names_the_four_runner_classes_it_must_cover():
    """The two the 2026-07-22 clearance question enumerated, PLUS the two it
    never named — which the audit itself found. Their presence here is the
    evidence that the enumeration was incomplete exactly as the bead claimed."""
    res = rl.audit(str(BIN.parent))
    covered = {c["file"]: c["via"] for c in res["covered"]}
    for runner in (".github/ci/shell-run-set.sh",
                   ".github/ci/test-tiers.sh",
                   "bin/sable-clean-room-verify",
                   "bin/sable_snapshot_lib.py"):
        assert runner in covered, f"{runner} dropped out of the audit's view"
        assert covered[runner] == "own registration", runner


def test_audit_flags_a_planted_unregistered_runner(tmp_path):
    """PLANT-AND-FAIL for the audit: a brand-new runner that execs suites and
    does not register must FAIL the gate. Without this the audit could be
    vacuously green."""
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    (repo / ".github" / "ci").mkdir(parents=True)
    (repo / "bin" / "conftest.py").write_text("register('pytest')\n")
    (repo / ".github" / "ci" / "new-runner.sh").write_text(
        '#!/usr/bin/env bash\nbash "$TESTDIR/test-role.sh"\n')
    res = rl.audit(str(repo))
    assert res["ok"] is False
    assert any("new-runner.sh" in v for v in res["violations"])


def test_audit_fails_when_the_pytest_half_stops_registering(tmp_path):
    """The python half rests entirely on bin/conftest.py. If it stops
    registering, EVERY pytest runner silently loses coverage at once — so that
    single point of failure is asserted directly."""
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    (repo / "bin" / "conftest.py").write_text("# no registration here\n")
    res = rl.audit(str(repo))
    assert res["ok"] is False
    assert any("conftest" in v for v in res["violations"])


# --- CLI contract -------------------------------------------------------------


def _cli(*args, env=None):
    e = {**os.environ, **(env or {})}
    return subprocess.run([sys.executable, str(CLI), *args],
                          capture_output=True, text=True, env=e)


def test_cli_exit_codes_are_the_state(tmp_path):
    """A shell caller reads the exit code, so 0 must mean CLEAR and nothing
    else may."""
    d = tmp_path / "reg"
    d.mkdir()
    env = {rl.ENV_REGISTRY_DIR: str(d)}
    quiet = _cli("clearance", "--json", "--no-probe", env=env)
    assert quiet.returncode == 0
    assert json.loads(quiet.stdout)["state"] == rl.CLEAR

    tok = _cli("register", "--runner", "cli-test", "--pid", str(os.getpid()),
               env=env).stdout.strip()
    busy = _cli("clearance", "--json", "--no-probe", env=env)
    assert busy.returncode == 1
    assert json.loads(busy.stdout)["state"] == rl.BUSY

    _cli("release", tok, env=env)
    assert _cli("clearance", "--json", "--no-probe", env=env).returncode == 0


def test_cli_reap_refuses_a_live_entry(tmp_path):
    """Reap is for STALE entries. Refusing a live one keeps `reap` from becoming
    a way to talk the gate into releasing."""
    d = tmp_path / "reg"
    d.mkdir()
    env = {rl.ENV_REGISTRY_DIR: str(d)}
    tok = _cli("register", "--runner", "live", "--pid", str(os.getpid()),
               env=env).stdout.strip()
    r = _cli("reap", tok, env=env)
    assert r.returncode == 1
    assert "ALIVE" in r.stderr


def test_cli_clearance_states_its_enumeration(tmp_path):
    """SABLE-pk15w fix-direction 3, kept as a floor even though direction 1
    landed: a reader must be able to see what was NOT checked, so the
    clearance's scope is auditable instead of implicit."""
    d = tmp_path / "reg"
    d.mkdir()
    out = _cli("clearance", env={rl.ENV_REGISTRY_DIR: str(d)}).stdout
    assert "checked:" in out
    assert "run registry:" in out
    for label, _pat in rl.SUITE_PATTERNS:
        assert label in out
