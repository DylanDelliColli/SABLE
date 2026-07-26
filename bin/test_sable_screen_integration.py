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
"""
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


def _setup(tmp_path):
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


def _screen(work, home, *args, check=False):
    argv = [sys.executable, str(BIN), *args, "--repo", str(work)]
    return _run(argv, work, home, check=check)


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


def test_dispatch_catches_undeclared_collateral_via_branch_ground_truth(tmp_path):
    """SABLE-krbxd: an uncontained wk-* branch whose bead declares NOTHING
    still occupies the files it actually changed — a declaration-only
    screen would be blind to this collision entirely."""
    origin, work, home = _setup(tmp_path)
    collateral_bead = _create_bead(work, home, title="bead with no declared footprint")
    _push_branch(work, "wk-collateral", file_name="bin/undeclared.py")
    _bd(work, home, "update", collateral_bead, "--sandbox",
        "--set-metadata", "branch=wk-collateral")

    candidate = _create_bead(work, home, title="candidate that would touch the same file",
                             metadata={"footprint_writes": "bin/undeclared.py"})

    r = _screen(work, home, "dispatch", candidate, "--format", "json")
    payload = json.loads(r.stdout)
    row = payload["results"][0]
    assert row["verdict"] == "collides", row
    assert any(label.startswith("branch:wk-collateral") for label in row["hits"]), row


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
