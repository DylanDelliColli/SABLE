#!/usr/bin/env python3
"""sable_quiescence_validity_lib — a quiescence clearance is a STATEMENT ABOUT
A MOMENT, NOT A PERMISSION THAT PERSISTS (SABLE-r0mzn).

THE DEFECT THIS EXISTS FOR
--------------------------
A quiescence probe answers "is anything live that would be hurt if I act?".
The probe is fine. What is missing is that ITS ANSWER HAS NO STATED SHELF
LIFE. `sable-probe` (SABLE-xhrt0) made a probe's ZERO trustworthy at the
moment it was taken; nothing then said how long that zero remains true.

Live instance, 2026-07-23 hook-refresh window. An event-scoped probe naming
the exact strings at risk (`git push`, `pre-push`, `pre-push-rebase-test`,
`tripwire-watcher`) read ZERO fleet-wide. THAT READING WAS CORRECT WHEN
TAKEN, and re-running it today returns the same answer. At the moment of
acting, a re-probe found THREE live processes running the suite that
references tripwire 22 times, out of a worktree that DID NOT EXIST when the
first probe ran. Nobody measured wrong. Nothing in either output said the
first had expired.

THE PART THAT MAKES THIS MORE THAN BAD LUCK
-------------------------------------------
*** THE SEAT'S OWN UNBLOCK REPORT CREATED THE CONSUMER THAT VOIDED THE
SEAT'S OWN CLEARANCE. *** The seat reported a bead unblocked; a manager
dispatched on that report; that worker became a live consumer of the exact
file the window was going to swap. So the cleared population does not merely
GROW — its growth is CAUSED BY THE ACT OF ANNOUNCING READINESS. A clearance
taken before such an announcement is invalidated BY THE ANNOUNCEMENT ITSELF,
deterministically.

That is why `evaluate` NEVER exempts an event because the clearance holder
caused it. A design that only guards against outsiders arriving does not fix
this bead: the archetypal invalidator here is self-inflicted. Same shape as
SABLE-wvxb4's spawn-rebase — the act of preparing changed the thing that had
just been verified.

WHAT THIS MODULE ADDS
---------------------
  1. A `Clearance` that CANNOT BE CONSTRUCTED without an explicit validity
     interval, an explicit invalidating-event list, and an explicit actor
     scope. There is no default-to-unbounded: an unbounded clearance is the
     bug, so it is rejected at construction rather than rendered.
  2. `evaluate(clearance, events, now)` — VOID if an invalidating event fell
     INSIDE the window (taken_at, now], or if the interval ran out; VALID
     otherwise. Events outside the window do not void, which is what keeps
     this from degenerating into the SABLE-47try always-refuse predicate:
     a gate that never clears is indistinguishable from correct caution and
     is strictly worse than the gap it replaced.
  3. `act_gate(...)` — the act is PERMITTED only against a re-probe taken at
     the moment of the act, and it always emits a report line whether or not
     the verdict changed. An unreported re-probe is indistinguishable from an
     unperformed one (the SABLE-4jogz shape).
  4. `Hold` — a hold is issued AT the act, names its addressee, says what to
     do with work ALREADY IN FLIGHT, and reports its measured drain ETA
     SEPARATELY from its forward "no new starts" guarantee. A stale clearance
     fails loudly at the moment of the act; A STALE HOLD FAILS SILENTLY, by
     accruing blocked work nobody is measuring.

CLOCKS ARE INJECTED, NEVER READ, in every decision function here. A test that
sleeps through a validity interval is slow and flaky, and a decision function
that reads the wall clock cannot be forced into its own boundary cases.

EXIT CODES (CLI)
  0  CLEAR      — nothing matching is live; a Clearance is rendered WITH its
                  validity interval and invalidators.
  1  NOT CLEAR  — live matches, each one named.
  2  usage error (including a missing --validity-seconds: the CLI refuses to
                  invent an interval for the same reason the constructor does)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass

# --- vocabulary --------------------------------------------------------------

KIND_DISPATCH = "dispatch"
KIND_READINESS_ANNOUNCEMENT = "readiness-announcement"

# The default invalidator set deliberately includes the announcement kind.
# Announcing readiness is a STATE CHANGE, not a report: it is the event that
# manufactures the consumer whose absence was just measured (SABLE-r0mzn).
DEFAULT_INVALIDATORS = (KIND_DISPATCH, KIND_READINESS_ANNOUNCEMENT)

VALID = "valid"
VOID = "void"

CAUSE_NONE = "none"
CAUSE_INTERVENING_EVENT = "intervening-event"
CAUSE_INTERVAL_EXPIRED = "interval-expired"

# Hold dispositions. A hold that does not say which of these it means forces
# the addressee to GUESS about work already running (SABLE-r0mzn gap 3
# corollary: a worker read "do not run whole-directory suites" as "kill the
# one in flight" and aborted ~9 minutes of gate-class verification that the
# merge seat had explicitly decided to spend).
DISPOSITION_FINISH_IN_FLIGHT = "finish-in-flight"
DISPOSITION_STOP_NOW = "stop-now"
DISPOSITIONS = (DISPOSITION_FINISH_IN_FLIGHT, DISPOSITION_STOP_NOW)

WINDOW_PENDING = "pending"
WINDOW_ACTING = "acting"
WINDOW_DEFERRED = "deferred"
WINDOW_STATES = (WINDOW_PENDING, WINDOW_ACTING, WINDOW_DEFERRED)

ADVICE_IN_FORCE = "in-force"
ADVICE_PREMATURE = "premature"
ADVICE_LIFT_AND_REISSUE = "lift-and-reissue"

# How stale a "re-probe at the moment of the act" is allowed to be before it
# stops being a re-probe and becomes just another aging clearance.
REPROBE_TOLERANCE_SECONDS = 5.0


class ClearanceIntervalRequired(ValueError):
    """Raised when a clearance is built without a usable validity interval.
    Rejecting at CONSTRUCTION is the whole point: an unbounded clearance is
    exactly the artefact this bead says must not exist, so it must never
    become a value that other code can hold and later render."""


class ClearanceScopeRequired(ValueError):
    """Raised when a clearance names no invalidating events or no actor
    scope. A clearance is correct only FOR the population it measured, and
    requesters self-select for caution while causers do not -- so the actor
    set has to be stated by the taker, not inferred by the reader."""


# --- the clearance -----------------------------------------------------------

@dataclass(frozen=True)
class Event:
    """A coordination event that may or may not void a clearance.

    `actor` is recorded for the report and is DELIBERATELY NOT CONSULTED when
    deciding whether the event voids: the archetypal invalidator in this bead
    was caused by the clearance holder itself."""
    kind: str
    at: float
    actor: str = ""
    detail: str = ""


@dataclass(frozen=True)
class Clearance:
    subject: str
    holder: str
    taken_at: float
    validity_seconds: float
    invalidators: tuple[str, ...] = DEFAULT_INVALIDATORS
    actor_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.validity_seconds is None:
            raise ClearanceIntervalRequired(
                f"clearance for {self.subject!r} has no validity interval -- "
                "a clearance with no stated shelf life is the defect, not a "
                "convenience default")
        try:
            seconds = float(self.validity_seconds)
        except (TypeError, ValueError) as exc:
            raise ClearanceIntervalRequired(
                f"clearance for {self.subject!r}: validity interval "
                f"{self.validity_seconds!r} is not a number") from exc
        if math.isnan(seconds) or math.isinf(seconds):
            raise ClearanceIntervalRequired(
                f"clearance for {self.subject!r}: validity interval "
                f"{self.validity_seconds!r} is not finite -- an infinite "
                "interval is an unbounded clearance wearing a number")
        if seconds <= 0:
            raise ClearanceIntervalRequired(
                f"clearance for {self.subject!r}: validity interval must be "
                f"positive, got {seconds}")
        if not self.invalidators:
            raise ClearanceScopeRequired(
                f"clearance for {self.subject!r} names no invalidating "
                "events -- an interval alone does not say what ELSE ends it")
        if not self.actor_scope:
            raise ClearanceScopeRequired(
                f"clearance for {self.subject!r} names no actor scope -- the "
                "correct scope is the set of actors that can touch the "
                "artifact, which is rarely the set that asked about it")

    @property
    def valid_until(self) -> float:
        return self.taken_at + float(self.validity_seconds)


@dataclass(frozen=True)
class Verdict:
    state: str
    cause: str
    reason: str
    voided_by: Event | None = None


def _stamp(t: float) -> str:
    """Deterministic rendering of an injected timestamp -- derived only from
    the value passed in, never from the ambient clock, so a fixture time
    renders identically in every run."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def render_clearance(clearance: Clearance) -> str:
    """The clearance line. States ALL THREE of the things a bare probe output
    omitted: when it was taken, how long it is good for, and what ends it
    early. A reader must never have to ask "as of when?" of a clearance."""
    return (
        f"CLEAR as of t={clearance.taken_at:g} ({_stamp(clearance.taken_at)}) "
        f"for {clearance.subject!r}; "
        f"VALID UNTIL t={clearance.valid_until:g} "
        f"({_stamp(clearance.valid_until)}), i.e. "
        f"{float(clearance.validity_seconds):g}s, OR until any of these "
        f"occurs, whichever is sooner: "
        f"{', '.join(clearance.invalidators)}. "
        f"Actor scope: {', '.join(clearance.actor_scope)}. "
        f"Taken by {clearance.holder}. "
        "An invalidating event voids this clearance even when this holder "
        "caused it -- announcing readiness is a state change, not a report."
    )


