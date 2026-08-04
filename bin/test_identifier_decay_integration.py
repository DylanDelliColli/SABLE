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
import re
import shlex
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


# --------------------------------------------------------------------------
# SABLE-wr6zp: timing under a realistically-sized, real bd store
#
# The bug this bead fixes was never about correctness — it was that the
# check's own bd query timed out under burst load precisely when the burst
# (many identifiers retiring at once, on a slow store) made the check most
# valuable. Both tests below run the REAL CLI against a REAL bd store, no
# mocks, per the bead's own file footprint.
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def bulk_sandbox(tmp_path_factory):
    """A SEPARATE sandbox DB seeded to the size seen the night this bead was
    filed (50+ open beads) via `bd import`, real end-to-end. A fresh module
    fixture rather than reusing `sandbox` above: bulk-seeding 50+ beads into
    the same store the four-bead fixture's identity assertions depend on
    would make those tests' referrer counts a moving target."""
    root = tmp_path_factory.mktemp("iddecay-bulk")
    work = root / "work"
    work.mkdir()
    home = root / "home"
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    _robust_bd_init(work, home)

    # The real merge_preview SSOT, copied in so bd_timeout() against this
    # sandbox resolves the SAME derived budget (900s) a real promote would
    # get — not the repo-less 20s fallback, which would leave the "budget is
    # now derived, not hardcoded" half of the fix unexercised here.
    ci_dir = work / ".github" / "ci"
    ci_dir.mkdir(parents=True)
    shutil.copy(REPO / ".github" / "ci" / "test-tiers.sh", ci_dir / "test-tiers.sh")

    seed = root / "seed.jsonl"
    lines = [json.dumps({"title": f"bulk seed bead {i}", "issue_type": "task",
                         "priority": 2, "status": "open"}) for i in range(55)]
    seed.write_text("\n".join(lines) + "\n")
    imported = _run(["bd", "import", "--sandbox", "-i", str(seed)], work, home)
    assert imported.returncode == 0, imported.stdout + imported.stderr

    return {"work": work, "home": home}


def test_sweep_completes_against_a_realistically_sized_store(bulk_sandbox):
    """The real CLI, against a real 55-open-bead store carrying the real
    SSOT, must return a genuine (assessed=True) verdict — not a
    could-not-assess — well within its derived budget."""
    cp = _run([sys.executable, str(SWEEPER), "--json", "SABLE-nonexistent-id"],
              bulk_sandbox["work"], bulk_sandbox["home"])
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["assessed"] is True, (
        f"a realistically-sized store must produce a real verdict, not could-not-assess: {payload}")
    assert payload["flags"] == []


