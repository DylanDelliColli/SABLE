#!/usr/bin/env python3
"""Integration tests for bin/sable-screen against REAL git + a REAL sandbox
bd store (SABLE-23upx).

Fixture shape mirrors test_sable_reconcile_handoffs_integration.py: a bare
`origin.git` + a working clone, a `.sable` file naming the integration
branch, and a real sandbox beads DB (`bd init --non-interactive` under a
throwaway HOME) — never the developer's own beads DB.

Self-skips when bd is absent from PATH (SABLE-k35mw: the ci-verify
clean-room is tmux+pytest only, and an unguarded `bd` call ERRORS the gate
rather than skipping it).

Per bead spec, `holds` is exercised with all three dispositions of a closed
bead's branch in ONE pass: landed-and-deleted (must NOT need a hold),
uncontained (must need a hold), and contained/still-present (must NOT need a
hold). `dispatch` is exercised with a real occupant built from BOTH legs —
an in-progress bead's declared writes, and an uncontained wk-* branch's
ACTUAL changed files for a bead that declares nothing at all (SABLE-krbxd:
the undeclared-collateral-edit case a declaration-only screen cannot see).

The three hardening axes are each driven end-to-end through the REAL CLI:
  * SABLE-1muvg — the MULTI-ARG form over real beads with real
    footprint_writes metadata, which is the only form the defect lives in.
  * SABLE-qyr0f — a real `bd dep add` blocker left OPEN, then really closed,
    asserting the SAME invocation flips. Verified against a live sandbox
    before it was written: `bd dep add <child> <blocker>` stores type
    'blocks', `bd show --json` resolves the dep with a status that really
    does go open -> closed, and `bd ready` really does drop and re-admit the
    child — a fixture whose blocked state is indistinguishable from its
    ready state would assert nothing.
  * SABLE-jhako — a real bundled branch on the real remote carrying two real
    closed beads with the hold on only one.

Assertions are by ATTRIBUTABLE IDENTITY — the specific ids/branches the
fixture created — never a global count (SABLE-jd5fj.15).
"""
# sable-test-load: nested-runner -- one self-skip E2E proves the real module is sealed without bd
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-screen"
BASE = "trunk"

HAVE_BD = shutil.which("bd") is not None
pytestmark = pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd/dolt by design; real-bd integration self-skips",
)

_ENV_LEAKS = ("CLAUDE_AGENT_NAME", "TMUX_PANE", "SABLE_TMUX_SOCKET",
              "SABLE_INTEGRATION_BRANCH", "SABLE_BASE_BRANCH")
_FIXTURE_TEMPLATE_ROOT = None


def _env(home):
    env = {k: v for k, v in os.environ.items() if k not in _ENV_LEAKS}
    env["HOME"] = str(home)
    env["BD_NON_INTERACTIVE"] = "1"
    env["CI"] = "true"
    return env


def _run(argv, cwd, home, extra_env=None, check=True):
    env = _env(home)
    if extra_env:
        env.update(extra_env)
    cp = subprocess.run(argv, cwd=str(cwd), env=env, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    if check and cp.returncode != 0:
        raise AssertionError(f"{argv} failed: {cp.stdout}")
    return cp


def _git(cwd, *args, check=True):
    cp = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                        cwd=str(cwd), text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and cp.returncode != 0:
        raise AssertionError(f"git {args} failed: {cp.stdout}")
    return cp.stdout.strip()


def _bd(cwd, home, *args, check=True):
    return _run(["bd", *args], cwd, home, check=check)


def _robust_bd_init(work, home):
    """Mirrors test_sable_reconcile_handoffs_integration.py's helper: `bd
    init` on the embedded-Dolt backend can leave a partial DB on a first-run
    race (rc 0 but no .beads/config.yaml) — gate success on that artifact
    and wipe+retry rather than run against a broken DB."""
    beads = work / ".beads"
    last = None
    for _ in range(4):
        if beads.exists():
            shutil.rmtree(beads)
        last = _run(["bd", "init", "--non-interactive"], work, home, check=False)
        if last.returncode == 0 and (beads / "config.yaml").is_file():
            return last
    raise AssertionError(f"bd init never produced a clean DB: {last.stdout if last else '<none>'}")


def _setup_fresh(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    home = tmp_path / "home"
    home.mkdir()

    _git(tmp_path, "init", "--bare", "-b", BASE, str(origin))
    _git(tmp_path, "clone", str(origin), str(work))

    exclude = work / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text() + ".beads/\n")
    nohooks = tmp_path / "nohooks"
    nohooks.mkdir()
    _git(work, "config", "--local", "core.hooksPath", str(nohooks))

    (work / ".sable").write_text(f"integrationBranch={BASE}\n")
    (work / "README.md").write_text("base\n")
    _git(work, "add", "README.md", ".sable")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "origin", BASE)

    _robust_bd_init(work, home)
    return origin, work, home