def intervening_events(clearance: Clearance, events, now: float) -> list[Event]:
    """Invalidating events that landed INSIDE the window, oldest first.

    The window is the half-open interval (taken_at, now]: an event at or
    before the probe is already reflected IN the probe's reading, and an
    event after the act cannot have affected it. Bounding it on BOTH sides is
    what keeps this from becoming an always-refuse predicate -- see
    ``test_dispatch_outside_the_window_does_not_void_the_clearance``."""
    hits = [e for e in events
            if e.kind in clearance.invalidators
            and clearance.taken_at < e.at <= now]
    return sorted(hits, key=lambda e: e.at)


def evaluate(clearance: Clearance, events, now: float) -> Verdict:
    """Is this clearance still true AT `now`? Two independent ways to lose
    it, and the EARLIER cause is the one reported -- the honest answer to
    "why can't I act" is whatever actually happened first."""
    if now < clearance.taken_at:
        raise ValueError(
            f"now={now!r} precedes the clearance's taken_at="
            f"{clearance.taken_at!r}; a clearance cannot be evaluated before "
            "it was taken")

    hits = intervening_events(clearance, events, now)
    first_event = hits[0] if hits else None
    expired_at = clearance.valid_until if now > clearance.valid_until else None

    if first_event is not None and (expired_at is None
                                     or first_event.at <= expired_at):
        self_caused = (first_event.actor
                       and first_event.actor == clearance.holder)
        origin = ("BY THIS CLEARANCE'S OWN HOLDER"
                  if self_caused else f"by {first_event.actor or 'an actor'}")
        return Verdict(
            VOID, CAUSE_INTERVENING_EVENT,
            f"voided: {first_event.kind} at t={first_event.at:g} "
            f"({_stamp(first_event.at)}), caused {origin}"
            + (f" -- {first_event.detail}" if first_event.detail else "")
            + f"; inside the window (t={clearance.taken_at:g}, t={now:g}]",
            first_event)

    if expired_at is not None:
        return Verdict(
            VOID, CAUSE_INTERVAL_EXPIRED,
            f"voided: stated validity interval of "
            f"{float(clearance.validity_seconds):g}s ran out at "
            f"t={expired_at:g} ({_stamp(expired_at)}); the act is at "
            f"t={now:g}, {now - expired_at:g}s late",
            None)

    return Verdict(
        VALID, CAUSE_NONE,
        f"still valid at t={now:g}: no {'/'.join(clearance.invalidators)} "
        f"inside (t={clearance.taken_at:g}, t={now:g}], and "
        f"{clearance.valid_until - now:g}s of the stated interval remain",
        None)


