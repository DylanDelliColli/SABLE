#!/usr/bin/env python3
"""Integration tests for the window RE-PROBE discipline (SABLE-r0mzn).

WHY THIS IS PYTHON AND NOT hooks/test/test-window-reprobe.sh
------------------------------------------------------------
Two independent reasons, in the order that matters.

FIRST, ON THE MERITS: the behaviour under test is not shell-specific. It is a
real `ps` sweep for event-scoped terms, argv self-exclusion, and CLI exit
codes -- a process boundary that pytest driving real subprocesses exercises
exactly as well as bash would, with better control over teardown of the
marker processes.

SECOND, AND SEPARATELY: .github/ci/shell-run-set.sh is under a human-gated
hold, and every new hooks/test/*.sh suite forces a companion edit there
because the classification is mandatory and fail-closed (SABLE-lcevs). A
Python test is auto-discovered and needs no classification, so the conflict
does not arise. Filed as SABLE-m71wa: the shell-suite footprint is incomplete
by construction, which is why the collision surfaced mid-task and not at
dispatch.

WHAT IS REAL HERE AND WHAT IS INJECTED
--------------------------------------
REAL: the marker processes (actually spawned, actually reaped), the actual
system process table read by the actual `ps` the tool shells out to, the
filesystem path that carries the marker, and the CLI invoked as a real
subprocess with its real exit codes. Nothing about the probe is mocked --
mocking the thing under test is what makes an integration test worthless.

INJECTED: the clock and the coordination-event list, in the unit suite only.
Sleeping through a real validity interval would be slow, flaky, AND a load
event on a shared host. Nothing here manufactures host load: the marker set
is one idle sleeping process at a time, torn down in a finally.

HERMETICITY (SABLE-b0w8k): every marker string is unique per run -- pid plus
random hex -- never a shared fixture constant, so two of these suites running
concurrently in different worktrees cannot see each other's processes.
"""
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent
LIB = BIN / "sable_quiescence_validity_lib.py"

sys.path.insert(0, str(BIN))
import sable_quiescence_validity_lib as lib  # noqa: E402

HOLDER = "chuck"
SCOPE = "chuck,optimus,tarzan"
VALIDITY = "300"

# Bounded polls. A real process takes milliseconds to appear in / vanish from
# the process table; these are ceilings that fail loudly, not sleeps.
APPEAR_TIMEOUT = 15.0
POLL_INTERVAL = 0.05


def unique_marker() -> str:
    """Unique per run AND per call. A shared fixture string would make two
    concurrent runs of this suite invalidate each other's clearances -- which
    would be an ironically on-topic flake."""
    return f"sable-r0mzn-marker-{os.getpid()}-{uuid.uuid4().hex[:10]}"


def run_probe(marker: str, *extra: str) -> subprocess.CompletedProcess:
    """Invoke the real CLI as a real subprocess. Note the marker is on THIS
    command line too -- the probe must exclude itself or it can never clear."""
    return subprocess.run(
        [sys.executable, str(LIB), "probe", "--term", marker,
         "--validity-seconds", VALIDITY, "--holder", HOLDER,
         "--actor-scope", SCOPE, *extra],
        text=True, capture_output=True, timeout=60)