@pytest.fixture(scope="session", autouse=True)
def _immutable_fixture_template(tmp_path_factory):
    """Build the cold real-git/real-bd baseline once per pytest session."""
    global _FIXTURE_TEMPLATE_ROOT
    if not HAVE_BD:
        yield
        return

    root = tmp_path_factory.mktemp("sable-screen-template")
    _setup_fresh(root)
    _FIXTURE_TEMPLATE_ROOT = root
    try:
        yield
    finally:
        _FIXTURE_TEMPLATE_ROOT = None


def _rewrite_bd_remote(work, origin):
    """Rebind the copied store's only location-bearing setting."""
    config = work / ".beads" / "config.yaml"
    lines = config.read_text().splitlines()
    rewritten = [
        f'sync.remote: "{origin}"' if line.startswith("sync.remote:") else line
        for line in lines
    ]
    config.write_text("\n".join(rewritten) + "\n")


def _setup(tmp_path):
    """Deep-copy the immutable baseline into one isolated test world."""
    if _FIXTURE_TEMPLATE_ROOT is None:
        return _setup_fresh(tmp_path)

    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    home = tmp_path / "home"
    home.mkdir()
    shutil.copytree(_FIXTURE_TEMPLATE_ROOT / "origin.git", origin)
    shutil.copytree(_FIXTURE_TEMPLATE_ROOT / "work", work)

    nohooks = tmp_path / "nohooks"
    nohooks.mkdir()
    _git(work, "remote", "set-url", "origin", str(origin))
    _git(work, "config", "--local", "core.hooksPath", str(nohooks))
    _rewrite_bd_remote(work, origin)
    return origin, work, home


def test_fixture_template_copies_share_no_mutable_state(tmp_path):
    """Guard the optimization: Git remotes and copied stores stay isolated."""
    left_root = tmp_path / "left"
    right_root = tmp_path / "right"
    left_root.mkdir()
    right_root.mkdir()
    left_origin, left_work, _ = _setup(left_root)
    right_origin, right_work, _ = _setup(right_root)

    assert _git(left_work, "remote", "get-url", "origin") == str(left_origin)
    assert _git(right_work, "remote", "get-url", "origin") == str(right_origin)
    assert str(left_origin) in (left_work / ".beads" / "config.yaml").read_text()
    assert str(right_origin) in (right_work / ".beads" / "config.yaml").read_text()

    (left_work / "README.md").write_text("left-only\n")
    assert (right_work / "README.md").read_text() == "base\n"

    compared = 0
    for left_file in (left_work / ".beads").rglob("*"):
        if not left_file.is_file():
            continue
        right_file = right_work / ".beads" / left_file.relative_to(left_work / ".beads")
        if right_file.is_file():
            compared += 1
            assert not os.path.samefile(left_file, right_file), left_file
    assert compared > 0, "isolation probe compared no copied beads files"


def _push_branch(work, name, *, base=BASE, file_name=None, merge_into=None, delete_after=False):
    """Create + push a branch under an explicit name, with one commit
    touching `file_name` (default: `<name>.txt`). If merge_into is set,
    fast-forward that branch to the new tip and push it too (models an
    already-merged branch). If delete_after, delete the branch on origin
    after any merge — models a landed-and-reaped branch."""
    feat = file_name or f"{name}.txt"
    _git(work, "checkout", "-b", name, f"origin/{base}")
    (work / feat).parent.mkdir(parents=True, exist_ok=True)
    (work / feat).write_text("feature\n")
    _git(work, "add", feat)
    _git(work, "commit", "-m", f"work on {name}")
    _git(work, "push", "origin", name)
    if merge_into:
        _git(work, "checkout", merge_into)
        _git(work, "merge", "--ff-only", name)
        _git(work, "push", "origin", merge_into)
    if delete_after:
        _git(work, "push", "origin", "--delete", name)
    _git(work, "checkout", BASE)
    return feat


