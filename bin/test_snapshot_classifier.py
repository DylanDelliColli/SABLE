#!/usr/bin/env python3
"""Unit tests for the green-snapshot classifier + freeze/quarantine state
(SABLE-jd5fj.5, columbo's S3 cases).

Three properties, one per thing that can go wrong with a safety mechanism:

  1. THE CLASSIFIER IS TOTAL AND ENUMERATED. classify_snapshot is a pure
     function of (first pass, re-run, quarantine list), so the matrix is
     covered by ENUMERATION rather than by example — the same posture
     bin/test_promote_decision.py takes for the promote decision table. The
     rows that matter most are the ones nobody expects to reach: a re-run
     result that is MISSING, a quarantined suite that is deterministically
     red, a snapshot that ran nothing at all.

  2. QUARANTINE IS AN EXCLUSION FROM THE FREEZE TRIGGER, NOT A SKIP. The
     distinction is asserted directly: a quarantined suite that fails twice
     appears in the verdict (recorded) and does NOT freeze (excluded).

  3. THE FREEZE READ IS FAIL-CLOSED. An unreadable freeze file reads as FROZEN.
     Asserted rather than commented, because a fail-open safety mechanism looks
     identical to a working one right up until it matters.

Everything here is unit-level: no repo, no bd, no suite runs — the state
functions get a tmp_path via the SABLE_MERGE_GATE_STATE seam. Real composition
(real git, real sandboxed bd, real promote refusal) is
hooks/test/test-snapshot-freeze.sh.
"""
import importlib.util
import inspect
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

import sable_snapshot_lib as snap

_BIN = Path(__file__).resolve().parent


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A state dir under tmp_path, via the documented override seam. Nothing in
    this file may touch the real ~/.claude or the real repo's state."""
    monkeypatch.setenv("SABLE_MERGE_GATE_STATE", str(tmp_path / "merge-gate"))
    return str(tmp_path)


# --- 1. the classifier matrix ------------------------------------------------

def _v(first, rerun=None, quarantined=()):
    return snap.classify_snapshot(first, rerun or {}, set(quarantined))


def test_all_green_first_pass_is_green_and_does_not_freeze():
    v = _v({"a.sh": True, "b.sh": True})
    assert v.green is True
    assert v.freeze is False
    assert (v.deterministic, v.flaky, v.quarantined_red) == ((), (), ())
    assert v.clears_freeze is True


def test_red_then_red_is_deterministic_and_freezes():
    """The core case: a failure that reproduces is a content defect, and the
    fleet stops promoting on top of it."""
    v = _v({"a.sh": True, "b.sh": False}, {"b.sh": False})
    assert v.deterministic == ("b.sh",)
    assert v.freeze is True
    assert v.flaky == ()
    assert v.green is False
    assert v.clears_freeze is False


def test_red_then_green_is_flaky_and_does_NOT_freeze():
    """The case the whole classification exists for. Freezing the fleet on noise
    is the failure mode being prevented, so this MUST NOT freeze."""
    v = _v({"a.sh": True, "b.sh": False}, {"b.sh": True})
    assert v.flaky == ("b.sh",)
    assert v.freeze is False
    assert v.deterministic == ()
    assert v.green is False, "a flake is not a clean pass, even though it does not freeze"


def test_a_missing_rerun_result_is_treated_as_still_red():
    """FAIL-CLOSED. A re-run that never produced a result (crashed, timed out,
    was never scheduled) told us nothing — and 'told us nothing' must not be
    read as 'it passed', which is the only other option available."""
    v = _v({"b.sh": False}, {})
    assert v.deterministic == ("b.sh",)
    assert v.freeze is True


def test_mixed_flake_and_deterministic_freezes_on_the_deterministic_one():
    v = _v({"a.sh": False, "b.sh": False, "c.sh": True},
           {"a.sh": True, "b.sh": False})
    assert v.flaky == ("a.sh",)
    assert v.deterministic == ("b.sh",)
    assert v.freeze is True, "one flake does not excuse a real break in the same run"


def test_a_snapshot_that_ran_nothing_is_not_evidence_of_health():
    """A zero-suite run must not be able to lift a freeze. Same instinct as the
    merge gate treating 'the impact tier selected no suites' as ERROR rather
    than green."""
    v = _v({})
    assert v.green is False
    assert v.freeze is False
    assert v.clears_freeze is False, "an empty snapshot must never clear a freeze"


