"""Tests for sable_stall_probe_lib and the night-stall-probe it backs.

PART 1 (SABLE-cod50) guards deliberate_hold's enumerate-vs-derive regression
class directly: the original deliberate_idle() matched a hardcoded list of hold
PHRASES and false-STALLed on optimus's real "standing down" hold because that
exact wording wasn't in the list. These fixtures build pane captures by SHAPE
(settled composer with turn content above it, vs. an unsent/unanswered wake,
vs. a truncated turn) and never assert on any particular wording, so a new
phrasing can never regress this the way it regressed the old marker list.

PART 2 (SABLE-r69ho) guards the same class one layer up, in the manager map:
the probe hardcoded `{"%1": "optimus", "%2": "tarzan", "%3": "chuck"}` while
the live pane ids had moved to %10/%11/%12, so all three manager axes read
COULD-NOT-ASSESS on every run and *** THE EXIT-1 STALL PATH WAS STRUCTURALLY
UNREACHABLE -- THE STALL DETECTOR COULD NOT DETECT A STALL. *** It failed
honestly and loudly, and stayed broken anyway, because a PERMANENT DEGRADED is
operationally indistinguishable from NO-STALL to anyone not reading exit codes.

::test_stall_path_is_reachable IS THE TEST THIS FILE EXISTS FOR. A suite that
only asserted "DEGRADED when unreadable" would have passed green against the
broken probe for the entire time it was blind, so reachability of exit 1 is
asserted directly, and paired with the opposite polarity so the probe is shown
capable of BOTH outcomes rather than merely of one.

PART 3 (SABLE-ocnld) covers the import layer: an unguarded module-level import
crashed the probe with a traceback instead of honouring its own contract, in a
cron job whose stderr nobody reads.

WHERE THE ASSERTIONS LIVE, AND WHY IT IS NOT ARBITRARY. .claude/sable/state/
night-stall-probe.py is GITIGNORED and worktree-local (SABLE-0m78j), so no
fresh checkout and no CI clean room can run it. *** THE LOAD-BEARING
ASSERTIONS THEREFORE RUN AGAINST THE TRACKED LIB, WHICH IS PRESENT
EVERYWHERE. *** The subprocess leg drives the real script where it exists and
DIFFERENTIALLY asserts its exit code matches the lib's, so the two cannot
drift unobserved; where the script is absent that leg skips with a reason
naming every path it searched. Putting reachability only in the subprocess leg
would leave the suite green while asserting nothing about the exact property
the bead exists to protect -- the bug's own shape, in the test for the bug.
"""
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from sable_stall_probe_lib import (
    MANAGER_ROLES,
    deliberate_hold,
    manager_axis_states,
    resolve_manager_panes,
    stall_verdict,
)

_BORDER = "─" * 40
_CWD = "  ddc@host:~/wk-example"


def _settled_pane(content_lines):
    """A settled idle composer with `content_lines` rendered above it."""
    body = "\n".join(content_lines)
    return f"{body}\n{_BORDER}\n❯ \n{_BORDER}\n{_CWD}\n"


def _stuck_wake_pane(sender="lincoln", to="optimus", body="cap in force"):
    """A wake still sitting UNSENT in the composer (never submitted)."""
    return (
        f"● prior turn output\n{_BORDER}\n"
        f"❯ ⟦SABLE-MSG⟧ from={sender} to={to} :: {body}\n"
        f"{_BORDER}\n{_CWD}\n"
    )


def _truncated_pane():
    """Composer settled empty with NOTHING rendered above it."""
    return f"{_BORDER}\n❯ \n{_BORDER}\n{_CWD}\n"


def _busy_pane():
    return f"● doing work\n✻ Thinking… (12s · esc to interrupt)\n❯ \n"


def _held_pane():
    return _settled_pane(["● Standing down — nothing dispatchable until morning."])


# ===========================================================================
# PART 1 -- deliberate_hold: structural, not lexical (SABLE-cod50)
# ===========================================================================

