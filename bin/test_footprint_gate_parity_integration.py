#!/usr/bin/env python3
"""GATE PARITY integration test — real bd, real shell hook, no mocks.
(SABLE-7gesd / SABLE-g0elq / SABLE-546m5 / SABLE-9dmuu, the footprint-parser
consolidation.)

*** THE PROPERTY UNDER TEST IS NOT "A GATE DENIES". IT IS "THE TWO GATES AGREE". ***

Two independent implementations answer the same question at dispatch time:

    bin/sable-spawn-worker           overlap_check()   — the python gate
    hooks/multi-manager/pre-dispatch-overlap.sh        — the shell gate

They are supposed to be redundant. They were not: the shell gate read the
IN-PROGRESS side from `metadata.wip_claims` AND NOTHING ELSE, while the python
gate read the full union. So an in-progress bead declaring its footprint only in
a `## File footprint` description section — the DECOMPOSITION-authored form
planners are TOLD to write — contributed NOTHING to the shell gate's comparison.
It could not be overlapped with. Failure shape: RELEASING AND SILENT. The gate
returned "no overlap" having never looked at the declared footprint of the bead it
should have collided with.

`pre-dispatch-claim.sh` normally writes `wip_claims`, which MASKED this whenever
it had already fired — but the hooks share a trigger with NO ORDERING GUARANTEE,
and that unordered window is precisely what this gate exists to cover.

Every case below is therefore run through BOTH gates and their verdicts compared,
because a test that exercises one gate proves nothing about the divergence. Each
declaration field is tested ONE AT A TIME (SABLE-7gesd's named negative control):
a combined case passes because some other field happened to be populated too,
which is exactly how the `footprint_writes` gap survived — SABLE-21rug.4 declared
only `footprint_writes` and was covered by luck of also carrying a section.

HERMETIC (SABLE-b0w8k): a FRESH throwaway bd DB per run via `BEADS_DB`, never the
shared live pool. Scratch beads created here were once made in the live pool,
where a leftover in-progress fixture bead DENIED A REAL AGENT'S DISPATCH.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "multi-manager" / "pre-dispatch-overlap.sh"

HAVE_BD = shutil.which("bd") is not None
pytestmark = pytest.mark.skipif(
    not HAVE_BD,
    reason="needs a real bd — this suite has no mocked half to fall back to "
           "(SABLE-59zu clean room skips it whole)")

_LOADER = SourceFileLoader("sable_spawn_worker_gp", str(REPO / "bin" / "sable-spawn-worker"))
_SPEC = importlib.util.spec_from_loader("sable_spawn_worker_gp", _LOADER)
ssw = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(ssw)

SHARED = "hooks/foo-gate-parity-fixture.sh"
OTHER = "hooks/bar-gate-parity-fixture.sh"


# --------------------------------------------------------------------------
# Isolated per-run bd DB
# --------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    """A fresh bd DB plus the env every `bd` call and the hook subprocess must
    inherit to resolve against it.

    `--prefix=sable` is LOAD-BEARING, not cosmetic: pre-dispatch-overlap.sh's own
    DISPATCH_IDS regex only recognises (bd|sable|epic|task|bug|feat)-* ids, so any
    other prefix yields bead ids the hook never matches — DISPATCH_IDS comes back
    empty and the hook silently no-ops on EVERY case, which reads as a clean pass.
    """
    root = tmp_path / "beads"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_AGENT_NAME", "CLAUDE_AGENT_ROLE", "SABLE_WORKER_PANE",
                        "SABLE_BEAD", "TMUX_PANE", "SABLE_TMUX_SOCKET", "SABLE_MG_BD")}
    env.update({"HOME": str(home), "BD_NON_INTERACTIVE": "1", "CI": "true"})
    init = subprocess.run(["bd", "init", "--prefix=sable"], cwd=str(root),
                          env=env, capture_output=True, text=True, timeout=180)
    if not (root / ".beads").is_dir():
        pytest.skip(f"could not initialise an isolated bd DB: {init.stdout}{init.stderr}")
    env["BEADS_DB"] = str(root / ".beads")

    agents = tmp_path / "agents.yaml"
    agents.write_text("agents:\n  optimus:\n    type: epic_manager\n")
    mode = tmp_path / "mode-exec.json"
    mode.write_text(json.dumps({"mode": "execution", "since": "2026-07-26"}))
    env["SABLE_AGENTS_YAML"] = str(agents)
    env["SABLE_MODE_STATE"] = str(mode)
    return env


def _bd(env, *args, check=True):
    cp = subprocess.run(["bd", *args], env=env, capture_output=True, text=True,
                        timeout=180)
    if check and cp.returncode != 0:
        raise AssertionError(f"bd {' '.join(args)} failed: {cp.stdout}{cp.stderr}")
    return cp


def _create(env, title, description) -> str:
    cp = _bd(env, "create", "--sandbox", "--title", title,
             "--description", description, "--type=task")
    m = re.search(r"\b(sable-[A-Za-z0-9._-]+)\b", cp.stdout, re.IGNORECASE)
    if not m:
        pytest.skip(f"bd create returned no parseable id: {cp.stdout}")
    return m.group(1)


def _show(env, bead_id) -> dict:
    data = json.loads(_bd(env, "show", bead_id, "--json").stdout)
    return data[0] if isinstance(data, list) else data


# --------------------------------------------------------------------------
# The two gates, each invoked FOR REAL
# --------------------------------------------------------------------------

def _shell_gate(env, dispatch_id) -> tuple[str, str]:
    """Run the REAL hook and return (decision, reason). Decision is one of
    'deny' / 'allow' / 'context' / 'silent'."""
    payload = json.dumps({
        "tool_name": "Agent",
        "hook_event_name": "PreToolUse",
        "agent_type": "optimus",
        "cwd": str(REPO),
        "tool_input": {"subagent_type": "general-purpose",
                       "prompt": f"Work {dispatch_id}: read the dispatch and do it."},
    })
    cp = subprocess.run(["bash", str(HOOK)], input=payload, env=env,
                        capture_output=True, text=True, timeout=180)
    if not cp.stdout.strip():
        return "silent", ""
    try:
        out = json.loads(cp.stdout)["hookSpecificOutput"]
    except Exception:
        return "silent", cp.stdout
    if out.get("permissionDecision") == "deny":
        return "deny", out.get("permissionDecisionReason", "")
    return "context", out.get("additionalContext", "")


def _python_gate(env, dispatch_id) -> "ssw.OverlapVerdict":
    """Run the REAL overlap_check against the same real bd store."""
    bead = _show(env, dispatch_id)
    listing = json.loads(_bd(env, "list", "--status=in_progress", "--json",
                             "--limit", "0").stdout or "[]")
    in_progress = [b for b in listing if isinstance(b, dict)]
    return ssw.overlap_check(dispatch_id, bead, in_progress)


def _both_deny(env, dispatch_id, expect_bead):
    shell_kind, shell_reason = _shell_gate(env, dispatch_id)
    verdict = _python_gate(env, dispatch_id)
    assert shell_kind == "deny", (
        f"SHELL gate did not deny (got {shell_kind!r}: {shell_reason[:400]})")
    assert expect_bead in shell_reason, (
        f"shell deny does not name {expect_bead}: {shell_reason[:400]}")
    assert verdict.decision == "deny", (
        f"PYTHON gate did not deny (got {verdict.decision!r}: {verdict.message[:400]})")
    assert expect_bead in verdict.message
    return shell_reason, verdict.message


# --------------------------------------------------------------------------
# THE BEAD: an in-progress bead declaring ONLY in a section
# --------------------------------------------------------------------------

def test_section_only_in_progress_bead_is_seen_by_both_gates(store):
    """SABLE-7gesd, the bead, end to end. Bead A is in_progress with its footprint
    declared ONLY in a `## File footprint` description section and NO wip_claims
    metadata (unlike the pre-existing e2e case, which sets it). Bead B declares
    the same path.

    PLANT-AND-FAIL: against pre-fix code the SHELL leg MUST fail. The
    OVERLAPS_JSON block read `metadata.get('wip_claims')` for each in-progress
    bead and nothing else, so A's section was invisible and B dispatched straight
    into a live collision — while the python gate, reading the same two beads,
    denied. Two gates, same question, opposite answers."""
    env = store
    a = _create(env, "[int] gate-parity A: section-only declaration",
                f"Scratch A.\n\n## File footprint\n{SHARED}\n")
    _bd(env, "update", a, "--sandbox", "--claim")
    # Prove the premise rather than assuming it: if some hook or bd default
    # populated wip_claims, this test would pass for the wrong reason.
    meta = _show(env, a).get("metadata") or {}
    assert not (meta.get("wip_claims") or "").strip(), (
        f"fixture invalid — A carries wip_claims metadata: {meta}")
    assert _show(env, a)["status"] == "in_progress"

    b = _create(env, "[int] gate-parity B: same path",
                f"Scratch B.\n\n## File footprint\n{SHARED}\n")
    shell_reason, py_message = _both_deny(env, b, a)
    assert SHARED in shell_reason and SHARED in py_message


def test_metadata_borne_declaration_still_denies(store):
    """COMPLEMENT LEG, named by the bead: the pre-existing metadata case must
    STILL deny, proving both sources are read rather than one REPLACING the
    other. A fix that swapped wip_claims for the section would satisfy the test
    above and silently break every bead pre-dispatch-claim.sh has claimed."""
    env = store
    a = _create(env, "[int] gate-parity A: metadata-only declaration", "Scratch A.")
    _bd(env, "update", a, "--sandbox", "--claim")
    _bd(env, "update", a, "--sandbox", "--set-metadata", f"wip_claims={SHARED}")
    b = _create(env, "[int] gate-parity B: same path",
                f"Scratch B.\n\n## File footprint\n{SHARED}\n")
    _both_deny(env, b, a)


def test_footprint_writes_only_declaration_denies_in_both_gates(store):
    """THE THIRD FIELD NO GATE READ (SABLE-7gesd's notes). `footprint_writes` is
    written by sable-spawn-worker's own tag_footprint_metadata, and grep for it
    returned ZERO in both the installed hook and the spine copy. Caught live at
    dispatch on SABLE-21rug.4: `footprint_writes` set, `wip_claims` None — covered
    only because it ALSO carried a section naming the same paths.

    Tested with that masking field REMOVED, which is the whole point: A declares
    via `footprint_writes` and nothing else."""
    env = store
    a = _create(env, "[int] gate-parity A: footprint_writes only", "Scratch A.")
    _bd(env, "update", a, "--sandbox", "--claim")
    _bd(env, "update", a, "--sandbox", "--set-metadata", f"footprint_writes={SHARED}")
    meta = _show(env, a).get("metadata") or {}
    assert not (meta.get("wip_claims") or "").strip(), "fixture invalid — wip_claims set"
    b = _create(env, "[int] gate-parity B: same path",
                f"Scratch B.\n\n## File footprint\n{SHARED}\n")
    _both_deny(env, b, a)


def test_disjoint_beads_still_co_dispatch(store):
    """*** NEGATIVE CONTROL, LOAD-BEARING. *** A parser that widens until
    everything collides is trivially "safe" and destroys dispatch entirely — it
    is the exact mirror of the failure being fixed, and it is the failure a
    loss-focused fix will not notice. Two genuinely non-overlapping beads must
    STILL co-dispatch after every change in this branch.

    Both beads carry prose full of commas and file-shaped words, so this also
    proves the widened tokenizer did not start harvesting the description into the
    claim set."""
    env = store
    a = _create(env, "[int] gate-parity A: disjoint",
                "Scratch A. It mentions bin/sable-spawn-worker, and hooks, and "
                f"prose, never, so, failing.\n\n## File footprint\n{OTHER}\n")
    _bd(env, "update", a, "--sandbox", "--claim")
    b = _create(env, "[int] gate-parity B: disjoint",
                "Scratch B. It also mentions prose, with commas, and words.\n\n"
                f"## File footprint\n{SHARED}\n")

    shell_kind, shell_reason = _shell_gate(env, b)
    verdict = _python_gate(env, b)
    assert shell_kind != "deny", f"SHELL gate denied a disjoint pair: {shell_reason[:400]}"
    assert verdict.decision == "none", (
        f"PYTHON gate did not report a completed clean check: "
        f"{verdict.decision!r} {verdict.message[:400]}")


def test_prose_in_the_section_never_becomes_a_claimed_path_at_either_gate(store):
    """SABLE-g0elq's integration leg. A real bead whose footprint section is
    FOLLOWED BY PROSE collides with a second real bead ONLY on genuine paths, and
    the emitted refusal text contains NO non-path token.

    The prose here is the verbatim specimen from the bead: three commas that used
    to yield the phantom claims `failing`, `never`, `so`, which appeared IN THE
    REFUSAL MESSAGE and made a legitimate refusal read as garbage. That is the
    prove-the-gate-can-release erosion arriving from the noise side — a reader who
    sees `failing, never, so` in a blocker list learns the gate is noisy."""
    env = store
    prose = ("the fleet-binding lane rule (derive membership from declared "
             "footprints,\nnever from an enumerated list) is SILENT on a bead "
             "that declares nothing,\nfailing in the RELEASING direction. It had "
             "no footprint section, so it was\ndispatchable.\n")
    a = _create(env, "[int] gate-parity A: prose tail",
                f"Scratch A.\n\n## File footprint\n{SHARED}\n\n{prose}")
    _bd(env, "update", a, "--sandbox", "--claim")
    b = _create(env, "[int] gate-parity B: prose tail",
                f"Scratch B.\n\n## File footprint\n{SHARED}\n\n{prose}")

    shell_reason, py_message = _both_deny(env, b, a)
    for phantom in ("failing", "never", " so", "SILENT", "dispatchable"):
        assert phantom not in shell_reason, (
            f"prose token {phantom!r} reached the SHELL refusal text:\n{shell_reason}")
        assert phantom not in py_message, (
            f"prose token {phantom!r} reached the PYTHON refusal text:\n{py_message}")
    # And the refusal names EXACTLY the one real shared path.
    assert SHARED in shell_reason and SHARED in py_message


def test_both_gates_read_the_same_union_for_the_same_bead(store):
    """PARSER PARITY at the GATE boundary, on a real bead through real bd: the
    shell gate's resolver stream and the python gate's read_bead_footprint must
    return the SAME file set for the SAME bead.

    Asserted on a bead that declares in ALL FOUR sources at once, with each source
    naming a DIFFERENT path, so a gate that reads three of four fails here rather
    than passing on an accidental agreement."""
    env = store
    bead_id = _create(
        env, "[int] gate-parity: four-source union",
        "Scratch.\n\nWIP-CLAIMS: hooks/from-prose-line.sh\n\n"
        "## File footprint\nhooks/from-section.sh\n")
    _bd(env, "update", bead_id, "--sandbox", "--set-metadata",
        "wip_claims=hooks/from-wip-metadata.sh")
    _bd(env, "update", bead_id, "--sandbox", "--set-metadata",
        "footprint_writes=hooks/from-writes-metadata.sh")

    expected = {"hooks/from-prose-line.sh", "hooks/from-section.sh",
                "hooks/from-wip-metadata.sh", "hooks/from-writes-metadata.sh"}

    lib = REPO / "bin" / "sable_footprint_lib.py"
    cp = subprocess.run(["python3", str(lib), "--read-declared"],
                        input=_bd(env, "show", bead_id, "--json").stdout,
                        env=env, capture_output=True, text=True, timeout=60)
    shell_side = {ln[1:] for ln in cp.stdout.splitlines() if ln.startswith("f")}
    python_side = set(ssw.read_bead_footprint(_show(env, bead_id)).files)

    assert shell_side == python_side == expected, (
        f"gates disagree — shell={sorted(shell_side)} "
        f"python={sorted(python_side)} expected={sorted(expected)}")


def test_declares_nothing_still_dispatches_at_both_gates(store):
    """The other load-bearing release control (SABLE-47try's do-not clause): a
    gate that can never release is indistinguishable from correct caution. A bead
    declaring NO footprint at all must still dispatch while an overlapping bead is
    in progress — announced loudly as NO-DECLARATION, never refused."""
    env = store
    a = _create(env, "[int] gate-parity A: occupant",
                f"Scratch A.\n\n## File footprint\n{SHARED}\n")
    _bd(env, "update", a, "--sandbox", "--claim")
    b = _create(env, "[int] gate-parity B: declares nothing",
                "Scratch B. No footprint section, no claims, nothing file-shaped.")

    shell_kind, shell_text = _shell_gate(env, b)
    verdict = _python_gate(env, b)
    assert shell_kind != "deny", f"SHELL gate refused an undeclared bead: {shell_text[:400]}"
    assert "NO-DECLARATION" in shell_text, (
        f"the shell gate released SILENTLY instead of announcing it: {shell_text[:400]}")
    assert verdict.decision == "no-declaration", verdict.message


def test_prose_only_section_is_could_not_assess_at_both_gates(store):
    """The SABLE-47try trichotomy, preserved through the consolidation and checked
    at BOTH gates: a section whose only content is PROSE is not a successful empty
    read. It reports could-not-assess and REFUSES, distinctly from both a clean
    check and a declares-nothing release.

    This is the arm every one of the four bundled beads could have collapsed, and
    the collapse fails in the RELEASING direction."""
    env = store
    b = _create(env, "[int] gate-parity: prose-only section",
                "Scratch.\n\n## File footprint\nGitHub repo settings, probably\n")
    shell_kind, shell_reason = _shell_gate(env, b)
    verdict = _python_gate(env, b)
    assert shell_kind == "deny", f"SHELL gate did not refuse: {shell_kind} {shell_reason[:400]}"
    assert "COULD NOT RUN" in shell_reason
    assert verdict.decision == "could-not-assess", verdict.message
    # And the refusal is ACTIONABLE — it names the token it rejected, so the
    # author is not left guessing which of their words was the problem.
    assert "GitHub" in verdict.message, (
        f"the rejected fragment is not surfaced: {verdict.message}")


def test_two_contradicting_sections_are_reported_at_the_gate(store):
    """SABLE-9dmuu's gate-visible half, on a real bead. Two `## File footprint`
    sections used to resolve as the FIRST one silently, with a clean-read report.
    Now both are unioned and the multiplicity rides out on the overlap check's
    DEGRADED warning channel — so a manager appending a correction sees that the
    bead has two declarations instead of discovering it three hours later."""
    env = store
    b = _create(env, "[int] gate-parity: two sections",
                f"Scratch.\n\n## File footprint\n{OTHER}\n\n"
                f"## Test spec\nwords\n\n## File footprint\n{SHARED}\n")
    verdict = _python_gate(env, b)
    read = ssw.read_bead_footprint(_show(env, b))
    assert read.files == {OTHER, SHARED}, (
        f"the appended section was discarded: {sorted(read.files)}")
    assert any("File footprint" in w and "2" in w for w in verdict.warnings), (
        f"multiplicity not reported at the gate: {verdict.warnings}")


def test_missing_shared_lib_fails_OPEN_and_says_so(store, tmp_path):
    """The consolidation gave the shell gate a DEPENDENCY it did not have before,
    and a dependency is a new way to be inert. Hooks are COPY-installed, so
    bin/sable_footprint_lib.py resolves from the INSTALLED path — and
    SABLE-nn54x is the bead about a hook that was WIRED, FIRING AND INERT for
    exactly this reason, while an installed-path presence probe, a settings.json
    wiring grep, and a cmp against the landed blob all reported it ACTIVE. None
    of them inspected the dependency closure.

    Two properties, and BOTH matter:
      FAIL OPEN — a scheduling constraint that cannot run must not block work
                  (SABLE-47try's do-not clause: a gate that can never release is
                  indistinguishable from correct caution).
      FAIL LOUD — but an unannounced stand-down is the inert state itself. The
                  hook must SAY it did not run.

    Exercised by running a COPY of the hook from a directory whose ../../bin has
    no lib, which is precisely the installed-without-closure shape."""
    env = store
    a = _create(env, "[int] gate-parity A: occupant", 
                f"Scratch A.\n\n## File footprint\n{SHARED}\n")
    _bd(env, "update", a, "--sandbox", "--claim")
    b = _create(env, "[int] gate-parity B: same path",
                f"Scratch B.\n\n## File footprint\n{SHARED}\n")

    # A hook copy two levels under a tree with an EMPTY bin/ — same relative
    # resolution, nothing to resolve to.
    fake = tmp_path / "installed" / "hooks" / "multi-manager"
    fake.mkdir(parents=True)
    (tmp_path / "installed" / "bin").mkdir()
    shutil.copy(HOOK, fake / "pre-dispatch-overlap.sh")
    # lib-identity.sh is sourced by path and must come along, or the copy dies
    # before it ever reaches the lib check.
    shutil.copy(Path(HOOK).parent / "lib-identity.sh", fake / "lib-identity.sh")
    for extra in Path(HOOK).parent.glob("lib-*.sh"):
        shutil.copy(extra, fake / extra.name)

    payload = json.dumps({
        "tool_name": "Agent", "hook_event_name": "PreToolUse",
        "agent_type": "optimus", "cwd": str(REPO),
        "tool_input": {"subagent_type": "general-purpose",
                       "prompt": f"Work {b}: do the thing."},
    })
    cp = subprocess.run(["bash", str(fake / "pre-dispatch-overlap.sh")],
                        input=payload, env=env, capture_output=True, text=True,
                        timeout=180)
    assert cp.returncode == 0, f"the hook crashed instead of failing open: {cp.stderr[:400]}"
    out = json.loads(cp.stdout)["hookSpecificOutput"]
    assert "permissionDecision" not in out or out.get("permissionDecision") != "deny", (
        f"a missing library DENIED a dispatch — gate-that-can-never-release: {out}")
    assert "DID NOT RUN" in out.get("additionalContext", ""), (
        f"the stand-down was SILENT — this is the wired-and-inert state: {out}")

    # POSITIVE CONTROL, load-bearing: the very same dispatch through the REAL
    # hook (whose lib IS resolvable) denies. Without this the test above passes
    # for a hook that never checks anything under any condition.
    kind, reason = _shell_gate(env, b)
    assert kind == "deny", f"the real hook did not deny the same dispatch: {kind} {reason[:300]}"
