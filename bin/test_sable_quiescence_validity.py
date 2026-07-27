#!/usr/bin/env python3
"""Unit tests for bin/sable_quiescence_validity_lib.py (SABLE-r0mzn).

Every clock here is INJECTED. Nothing sleeps: a test that waited out a real
validity interval would be slow, flaky, and unable to force its own boundary
cases (t exactly at valid_until, an event one tick before the probe).

The controls are the point of this file, not decoration. The failure mode
being guarded against on the other side is SABLE-47try erosion: a predicate
that voids EVERY clearance is indistinguishable from correct caution, passes
every "it refused" assertion, and is strictly worse than the gap it replaced.
So each void assertion is paired with a clear assertion that must survive.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_quiescence_validity_lib as lib  # noqa: E402

# Fixture clock. T0 is when the probe is taken; the window is 300s wide.
T0 = 1000.0
WINDOW = 300.0
HOLDER = "chuck"
SCOPE = ("chuck", "optimus", "tarzan")


def a_clearance(**overrides) -> lib.Clearance:
    kwargs = dict(subject="tripwire-watcher", holder=HOLDER, taken_at=T0,
                  validity_seconds=WINDOW, actor_scope=SCOPE)
    kwargs.update(overrides)
    return lib.Clearance(**kwargs)


# --- named by the bead's test spec -------------------------------------------

def test_clearance_carries_validity_interval_and_invalidators():
    """A clearance record exposes taken_at, its validity window, and its
    invalidating-event list, and the RENDERED output names all three -- a
    reader must never have to ask a clearance "as of when?"."""
    clearance = a_clearance()

    assert clearance.taken_at == T0
    assert clearance.validity_seconds == WINDOW
    assert clearance.valid_until == T0 + WINDOW
    assert lib.KIND_DISPATCH in clearance.invalidators
    assert lib.KIND_READINESS_ANNOUNCEMENT in clearance.invalidators

    rendered = lib.render_clearance(clearance)
    assert "t=1000" in rendered, "rendered output omits when it was taken"
    assert "1300" in rendered and "300s" in rendered, \
        "rendered output omits the validity window"
    for kind in clearance.invalidators:
        assert kind in rendered, f"rendered output omits invalidator {kind!r}"
    assert "whichever is sooner" in rendered
    # The actor scope is the third thing a clearance is only correct FOR.
    for actor in SCOPE:
        assert actor in rendered

    # CONTROL: a clearance with no interval is REJECTED AT CONSTRUCTION,
    # rather than being built and defaulting to unbounded.
    with pytest.raises(lib.ClearanceIntervalRequired):
        a_clearance(validity_seconds=None)
    # ...and the two ways an "interval" can secretly mean unbounded.
    with pytest.raises(lib.ClearanceIntervalRequired):
        a_clearance(validity_seconds=float("inf"))
    with pytest.raises(lib.ClearanceIntervalRequired):
        a_clearance(validity_seconds=0)
    # CONTROL ON THE CONTROL: the rejection is specific to a defective
    # interval, not a constructor that refuses everything.
    assert a_clearance(validity_seconds=1).valid_until == T0 + 1


def test_dispatch_between_probe_and_act_voids_the_clearance():
    """A dispatch landing between the probe and the act voids the clearance,
    and the verdict NAMES the dispatch. This is the 2026-07-23 window: the
    probe was right when taken and wrong when acted on."""
    clearance = a_clearance()
    dispatch = lib.Event(
        kind=lib.KIND_DISPATCH, at=T0 + 60, actor="optimus",
        detail="wk-overlap-warning SABLE-pfbjw running "
               "hooks/test/test-pre-push-rebase-test.sh")

    verdict = lib.evaluate(clearance, [dispatch], now=T0 + 120)
    assert verdict.state == lib.VOID
    assert verdict.cause == lib.CAUSE_INTERVENING_EVENT
    assert verdict.voided_by is dispatch
    assert lib.KIND_DISPATCH in verdict.reason
    assert "wk-overlap-warning" in verdict.reason, \
        "the verdict must name the dispatch, not merely refuse"

    # CONTROL (named by the spec): no intervening event -> STILL VALID.
    still = lib.evaluate(clearance, [], now=T0 + 120)
    assert still.state == lib.VALID
    assert still.cause == lib.CAUSE_NONE


def test_dispatch_outside_the_window_does_not_void_the_clearance():
    """THE PAIRED CONTROL THAT MAKES THE ABOVE MEAN ANYTHING.

    Without this, ``test_dispatch_between_probe_and_act_voids_the_clearance``
    passes against an implementation that voids every clearance
    unconditionally. Both sides of the window are checked: a dispatch BEFORE
    the probe is already reflected in the probe's own reading, and a dispatch
    AFTER the act cannot have affected it."""
    clearance = a_clearance()
    before = lib.Event(kind=lib.KIND_DISPATCH, at=T0 - 1, actor="optimus",
                       detail="already running when the probe was taken")
    after = lib.Event(kind=lib.KIND_DISPATCH, at=T0 + 121, actor="optimus",
                      detail="dispatched after the act completed")

    act_at = T0 + 120
    assert lib.evaluate(clearance, [before], now=act_at).state == lib.VALID
    assert lib.evaluate(clearance, [after], now=act_at).state == lib.VALID
    assert lib.evaluate(clearance, [before, after], now=act_at).state == lib.VALID

    # And the boundary itself: an event exactly AT taken_at is outside
    # (already measured); an event exactly AT the act is inside.
    at_probe = lib.Event(kind=lib.KIND_DISPATCH, at=T0, actor="optimus")
    at_act = lib.Event(kind=lib.KIND_DISPATCH, at=act_at, actor="optimus")
    assert lib.evaluate(clearance, [at_probe], now=act_at).state == lib.VALID
    assert lib.evaluate(clearance, [at_act], now=act_at).state == lib.VOID

    # A non-invalidating event kind inside the window must NOT void either --
    # the discriminator is the kind, not merely "something happened".
    noise = lib.Event(kind="heartbeat", at=T0 + 60, actor="tarzan")
    assert lib.evaluate(clearance, [noise], now=act_at).state == lib.VALID


# --- the self-invalidation half, which is the interesting one ----------------

def test_a_readiness_announcement_by_the_holder_itself_voids_the_clearance():
    """The seat's own unblock report created the consumer that voided the
    seat's own clearance. An implementation that exempts self-caused events --
    a plausible reading of "guard against other lanes arriving" -- passes
    every other test in this file and does not fix this bead."""
    clearance = a_clearance()
    own_report = lib.Event(
        kind=lib.KIND_READINESS_ANNOUNCEMENT, at=T0 + 30, actor=HOLDER,
        detail="reported SABLE-pfbjw unblocked by the 52aym landing")

    verdict = lib.evaluate(clearance, [own_report], now=T0 + 90)
    assert verdict.state == lib.VOID
    assert verdict.voided_by is own_report
    assert "OWN HOLDER" in verdict.reason, \
        "a self-caused invalidation must be reported AS self-caused"

    # CONTROL: an identical announcement from another actor voids it too, so
    # the rule is "announcements void", not "only mine void" -- and the same
    # announcement outside the window still does not void.
    theirs = lib.Event(kind=lib.KIND_READINESS_ANNOUNCEMENT, at=T0 + 30,
                       actor="tarzan")
    assert lib.evaluate(clearance, [theirs], now=T0 + 90).state == lib.VOID
    stale = lib.Event(kind=lib.KIND_READINESS_ANNOUNCEMENT, at=T0 - 30,
                      actor=HOLDER)
    assert lib.evaluate(clearance, [stale], now=T0 + 90).state == lib.VALID

    # The announcement kind is in the DEFAULT invalidator set: a caller must
    # opt OUT of it deliberately, never forget to opt in.
    assert lib.KIND_READINESS_ANNOUNCEMENT in lib.DEFAULT_INVALIDATORS
    assert lib.KIND_READINESS_ANNOUNCEMENT in lib.render_clearance(clearance)


def test_the_interval_expires_on_its_own_and_reports_the_earlier_cause():
    """Time alone voids a clearance, with no event at all -- the interval is
    load-bearing, not decorative. And when both causes apply, the EARLIER one
    is reported: the honest answer to "why can't I act" is what happened
    first."""
    clearance = a_clearance()

    # CONTROL: exactly at valid_until is still valid (the boundary is not
    # quietly one-sided), one tick past it is not.
    assert lib.evaluate(clearance, [], now=T0 + WINDOW).state == lib.VALID
    expired = lib.evaluate(clearance, [], now=T0 + WINDOW + 1)
    assert expired.state == lib.VOID
    assert expired.cause == lib.CAUSE_INTERVAL_EXPIRED
    assert expired.voided_by is None
    assert "300s" in expired.reason

    # Event first, then expiry -> the event is the reported cause.
    early = lib.Event(kind=lib.KIND_DISPATCH, at=T0 + 10, actor="optimus")
    both = lib.evaluate(clearance, [early], now=T0 + WINDOW + 100)
    assert both.cause == lib.CAUSE_INTERVENING_EVENT

    # Expiry first, then a later event -> expiry is the reported cause.
    late = lib.Event(kind=lib.KIND_DISPATCH, at=T0 + WINDOW + 50,
                     actor="optimus")
    also = lib.evaluate(clearance, [late], now=T0 + WINDOW + 100)
    assert also.cause == lib.CAUSE_INTERVAL_EXPIRED

    # Evaluating before the clearance was taken is a caller bug, not a
    # verdict to be guessed at.
    with pytest.raises(ValueError):
        lib.evaluate(clearance, [], now=T0 - 1)


# --- the re-probe discipline -------------------------------------------------

def test_the_act_is_refused_without_a_reprobe_at_the_moment_of_the_act():
    """Re-probing is what turned the live instance into a message rather than
    an incident. This encodes it in the protocol instead of leaving it to seat
    discipline -- and forces the result to be REPORTED either way."""
    original = a_clearance()
    act_at = T0 + 200

    # No re-probe at all: refused even though the original is still VALID and
    # nothing has happened. "Nothing changed" is not knowable without looking.
    none_offered = lib.act_gate(original, None, [], now=act_at)
    assert none_offered.permitted is False
    assert "no re-probe" in none_offered.report

    # A "re-probe" that is actually just an old reading: refused. Calling a
    # stale clearance a re-probe only renames the problem.
    stale = lib.ProbeResult(taken_at=act_at - 60,
                            clearance=a_clearance(taken_at=act_at - 60))
    aged = lib.act_gate(original, stale, [], now=act_at)
    assert aged.permitted is False
    assert "tolerance" in aged.report

    # CONTROL: a genuine re-probe at the moment of the act PERMITS the act,
    # so the gate is not an always-refuse predicate.
    fresh = lib.ProbeResult(taken_at=act_at,
                            clearance=a_clearance(taken_at=act_at))
    ok = lib.act_gate(original, fresh, [], now=act_at)
    assert ok.permitted is True
    assert ok.changed is False
    assert "UNCHANGED" in ok.report, \
        "the re-probe result is reported out loud even when it did not change"

    # The re-probe came back NOT CLEAR: refused, CHANGED, and it NAMES what
    # it found. This is the live 2026-07-23 shape.
    busy = lib.ProbeResult(
        taken_at=act_at, clearance=None,
        matches=((4242, "bash hooks/test/test-pre-push-rebase-test.sh"),))
    changed = lib.act_gate(original, busy, [], now=act_at)
    assert changed.permitted is False
    assert changed.changed is True
    assert "CHANGED" in changed.report
    assert "test-pre-push-rebase-test.sh" in changed.report
    assert "4242" in changed.report

    # "did not look" and "looked and it was busy" are DIFFERENT refusals and
    # must not collapse into one -- that collapse is the zero-reading bug.
    assert none_offered.report != changed.report
    assert "no re-probe" not in changed.report

    # THE PERMIT DIRECTION IS TWO-WAY. A prior reading that has gone VOID but
    # whose re-probe comes back clear IS a permit; a gate that only ratchets
    # toward refusal is the always-refuse predicate wearing a re-probe.
    dispatch = lib.Event(kind=lib.KIND_DISPATCH, at=T0 + 100, actor="optimus",
                         detail="dispatched, then finished before the act")
    assert lib.evaluate(original, [dispatch], now=act_at).state == lib.VOID
    recovered = lib.act_gate(original, fresh, [dispatch], now=act_at)
    assert recovered.permitted is True
    assert recovered.changed is True
    assert "CHANGED" in recovered.report


# --- holds -------------------------------------------------------------------

def test_a_hold_is_issued_at_the_act_and_lifted_when_the_window_defers():
    """A stale clearance fails loudly at the moment of the act; a stale hold
    fails SILENTLY, by accruing blocked work nobody is measuring."""
    hold = lib.Hold(addressee="%14", scope_actors=("tarzan-manager", "%14",
                                                    "%16"),
                    disposition=lib.DISPOSITION_FINISH_IN_FLIGHT,
                    issued_at=T0)

    assert lib.hold_advice(lib.WINDOW_ACTING) == lib.ADVICE_IN_FORCE
    assert lib.hold_advice(lib.WINDOW_DEFERRED) == lib.ADVICE_LIFT_AND_REISSUE
    assert lib.hold_advice(lib.WINDOW_PENDING) == lib.ADVICE_PREMATURE

    deferred = lib.render_hold(hold, lib.WINDOW_DEFERRED)
    assert "LIFT AND RE-ISSUE" in deferred
    assert "%14" in deferred, "a hold must name its addressee in its own text"
    assert "tarzan-manager" in deferred, \
        "the scope must name the manager pane, not only its workers"
    assert lib.DISPOSITION_FINISH_IN_FLIGHT in deferred
    # Gap 1: the forward guarantee and the drain are SEPARATE facts.
    assert "no new starts" in deferred
    assert "UNMEASURED" in deferred, \
        "an unmeasured drain must be stated as unmeasured, not omitted"

    # CONTROL: with a measured drain and an active window the same renderer
    # reports IN FORCE and the measured quiet time -- it is not a renderer
    # that always warns.
    measured = lib.render_hold(
        lib.Hold(addressee="%14", scope_actors=("tarzan-manager", "%14"),
                 disposition=lib.DISPOSITION_STOP_NOW, issued_at=T0,
                 drain_eta=T0 + 840),
        lib.WINDOW_ACTING)
    assert "IN FORCE" in measured
    assert "UNMEASURED" not in measured
    assert "measured" in measured

    # A hold missing any of addressee / scope / disposition is refused at
    # construction: each omission made a real actor guess.
    with pytest.raises(lib.ClearanceScopeRequired):
        lib.Hold(addressee="", scope_actors=("%14",),
                 disposition=lib.DISPOSITION_STOP_NOW, issued_at=T0)
    with pytest.raises(lib.ClearanceScopeRequired):
        lib.Hold(addressee="%14", scope_actors=(),
                 disposition=lib.DISPOSITION_STOP_NOW, issued_at=T0)
    with pytest.raises(lib.ClearanceScopeRequired):
        lib.Hold(addressee="%14", scope_actors=("%14",),
                 disposition="be careful", issued_at=T0)


# --- probe internals ---------------------------------------------------------

def test_the_probe_excludes_itself_and_matches_as_a_fixed_string():
    """The probe is invoked WITH the search term on its own command line. A
    probe that does not exclude itself always finds exactly one match and can
    therefore never report CLEAR -- an always-refuse gate."""
    rows = lib.parse_ps(
        "  123 python3 bin/sable_quiescence_validity_lib.py probe --term MARK\n"
        "  456 bash /tmp/MARK.sh\n"
        "  789 sleep 300\n"
        "notapid garbage line\n"
        "\n")
    assert rows == [
        (123, "python3 bin/sable_quiescence_validity_lib.py probe --term MARK"),
        (456, "bash /tmp/MARK.sh"),
        (789, "sleep 300"),
    ], "parse_ps must drop unparseable lines rather than guess at them"

    hits = lib.matching_processes(rows, ("MARK",), exclude_pids=(123,))
    assert [pid for pid, _ in hits] == [456]

    # CONTROL: without the exclusion the probe's own line is a match, which is
    # exactly the always-refuse failure -- proving the exclusion discriminates.
    unfiltered = lib.matching_processes(rows, ("MARK",))
    assert [pid for pid, _ in unfiltered] == [123, 456]

    # CONTROL: a term matching nothing yields nothing, so the matcher is not
    # simply returning every row.
    assert lib.matching_processes(rows, ("no-such-marker",),
                                  exclude_pids=(123,)) == []

    # Terms are FIXED STRINGS, never regexes: a term copied out of a real
    # argv routinely carries '.', '*', '(' and would otherwise change meaning.
    dotted = lib.parse_ps("  11 bash /tmp/aXb.sh\n  12 bash /tmp/a.b.sh\n")
    assert [pid for pid, _ in lib.matching_processes(dotted, ("a.b",))] == [12]


def test_the_probe_walks_its_own_ancestry_and_discloses_what_it_excluded(tmp_path):
    """FOUND BY USING THE TOOL, not by reasoning about it: an agent harness
    runs commands as `bash -c '<the whole command line>'`, so the invoking
    SHELL's argv contains the search term verbatim and self-pid exclusion
    alone still matched. A probe that always finds itself can never clear.

    /proc is fabricated here so the chain shape is forced rather than
    observed -- including the awkward case the naive parser gets wrong."""
    def write_stat(pid, ppid, comm="bash"):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "stat").write_text(
            f"{pid} ({comm}) S {ppid} 1 1 0 -1 4194304 100 0 0 0 1 2 3 4\n")

    write_stat(400, 300)
    write_stat(300, 200)
    write_stat(200, 1)
    write_stat(1, 0, comm="systemd")

    assert lib.ancestor_pids(400, proc_root=str(tmp_path)) == (300, 200, 1)

    # A comm containing spaces AND parens: fields are taken after the LAST
    # ')', so this parses; splitting the whole line on whitespace would read
    # the wrong field and silently return a bogus parent.
    awkward = tmp_path / "500"
    awkward.mkdir()
    (awkward / "stat").write_text(
        "500 (my proc (odd)) S 300 1 1 0 -1 4194304 100 0 0 0 1 2 3 4\n")
    assert lib.ancestor_pids(500, proc_root=str(tmp_path)) == (300, 200, 1)

    # CONTROL: an unreadable /proc yields an EMPTY chain rather than a guess,
    # so the probe degrades to self-exclusion instead of excluding the world.
    assert lib.ancestor_pids(400, proc_root=str(tmp_path / "nope")) == ()

    # A cycle cannot hang the walk.
    cyc = tmp_path / "cyc"
    cyc.mkdir()
    for pid, ppid in ((10, 11), (11, 10)):
        d = cyc / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} (x) S {ppid} 1 1 0 -1 0 0 0 0 0 0 0\n")
    assert lib.ancestor_pids(10, proc_root=str(cyc)) == (11,)

    # And the disclosure: an excluded match is REPORTED, never dropped
    # silently. A bounded search that does not name its bound renders
    # "I did not look there" identically to "it is not there".
    def wrapper(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0,
            "  1 /sbin/init\n"
            "  4321 bash -c python3 probe --term WRAP\n", "")

    result = lib.probe(terms=("WRAP",), now=T0, validity_seconds=WINDOW,
                       holder=HOLDER, actor_scope=SCOPE, runner=wrapper,
                       exclude_pids=(4321,))
    assert result.clear is True, \
        "the probe's own wrapper shell must not block its own clearance"
    assert [pid for pid, _ in result.self_matches] == [4321]
    disclosure = lib.render_self_matches(result.self_matches)
    assert "4321" in disclosure and "own process ancestry" in disclosure

    # CONTROL: the same row from a pid that is NOT in the probe's ancestry is
    # a real consumer and DOES block -- the exclusion is not "ignore anything
    # that looks like a shell".
    other = lib.probe(terms=("WRAP",), now=T0, validity_seconds=WINDOW,
                      holder=HOLDER, actor_scope=SCOPE, runner=wrapper,
                      exclude_pids=(999,))
    assert other.clear is False
    assert [pid for pid, _ in other.matches] == [4321]
    assert other.self_matches == ()


def test_an_unreadable_process_table_is_an_error_not_a_clear_reading():
    """A ps that cannot run returns no rows, and no rows looks exactly like a
    quiet host. That is the zero-reading confusion sable-probe exists to
    refuse, one layer out."""
    def failing(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "ps: boom")

    with pytest.raises(RuntimeError, match="never a clear reading"):
        lib.run_ps(runner=failing)

    # CONTROL: a ps that DOES run is passed through untouched.
    def working(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "  1 /sbin/init\n", "")

    assert lib.run_ps(runner=working) == "  1 /sbin/init\n"


def test_a_clear_probe_hands_back_a_clearance_that_carries_its_own_expiry():
    """The probe cannot return a bare 'nothing found' -- the artefact with no
    shelf life is precisely what this bead exists to delete."""
    def quiet(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "  1 /sbin/init\n", "")

    result = lib.probe(
        terms=("no-such-marker",), now=T0, validity_seconds=WINDOW,
        holder=HOLDER, actor_scope=SCOPE, runner=quiet, exclude_pids=())
    assert result.clear is True and result.matches == ()
    assert result.clearance is not None
    assert result.clearance.valid_until == T0 + WINDOW
    assert lib.KIND_READINESS_ANNOUNCEMENT in result.clearance.invalidators

    # CONTROL: a busy table returns NO clearance at all, so the probe is not
    # minting clearances unconditionally.
    def busy(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0, "  1 /sbin/init\n  42 bash /tmp/no-such-marker.sh\n", "")

    result = lib.probe(
        terms=("no-such-marker",), now=T0, validity_seconds=WINDOW,
        holder=HOLDER, actor_scope=SCOPE, runner=busy, exclude_pids=())
    assert result.clear is False and result.clearance is None
    assert [pid for pid, _ in result.matches] == [42]
    assert "42" in lib.render_not_clear(("no-such-marker",), result.matches)


def test_the_protocol_text_states_every_rule_this_bead_bought():
    """The protocol is a shipped artefact, not a comment: it is what a seat
    reads at 3am. Each clause below was paid for by a live instance."""
    text = lib.PROTOCOL
    assert "RE-PROBE AT THE MOMENT OF THE ACT" in text
    assert "WHETHER OR NOT IT CHANGED" in text
    assert "READINESS / UNBLOCK ANNOUNCEMENT IS A CLEARANCE-INVALIDATING" in text
    assert "LIFT the hold" in text
    assert "acting-now signal" in text
    assert "manager pane" in text
    assert "WORK ALREADY IN FLIGHT" in text
    assert "SEPARATELY" in text