def test_settled_hold_is_not_stall():
    """optimus's REAL hold text (SABLE-cod50 live false positive)."""
    pane = _settled_pane([
        "● Standing down — a wake will come from a landing, a message, or the",
        "  fresh Lincoln once it is booted. Recycle duty is complete and",
        "  verified... nothing is owed until morning.",
    ])
    assert deliberate_hold(pane) is True


def test_dropped_wake_is_stall():
    """Negative control: a delivered wake with no subsequent action."""
    assert deliberate_hold(_stuck_wake_pane()) is False


def test_truncated_turn_is_stall():
    """An empty, settled composer with no rendered turn content at all."""
    assert deliberate_hold(_truncated_pane()) is False


def test_session_limit_cut_is_stall():
    """A turn cut by the rate-limit banner still repaints a normal composer
    (SABLE-ita7) -- must not be misread as a completed, deliberate hold."""
    pane = _settled_pane(["You have hit your session limit - resets 2pm"])
    assert deliberate_hold(pane) is False


def test_busy_pane_is_not_assessed():
    assert deliberate_hold(_busy_pane()) is None


def test_no_composer_row_is_not_assessed():
    assert deliberate_hold("● some scrollback with no composer visible\n") is None


@pytest.mark.parametrize("phrase", [
    "Wrapping up for tonight — nothing left in my queue.",
    "Pausing here until the next signal arrives.",
    "Taking a break; ping me if something comes up.",
    "All caught up for now, going quiet.",
    "Calling it — I'll pick this back up when there's a reason to.",
])
def test_hold_phrasing_variants(phrase):
    """None of these phrasings appear in the OLD DELIBERATE_IDLE_MARKERS
    list this bead removed -- guards the enumeration regression class: any
    phrasing must classify as a hold, not just the ones on some list."""
    pane = _settled_pane([f"● {phrase}"])
    assert deliberate_hold(pane) is True


# ===========================================================================
# PART 2 -- manager resolution and verdict reachability (SABLE-r69ho)
#
# These run on EVERY machine, script present or not. Pane ids throughout are
# %90/%91/%92: NEITHER the ids that were hardcoded (%1/%2/%3) NOR the ids that
# happened to be live when the bug was found (%10/%11/%12). Resolution that
# passes these cannot be passing by id coincidence.
# ===========================================================================

_ALIEN = {"optimus": "%90", "tarzan": "%91", "chuck": "%92"}


def _pane_dump(entries):
    """Render a `tmux list-panes -a -F '#{pane_id} #{@sable_role}'` dump.
    A pane carrying no @sable_role prints its id and an EMPTY second field."""
    return "".join(f"{pane} {role}\n" for pane, role in entries)


def _fleet_dump(roles=None, extra=()):
    roles = _ALIEN if roles is None else roles
    entries = [("%0", "lincoln")]
    entries += [(pane, role) for role, pane in roles.items()]
    entries += list(extra)
    return _pane_dump(entries)


def _capture_from(by_pane):
    """A capture callable in the probe's (text, err) shape."""
    def capture(pane):
        if pane not in by_pane:
            return None, f"can't find pane: {pane}"
        return by_pane[pane], None
    return capture


def test_managers_resolved_by_role_tag_not_pane_id():
    """THE BUG, DIRECTLY. Pane ids are session-lifetime-unique and
    monotonically increasing -- guaranteed by their own specification to
    change -- so resolution must key on the @sable_role tag. Fails against
    any reintroduced hardcoded id map."""
    dump = _fleet_dump(extra=[("%93", ""), ("%94", "")])
    assert resolve_manager_panes(dump) == _ALIEN


def test_untagged_pane_is_never_guessed_into_a_manager():
    """The complement, and it bites the exact stale map that caused this: the
    old code named %1/%2/%3, so a dump where those ids exist but carry NO role
    tag must resolve nothing. Id coincidence alone is never a manager."""
    dump = _pane_dump([("%1", ""), ("%2", ""), ("%3", "")])
    assert resolve_manager_panes(dump) == {}