def render_verdict(clearance: Clearance, verdict: Verdict) -> str:
    head = "VALID" if verdict.state == VALID else "VOID"
    return f"{head} [{verdict.cause}] {clearance.subject!r}: {verdict.reason}"


# --- the re-probe discipline -------------------------------------------------

@dataclass(frozen=True)
class ProbeResult:
    """What a probe actually returns: EITHER a Clearance (nothing live) OR an
    explicit NOT-CLEAR naming the live matches.

    This type exists because a bare ``None`` cannot tell "I did not look"
    apart from "I looked and the host was busy" -- the same zero-reading
    confusion sable-probe refuses one layer down, and the difference between
    a missing re-probe and a failed one is the difference between a protocol
    breach and a correctly closed window.

    `self_matches` are rows that matched but belong to THIS PROBE'S OWN
    PROCESS ANCESTRY. They are reported rather than dropped: an exclusion
    nobody can see is how a probe quietly stops being able to find things."""
    taken_at: float
    clearance: Clearance | None = None
    matches: tuple[tuple[int, str], ...] = ()
    self_matches: tuple[tuple[int, str], ...] = ()

    @property
    def clear(self) -> bool:
        return self.clearance is not None


@dataclass(frozen=True)
class ActReport:
    permitted: bool
    changed: bool
    report: str