def test_verdict_lists_are_sorted_and_deterministic():
    """The bisect key is computed from these, so a set-iteration-order-dependent
    ordering would make the idempotency key unstable across runs."""
    v = _v({"z.sh": False, "a.sh": False, "m.sh": False},
           {"z.sh": False, "a.sh": False, "m.sh": False})
    assert v.deterministic == ("a.sh", "m.sh", "z.sh")


@pytest.mark.parametrize("first_ok,rerun_ok,quarantined,expect", [
    (True,  None,  False, "green"),
    (True,  None,  True,  "green"),
    (False, True,  False, "flaky"),
    (False, True,  True,  "flaky"),
    (False, False, False, "deterministic"),
    (False, False, True,  "quarantined_red"),
    (False, None,  False, "deterministic"),
    (False, None,  True,  "quarantined_red"),
])
def test_the_whole_matrix(first_ok, rerun_ok, quarantined, expect):
    """Every combination of (first pass, re-run, quarantined) enumerated, so
    'the classifier is total' is checked rather than asserted in prose."""
    rerun = {} if rerun_ok is None else {"s.sh": rerun_ok}
    v = _v({"s.sh": first_ok}, rerun, ("s.sh",) if quarantined else ())
    buckets = {"green": v.green, "flaky": "s.sh" in v.flaky,
               "deterministic": "s.sh" in v.deterministic,
               "quarantined_red": "s.sh" in v.quarantined_red}
    assert buckets[expect] is True, f"{expect} expected, got {buckets}"
    assert sum(bool(x) for x in buckets.values()) == 1, f"non-exclusive buckets: {buckets}"
    assert v.freeze is (expect == "deterministic")


# --- 2. quarantine excludes from the trigger, it does not skip ---------------

def test_quarantined_suite_is_excluded_from_the_freeze_trigger():
    v = _v({"flappy.sh": False}, {"flappy.sh": False}, quarantined=("flappy.sh",))
    assert v.freeze is False, "a quarantined suite must never freeze the fleet"
    assert v.deterministic == ()


def test_quarantined_suite_is_still_RECORDED_when_it_fails():
    """The non-skip half. If quarantine merely dropped the suite, this list
    would be empty and a permanently-broken quarantined suite would be
    invisible — which is the coverage hole quarantine is supposed to avoid."""
    v = _v({"flappy.sh": False}, {"flappy.sh": False}, quarantined=("flappy.sh",))
    assert v.quarantined_red == ("flappy.sh",)
    assert "flappy.sh" in v.reason


def test_quarantine_does_not_shield_a_DIFFERENT_suite():
    v = _v({"flappy.sh": False, "real.sh": False},
           {"flappy.sh": False, "real.sh": False}, quarantined=("flappy.sh",))
    assert v.deterministic == ("real.sh",)
    assert v.freeze is True


def test_only_quarantined_reds_still_clears_an_existing_freeze():
    """A freeze can only be lifted by evidence about the suites that CAN
    freeze. If quarantined reds blocked the unfreeze, a permanently-flaky suite
    would make every freeze permanent — a trap worth asserting against."""
    v = _v({"flappy.sh": False, "ok.sh": True}, {"flappy.sh": False},
           quarantined=("flappy.sh",))
    assert v.freeze is False
    assert v.clears_freeze is True


# --- idempotency keys --------------------------------------------------------

def test_bisect_key_is_stable_under_ordering():
    assert snap.bisect_key(["b.sh", "a.sh"]) == snap.bisect_key(["a.sh", "b.sh"])


def test_bisect_key_changes_when_the_broken_set_changes():
    assert snap.bisect_key(["a.sh"]) != snap.bisect_key(["a.sh", "b.sh"])


def test_flake_key_is_per_suite():
    assert snap.flake_key("a.sh") != snap.flake_key("b.sh")
    assert snap.flake_key("a.sh") == snap.flake_key("a.sh")


# --- 3. freeze-flag read/write ----------------------------------------------

def test_no_freeze_file_means_not_frozen(repo):
    assert snap.read_freeze(repo) is None


def test_write_then_read_freeze_round_trips(repo):
    snap.write_freeze(repo, suites=["b.sh", "a.sh"], reason="because", bead="SABLE-x")
    got = snap.read_freeze(repo)
    assert got is not None
    assert got["suites"] == ["a.sh", "b.sh"]
    assert got["reason"] == "because" and got["bead"] == "SABLE-x"
    assert got["since"]


def test_clear_freeze_removes_it_and_reports_whether_it_did(repo):
    snap.write_freeze(repo, suites=["a.sh"], reason="r")
    assert snap.clear_freeze(repo) is True
    assert snap.read_freeze(repo) is None
    assert snap.clear_freeze(repo) is False, "clearing an unfrozen fleet is not a lift"