def test_slow_store_still_never_reports_a_false_clean(bulk_sandbox):
    """LOAD-BEARING NEGATIVE CONTROL. Constrain the budget far below what the
    real store needs to answer (SABLE_IDREF_TIMEOUT=0.001s — the same override
    seam a real burst-load operator would use, just pushed to an extreme that
    is guaranteed to be exceeded, real bd process and all) and assert the
    output is could-not-assess, not a false clean: exit code EXIT_UNASSESSED
    (3), never 0-with-no-flags, and the stdout never silently reads as
    success. Without this, a fix that merely raises the timeout number could
    silently reintroduce the false-clean failure at the next load level."""
    env_over = {"SABLE_IDREF_TIMEOUT": "0.001"}
    env = _env(bulk_sandbox["home"])
    env.update(env_over)
    cp = subprocess.run([sys.executable, str(SWEEPER), "--json", "SABLE-nonexistent-id"],
                        cwd=str(bulk_sandbox["work"]), env=env, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

    assert cp.returncode == 3, (
        f"an unanswerable-in-budget store must exit EXIT_UNASSESSED (3), not read as success: "
        f"rc={cp.returncode} stdout={cp.stdout!r}")
    payload = json.loads(cp.stdout)
    assert payload["assessed"] is False, (
        "the process exit and payload must never read as success-with-no-findings")
    assert payload["flags"] == []
    assert "timed out" in payload["reason"]
    assert "retried once" in payload["reason"], "the retry-once path must still have run and failed"

    # Non-JSON mode carries the same loud, unmistakable text on stdout.
    cp_text = subprocess.run([sys.executable, str(SWEEPER), "SABLE-nonexistent-id"],
                             cwd=str(bulk_sandbox["work"]), env=env, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert cp_text.returncode == 3
    assert "COULD NOT ASSESS" in cp_text.stdout
    assert "NOT a clean result" in cp_text.stdout


# --------------------------------------------------------------------------
# SABLE-l662t: the three false-positive classes, against a REAL bd store
#
# The measured live failure was a CORPUS effect — closing the completed
# 9-child epic SABLE-be4lo reported 61 hits of which approximately zero were
# real decay. A unit test over synthetic dicts cannot show that the corpus
# behaviour changed, because the thing that broke was the interaction between
# real hierarchical bead ids (`X.1` is a DIFFERENT bead from `X`), real
# paragraph-shaped description prose, and the report's own truncation. So
# these run against real `bd create --parent` children in a real store.
#
# Every assertion below is about beads THIS fixture created (SABLE-jd5fj.15 —
# a global count against a live store the fleet is filing into is a flake
# generator), and every one is paired with a control proving the detector can
# still produce the other outcome.
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def scope_sandbox(tmp_path_factory):
    """A real store shaped like the live failure: a real epic with real
    `--parent` children, referrers naming ONLY the children, one referrer
    citing the epic as history, one carrying a genuine instruction about it,
    and a separate hub identifier with more referrers than the render cap."""
    root = tmp_path_factory.mktemp("iddecay-scope")
    work = root / "work"
    work.mkdir()
    home = root / "home"
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    _robust_bd_init(work, home)

    def create(title, *, parent=None, issue_type="task", notes=None):
        argv = ["create", "--sandbox", "--json", "--title", title,
                f"--type={issue_type}", "--priority=2"]
        if parent:
            argv += [f"--parent={parent}"]
        bead_id = json.loads(_bd(work, home, *argv).stdout)["id"]
        if notes:
            _bd(work, home, "update", bead_id, "--sandbox", "--notes", notes)
        return bead_id

    epic = create("the completed epic", issue_type="epic")
    kids = [create(f"child {i} of the epic", parent=epic) for i in (1, 2, 3)]
    # Real hierarchical ids are the whole point of layer 1 — if bd ever stops
    # minting them this way the fixture is no longer reproducing the bug, and
    # that must fail loudly rather than pass vacuously.
    assert all(k.startswith(f"{epic}.") for k in kids), kids

    child_referrers = [
        create("names only child 1", notes=f"you must verify {kids[0]} before landing"),
        create("names only child 2", notes=f"DO NOT CLOSE until {kids[1]} is merged"),
        create("names only child 3", notes=f"this hold needs {kids[2]} to land first"),
    ]
    # A description-shaped paragraph: the instructional vocabulary is present on
    # the line but several sentences away from the mention — the exact shape
    # that made four of the seven live false positives fire.
    citation = create("cites the epic as history", notes=(
        f"The checker misfired here. This was first observed on {epic} during the "
        f"2026-07-24 drain. We must check the tier budget before the next run."))
    instruction = create("carries a live instruction about the epic", notes=(
        f"hold_until: {epic} must land on the integration branch"))

    # A separate hub identifier with more instructional referrers than the
    # render cap, so the truncation line — and the remedy it prints — is
    # exercised end to end against a real store.
    hub = create("the hub identifier")
    seed = root / "seed.jsonl"
    seed.write_text("\n".join(
        json.dumps({"title": f"hub referrer {i:02d} must verify {hub} before landing",
                    "issue_type": "task", "priority": 2, "status": "open"})
        for i in range(14)) + "\n")
    imported = _run(["bd", "import", "--sandbox", "-i", str(seed)], work, home)
    assert imported.returncode == 0, imported.stdout + imported.stderr

    return {"work": work, "home": home, "epic": epic, "kids": kids,
            "child_referrers": child_referrers, "citation": citation,
            "instruction": instruction, "hub": hub, "hub_referrers": 14}


def _sweep(scope_sandbox, *args):
    cp = _run([sys.executable, str(SWEEPER), *args],
              scope_sandbox["work"], scope_sandbox["home"])
    assert cp.returncode == 0, f"rc={cp.returncode} {cp.stderr}"
    return cp


def _sweep_json(scope_sandbox, *args):
    return json.loads(_sweep(scope_sandbox, "--json", *args).stdout)


# ---- LAYER 1 against a real hierarchy ------------------------------------

def test_retiring_a_parent_does_not_attribute_its_childrens_references(scope_sandbox):
    """Retiring the epic retires nothing of its children's — they were closed on
    their own schedule. None of the three child-only referrers may appear."""
    referrers = {f["referrer_id"] for f in _sweep_json(scope_sandbox, scope_sandbox["epic"])["flags"]}
    leaked = set(scope_sandbox["child_referrers"]) & referrers
    assert not leaked, f"child references must not be attributed to the parent: {leaked}"


def test_retiring_a_child_still_flags_that_childs_referrer(scope_sandbox):
    """POSITIVE CONTROL for layer 1, same store: those referrers ARE findable —
    they simply belong to a different retirement event. Without this the test
    above would pass just as happily on a detector that found nothing at all."""
    child, referrer = scope_sandbox["kids"][0], scope_sandbox["child_referrers"][0]
    referrers = {f["referrer_id"] for f in _sweep_json(scope_sandbox, child)["flags"]}
    assert referrer in referrers, (
        f"retiring {child} must still flag its own referrer: {referrers}")


# ---- LAYER 2 against real paragraph prose --------------------------------

def test_a_real_citation_is_classed_apart_from_a_real_instruction(scope_sandbox):
    """Both beads name the epic and both carry instructional vocabulary on the
    line. Only the one whose mention sits in an instructional SENTENCE is decay;
    the citation is still reported, in its own non-actionable class."""
    kinds = {f["referrer_id"]: f["kind"]
             for f in _sweep_json(scope_sandbox, scope_sandbox["epic"])["flags"]}
    assert kinds.get(scope_sandbox["instruction"]) == "instruction"
    assert kinds.get(scope_sandbox["citation"]) == "citation", (
        f"the historical citation must not be counted as decay: {kinds}")


def test_the_epic_close_reports_one_instruction_not_the_whole_prefix_family(scope_sandbox):
    """The bead's headline number, reproduced in miniature against real beads:
    five open beads name this identifier or its children, and exactly ONE is
    something a closer can act on. That one is what the report counts."""
    flags = _sweep_json(scope_sandbox, scope_sandbox["epic"])["flags"]
    instructions = [f["referrer_id"] for f in flags if f["kind"] == "instruction"]
    assert instructions == [scope_sandbox["instruction"]], instructions

    report = _sweep(scope_sandbox, scope_sandbox["epic"]).stdout
    assert "leaves 1 open instruction still naming it" in report, report
    for child_ref in scope_sandbox["child_referrers"]:
        assert child_ref not in report, f"{child_ref} is a child's referrer: {report}"
    assert "citation" in report.lower(), "the citation is separated, not suppressed"


# ---- LAYER 3: the printed remedy, executed against a real store ----------

def test_see_them_all_actually_shows_them_all(scope_sandbox):
    """Run the EXACT command the tool prints in its own 'see them all' line and
    require the output to contain every hit.

    Against the pre-fix tool this looped: the printed command was the bare
    invocation that had just truncated, so following it produced the same ten
    lines and the same message. POSITIVE CONTROL first — the default run really
    is truncated, so the comparison below measures a real difference rather than
    an empty one."""
    default = _sweep(scope_sandbox, scope_sandbox["hub"]).stdout
    n = scope_sandbox["hub_referrers"]
    assert "more not shown" in default, (
        f"the fixture must exceed the render cap for this test to mean anything: {default}")
    assert f"leaves {n} open instructions" in default, default
    # DISTINCT referrers — the report renders two lines per flag (the referrer
    # and its matching line), so a raw match count double-counts and would make
    # this control unable to fail.
    shown_by_default = {m for m in re.findall(r"hub referrer (\d\d)", default)}
    assert len(shown_by_default) < n, (
        f"the default run must NOT already show them all: {sorted(shown_by_default)}")

    m = re.search(r"see them all with:\s*(.+?)\s*$", default, re.M)
    assert m, f"the truncation line must name a remedy: {default}"
    remedy = shlex.split(m.group(1))
    assert remedy[0] == "sable-identifier-decay", remedy

    full = _sweep(scope_sandbox, *remedy[1:]).stdout
    missing = [i for i in range(n) if f"hub referrer {i:02d}" not in full]
    assert not missing, f"the printed remedy did not show referrers {missing}"
    assert "more not shown" not in full, "the remedy must terminate, not re-truncate"


def test_the_close_hook_reports_the_narrowed_count_end_to_end(scope_sandbox):
    """The whole seam as an operator meets it: the REAL PreToolUse hook, the
    REAL sweeper off PATH, closing the REAL epic. What reaches the screen must
    be the one actionable instruction — not the prefix family."""
    shim = scope_sandbox["work"].parent / "shimbin"
    if not shim.exists():
        shim.mkdir()
        (shim / "sable-identifier-decay").symlink_to(SWEEPER)
    rc, ctx = _fire_hook({**scope_sandbox, "shim": shim},
                         f"bd close {scope_sandbox['epic']} --sandbox")
    assert rc == 0, "the hook must never fail the tool call"
    assert scope_sandbox["instruction"] in ctx, ctx
    for child_ref in scope_sandbox["child_referrers"]:
        assert child_ref not in ctx, f"prefix-collision noise reached the operator: {ctx}"