def act_gate(original: Clearance, reprobe: ProbeResult | None, events,
             now: float,
             tolerance: float = REPROBE_TOLERANCE_SECONDS) -> ActReport:
    """Gate the ACT on a re-probe taken at the moment of the act.

    Four refusals, in order of how quietly they would otherwise pass:
      - NO re-probe at all. The 2026-07-23 window is a message rather than an
        incident ONLY because the seat re-probed; leaving that to seat
        discipline is what this function removes.
      - A re-probe that is itself stale by more than `tolerance`. Calling an
        old reading a re-probe just renames the problem.
      - A re-probe that came back NOT CLEAR (live matches).
      - A re-probe that cleared, but was voided by an event before the act.

    The report is emitted in ALL cases, and says explicitly whether the
    verdict CHANGED -- an unreported re-probe is indistinguishable from an
    unperformed one. NOTE the permit direction is genuinely two-way: a prior
    reading that has gone VOID but whose re-probe comes back clear IS a
    permit. A gate that only ever ratchets toward refusal is the SABLE-47try
    always-refuse predicate."""
    before = evaluate(original, events, now)

    if reprobe is None:
        return ActReport(
            False, False,
            "RE-PROBE REPORT (mandatory): REFUSED -- no re-probe was taken at "
            f"the moment of the act (t={now:g}). The prior reading for "
            f"{original.subject!r} was {before.state.upper()} as of "
            f"t={original.taken_at:g}; a prior reading is not a re-probe.")

    age = now - reprobe.taken_at
    if age > tolerance or age < 0:
        return ActReport(
            False, False,
            "RE-PROBE REPORT (mandatory): REFUSED -- the offered re-probe was "
            f"taken at t={reprobe.taken_at:g}, {age:g}s from the act at "
            f"t={now:g}, outside the {tolerance:g}s "
            "at-the-moment-of-the-act tolerance.")

    if not reprobe.clear:
        named = "; ".join(f"pid {pid}: {argv}" for pid, argv in reprobe.matches)
        changed = before.state == VALID
        changed_word = "CHANGED" if changed else "UNCHANGED"
        return ActReport(
            False, changed,
            f"RE-PROBE REPORT (mandatory, {changed_word}): "
            f"prior {before.state.upper()} taken t={original.taken_at:g} -> "
            f"re-probe NOT CLEAR at t={reprobe.taken_at:g}; live now: "
            f"{named or 'unnamed matches'}")

    after = evaluate(reprobe.clearance, events, now)
    changed = after.state != before.state
    changed_word = "CHANGED" if changed else "UNCHANGED"
    return ActReport(
        after.state == VALID, changed,
        f"RE-PROBE REPORT (mandatory, {changed_word}): "
        f"prior {before.state.upper()} taken t={original.taken_at:g} -> "
        f"re-probe {after.state.upper()} taken t={reprobe.taken_at:g}; "
        f"{after.reason}")


# --- holds -------------------------------------------------------------------

@dataclass(frozen=True)
class Hold:
    addressee: str
    scope_actors: tuple[str, ...]
    disposition: str
    issued_at: float
    drain_eta: float | None = None

    def __post_init__(self) -> None:
        if not self.addressee:
            raise ClearanceScopeRequired(
                "a hold must name its addressee -- any decision that narrows "
                "or lifts it has to reach the same actor, and re-deciding "
                "between coordinators does not")
        if not self.scope_actors:
            raise ClearanceScopeRequired(
                "a hold must name its scope, INCLUDING the manager pane -- a "
                "manager scopes to the actors it MANAGES rather than to the "
                "actors that can EXEC the artifact, and is structurally the "
                "one actor it cannot see from inside")
        if self.disposition not in DISPOSITIONS:
            raise ClearanceScopeRequired(
                f"hold disposition must be one of {DISPOSITIONS}, got "
                f"{self.disposition!r} -- 'finish what is running, start "
                "nothing new' and 'stop now' are different instructions and "
                "an unstated one makes the addressee guess")