def test_an_unreadable_freeze_file_reads_as_FROZEN(repo):
    """FAIL-CLOSED, the load-bearing one. 'We cannot prove the fleet is
    unfrozen' and 'the fleet is unfrozen' are different claims and only one of
    them is safe to promote on."""
    d = snap.ensure_state_dir(repo)
    (d / snap.FREEZE_FILE).write_text("{not json at all")
    got = snap.read_freeze(repo)
    assert got is not None and got["frozen"] is True
    assert "fail-closed" in got["reason"]


def test_a_freeze_file_saying_frozen_false_is_not_a_freeze(repo):
    d = snap.ensure_state_dir(repo)
    (d / snap.FREEZE_FILE).write_text(json.dumps({"frozen": False}))
    assert snap.read_freeze(repo) is None


def test_freeze_state_is_json_a_human_can_read(repo):
    snap.write_freeze(repo, suites=["a.sh"], reason="r", bead="SABLE-y")
    text = (snap.state_dir(repo) / snap.FREEZE_FILE).read_text()
    assert json.loads(text)["bead"] == "SABLE-y"
    assert "\n" in text, "state files are indented JSON — an operator reads these by hand"


# --- quarantine state --------------------------------------------------------

def test_quarantine_add_is_idempotent(repo):
    assert snap.add_quarantine(repo, "a.sh", reason="flaky") is True
    assert snap.add_quarantine(repo, "a.sh", reason="flaky again") is False
    assert list(snap.read_quarantine(repo)) == ["a.sh"]


def test_quarantine_remove_reports_whether_it_did(repo):
    snap.add_quarantine(repo, "a.sh")
    assert snap.remove_quarantine(repo, "a.sh") is True
    assert snap.remove_quarantine(repo, "a.sh") is False
    assert snap.read_quarantine(repo) == {}


def test_an_unreadable_quarantine_list_reads_as_EMPTY(repo):
    """The conservative direction for THIS file is the opposite of the freeze
    file's: an empty quarantine excludes nothing from the trigger, so a corrupt
    list can only cause an over-eager freeze, never a missed one."""
    d = snap.ensure_state_dir(repo)
    (d / snap.QUARANTINE_FILE).write_text("}}}")
    assert snap.read_quarantine(repo) == {}


def test_quarantine_records_survive_a_second_suite(repo):
    snap.add_quarantine(repo, "b.sh", bead="SABLE-2")
    snap.add_quarantine(repo, "a.sh", bead="SABLE-1")
    q = snap.read_quarantine(repo)
    assert list(q) == ["a.sh", "b.sh"], "written sorted so diffs of the state file are readable"
    assert q["a.sh"]["bead"] == "SABLE-1"


# --- state dir resolution ----------------------------------------------------

def test_state_dir_honours_the_override_seam(tmp_path, monkeypatch):
    monkeypatch.setenv("SABLE_MERGE_GATE_STATE", str(tmp_path / "elsewhere"))
    assert snap.state_dir(".") == tmp_path / "elsewhere"


def test_state_dir_resolves_to_the_repo_root_via_git_common_dir(monkeypatch):
    """Same resolution as mode-state.json (bin/sable-mode resolve_state_path),
    one level deeper. Note this resolves to the MAIN checkout even when called
    from a linked worktree — that is the property, not an accident: all of a
    repo's worktrees must share ONE freeze, or a worker's tree could promote
    past a freeze the merge seat set."""
    import subprocess
    monkeypatch.delenv("SABLE_MERGE_GATE_STATE", raising=False)
    common = subprocess.run(["git", "-C", str(_BIN.parent), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True).stdout.strip()
    root = (Path(_BIN.parent) / common).resolve().parent
    assert snap.state_dir(str(_BIN.parent)) == root / ".claude" / "sable" / "state" / "merge-gate"


def test_every_worktree_of_a_repo_shares_one_freeze(tmp_path, monkeypatch):
    """The reason git-common-dir is the resolver rather than --show-toplevel."""
    import subprocess
    monkeypatch.delenv("SABLE_MERGE_GATE_STATE", raising=False)
    main = tmp_path / "main"
    main.mkdir()
    run = lambda *a, cwd=main: subprocess.run(a, cwd=str(cwd), capture_output=True, text=True)
    run("git", "init", "-q", "-b", "trunk")
    run("git", "config", "user.email", "t@sable.invalid")
    run("git", "config", "user.name", "T")
    (main / "f").write_text("x")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "init")
    linked = tmp_path / "wk"
    run("git", "worktree", "add", "-q", "-b", "wk", str(linked))
    assert snap.state_dir(str(linked)) == snap.state_dir(str(main))