def _create_bead(work, home, *, title, status="open", metadata=None):
    cp = _bd(work, home, "create", "--sandbox", "--json",
             "--title", title, "--type=task", "--priority=2")
    bead_id = json.loads(cp.stdout)["id"]
    for k, v in (metadata or {}).items():
        _bd(work, home, "update", bead_id, "--sandbox", "--set-metadata", f"{k}={v}")
    if status == "closed":
        _bd(work, home, "close", bead_id, "--sandbox")
    elif status == "in_progress":
        _bd(work, home, "update", bead_id, "--sandbox", "--status", "in_progress")
    return bead_id


def _dep_add(work, home, blocked, blocker):
    """Real `bd dep add <blocked> <blocker>` — requirement language, not
    temporal: the FIRST id is the one that needs the second. Verified in a
    live sandbox to store dependency_type 'blocks' and to drop `blocked` out
    of `bd ready` while `blocker` is open."""
    return _bd(work, home, "dep", "add", blocked, blocker, "--sandbox")


def _screen(work, home, *args, check=False):
    argv = [sys.executable, str(BIN), *args, "--repo", str(work)]
    return _run(argv, work, home, check=check)


def _rows_by_id(cp):
    payload = json.loads(cp.stdout)
    return {row["bead_id"]: row for row in payload["results"]}


# ===========================================================================
# holds: all three dispositions in one pass
# ===========================================================================

def test_holds_three_dispositions_in_one_pass(tmp_path):
    origin, work, home = _setup(tmp_path)

    # (1) landed-and-deleted: merged into trunk, then the ref removed from
    # origin entirely — must NOT be reported as needing a hold.
    reaped_bead = _create_bead(work, home, title="landed and reaped work",
                               status="closed")
    _push_branch(work, "wk-reaped", merge_into=BASE, delete_after=True)
    _bd(work, home, "update", reaped_bead, "--sandbox",
        "--set-metadata", "branch=wk-reaped")

    # (2) uncontained, no hold fields — must be reported NEEDS HOLD.
    stranded_bead = _create_bead(work, home, title="stranded unlanded work",
                                 status="closed")
    _push_branch(work, "wk-stranded")
    _bd(work, home, "update", stranded_bead, "--sandbox",
        "--set-metadata", "branch=wk-stranded")

    # (3) contained (still merged, ref still present) — must NOT need a hold.
    landed_bead = _create_bead(work, home, title="landed and still present",
                               status="closed")
    _push_branch(work, "wk-landed-present", merge_into=BASE)
    _bd(work, home, "update", landed_bead, "--sandbox",
        "--set-metadata", "branch=wk-landed-present")

    r = _screen(work, home, "holds", "--format", "json")
    payload = json.loads(r.stdout)
    by_id = {row["bead_id"]: row for row in payload["results"]}

    assert by_id[reaped_bead]["verdict"] == "reaped-or-unpushed", by_id[reaped_bead]
    assert by_id[stranded_bead]["verdict"] == "needs-hold", by_id[stranded_bead]
    assert set(by_id[stranded_bead]["fields"]) == {"hold", "hold_by", "hold_since", "hold_until"}
    assert by_id[landed_bead]["verdict"] == "landed", by_id[landed_bead]

    # exit code reflects the one real finding
    assert r.returncode == 1, r.stdout


def test_holds_exit_zero_when_nothing_needs_a_hold(tmp_path):
    origin, work, home = _setup(tmp_path)
    bead = _create_bead(work, home, title="clean landed work", status="closed")
    _push_branch(work, "wk-clean", merge_into=BASE)
    _bd(work, home, "update", bead, "--sandbox", "--set-metadata", "branch=wk-clean")

    r = _screen(work, home, "holds")
    assert r.returncode == 0, r.stdout
    assert "none" in r.stdout