def spawn_marker_process(tmp_path: Path, marker: str) -> subprocess.Popen:
    """A REAL process carrying the marker in its argv, via a real file on
    disk -- the same shape as the live instance, where the consumer was
    identified by the test-script path in its command line.

    start_new_session puts it in its own process group so teardown can reap
    the whole tree; an orphaned `sleep` would otherwise outlive the test."""
    script = tmp_path / f"{marker}.sh"
    script.write_text("#!/usr/bin/env bash\n# SABLE-r0mzn marker\nsleep 45\n")
    script.chmod(0o755)
    return subprocess.Popen(["bash", str(script)], start_new_session=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def reap(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # pragma: no cover
        proc.kill()
    proc.wait(timeout=15)


def wait_until(predicate, timeout: float = APPEAR_TIMEOUT) -> bool:
    """Poll a REAL condition to a bounded ceiling. Returns whether it held;
    callers assert on that so a timeout fails loudly instead of drifting into
    a wrong verdict."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(POLL_INTERVAL)
    return predicate()


def marker_live_in_real_ps(marker: str, pid: int) -> bool:
    """Reads the ACTUAL system process table through the tool's own ps path,
    so the test and the tool agree on what 'live' means."""
    rows = lib.parse_ps(lib.run_ps())
    return any(row_pid == pid and marker in argv for row_pid, argv in rows)


# --- the whole re-probe cycle, on real processes -----------------------------

def test_reprobe_cycle_clear_then_not_clear_then_clear_again(tmp_path):
    """THE BEAD, END TO END.

    Take a real event-scoped probe and record CLEAR. Start a real matching
    process. Re-probe: NOT CLEAR, naming it -- the first reading has expired
    and nothing in its own text said so, which is why the re-probe is
    mandatory rather than optional. Stop the process and re-probe again:
    CLEAR.

    THE THIRD STEP IS THE ONE THAT MATTERS MOST. Without it this suite passes
    against a gate that refuses unconditionally -- the SABLE-47try erosion,
    an always-refuse predicate indistinguishable from correct caution and
    strictly worse than the gap it replaces. It proves the window is still
    obtainable."""
    marker = unique_marker()

    # 1. CLEAR, before anything exists.
    first = run_probe(marker)
    assert first.returncode == 0, (
        f"expected CLEAR before the marker process exists; "
        f"rc={first.returncode} stdout={first.stdout!r} "
        f"stderr={first.stderr!r}")
    assert "CLEAR" in first.stdout
    assert "NOT CLEAR" not in first.stdout

    proc = spawn_marker_process(tmp_path, marker)
    try:
        assert wait_until(lambda: marker_live_in_real_ps(marker, proc.pid)), (
            f"marker process pid={proc.pid} never appeared in the real "
            f"process table; the test cannot distinguish a working gate from "
            f"a process that never started")

        # 2. RE-PROBE at the moment of the act: NOT CLEAR, and it names the
        #    consumer rather than merely refusing.
        second = run_probe(marker)
        assert second.returncode == 1, (
            f"a live matching process must NOT clear; rc={second.returncode} "
            f"stdout={second.stdout!r}")
        assert "NOT CLEAR" in second.stdout
        assert marker in second.stdout, \
            "the refusal must name what it found, not just refuse"
        assert str(proc.pid) in second.stdout, \
            "the refusal must name the pid so the actor can go look at it"
    finally:
        reap(proc)

    # 3. NEGATIVE CONTROL: with the consumer gone, the window clears again.
    assert wait_until(
        lambda: not marker_live_in_real_ps(marker, proc.pid)), \
        "the marker process never left the real process table"
    third = run_probe(marker)
    assert third.returncode == 0, (
        f"the gate must RELEASE once the consumer is gone, or it is an "
        f"always-refuse predicate; rc={third.returncode} "
        f"stdout={third.stdout!r}")
    assert "CLEAR" in third.stdout and "NOT CLEAR" not in third.stdout


def test_a_second_marker_arriving_between_probe_and_act_is_what_voids_it(tmp_path):
    """The population is not static between the probe and the act. Two
    DIFFERENT markers, so this cannot pass by accident on the first one's
    residue: probe for B while only A exists (CLEAR), then start B and
    re-probe (NOT CLEAR). This is the live instance's shape -- a worker that
    did not exist when the probe ran."""
    marker_a = unique_marker()
    marker_b = unique_marker()
    assert marker_a != marker_b

    proc_a = spawn_marker_process(tmp_path, marker_a)
    proc_b = None
    try:
        assert wait_until(lambda: marker_live_in_real_ps(marker_a, proc_a.pid))

        # A is live and B is not: a probe scoped to B is genuinely CLEAR.
        # This is the reading that was CORRECT WHEN TAKEN.
        assert run_probe(marker_b).returncode == 0
        # ...and the same instant, a probe scoped to A is NOT clear, proving
        # the CLEAR above is a real discrimination and not a broken term.
        assert run_probe(marker_a).returncode == 1

        proc_b = spawn_marker_process(tmp_path, marker_b)
        assert wait_until(lambda: marker_live_in_real_ps(marker_b, proc_b.pid))

        voided = run_probe(marker_b)
        assert voided.returncode == 1, (
            "the clearance taken moments ago must not survive the arrival of "
            "a consumer that did not exist when it was taken")
        assert marker_b in voided.stdout
    finally:
        reap(proc_a)
        if proc_b is not None:
            reap(proc_b)


# --- AC1 verified against the real shipped output ----------------------------

def test_real_clear_output_states_its_validity_interval_and_invalidators():
    """The acceptance criterion is about what the PROBE OUTPUT SAYS, so it is
    asserted against the real CLI's real stdout, not against the renderer in
    isolation."""
    marker = unique_marker()
    result = run_probe(marker)
    assert result.returncode == 0

    text = result.stdout
    assert "VALID UNTIL" in text, "the clearance does not state its expiry"
    assert "300s" in text, "the clearance does not state its interval"
    assert "whichever is sooner" in text
    assert lib.KIND_DISPATCH in text
    assert lib.KIND_READINESS_ANNOUNCEMENT in text, \
        "a readiness announcement must be named as clearance-invalidating"
    assert "announcing readiness is a state change, not a report" in text
    for actor in SCOPE.split(","):
        assert actor in text, f"the clearance omits actor {actor!r} from scope"

    payload = json.loads(run_probe(marker, "--format", "json").stdout)
    assert payload["clear"] is True
    assert payload["validity_seconds"] == 300.0
    assert payload["valid_until"] == pytest.approx(
        payload["taken_at"] + 300.0)
    assert lib.KIND_READINESS_ANNOUNCEMENT in payload["invalidators"]
    assert payload["actor_scope"] == SCOPE.split(",")

    # The two clearances above were taken at genuinely different real
    # moments: the CLI stamps a real clock rather than a constant.
    other = json.loads(run_probe(marker, "--format", "json").stdout)
    assert other["taken_at"] != payload["taken_at"]


def test_the_cli_refuses_to_invent_a_validity_interval():
    """No unbounded default at the process boundary either -- omitting the
    interval is a usage error, not a clearance that quietly never expires."""
    marker = unique_marker()
    missing = subprocess.run(
        [sys.executable, str(LIB), "probe", "--term", marker,
         "--holder", HOLDER, "--actor-scope", SCOPE],
        text=True, capture_output=True, timeout=60)
    assert missing.returncode == 2, (
        f"a probe with no stated interval must be refused; "
        f"rc={missing.returncode} stderr={missing.stderr!r}")
    assert "validity-seconds" in missing.stderr

    # CONTROL: the identical invocation WITH an interval succeeds, so the
    # refusal is about the missing interval and not about the command shape.
    assert run_probe(marker).returncode == 0

    # The scope is required for the same reason -- a clearance is only
    # correct for the population it measured.
    no_scope = subprocess.run(
        [sys.executable, str(LIB), "probe", "--term", marker,
         "--validity-seconds", VALIDITY, "--holder", HOLDER,
         "--actor-scope", "  ,  "],
        text=True, capture_output=True, timeout=60)
    assert no_scope.returncode == 2
    assert "actor-scope" in no_scope.stderr


def test_a_wrapper_shell_carrying_the_term_does_not_block_the_clearance():
    """THE REAL REPRODUCTION, found by running the tool by hand rather than
    by reasoning about it.

    An agent harness invokes commands as `bash -c '<the whole command line>'`,
    so the invoking SHELL's argv contains the search term verbatim. With only
    self-pid exclusion, a live probe read NOT CLEAR against its own wrapper
    -- a gate that can never open. Here the wrapper shell is REAL, spawned by
    this test, and its argv genuinely contains the marker."""
    marker = unique_marker()
    # `&& true` is LOAD-BEARING, and finding out why is the reason this test
    # is honest: `bash -c "<one simple command>"` EXEC-OPTIMISES, replacing
    # its own image with the child, so the wrapper shell never exists as a
    # separate process and the test would pass without reproducing anything.
    # A compound command forces bash to stay alive as the parent -- which is
    # exactly the shape of the real harness wrapper (a `&&` chain).
    command = (f"echo WRAPPER_PID=$$ && {sys.executable} {LIB} probe "
               f"--term {marker} --validity-seconds {VALIDITY} "
               f"--holder {HOLDER} --actor-scope {SCOPE} --format json "
               f"&& true")
    wrapped = subprocess.run(["bash", "-c", command], text=True,
                             capture_output=True, timeout=60)
    assert wrapped.returncode == 0, (
        f"the probe's own wrapper shell must not block its clearance; "
        f"rc={wrapped.returncode} stdout={wrapped.stdout!r} "
        f"stderr={wrapped.stderr!r}")

    head, _, body = wrapped.stdout.partition("\n")
    wrapper_pid = int(head.split("=", 1)[1])
    payload = json.loads(body)

    assert payload["clear"] is True
    assert payload["matches"] == [], \
        "the probe's own ancestry must not count as a live consumer"

    # THE ASSERTION THAT MAKES THIS A REPRODUCTION rather than a coincidence:
    # the WRAPPER SHELL specifically -- a different process from the probe --
    # really did carry the marker and really was excluded. Without this, the
    # test passes on the probe's own pid alone and never exercises the
    # ancestry walk at all.
    self_pids = [row["pid"] for row in payload["self_matches"]]
    assert wrapper_pid in self_pids, (
        f"wrapper shell pid {wrapper_pid} was not among the disclosed "
        f"ancestry matches {self_pids}; the ancestry walk did not fire")
    assert any(marker in row["argv"] and row["pid"] == wrapper_pid
               for row in payload["self_matches"])
    assert len(self_pids) >= 2, \
        "expected at least the probe itself AND its wrapper shell"

    # The exclusion is DISCLOSED in the human output too, not silent.
    text = subprocess.run(
        ["bash", "-c", command.replace(" --format json", "")], text=True,
        capture_output=True, timeout=60).stdout
    assert "own process ancestry" in text, \
        "an exclusion nobody can see is how a probe stops being able to find"

    # CONTROL: the real ancestry walk actually finds this process's real
    # parent, so the mechanism is reading a real /proc and not returning ().
    assert os.getppid() in lib.ancestor_pids(os.getpid())


def test_the_protocol_is_printable_from_the_shipped_tool():
    """A protocol that only exists in a docstring is not reachable by the
    seat that needs it at the moment of the act."""
    result = subprocess.run([sys.executable, str(LIB), "protocol"],
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0
    assert "RE-PROBE AT THE MOMENT OF THE ACT" in result.stdout
    assert "WHETHER OR NOT IT CHANGED" in result.stdout
    assert "READINESS / UNBLOCK ANNOUNCEMENT" in result.stdout
    assert "LIFT the hold" in result.stdout