def test_the_shipped_state_dir_exists_and_is_documented():
    """The verify command on the bead: `test -d .claude/sable/state/merge-gate`.
    The location is part of the contract, so it exists in a fresh checkout
    rather than springing into being after the first run."""
    d = _BIN.parent / ".claude" / "sable" / "state" / "merge-gate"
    assert d.is_dir()
    assert (d / "README.md").is_file()


# --- module boundaries -------------------------------------------------------

_PROMOTE = (_BIN / "sable_gate_promote_lib.py").read_text()


def test_snapshot_lib_does_not_import_the_merge_gate():
    """The gate imports the snapshot lib (for the freeze check); the reverse
    would be a cycle. Asserted against source so the DAG cannot rot."""
    src = inspect.getsource(snap)
    for gate_mod in ("sable_gate_promote_lib", "sable_gate_preview_lib",
                     "sable_gate_classify_lib", "sable_gate_git_lib"):
        assert f"import {gate_mod}" not in src, f"snapshot lib imports {gate_mod} — cycle"


def test_promote_checks_the_freeze_MECHANICALLY_as_its_first_act():
    """Not a convention, not a manager's checklist: a code path in the only
    function that writes to the integration branch, and the FIRST thing it
    does. Checked against source because 'we always check the freeze' is
    exactly the kind of claim that survives the code that implemented it."""
    body = _PROMOTE.split("def promote(", 1)[1].split("-> int:", 1)[1]
    first = next(ln.strip() for ln in body.splitlines()
                 if ln.strip() and not ln.strip().startswith(("#", '"""')))
    assert first == "assert_not_frozen(repo)", f"promote's first act is {first!r}"


def test_there_is_no_env_kill_switch_for_the_freeze():
    """SABLE_MG_OPTIMISTIC exists because its off-state is the SAFE one. A
    freeze bypass env var would have the opposite polarity and would leave no
    name attached to a bypass — the two ways out are a green snapshot or a
    recorded `sable-snapshot unfreeze`."""
    fn = inspect.getsource(_freeze_fn())
    code = fn.split('"""')[2]      # body only — the docstring EXPLAINS the absence
    assert "environ" not in code, "the freeze check reads an env var — that is a silent bypass"
    assert "getenv" not in code


def _freeze_fn():
    loader = SourceFileLoader("sable_merge_gate_j5", str(_BIN / "sable-merge-gate"))
    spec = importlib.util.spec_from_loader("sable_merge_gate_j5", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod.promote_lib.assert_not_frozen


def test_the_gate_exposes_exit_25_in_its_taxonomy():
    import sable_gate_classify_lib as classify
    assert classify.EXIT_FROZEN == 25
    assert 25 not in classify.OUTCOME_EXIT.values(), \
        "25 must not be reachable from a CI conclusion — a freeze is not a verdict about this merge"


def test_assert_not_frozen_raises_gate_error_25_with_the_reason(repo):
    import sable_gate_classify_lib as classify
    import sable_gate_promote_lib as promote_lib
    snap.write_freeze(repo, suites=["b.sh"], reason="deterministic red", bead="SABLE-z")
    with pytest.raises(classify.GateError) as exc:
        promote_lib.assert_not_frozen(repo)
    assert exc.value.code == 25
    assert "b.sh" in str(exc.value) and "SABLE-z" in str(exc.value)


def test_assert_not_frozen_is_a_no_op_when_not_frozen(repo):
    import sable_gate_promote_lib as promote_lib
    promote_lib.assert_not_frozen(repo)  # must not raise


# --- tier membership comes from the SSOT, never a local list -----------------

def test_tier_suites_reads_the_cmar4_ssot():
    suites = snap.tier_suites(str(_BIN.parent), "full_snapshot")
    assert "test-tdd-gate.sh" in suites
    assert len(suites) > 20


def test_tier_suites_refuses_an_unknown_tier():
    with pytest.raises(RuntimeError):
        snap.tier_suites(str(_BIN.parent), "not_a_tier")


def test_the_snapshot_runner_carries_no_suite_list_of_its_own():
    """The cmar4.1 'no duplicated test lists anywhere' rule, enforced rather
    than trusted: the only place suite names may come from is test-tiers.sh."""
    src = inspect.getsource(snap) + (_BIN / "sable-snapshot").read_text()
    assert "test-tdd-gate.sh" not in src
    assert src.count("hooks/test") <= 6, "suite paths are proliferating in the runner"