# ===========================================================================
# dispatch: both occupant legs, plus NO-DECLARATION and negative control
# ===========================================================================

def test_dispatch_collides_with_in_progress_declared_write(tmp_path):
    origin, work, home = _setup(tmp_path)
    _create_bead(work, home, title="occupant in progress", status="in_progress",
                 metadata={"wip_claims": "bin/shared.py"})
    candidate = _create_bead(work, home, title="candidate touching shared file",
                             metadata={"footprint_writes": "bin/shared.py"})

    r = _screen(work, home, "dispatch", candidate, "--format", "json")
    payload = json.loads(r.stdout)
    row = payload["results"][0]
    assert row["verdict"] == "collides", row
    assert r.returncode == 1


def test_closed_bead_uncontained_branch_occupies_then_merge_releases(tmp_path):
    """SABLE-m2fyf: close a REAL bead after pushing its REAL branch, then
    prove the branch's attributable path remains occupied until that same
    branch is merged into the integration ref."""
    origin, work, home = _setup(tmp_path)
    collateral_bead = _create_bead(
        work, home, title="closed authoring lifecycle with live branch",
        status="closed")
    _push_branch(work, "wk-collateral", file_name="bin/undeclared.py")
    _bd(work, home, "update", collateral_bead, "--sandbox",
        "--set-metadata", "branch=wk-collateral")

    candidate = _create_bead(work, home, title="candidate that would touch the same file",
                             metadata={"footprint_writes": "bin/undeclared.py"})

    occupied = _screen(work, home, "occupants", "--exclude", candidate,
                       "--format", "json")
    occupied_payload = json.loads(occupied.stdout)
    assert occupied_payload["occupants"]["branch:wk-collateral"] == [
        "bin/undeclared.py"]

    r = _screen(work, home, "dispatch", candidate, "--format", "json")
    payload = json.loads(r.stdout)
    row = payload["results"][0]
    assert row["verdict"] == "collides", row
    assert any(label.startswith("branch:wk-collateral") for label in row["hits"]), row
    assert row["hits"]["branch:wk-collateral"] == ["bin/undeclared.py"], row

    _git(work, "checkout", BASE)
    _git(work, "merge", "--ff-only", "wk-collateral")
    _git(work, "push", "origin", BASE)

    released_occupants = _screen(work, home, "occupants", "--exclude", candidate,
                                 "--format", "json")
    assert "branch:wk-collateral" not in json.loads(
        released_occupants.stdout)["occupants"]

    released = _screen(work, home, "dispatch", candidate, "--format", "json")
    released_row = json.loads(released.stdout)["results"][0]
    assert released_row["verdict"] == "clear", released_row
    assert "branch:wk-collateral" not in released_row["hits"], released_row
    assert released.returncode == 0


def test_dispatch_negative_control_disjoint_candidate_clears(tmp_path):
    origin, work, home = _setup(tmp_path)
    _create_bead(work, home, title="occupant in progress", status="in_progress",
                 metadata={"wip_claims": "bin/other.py"})
    _push_branch(work, "wk-unrelated", file_name="bin/unrelated.py")

    candidate = _create_bead(work, home, title="candidate touching nothing shared",
                             metadata={"footprint_writes": "bin/mine_only.py"})

    r = _screen(work, home, "dispatch", candidate, "--format", "json")
    payload = json.loads(r.stdout)
    row = payload["results"][0]
    assert row["verdict"] == "clear", row
    assert r.returncode == 0


def test_dispatch_candidate_declaring_nothing_is_no_declaration(tmp_path):
    origin, work, home = _setup(tmp_path)
    candidate = _create_bead(work, home, title="candidate with no footprint at all")

    r = _screen(work, home, "dispatch", candidate, "--format", "json")
    payload = json.loads(r.stdout)
    row = payload["results"][0]
    assert row["verdict"] == "no-declaration", row
    # a no-declaration candidate must still be allowed to dispatch
    assert r.returncode == 0