def hold_advice(window_state: str) -> str:
    """A hold ages exactly like the clearance it protects, and it fails in the
    worse direction: a stale clearance fails LOUDLY at the moment of the act,
    a stale hold fails SILENTLY by accruing blocked work nobody measures."""
    if window_state not in WINDOW_STATES:
        raise ValueError(f"window_state must be one of {WINDOW_STATES}, "
                         f"got {window_state!r}")
    if window_state == WINDOW_ACTING:
        return ADVICE_IN_FORCE
    if window_state == WINDOW_DEFERRED:
        return ADVICE_LIFT_AND_REISSUE
    return ADVICE_PREMATURE


def render_hold(hold: Hold, window_state: str) -> str:
    """States the three facts a lane clearance needs, NONE of which implies
    the others: the scope, the forward 'no new starts' guarantee, and the
    MEASURED in-flight drain time. A hold reported without (3) is a promise
    about the future presented as a measurement of the present."""
    advice = hold_advice(window_state)
    if hold.drain_eta is None:
        drain = ("in-flight drain: UNMEASURED -- 'no new starts' is a FORWARD "
                 "guarantee only; work already running is untouched by this "
                 "hold and this lane is NOT yet quiet")
    else:
        drain = (f"in-flight drain: measured, quiet at t={hold.drain_eta:g} "
                 f"({_stamp(hold.drain_eta)})")
    guidance = {
        ADVICE_IN_FORCE: "IN FORCE -- the window is being acted on now",
        ADVICE_LIFT_AND_REISSUE: (
            "LIFT AND RE-ISSUE -- the window deferred, so this hold is now "
            "freezing a lane for a window that may never come; re-issue on an "
            "explicit acting-now signal"),
        ADVICE_PREMATURE: (
            "PREMATURE -- issue holds AT the moment of the act, not "
            "speculatively ahead of a window that may slip"),
    }[advice]
    return (
        f"HOLD [{advice}] addressee={hold.addressee}; "
        f"scope={', '.join(hold.scope_actors)}; "
        f"disposition={hold.disposition}; "
        f"issued t={hold.issued_at:g} ({_stamp(hold.issued_at)}); "
        f"forward guarantee: no new starts; {drain}. {guidance}"
    )


# --- the protocol text -------------------------------------------------------

PROTOCOL = """\
SABLE WINDOW PROTOCOL — quiescence clearances have a shelf life (SABLE-r0mzn)

1. STATE THE INTERVAL. Every clearance says when it was taken, how long it is
   good for, what actor scope it covers, and which events end it early. A
   clearance with no interval is refused at construction, not defaulted to
   unbounded.

2. RE-PROBE AT THE MOMENT OF THE ACT. Mandatory, never optional. The actor
   REPORTS the re-probe result out loud WHETHER OR NOT IT CHANGED — an
   unreported re-probe is indistinguishable from an unperformed one.

3. A READINESS / UNBLOCK ANNOUNCEMENT IS A CLEARANCE-INVALIDATING EVENT.
   Reporting work as available is a dispatch-generating state change, not a
   report. Any clearance taken BEFORE such an announcement is void; re-take it
   after. This holds even when the announcement came from the clearance
   holder itself — that is the ordinary case, not the exotic one.

4. SCOPE TO THE CAUSERS, NOT THE ASKERS. A window request goes to the set of
   actors that can exec/import/pull the artifact, and the broker states that
   derivation. Requesters self-select for caution; causers do not. The scope
   includes the manager pane, not only the workers it manages.

5. ISSUE HOLDS AT THE MOMENT OF THE ACT. A hold ages like the clearance but
   fails silently rather than loudly. A deferred window means LIFT the hold
   and re-issue on an explicit acting-now signal. Every hold names an
   addressee, and any decision that narrows or lifts it must reach that same
   addressee before it counts as in force.

6. A HOLD SAYS WHAT TO DO WITH WORK ALREADY IN FLIGHT. 'finish what is
   running, start nothing new' and 'stop now' are different instructions.
   A hold's forward 'no new starts' guarantee is reported SEPARATELY from the
   MEASURED in-flight drain time; a clearance missing the drain measurement is
   a promise about the future presented as a measurement of the present.

7. PREFER RE-PROBE OVER DISPATCH-FREEZE. Freezing a lane to keep a population
   static costs throughput continuously; a re-probe costs one command at the
   moment it matters.
"""