def test_non_manager_roles_are_not_resolved_as_managers():
    dump = _pane_dump([("%0", "lincoln"), ("%5", "worker"), ("%90", "optimus")])
    assert resolve_manager_panes(dump) == {"optimus": "%90"}


def test_missing_role_tag_is_could_not_assess_not_healthy():
    """*** THE TRAP IN THE FIX, AND IT IS LOAD-BEARING. *** A naive derivation
    returns whatever roles it happens to find, so a two-entry map sails
    through `all(v == "IDLE" ...)` and the probe reports RUNNING BECAUSE IT
    FORGOT A MANAGER EXISTS -- silent-healthy, strictly worse than the loud
    bug it replaced. Every role must get an entry, and an unresolved one must
    be COULD-NOT-ASSESS and must withhold the verdict."""
    resolved = resolve_manager_panes(
        _fleet_dump(roles={"optimus": "%90", "tarzan": "%91"}))
    states, degraded = manager_axis_states(
        resolved, _capture_from({"%90": _held_pane(), "%91": _held_pane()}))

    assert set(states) == set(MANAGER_ROLES), "a role must never be dropped"
    assert states["chuck"] == "COULD-NOT-ASSESS"
    assert states["chuck"] not in ("IDLE", "BUSY")
    assert any("@sable_role=chuck" in d for d in degraded)
    assert any("optimus" in d and "tarzan" in d for d in degraded), (
        "the reason must name the roles that DID resolve, so it points at itself")

    verdict, code = stall_verdict(states, {}, ready=7, in_flight=1, cap=4,
                                  degraded=degraded)
    assert (verdict, code) == ("DEGRADED", 2)


def test_unreadable_pane_is_could_not_assess_not_healthy():
    """The other way an axis goes dark: the role resolves but the pane will
    not capture. Also never healthy."""
    states, degraded = manager_axis_states(
        dict(_ALIEN),
        _capture_from({"%90": _held_pane(), "%91": _held_pane()}))

    assert states["chuck"] == "COULD-NOT-ASSESS"
    assert any("unreadable" in d and "%92" in d for d in degraded)
    assert stall_verdict(states, {}, 7, 1, 4, degraded) == ("DEGRADED", 2)


def test_stall_path_is_reachable():
    """*** THE POSITIVE CONTROL, AND THE REASON THIS FILE EXISTS. *** The
    entire defect was that exit 1 could not be reached, so a suite asserting
    only "DEGRADED when unreadable" passes green against the broken probe.
    All managers idle with no stated hold decision, ready work in the pool,
    fleet below cap -> STALL, exit 1."""
    resolved = resolve_manager_panes(_fleet_dump())
    captures = {pane: _stuck_wake_pane() for pane in _ALIEN.values()}
    states, degraded = manager_axis_states(resolved, _capture_from(captures))

    assert degraded == [], "no axis may be unreadable in the STALL fixture"
    assert set(states.values()) == {"IDLE"}

    held = {role: deliberate_hold(captures[pane])
            for role, pane in resolved.items()}
    assert set(held.values()) == {False}, "the dropped-wake shape, all three"

    assert stall_verdict(states, held, ready=7, in_flight=1, cap=4,
                         degraded=degraded) == ("STALL", 1)


def test_running_when_a_manager_is_busy():
    """The other polarity, so the probe is shown capable of BOTH outcomes and
    not merely of the one (SABLE-7sfvj). Same fixture as the STALL case
    except tarzan is working."""
    resolved = resolve_manager_panes(_fleet_dump())
    captures = {pane: _stuck_wake_pane() for pane in _ALIEN.values()}
    captures["%91"] = _busy_pane()
    states, degraded = manager_axis_states(resolved, _capture_from(captures))

    assert states["tarzan"] == "BUSY"
    assert degraded == []
    held = {role: deliberate_hold(captures[pane])
            for role, pane in resolved.items()}
    assert stall_verdict(states, held, ready=7, in_flight=1, cap=4,
                         degraded=degraded) == ("RUNNING", 0)