# ===========================================================================
# dispatch, multi-arg: candidate-vs-candidate (SABLE-1muvg)
# ===========================================================================

def test_dispatch_multiarg_reports_sibling_collision_and_spares_the_disjoint_one(tmp_path):
    """THE DEFECT, end-to-end through the real CLI in the form it actually
    bit: three real beads with real footprint_writes metadata, dispatched in
    ONE invocation. Two share a path, one shares nothing.

    The argument ORDER is deliberate — the disjoint bead goes FIRST, so the
    overlapping pair is the 2nd and 3rd candidate. An implementation that
    compares every candidate only against the first would return all-CLEAR
    here (and would still pass a two-candidate fixture)."""
    origin, work, home = _setup(tmp_path)
    disjoint = _create_bead(work, home, title="candidate with its own file",
                            metadata={"footprint_writes": "bin/only_mine.py"})
    lead = _create_bead(work, home, title="lead candidate",
                        metadata={"footprint_writes": "bin/contested.py,bin/lead.py"})
    sibling = _create_bead(work, home, title="sibling candidate",
                           metadata={"footprint_writes": "bin/contested.py,bin/sib.py"})

    r = _screen(work, home, "dispatch", disjoint, lead, sibling, "--format", "json")
    rows = _rows_by_id(r)

    assert rows[lead]["verdict"] == "sibling-collision", rows[lead]
    assert rows[sibling]["verdict"] == "sibling-collision", rows[sibling]
    # both directions name the other bead AND the shared path
    assert rows[lead]["sibling_hits"] == {sibling: ["bin/contested.py"]}, rows[lead]
    assert rows[sibling]["sibling_hits"] == {lead: ["bin/contested.py"]}, rows[sibling]
    # NEGATIVE CONTROL inside the same invocation: the fix must not have
    # simply made co-dispatch impossible.
    assert rows[disjoint]["verdict"] == "clear", rows[disjoint]
    assert rows[disjoint]["sibling_hits"] == {}, rows[disjoint]
    assert r.returncode == 1, r.stdout


def test_dispatch_multiarg_byte_identical_declarations_do_not_read_clear(tmp_path):
    """The maximal-overlap case measured live (`dispatch SABLE-su3j3
    SABLE-xgb29` -> CLEAR/CLEAR on byte-identical declared sets). If the
    pairwise axis cannot catch total identity it is catching nothing."""
    origin, work, home = _setup(tmp_path)
    files = "bin/sable-recover,bin/test_sable_recover.py"
    first = _create_bead(work, home, title="twin one",
                         metadata={"footprint_writes": files})
    second = _create_bead(work, home, title="twin two",
                          metadata={"footprint_writes": files})

    r = _screen(work, home, "dispatch", first, second)
    assert r.returncode == 1, r.stdout
    assert "SIBLING-COLLISION" in r.stdout, r.stdout
    # the rendered line names the co-candidate and the remedy, because the
    # live failure was a reader trusting the verdict line above the operands
    assert second in r.stdout and first in r.stdout, r.stdout
    assert "--bundle" in r.stdout, r.stdout


def test_dispatch_single_arg_is_unaffected_by_the_sibling_leg(tmp_path):
    """Compatibility guarantee: with N=1 no pair can form, so a bead that
    would sibling-collide against a co-candidate still screens CLEAR on its
    own — and the exit status stays 0."""
    origin, work, home = _setup(tmp_path)
    files = "bin/sable-recover,bin/test_sable_recover.py"
    first = _create_bead(work, home, title="twin one",
                         metadata={"footprint_writes": files})
    _create_bead(work, home, title="twin two",
                 metadata={"footprint_writes": files})

    r = _screen(work, home, "dispatch", first, "--format", "json")
    row = _rows_by_id(r)[first]
    assert row["verdict"] == "clear", row
    assert row["sibling_hits"] == {}, row
    assert r.returncode == 0, r.stdout


