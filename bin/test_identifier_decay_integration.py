#!/usr/bin/env python3
"""Integration rehearsals for the identifier-decay sweep (SABLE-x9vby).

Real end-to-end, nothing mocked: a REAL sandbox beads DB (`bd init
--non-interactive` under a throwaway HOME), real beads created with real
`bd create --sandbox`, the REAL PreToolUse hook script driven with the real
hook-input JSON shape, the REAL sweeper resolved off PATH, and the REAL
`bd close` path exercised afterwards.

What these pin:
  * close of a bead that an OPEN bead instructs about => the operator-visible
    output names the referrer AND shows the matching line;
  * POSITIVE CONTROL in the same run: close of an UNREFERENCED bead emits
    nothing at all — proving the sweep is capable of staying quiet, so the flag
    above is real signal and not a hook that always talks;
  * a provenance-only referrer stays quiet (the banner-blindness direction,
    against a real DB rather than a fixture dict);
  * the sweep NEVER blocks: `bd close` still succeeds through the real path;
  * discipline 7 — a sweep that cannot run reports COULD NOT ASSESS out loud;
  * the promote-time branch seam reports through the same detector.

Fixture discipline: sandbox bd (own HOME, own DB), hermetic (env leaks
stripped), all work on tmp_path scratch (no real-repo mutation), PATH pinned to
THIS checkout's sweeper so a globally-installed one can never be under test.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BIN_DIR = Path(__file__).resolve().parent
REPO = BIN_DIR.parent
SWEEPER = BIN_DIR / "sable-identifier-decay"
HOOK = REPO / "hooks" / "multi-manager" / "close-decay-sweep.sh"

# The ci-verify clean-room is tmux+pytest only — no bd/dolt by design. These
# rehearsals drive a REAL sandbox beads DB, so the module self-skips when bd is
# absent, matching the bd/dolt-suites-self-skip contract in ci-verify.yml.
HAVE_BD = shutil.which("bd") is not None
pytestmark = pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd/dolt by design; real-bd integration self-skips",
)

_ENV_LEAKS = ("CLAUDE_AGENT_NAME", "TMUX_PANE", "SABLE_HOOK_TRACE_LOG",
              "SABLE_IDREF_TIMEOUT")


def _env(home, shim_bin=None):
    env = {k: v for k, v in os.environ.items() if k not in _ENV_LEAKS}
    env["HOME"] = str(home)
    env["BD_NON_INTERACTIVE"] = "1"
    env["CI"] = "true"
    # hook tracing must never touch the developer's real log
    env["SABLE_HOOK_TRACE_LOG"] = str(home / "hook-trace.log")
    if shim_bin is not None:
        env["PATH"] = f"{shim_bin}{os.pathsep}{env.get('PATH', '')}"
    return env


def _run(argv, cwd, home, *, shim_bin=None, stdin=None):
    return subprocess.run(argv, cwd=str(cwd), env=_env(home, shim_bin), text=True,
                          input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=180)


def _bd(work, home, *args, check=True):
    cp = _run(["bd", *args], work, home)
    if check and cp.returncode != 0:
        raise AssertionError(f"bd {args} failed: {cp.stdout}{cp.stderr}")
    return cp


def _robust_bd_init(work, home):
    """`bd init` on the embedded-Dolt backend can leave a PARTIAL database on a
    first-run race (rc 0 but no .beads/config.yaml). A clean init always writes
    config.yaml, so gate success on that artifact and wipe+retry."""
    beads = work / ".beads"
    last = None
    for _ in range(4):
        if beads.exists():
            shutil.rmtree(beads)
        last = _run(["bd", "init", "--non-interactive"], work, home)
        if last.returncode == 0 and (beads / "config.yaml").is_file():
            return
    raise AssertionError(f"bd init never produced a clean DB: {last.stdout if last else '<none>'}")


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    """One real sandbox DB for the module: A (referenced), B (the instructional
    referrer), C (unreferenced positive control), P (provenance-only referrer)."""
    root = tmp_path_factory.mktemp("iddecay")
    work = root / "work"
    work.mkdir()
    home = root / "home"
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    _robust_bd_init(work, home)

    # PATH shim: the sweeper under test is THIS checkout's, never a globally
    # installed one (the SABLE-33hw3 lesson — verification that silently runs
    # someone else's build is not verification).
    shim = root / "shimbin"
    shim.mkdir()
    (shim / "sable-identifier-decay").symlink_to(SWEEPER)

    def create(title, notes=None):
        cp = _bd(work, home, "create", "--sandbox", "--json", "--title", title,
                 "--type=task", "--priority=2")
        bead_id = json.loads(cp.stdout)["id"]
        if notes:
            _bd(work, home, "update", bead_id, "--sandbox", "--notes", notes)
        return bead_id

    a = create("bead A: the identifier that gets retired")
    c = create("bead C: unreferenced control")
    b = create("bead B: the instructional referrer",
               notes=f"HARD REQUIREMENT: must verify {a} before implementing this.")
    p = create("bead P: provenance-only referrer", notes=f"RELATES: {a}")
    return {"work": work, "home": home, "shim": shim,
            "A": a, "B": b, "C": c, "P": p}


def _fire_hook(sandbox, command, *, shim=True):
    """Drive the REAL PreToolUse hook with the REAL hook-input JSON shape.
    Returns (returncode, additionalContext-or-'')."""
    payload = json.dumps({"session_id": "itest", "tool_name": "Bash",
                          "tool_input": {"command": command}})
    cp = _run(["bash", str(HOOK)], sandbox["work"], sandbox["home"],
              shim_bin=sandbox["shim"] if shim else None, stdin=payload)
    ctx = ""
    out = cp.stdout.strip()
    if out:
        ctx = (json.loads(out).get("hookSpecificOutput") or {}).get("additionalContext", "")
    return cp.returncode, ctx


# --------------------------------------------------------------------------
# The contract: a stale instruction surfaces, and silence is achievable
# --------------------------------------------------------------------------

def test_close_surfaces_stale_instruction(sandbox):
    """Closing A must name B and show B's matching line, BEFORE the close — and
    the real close must then still succeed (the sweep never blocks)."""
    rc, ctx = _fire_hook(sandbox, f"bd close {sandbox['A']} --sandbox")
    assert rc == 0, "the hook must never fail the tool call"
    assert sandbox["B"] in ctx, f"the flag must name the referrer: {ctx!r}"
    assert "must verify" in ctx, f"the flag must show the matching line: {ctx!r}"
    assert sandbox["A"] in ctx
    assert "code path" in ctx.lower(), "the shipped flag must carry the v1 known limit"

    # ... and the REAL close path still completes.
    cp = _bd(sandbox["work"], sandbox["home"], "close", sandbox["A"], "--sandbox")
    assert cp.returncode == 0
    shown = _bd(sandbox["work"], sandbox["home"], "show", sandbox["A"], "--json")
    data = json.loads(shown.stdout)
    bead = data[0] if isinstance(data, list) else data
    assert bead["status"] == "closed"


def test_close_of_unreferenced_bead_emits_no_flag(sandbox):
    """POSITIVE CONTROL, same DB, same run: the sweep CAN stay quiet, so the
    flag on A above is signal rather than a hook that always talks."""
    rc, ctx = _fire_hook(sandbox, f"bd close {sandbox['C']} --sandbox")
    assert rc == 0
    assert ctx == "", f"unreferenced close must add zero noise, got: {ctx!r}"
    cp = _bd(sandbox["work"], sandbox["home"], "close", sandbox["C"], "--sandbox")
    assert cp.returncode == 0


def test_provenance_only_referrer_stays_quiet_against_a_real_db(sandbox):
    """The banner-blindness direction, end to end: P names A on a RELATES line
    and must not be flagged when A is retired."""
    _, ctx = _fire_hook(sandbox, f"bd close {sandbox['A']} --sandbox")
    assert sandbox["P"] not in ctx, f"a relate-link must never flag: {ctx!r}"


def test_non_close_commands_are_ignored(sandbox):
    """The hook is scoped to the one command that retires a bead id."""
    for cmd in (f"bd show {sandbox['A']}", "git status", f"echo bd close {sandbox['A']}"):
        rc, ctx = _fire_hook(sandbox, cmd)
        assert (rc, ctx) == (0, ""), cmd


def test_sweep_that_cannot_run_is_loud_not_silent(sandbox):
    """Discipline 7: fail-open on the DECISION, loud on the REPORT. Without the
    sweeper on PATH the hook must say COULD NOT ASSESS — never the silence that
    a clean sweep produces."""
    rc, ctx = _fire_hook(sandbox, f"bd close {sandbox['A']} --sandbox", shim=False)
    assert rc == 0
    # A globally-installed sweeper would make this vacuous; skip rather than lie.
    if shutil.which("sable-identifier-decay"):
        pytest.skip("a sable-identifier-decay is installed globally; absence case not reachable")
    assert "COULD NOT ASSESS" in ctx
    assert "NOT a clean result" in ctx


# --------------------------------------------------------------------------
# CLI against the real DB
# --------------------------------------------------------------------------

def test_cli_json_output_against_real_db(sandbox):
    cp = _run([sys.executable, str(SWEEPER), "--json", sandbox["A"]],
              sandbox["work"], sandbox["home"])
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["assessed"] is True
    referrers = {f["referrer_id"] for f in payload["flags"]}
    assert sandbox["B"] in referrers
    assert sandbox["P"] not in referrers


# --------------------------------------------------------------------------
# Promote-time branch seam, through the merge gate's own helper
# --------------------------------------------------------------------------

def test_promote_seam_reports_branch_name_decay(sandbox, capsys):
    """The gate deletes a merged branch, retiring the branch NAME. A hold keyed
    to that name must surface at that moment — same detector, real bd, real
    sweeper subprocess."""
    sys.path.insert(0, str(BIN_DIR))
    import sable_gate_promote_lib as promote_lib

    branch = "wk-identifier-decay-holdcase"
    hold = _bd(sandbox["work"], sandbox["home"], "create", "--sandbox", "--json",
               "--title", "hold notice", "--type=task", "--priority=2")
    hold_id = json.loads(hold.stdout)["id"]
    _bd(sandbox["work"], sandbox["home"], "update", hold_id, "--sandbox",
        "--notes", f"DO NOT MERGE {branch} while this hold stands.")

    os.environ["SABLE_MG_IDDECAY"] = f"{sys.executable} {SWEEPER}"
    try:
        promote_lib._report_identifier_decay(str(sandbox["work"]), branch)
    finally:
        os.environ.pop("SABLE_MG_IDDECAY", None)
    err = capsys.readouterr().err
    assert hold_id in err, f"branch-name decay must surface at promote: {err!r}"
    assert branch in err


def test_promote_seam_is_quiet_for_an_unreferenced_branch(sandbox, capsys):
    """Positive control for the promote seam: it can stay quiet too."""
    sys.path.insert(0, str(BIN_DIR))
    import sable_gate_promote_lib as promote_lib

    os.environ["SABLE_MG_IDDECAY"] = f"{sys.executable} {SWEEPER}"
    try:
        promote_lib._report_identifier_decay(str(sandbox["work"]), "wk-nobody-mentions-me")
    finally:
        os.environ.pop("SABLE_MG_IDDECAY", None)
    assert capsys.readouterr().err == ""


def test_promote_seam_never_raises_when_the_sweeper_is_missing(sandbox, capsys):
    """Fail-open on the decision: an absent sweeper must not break cleanup."""
    sys.path.insert(0, str(BIN_DIR))
    import sable_gate_promote_lib as promote_lib

    os.environ["SABLE_MG_IDDECAY"] = "/nonexistent/sable-identifier-decay"
    try:
        promote_lib._report_identifier_decay(str(sandbox["work"]), "wk-anything")
    finally:
        os.environ.pop("SABLE_MG_IDDECAY", None)
    assert "COULD NOT ASSESS" in capsys.readouterr().err