def test_all_idle_by_decision_is_running_held_not_stall():
    """The correct terminal state of a drain. Every manager idle, but every
    one of them idle BY DECISION -- must not be nudged."""
    resolved = resolve_manager_panes(_fleet_dump())
    captures = {pane: _held_pane() for pane in _ALIEN.values()}
    states, degraded = manager_axis_states(resolved, _capture_from(captures))
    held = {role: deliberate_hold(captures[pane])
            for role, pane in resolved.items()}

    assert set(held.values()) == {True}
    assert stall_verdict(states, held, ready=7, in_flight=1, cap=4,
                         degraded=degraded) == ("RUNNING (HELD)", 0)


def test_at_cap_is_not_stall():
    """The cap conjunct is load-bearing: idle managers with the fleet already
    full are not stalled, they are saturated."""
    states = {r: "IDLE" for r in MANAGER_ROLES}
    held = {r: False for r in MANAGER_ROLES}
    assert stall_verdict(states, held, ready=7, in_flight=4, cap=4) == ("RUNNING", 0)
    assert stall_verdict(states, held, ready=7, in_flight=3, cap=4) == ("STALL", 1)


def test_degraded_outranks_stall():
    """Verdict WITHHELD, never scored, when any axis went unread -- even when
    the readable axes would otherwise spell STALL."""
    states = {r: "IDLE" for r in MANAGER_ROLES}
    held = {r: False for r in MANAGER_ROLES}
    assert stall_verdict(states, held, 7, 1, 4,
                         degraded=["bd ready failed"]) == ("DEGRADED", 2)


@pytest.mark.parametrize("ready,in_flight", [(None, 1), (7, None)])
def test_unassessed_counts_withhold_the_verdict(ready, in_flight):
    """A count the probe could not read is COULD-NOT-ASSESS, not zero."""
    states = {r: "IDLE" for r in MANAGER_ROLES}
    held = {r: False for r in MANAGER_ROLES}
    assert stall_verdict(states, held, ready, in_flight, 4) == ("DEGRADED", 2)


# ===========================================================================
# PART 3 -- the real script, driven as a subprocess (SABLE-r69ho, SABLE-ocnld)
#
# night-stall-probe.py is gitignored and worktree-local, so this leg skips
# loudly wherever it is absent. Everything above still runs there.
# ===========================================================================

_PROBE_ENV = "SABLE_NIGHT_STALL_PROBE"
_PROBE_RELPATH = os.path.join(".claude", "sable", "state", "night-stall-probe.py")
_BIN = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_BIN)