def test_dispatch_multiarg_undeclared_pair_is_no_declaration_not_clear(tmp_path):
    """SABLE-e2ic3's trichotomy on the pairwise axis: two candidates that
    declare NOTHING have no operands to compare, so they must NOT come back
    CLEAR — 'nothing to check with' stays distinct from 'checked, found no
    overlap'. Both still dispatch."""
    origin, work, home = _setup(tmp_path)
    a = _create_bead(work, home, title="undeclared one")
    b = _create_bead(work, home, title="undeclared two")

    r = _screen(work, home, "dispatch", a, b, "--format", "json")
    rows = _rows_by_id(r)
    assert rows[a]["verdict"] == "no-declaration", rows[a]
    assert rows[b]["verdict"] == "no-declaration", rows[b]
    assert r.returncode == 0, r.stdout


# ===========================================================================
# dispatch: the readiness axis (SABLE-qyr0f)
# ===========================================================================

def test_dispatch_blocked_bead_is_not_presented_as_dispatchable_then_flips_on_close(tmp_path):
    """A real `bd dep add` blocker, left OPEN, on a candidate declaring only
    NEW files — the exact live shape (SABLE-21rug.3): it overlaps nothing, so
    the footprint axis is honestly clear and the OLD tool emitted its most
    confident ✓. Then the blocker is really closed and the SAME invocation
    must flip to dispatchable, which is what proves the fix reads dependency
    STATUS rather than dependency presence."""
    origin, work, home = _setup(tmp_path)
    blocker = _create_bead(work, home, title="earlier bead in the chain")
    child = _create_bead(work, home, title="later bead declaring only new files",
                         metadata={"footprint_writes": "bin/sable-tier-runner"})
    _dep_add(work, home, child, blocker)

    r = _screen(work, home, "dispatch", child, "--format", "json")
    row = _rows_by_id(r)[child]
    assert row["readiness"] == "blocked", row
    assert row["blockers"] == [blocker], row
    # the footprint axis is untouched — both facts are reported, neither
    # replaces the other
    assert row["verdict"] == "clear", row
    assert r.returncode == 1, r.stdout

    text = _screen(work, home, "dispatch", child)
    assert "BLOCKED" in text.stdout, text.stdout
    assert blocker in text.stdout, text.stdout

    _bd(work, home, "close", blocker, "--sandbox")

    after = _screen(work, home, "dispatch", child, "--format", "json")
    row_after = _rows_by_id(after)[child]
    assert row_after["readiness"] == "ready", row_after
    assert row_after["blockers"] == [], row_after
    assert row_after["verdict"] == "clear", row_after
    assert after.returncode == 0, after.stdout


def test_dispatch_ready_bead_with_the_same_footprint_shape_still_clears(tmp_path):
    """NEGATIVE CONTROL for the readiness axis, run against the same
    declaring-only-new-files shape as the blocked case above: the fix must
    not pass by making everything non-dispatchable."""
    origin, work, home = _setup(tmp_path)
    candidate = _create_bead(work, home, title="unblocked bead declaring only new files",
                             metadata={"footprint_writes": "bin/sable-tier-runner"})

    r = _screen(work, home, "dispatch", candidate, "--format", "json")
    row = _rows_by_id(r)[candidate]
    assert row["readiness"] == "ready", row
    assert row["verdict"] == "clear", row
    assert r.returncode == 0, r.stdout


def test_dispatch_relates_to_dependency_does_not_block(tmp_path):
    """Only 'blocks' gates readiness. A real non-blocking `bd dep relate`
    edge to an OPEN bead must leave the candidate dispatchable — otherwise
    every cross-referenced bead in the pool reads blocked."""
    origin, work, home = _setup(tmp_path)
    other = _create_bead(work, home, title="merely related bead")
    candidate = _create_bead(work, home, title="candidate with a relates-to edge",
                             metadata={"footprint_writes": "bin/mine.py"})
    _bd(work, home, "dep", "relate", candidate, other, "--sandbox")

    r = _screen(work, home, "dispatch", candidate, "--format", "json")
    row = _rows_by_id(r)[candidate]
    assert row["blockers"] == [], row
    assert row["readiness"] == "ready", row
    assert r.returncode == 0, r.stdout


# ===========================================================================
# holds: branch-scoped, bundled siblings (SABLE-jhako)
# ===========================================================================