# --- the process probe -------------------------------------------------------

# -ww defeats ps's width truncation, which would otherwise silently cut a long
# argv and turn a real match into a zero reading — the exact failure class
# sable-probe exists to refuse.
PS_ARGV = ("ps", "-eo", "pid=,args=", "-ww")


def parse_ps(text: str) -> list[tuple[int, str]]:
    """`ps -eo pid=,args=` output -> [(pid, argv-string)]. Lines that do not
    begin with a pid are dropped rather than guessed at."""
    rows: list[tuple[int, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        head, _, rest = stripped.partition(" ")
        try:
            pid = int(head)
        except ValueError:
            continue
        rest = rest.strip()
        if rest:
            rows.append((pid, rest))
    return rows


def matching_processes(rows, terms, exclude_pids=()) -> list[tuple[int, str]]:
    """Fixed-string (never regex) match of any term against each argv.

    `exclude_pids` MUST include the probing process itself: the probe is
    invoked WITH the search term on its own command line, so a probe that does
    not exclude itself always finds exactly one match and can therefore never
    report CLEAR — an always-refuse gate, which is the SABLE-47try erosion
    rather than a safety feature."""
    excluded = set(exclude_pids)
    return [(pid, argv) for pid, argv in rows
            if pid not in excluded
            and any(term in argv for term in terms)]


def _ppid_from_proc(pid: int, proc_root: str = "/proc") -> int | None:
    """Parent pid from /proc/<pid>/stat. The comm field is parenthesised and
    may itself contain spaces and parens, so fields are taken after the LAST
    ')' -- splitting the whole line on whitespace mis-indexes for any process
    whose name has a space in it."""
    try:
        with open(f"{proc_root}/{pid}/stat", encoding="utf-8",
                  errors="replace") as handle:
            data = handle.read()
    except OSError:
        return None
    close = data.rfind(")")
    if close == -1:
        return None
    fields = data[close + 1:].split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def ancestor_pids(pid: int, proc_root: str = "/proc",
                  limit: int = 64) -> tuple[int, ...]:
    """This probe's own process stack, walked to init.

    WHY THIS IS NEEDED AND WAS FOUND BY USING THE TOOL (SABLE-r0mzn): an
    agent harness runs commands as `bash -c '<the whole command line>'`, so
    the INVOKING SHELL's argv contains the search term verbatim. Self-pid
    exclusion alone therefore still matched -- a live probe read NOT CLEAR
    against its own wrapper shell. A probe that always finds itself can never
    clear, which is the always-refuse failure by a different route.

    The chain is the measuring instrument, not the measured population. It is
    bounded (`limit`) and loop-guarded because a corrupt/racing /proc read
    must not hang the probe, and it degrades to just the pid where /proc is
    unavailable rather than guessing."""
    chain: list[int] = []
    seen = {pid}
    current = pid
    for _ in range(limit):
        parent = _ppid_from_proc(current, proc_root)
        if parent is None or parent <= 0 or parent in seen:
            break
        chain.append(parent)
        seen.add(parent)
        current = parent
    return tuple(chain)


def run_ps(runner=None) -> str:
    """Process boundary. `SABLE_QUIESCENCE_PS` overrides the command so a
    fault-injection test can supply a failing ps; a ps that cannot run is an
    error, never an empty (and therefore CLEAR-looking) reading."""
    override = os.environ.get("SABLE_QUIESCENCE_PS")
    argv = override.split() if override else list(PS_ARGV)
    run = runner or subprocess.run
    proc = run(argv, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ps failed (rc={proc.returncode}): {proc.stderr.strip()!r} -- "
            "an unreadable process table is COULD-NOT-ASSESS, never a "
            "clear reading")
    return proc.stdout


def probe(terms, now: float, validity_seconds: float, holder: str,
          actor_scope, subject: str | None = None, runner=None,
          exclude_pids=None) -> ProbeResult:
    """Take an event-scoped quiescence probe. On CLEAR the result carries a
    Clearance with its own expiry — the probe cannot hand back a bare
    'nothing found', which is the artefact that had no shelf life."""
    rows = parse_ps(run_ps(runner=runner))
    if exclude_pids is None:
        me = os.getpid()
        excluded = frozenset((me,) + ancestor_pids(me))
    else:
        excluded = frozenset(exclude_pids)
    every = matching_processes(rows, terms, ())
    hits = tuple(row for row in every if row[0] not in excluded)
    mine = tuple(row for row in every if row[0] in excluded)
    if hits:
        return ProbeResult(taken_at=now, clearance=None, matches=hits,
                           self_matches=mine)
    clearance = Clearance(
        subject=subject or ", ".join(terms),
        holder=holder,
        taken_at=now,
        validity_seconds=validity_seconds,
        invalidators=DEFAULT_INVALIDATORS,
        actor_scope=tuple(actor_scope),
    )
    return ProbeResult(taken_at=now, clearance=clearance, self_matches=mine)


def render_self_matches(self_matches) -> str:
    """Disclosure, not a footnote. Every bounded search converts "I did not
    look there" into "it is not there" and renders them identically; naming
    what the bound removed is the only thing that keeps this exclusion from
    becoming that bug."""
    if not self_matches:
        return ""
    lines = [f"  (excluded {len(self_matches)} match(es) as this probe's own "
             "process ancestry -- the instrument, not the population:)"]
    lines += [f"    pid {pid}: {argv}" for pid, argv in self_matches]
    return "\n" + "\n".join(lines)


def render_not_clear(terms, hits, self_matches=()) -> str:
    lines = [f"NOT CLEAR: {len(hits)} live process(es) match "
             f"{', '.join(repr(t) for t in terms)} -- no clearance issued"]
    lines += [f"  pid {pid}: {argv}" for pid, argv in hits]
    return "\n".join(lines) + render_self_matches(self_matches)


# --- CLI ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sable_quiescence_validity_lib.py",
        description="Take a quiescence probe that STATES its validity "
                    "interval and its invalidating conditions, or print the "
                    "window protocol (SABLE-r0mzn).")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probe", help="event-scoped process probe")
    p.add_argument("--term", action="append", required=True, dest="terms",
                   help="fixed string to look for in process argv "
                        "(repeatable)")
    # No default. The CLI refuses to invent an interval for the same reason
    # Clearance.__post_init__ does.
    p.add_argument("--validity-seconds", type=float, required=True,
                   dest="validity_seconds",
                   help="REQUIRED. How long this clearance is good for; "
                        "there is no unbounded default")
    p.add_argument("--holder", required=True,
                   help="the seat taking the clearance")
    p.add_argument("--actor-scope", required=True, dest="actor_scope",
                   help="comma-separated actor set that can touch the "
                        "artifact -- the causers, not the askers")
    p.add_argument("--subject", default=None,
                   help="what is being cleared (default: the terms)")
    p.add_argument("--format", choices=["text", "json"], default="text")

    sub.add_parser("protocol", help="print the window protocol")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "protocol":
        print(PROTOCOL, end="")
        return 0

    scope = tuple(s.strip() for s in args.actor_scope.split(",") if s.strip())
    if not scope:
        parser.error("--actor-scope must name at least one actor")

    try:
        result = probe(
            terms=tuple(args.terms), now=time.time(),
            validity_seconds=args.validity_seconds, holder=args.holder,
            actor_scope=scope, subject=args.subject)
    except (ClearanceIntervalRequired, ClearanceScopeRequired) as exc:
        parser.error(str(exc))
        return 2  # pragma: no cover - parser.error raises SystemExit

    self_rows = [{"pid": pid, "argv": argv_s}
                 for pid, argv_s in result.self_matches]
    if result.clear:
        clearance = result.clearance
        message = render_clearance(clearance) + render_self_matches(
            result.self_matches)
        payload = {"clear": True, "terms": list(args.terms),
                   "taken_at": clearance.taken_at,
                   "valid_until": clearance.valid_until,
                   "validity_seconds": float(clearance.validity_seconds),
                   "invalidators": list(clearance.invalidators),
                   "actor_scope": list(clearance.actor_scope),
                   "matches": [], "self_matches": self_rows}
        rc = 0
    else:
        message = render_not_clear(args.terms, result.matches,
                                    result.self_matches)
        payload = {"clear": False, "terms": list(args.terms),
                   "matches": [{"pid": pid, "argv": argv_s}
                               for pid, argv_s in result.matches],
                   "self_matches": self_rows}
        rc = 1

    print(json.dumps({**payload, "message": message})
          if args.format == "json" else message)
    return rc


if __name__ == "__main__":
    sys.exit(main())