def _probe_candidates():
    """Every path searched for the probe, in order.

    A linked worktree materialises only TRACKED files, so the probe is never
    in one; it lives in the checkout that created it. Derive that checkout
    from git rather than hardcoding a machine-specific path -- the same
    derive-don't-enumerate rule this whole bead is about.
    """
    override = os.environ.get(_PROBE_ENV)
    if override:
        return [override]
    out = [os.path.join(_REPO, _PROBE_RELPATH)]
    try:
        r = subprocess.run(["git", "-C", _REPO, "rev-parse", "--git-common-dir"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return out
    if r.returncode == 0 and r.stdout.strip():
        main_root = os.path.dirname(os.path.abspath(
            os.path.join(_REPO, r.stdout.strip())))
        candidate = os.path.join(main_root, _PROBE_RELPATH)
        if candidate not in out:
            out.append(candidate)
    return out


_CANDIDATES = _probe_candidates()
_PROBE = next((p for p in _CANDIDATES if os.path.exists(p)), None)

requires_probe = pytest.mark.skipif(
    _PROBE is None,
    reason=("night-stall-probe.py not found, so the differential leg cannot "
            "run. It is GITIGNORED and worktree-local (SABLE-0m78j), so no "
            "fresh checkout or CI clean room has it; the load-bearing "
            "assertions in PART 2 run regardless. Searched: "
            + ", ".join([f"${_PROBE_ENV}"] + _CANDIDATES)))


def _worker_cap():
    """Read the cap out of the script rather than restating it here -- a
    second copy of a constant is the defect class this file guards."""
    for line in open(_PROBE, encoding="utf-8"):
        if line.startswith("WORKER_CAP"):
            return int(line.split("=")[1].split("#")[0].strip())
    raise AssertionError("night-stall-probe.py has no WORKER_CAP")


# Fixture-driven stand-in for tmux and bd. It answers ONLY the exact argv
# shapes the probe issues and exits non-zero on anything else, so a probe that
# grows a new call fails loudly here instead of silently reading an empty
# answer -- a shim that shrugs is the same silent-instrument bug one level out.
_SHIM = '''#!/usr/bin/env python3
import json, os, sys

fx = json.load(open(os.environ["SABLE_PROBE_FIXTURE"]))
tool = os.path.basename(sys.argv[0])
argv = sys.argv[1:]


def emit(text):
    sys.stdout.write(text)
    raise SystemExit(0)


def die(msg):
    sys.stderr.write(msg + "\\n")
    raise SystemExit(1)


if tool == "tmux":
    if argv[:3] == ["list-panes", "-a", "-F"]:
        if "@sable_role" in argv[3]:
            emit("".join("%s %s\\n" % (p, r) for p, r in fx["roles"]))
        emit("".join("%s %s\\n" % (p, w) for p, w in fx["windows"]))
    if argv[:1] == ["capture-pane"]:
        pane = argv[argv.index("-t") + 1]
        if pane not in fx["captures"]:
            die("can't find pane: %s" % pane)
        emit(fx["captures"][pane])
    die("unexpected tmux invocation: %r" % (argv,))

if tool == "bd":
    if argv[:1] == ["ready"]:
        emit(json.dumps([{"id": "SABLE-rdy%d" % i} for i in range(fx["ready"])]))
    if argv[:2] == ["list", "--status"]:
        emit(json.dumps([{"id": i} for i in fx["in_progress"]]))
    die("unexpected bd invocation: %r" % (argv,))

die("unexpected tool: %s" % tool)
'''

_WORKER_PANES = [("%20", "worker-SABLE-abc")]


def _fixture(captures_by_role, roles=None, ready=7, in_progress=("SABLE-abc",)):
    """A whole fabricated fleet, in the two `tmux list-panes` shapes the probe
    reads plus the two `bd` counts."""
    roles = _ALIEN if roles is None else roles
    role_rows = [["%0", "lincoln"]] + [[p, r] for r, p in roles.items()]
    role_rows += [[p, ""] for p, _ in _WORKER_PANES]
    window_rows = [["%0", "claude"]] + [[p, r] for r, p in roles.items()]
    window_rows += [list(p) for p in _WORKER_PANES]
    return {
        "roles": role_rows,
        "windows": window_rows,
        "captures": {p: captures_by_role[r]
                     for r, p in roles.items() if r in captures_by_role},
        "ready": ready,
        "in_progress": list(in_progress),
    }


def _stage(tmp_path, hide=()):
    """Materialise a standalone copy of the probe with its own bin/.

    The probe derives its lib directory from its own realpath (`../../../bin`),
    so staging it under tmp gives us exact control over which libs it can
    import -- which is how the SABLE-ocnld guard is tested against a REAL
    ImportError from a REAL missing file, rather than by mocking the import
    machinery. The copy also means the live cockpit instrument is never
    touched by this suite.
    """
    state = tmp_path / ".claude" / "sable" / "state"
    state.mkdir(parents=True)
    binroot = tmp_path / "bin"
    binroot.mkdir()
    probe = state / "night-stall-probe.py"
    shutil.copyfile(_PROBE, probe)
    for lib in ("sable_pane_lib.py", "sable_stall_probe_lib.py"):
        if lib not in hide:
            shutil.copyfile(os.path.join(_BIN, lib), binroot / lib)
    return probe


def _run(tmp_path, probe, fixture):
    fx_path = tmp_path / "fixture.json"
    fx_path.write_text(json.dumps(fixture), encoding="utf-8")
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    for name in ("tmux", "bd"):
        shim = shims / name
        shim.write_text(_SHIM, encoding="utf-8")
        shim.chmod(0o755)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)  # else a hidden lib is still importable
    env["PATH"] = f"{shims}{os.pathsep}{env['PATH']}"
    env["SABLE_PROBE_FIXTURE"] = str(fx_path)
    return subprocess.run([sys.executable, str(probe)], capture_output=True,
                          text=True, cwd=str(tmp_path), env=env, timeout=120)


def _all(pane_text):
    return {role: pane_text for role in MANAGER_ROLES}


@requires_probe
def test_script_resolves_managers_by_role_tag_not_pane_id(tmp_path):
    """Real script, real process, real exit code. The fixture's pane ids are
    %90/%91/%92 -- neither the ids that were hardcoded nor the ones that were
    live -- so a pass here cannot be id coincidence."""
    result = _run(tmp_path, _stage(tmp_path), _fixture(_all(_busy_pane())))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "COULD-NOT-ASSESS" not in result.stdout
    for role in MANAGER_ROLES:
        assert f"'{role}': 'BUSY'" in result.stdout


@requires_probe
def test_script_stall_path_is_reachable(tmp_path):
    """Exit 1, end to end, through the real script. This is the property whose
    absence was the bug."""
    fixture = _fixture(_all(_stuck_wake_pane()), in_progress=("SABLE-abc",))
    assert len(fixture["in_progress"]) < _worker_cap()
    result = _run(tmp_path, _stage(tmp_path), fixture)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "VERDICT: STALL" in result.stdout


@requires_probe
def test_script_missing_role_tag_is_degraded_not_healthy(tmp_path):
    """A manager that failed to register must not be silently dropped from a
    derived map -- it must go loud."""
    fixture = _fixture(_all(_stuck_wake_pane()),
                       roles={"optimus": "%90", "tarzan": "%91"})
    result = _run(tmp_path, _stage(tmp_path), fixture)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "no live pane carries @sable_role=chuck" in result.stdout
    assert "VERDICT: RUNNING" not in result.stdout
    assert "VERDICT: STALL" not in result.stdout


@requires_probe
@pytest.mark.parametrize("name,fixture_fn", [
    ("stall", lambda: _fixture(_all(_stuck_wake_pane()))),
    ("busy", lambda: _fixture(_all(_busy_pane()))),
    ("held", lambda: _fixture(_all(_held_pane()))),
    ("missing_role", lambda: _fixture(_all(_stuck_wake_pane()),
                                      roles={"optimus": "%90"})),
    ("at_cap", lambda: _fixture(_all(_stuck_wake_pane()),
                                in_progress=tuple(f"SABLE-abc{i}" for i in range(4)))),
])
def test_script_agrees_with_lib_verdict(tmp_path, name, fixture_fn):
    """*** THE MIRROR IS CHECKED, NOT ASSUMED (SABLE-4udwf). *** The script
    still carries its own inline copy of this logic because it cannot import
    an unmerged branch's lib without crashing the live cockpit. So assert the
    two agree on every polarity, and the drift is caught the moment it starts
    -- wherever the script exists to be read."""
    fixture = fixture_fn()
    result = _run(tmp_path, _stage(tmp_path), fixture)

    resolved = resolve_manager_panes(
        "".join(f"{p} {r}\n" for p, r in fixture["roles"]))
    captures = fixture["captures"]
    states, degraded = manager_axis_states(resolved, _capture_from(captures))
    held = {}
    if states and all(v == "IDLE" for v in states.values()):
        for role, pane in resolved.items():
            held[role] = deliberate_hold(captures[pane])
            if held[role] is None:
                degraded.append(f"deliberate-idle unreadable for {pane}")
    _, expected = stall_verdict(states, held, fixture["ready"],
                                len(fixture["in_progress"]), _worker_cap(),
                                degraded)

    assert result.returncode == expected, (
        f"{name}: script exited {result.returncode}, lib says {expected}\n"
        + result.stdout + result.stderr)


@requires_probe
@pytest.mark.parametrize("hidden", [
    ("sable_stall_probe_lib.py",),
    ("sable_pane_lib.py",),
    ("sable_pane_lib.py", "sable_stall_probe_lib.py"),
])
def test_script_degrades_when_a_lib_is_absent(tmp_path, hidden):
    """SABLE-ocnld. The libs are genuinely deleted from the staged bin/, so
    this is a real ImportError from a real missing file -- no mocking of the
    import machinery. *** IT MUST DEGRADE, NOT DENY AND NOT PASS. *** The
    fixture is the one that otherwise reaches STALL (exit 1), so a probe that
    ignored the missing lib would be caught scoring an axis it cannot read."""
    probe = _stage(tmp_path, hide=hidden)
    result = _run(tmp_path, probe, _fixture(_all(_stuck_wake_pane())))

    assert result.returncode == 2, result.stdout + result.stderr
    assert "VERDICT: DEGRADED" in result.stdout
    assert "Traceback" not in result.stderr, "crashed instead of degrading"
    for lib in hidden:
        assert lib.replace(".py", "") in result.stdout, (
            "the degraded axis must NAME the lib it could not import")


@requires_probe
def test_import_guard_is_inert_when_libs_are_present(tmp_path):
    """*** THE NEGATIVE CONTROL FOR THE ocnld GUARD, AND IT IS THE WHOLE
    POINT. *** A guard that appends to `degraded` unconditionally would make
    the probe permanently DEGRADED -- which is precisely the SABLE-r69ho
    failure it is being added next to. With the libs present, nothing is
    added and the STALL path is still reachable through the same fixture."""
    result = _run(tmp_path, _stage(tmp_path), _fixture(_all(_stuck_wake_pane())))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "VERDICT: STALL" in result.stdout
    assert "unavailable" not in result.stdout
    assert "VERDICT: DEGRADED" not in result.stdout


# ===========================================================================
# PART 4 -- the real script against a REAL tmux server (SABLE-r69ho)
#
# PART 3 answers the pane query from a fixture, which proves the probe's LOGIC
# but not that @sable_role is a real, readable tmux pane option or that the ids
# tmux hands out are actually independent of the map. This leg mocks NOTHING
# about the pane query: a private tmux server, real panes, real @sable_role
# options, real pane ids that the test does not choose, and the real script in
# a real process yielding a real exit code. Only `bd` stays shimmed -- it is a
# different axis, and requiring the live bead store would make the fleet's own
# backlog decide whether this test passes.
# ===========================================================================

_REAL_TMUX = shutil.which("tmux")

requires_tmux = pytest.mark.skipif(
    _REAL_TMUX is None,
    reason="tmux not on PATH, so the real-server leg cannot run (the shimmed "
           "leg in PART 3 and the lib assertions in PART 2 still do)")

# REAL tmux, redirected to a private socket. The pane query itself is genuine;
# only the server is isolated, so the probe can neither read nor perturb the
# live cockpit -- a probe under test that enumerated the real fleet would make
# this suite's result depend on whatever the managers happen to be doing.
_TMUX_SOCKET_SHIM = '#!/bin/sh\nexec {real} -L "$SABLE_TEST_TMUX_SOCKET" "$@"\n'


def _srv(sock, *args):
    return subprocess.run([_REAL_TMUX, "-L", sock, *args],
                          capture_output=True, text=True, timeout=30)


def _pane_ids(sock):
    """{window_name: pane_id} straight from the real server."""
    out = _srv(sock, "list-panes", "-a", "-F", "#{pane_id} #{window_name}").stdout
    return {w: p for p, w in (ln.split() for ln in out.splitlines() if ln.split())}


@pytest.fixture
def real_fleet(tmp_path):
    """A private tmux server carrying three role-tagged manager panes whose
    content is the dropped-wake shape.

    The panes are CHURNED first so tmux issues ids well past %1/%2/%3: pane ids
    are monotonically increasing and never reused, which is the property that
    made the hardcoded map rot, and it is also what lets this test prove the
    resolution does not depend on them.
    """
    sock = f"sable-stall-probe-it-{os.getpid()}"
    body = tmp_path / "pane-body.txt"
    body.write_text(_stuck_wake_pane(), encoding="utf-8")
    hold = f"cat {body}; sleep 300"
    try:
        # The burn session is killed only AFTER the fleet session exists:
        # killing the last session stops the server, which resets the id
        # counter and would silently hand the managers %0/%1/%2 -- the very
        # coincidence this test is built to exclude.
        _srv(sock, "new-session", "-d", "-s", "burn", "-n", "burn0", "sleep 300")
        for i in range(1, 6):  # churn: advance the id counter past the old map
            _srv(sock, "new-window", "-d", "-t", "burn:", "-n", f"burn{i}",
                 "sleep 300")

        _srv(sock, "new-session", "-d", "-s", "fleet", "-n", "optimus", hold)
        for role in ("tarzan", "chuck"):
            _srv(sock, "new-window", "-d", "-t", "fleet:", "-n", role, hold)
        _srv(sock, "kill-session", "-t", "burn")

        panes = _pane_ids(sock)
        assert set(panes) == set(MANAGER_ROLES), panes
        for role, pane in panes.items():
            _srv(sock, "set-option", "-p", "-t", pane, "@sable_role", role)

        deadline = time.time() + 10
        while time.time() < deadline:
            caps = [_srv(sock, "capture-pane", "-p", "-t", p).stdout
                    for p in panes.values()]
            if all("⟦SABLE-MSG⟧" in c for c in caps):
                break
            time.sleep(0.05)
        else:
            pytest.fail("panes never rendered their fixture body")

        yield sock, panes
    finally:
        subprocess.run([_REAL_TMUX, "-L", sock, "kill-server"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=30)


def _run_against_server(tmp_path, sock):
    probe = _stage(tmp_path)
    fx_path = tmp_path / "fixture.json"
    fx_path.write_text(json.dumps(_fixture({})), encoding="utf-8")
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    (shims / "bd").write_text(_SHIM, encoding="utf-8")
    (shims / "bd").chmod(0o755)
    (shims / "tmux").write_text(_TMUX_SOCKET_SHIM.format(real=_REAL_TMUX),
                                encoding="utf-8")
    (shims / "tmux").chmod(0o755)

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("TMUX", None)  # else the probe inherits the live cockpit's server
    env["PATH"] = f"{shims}{os.pathsep}{env['PATH']}"
    env["SABLE_PROBE_FIXTURE"] = str(fx_path)
    env["SABLE_TEST_TMUX_SOCKET"] = sock
    return subprocess.run([sys.executable, str(probe)], capture_output=True,
                          text=True, cwd=str(tmp_path), env=env, timeout=120)


@requires_probe
@requires_tmux
def test_real_tmux_stall_path_is_reachable(tmp_path, real_fleet):
    """*** THE POSITIVE CONTROL, END TO END, WITH NOTHING MOCKED ON THE PANE
    QUERY. *** Real server, real @sable_role options, real pane ids chosen by
    tmux, real process, real exit 1."""
    sock, panes = real_fleet
    assert not {"%1", "%2", "%3"} & set(panes.values()), (
        f"churn failed to move the ids off the old hardcoded map: {panes}")

    result = _run_against_server(tmp_path, sock)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "VERDICT: STALL" in result.stdout
    assert "COULD-NOT-ASSESS" not in result.stdout
    for role in MANAGER_ROLES:
        assert f"'{role}': 'IDLE'" in result.stdout, (
            "manager states must be READ, not withheld\n" + result.stdout)


@requires_probe
@requires_tmux
def test_real_tmux_untagged_manager_is_degraded_not_healthy(tmp_path, real_fleet):
    """The other polarity on the same real server: remove ONE real pane's
    @sable_role and the probe must go loud about that role rather than quietly
    proceeding on the two it still found."""
    sock, panes = real_fleet
    _srv(sock, "set-option", "-p", "-t", panes["chuck"], "-u", "@sable_role")

    result = _run_against_server(tmp_path, sock)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "no live pane carries @sable_role=chuck" in result.stdout
    assert "VERDICT: DEGRADED" in result.stdout
    assert "VERDICT: STALL" not in result.stdout