def test_holds_bundled_branch_held_via_one_sibling_reports_the_branch_held(tmp_path):
    """THE DEFECT end-to-end: one real uncontained branch on the real remote
    carrying TWO real closed beads, with a full four-field hold on only ONE
    of them — the wk-recycle-handoff shape. The old sweep printed the branch
    twice, once NEEDS-HOLD and once held-ok. It must now appear exactly once,
    held, naming both beads.

    A second bundled branch with NO hold anywhere is created in the same pass
    as the negative control: branch-scoping must not make every bundled
    branch read as held."""
    origin, work, home = _setup(tmp_path)

    held_hold = {"branch": "wk-bundled-held", "hold": "merge seat, hot-swap regime",
                 "hold_by": "chuck", "hold_since": "2026-07-24",
                 "hold_until": "2026-07-31"}
    holder = _create_bead(work, home, title="bundled sibling carrying the hold",
                          status="closed", metadata=held_hold)
    bare_sibling = _create_bead(work, home, title="bundled sibling with no hold fields",
                                status="closed", metadata={"branch": "wk-bundled-held"})
    _push_branch(work, "wk-bundled-held")

    # negative control: a bundled branch nobody held
    unheld_a = _create_bead(work, home, title="unheld bundled sibling A",
                            status="closed", metadata={"branch": "wk-bundled-unheld"})
    unheld_b = _create_bead(work, home, title="unheld bundled sibling B",
                            status="closed", metadata={"branch": "wk-bundled-unheld"})
    _push_branch(work, "wk-bundled-unheld")

    r = _screen(work, home, "holds", "--format", "json")
    payload = json.loads(r.stdout)
    rows = [row for row in payload["results"]
            if row["branch"] in ("wk-bundled-held", "wk-bundled-unheld")]
    by_branch = {row["branch"]: row for row in rows}

    # exactly one row per branch — no branch appears twice with two verdicts
    assert len(rows) == 2, rows
    held_row = by_branch["wk-bundled-held"]
    assert held_row["verdict"] == "held-ok", held_row
    assert held_row["verdict"] != "needs-hold", held_row
    assert set(held_row["beads"]) == {holder, bare_sibling}, held_row
    assert held_row["held_by"] == [holder], held_row

    unheld_row = by_branch["wk-bundled-unheld"]
    assert unheld_row["verdict"] == "needs-hold", unheld_row
    assert set(unheld_row["beads"]) == {unheld_a, unheld_b}, unheld_row

    text = _screen(work, home, "holds")
    needs_section = text.stdout.split("*** NEEDS HOLD")[1].split("other (")[0]
    assert "wk-bundled-held" not in needs_section, text.stdout
    assert needs_section.count("wk-bundled-unheld") == 1, text.stdout
    # the whole point of the branch row: both bundled beads are still named
    assert holder in text.stdout and bare_sibling in text.stdout, text.stdout


# ===========================================================================
# self-skip verification (SABLE-k35mw)
# ===========================================================================

def test_module_self_skips_when_bd_absent_from_path():
    """Verifies the skip mechanism itself (SABLE-k35mw): with the directory
    holding `bd` removed from PATH, this whole module's tests must be
    SKIPPED, not error. Run as a subprocess (this outer test only runs at
    all when bd IS present, via the module-level pytestmark) against a PATH
    with bd's directory excluded, on this file alone."""
    bd_path = shutil.which("bd")
    assert bd_path is not None, "this test itself only runs when bd is present"
    # NOT .resolve() — `bd` is commonly an nvm/npm symlink whose target lives
    # in a directory that is not itself on PATH; stripping the RESOLVED
    # directory leaves the real PATH entry (the symlink's directory) intact
    # and bd stays resolvable, which is exactly the bug this fixture hit.
    bd_dir = os.path.dirname(bd_path)
    stripped = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p != bd_dir]
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(stripped)
    cp = subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__)), "-q", "-p", "no:cacheprovider"],
        cwd=str(Path(__file__).resolve().parent), env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert shutil.which("bd", path=env["PATH"]) is None, \
        "fixture bug: bd is still resolvable on the stripped PATH"
    assert "skipped" in cp.stdout, cp.stdout
    assert "passed" not in cp.stdout, cp.stdout
