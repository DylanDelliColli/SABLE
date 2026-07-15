#!/usr/bin/env python3
"""Unit tests for bin/sable-dolt-push (SABLE-ipcf).

The dolt push-lock wrapper: the single blessed path for `bd dolt push` both
fleets adopt. Defense-in-depth serialization of cross-fleet pushes:

  1. a filesystem lock at ~/.claude/sable/dolt-push.lock carrying fleet-id +
     pid + timestamp, acquire-before / delete-after, stale-breakable at a TTL;
  2. pull-before-push folded in;
  3. the bounce-on-dangling stopgap folded in (on a dangling-chunk error: bounce
     the dolt sql-server, retry once, then fail loudly).

These are the UNIT tests — pure logic with every subprocess/clock injected as a
seam. No real `bd dolt push`, no real dolt server (standing convention: dolt
push is CHUCK-ONLY and the shared remote corrupts on concurrent pushes). Real
composition against a scratch dolt remote lives in the integration variant.
"""
import importlib.util
import json
import subprocess
import sys
import threading
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader(
    "sable_dolt_push", str(Path(__file__).resolve().parent / "sable-dolt-push")
)
_SPEC = importlib.util.spec_from_loader("sable_dolt_push", _LOADER)
sdp = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(sdp)


# --- clock seam --------------------------------------------------------------

class FakeClock:
    """Deterministic monotonic-ish clock. now() returns `start`; advance()
    steps it. sleep() advances by the slept amount and optionally fires a
    callback (used to model a holder releasing the lock mid-wait)."""

    def __init__(self, start=1000.0, on_sleep=None):
        self.t = float(start)
        self.on_sleep = on_sleep
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, secs):
        self.sleeps.append(secs)
        self.t += secs
        if self.on_sleep:
            self.on_sleep(self)


# --- lock path / fleet-id resolution ----------------------------------------

def test_default_lock_path_under_home():
    env = {"HOME": "/home/someone"}
    assert sdp.default_lock_path(env) == "/home/someone/.claude/sable/dolt-push.lock"


def test_lock_path_env_override_wins():
    env = {"HOME": "/home/someone", "SABLE_DOLT_PUSH_LOCK": "/tmp/custom.lock"}
    assert sdp.default_lock_path(env) == "/tmp/custom.lock"


def test_resolve_fleet_id_env_wins():
    assert sdp.resolve_fleet_id({"SABLE_FLEET_ID": "market-brief"}) == "market-brief"


def test_resolve_fleet_id_falls_back_to_hostname(monkeypatch):
    monkeypatch.setattr(sdp.socket, "gethostname", lambda: "boxy")
    assert sdp.resolve_fleet_id({}) == "boxy"


def test_resolve_fleet_id_ignores_blank(monkeypatch):
    monkeypatch.setattr(sdp.socket, "gethostname", lambda: "boxy")
    assert sdp.resolve_fleet_id({"SABLE_FLEET_ID": ""}) == "boxy"


# --- acquire / release round-trip -------------------------------------------

def test_acquire_writes_fleet_pid_timestamp(tmp_path):
    lock = tmp_path / "dolt-push.lock"
    clk = FakeClock(start=1234.5)
    sdp.acquire_lock(str(lock), "sable-lincoln", pid=4242,
                     now_fn=clk.now, sleep_fn=clk.sleep)
    info = json.loads(lock.read_text())
    assert info["fleet"] == "sable-lincoln"
    assert info["pid"] == 4242
    assert info["timestamp"] == 1234.5


def test_acquire_release_roundtrip(tmp_path):
    lock = tmp_path / "dolt-push.lock"
    clk = FakeClock()
    token = sdp.acquire_lock(str(lock), "f1", pid=1, now_fn=clk.now, sleep_fn=clk.sleep)
    assert lock.exists()
    assert sdp.release_lock(str(lock), token) is True
    assert not lock.exists()


def test_release_only_removes_own_lock(tmp_path):
    lock = tmp_path / "dolt-push.lock"
    clk = FakeClock()
    sdp.acquire_lock(str(lock), "f1", pid=1, now_fn=clk.now, sleep_fn=clk.sleep)
    # a stale/foreign token must NOT delete the current holder's lock
    assert sdp.release_lock(str(lock), "not-my-token") is False
    assert lock.exists()


# --- contention: the second acquire blocks or fails cleanly -----------------

def test_contention_second_acquire_fails_cleanly_no_wait(tmp_path):
    lock = tmp_path / "dolt-push.lock"
    clk = FakeClock(start=2000.0)
    sdp.acquire_lock(str(lock), "holder", pid=1, now_fn=clk.now, sleep_fn=clk.sleep)
    before = lock.read_text()
    with pytest.raises(sdp.LockContention) as e:
        # wait=0 -> single attempt, no clobber
        sdp.acquire_lock(str(lock), "latecomer", pid=2, wait=0.0,
                         now_fn=clk.now, sleep_fn=clk.sleep)
    # the incumbent's lock is untouched (never interleaves)
    assert lock.read_text() == before
    assert e.value.holder["fleet"] == "holder"


def test_contention_waits_then_acquires_after_release(tmp_path):
    lock = tmp_path / "dolt-push.lock"
    # holder takes the lock at t=3000 (well within TTL)
    holder_clk = FakeClock(start=3000.0)
    holder_token = sdp.acquire_lock(str(lock), "holder", pid=1,
                                    now_fn=holder_clk.now, sleep_fn=holder_clk.sleep)

    # the waiter releases the holder's lock on its first sleep, modelling the
    # incumbent finishing its push — the next loop iteration must acquire.
    def release_on_sleep(clock):
        sdp.release_lock(str(lock), holder_token)

    waiter_clk = FakeClock(start=3000.0, on_sleep=release_on_sleep)
    token = sdp.acquire_lock(str(lock), "waiter", pid=2, wait=600.0, poll=0.5,
                             now_fn=waiter_clk.now, sleep_fn=waiter_clk.sleep)
    assert token
    info = json.loads(lock.read_text())
    assert info["fleet"] == "waiter"          # the waiter now holds it
    assert waiter_clk.sleeps == [0.5]         # exactly one poll, then success


# --- stale-break at TTL ------------------------------------------------------

def test_lock_is_stale_boundary():
    info = {"timestamp": 1000.0}
    assert sdp.lock_is_stale(info, now=1000.0 + 601, ttl=600) is True
    assert sdp.lock_is_stale(info, now=1000.0 + 599, ttl=600) is False


def test_stale_lock_broken_and_reacquired(tmp_path):
    lock = tmp_path / "dolt-push.lock"
    # a lock written 700s ago by a dead fleet
    old = FakeClock(start=5000.0)
    sdp.acquire_lock(str(lock), "dead-fleet", pid=99,
                     now_fn=old.now, sleep_fn=old.sleep)
    now_clk = FakeClock(start=5000.0 + 700)  # past the 600s TTL
    token = sdp.acquire_lock(str(lock), "live-fleet", pid=7, ttl=600, wait=0.0,
                             now_fn=now_clk.now, sleep_fn=now_clk.sleep)
    assert token
    info = json.loads(lock.read_text())
    assert info["fleet"] == "live-fleet"       # stale holder was displaced
    assert info["pid"] == 7


def test_acquire_lock_no_leftover_temp_files(tmp_path):
    """Regression guard for SABLE-l7gd: acquire_lock now writes the payload to
    a `path.tmp.<pid>.<nonce>` file before hard-linking it into place. Every
    branch (immediate success, contention-then-wait, stale-break-then-retry)
    must clean the temp file up — none should be left behind."""
    lock1 = tmp_path / "immediate.lock"
    clk = FakeClock(start=9000.0)
    token = sdp.acquire_lock(str(lock1), "f1", pid=1, now_fn=clk.now, sleep_fn=clk.sleep)
    sdp.release_lock(str(lock1), token)

    lock2 = tmp_path / "contended.lock"
    holder_clk = FakeClock(start=9000.0)
    holder_token = sdp.acquire_lock(str(lock2), "holder", pid=2,
                                    now_fn=holder_clk.now, sleep_fn=holder_clk.sleep)

    def release_on_sleep(clock):
        sdp.release_lock(str(lock2), holder_token)

    waiter_clk = FakeClock(start=9000.0, on_sleep=release_on_sleep)
    sdp.acquire_lock(str(lock2), "waiter", pid=3, wait=600.0, poll=0.5,
                     now_fn=waiter_clk.now, sleep_fn=waiter_clk.sleep)

    lock3 = tmp_path / "stale.lock"
    old = FakeClock(start=9000.0)
    sdp.acquire_lock(str(lock3), "dead-fleet", pid=99, now_fn=old.now, sleep_fn=old.sleep)
    now_clk = FakeClock(start=9000.0 + 700)
    sdp.acquire_lock(str(lock3), "live-fleet", pid=7, ttl=600, wait=0.0,
                     now_fn=now_clk.now, sleep_fn=now_clk.sleep)

    leftovers = list(tmp_path.rglob("*.tmp.*"))
    assert not leftovers, f"leftover lock temp files: {leftovers}"


def test_acquire_lock_concurrent_threads_never_double_hold(tmp_path):
    """SABLE-l7gd regression: under the old os.open(O_CREAT|O_EXCL)-then-write
    implementation, a lock file could briefly exist with no content, so a
    racing acquirer could misread it as corrupt/stale, break it, and both
    sides would believe they held the lock (the CI interleave failure). Real
    concurrent threads hammering the same lock file must never both be
    'holding' it at once."""
    lock = tmp_path / "dolt-push.lock"
    n = 20
    events = []
    guard = threading.Lock()
    errors = []

    def worker(i):
        try:
            token = sdp.acquire_lock(str(lock), f"fleet-{i}", pid=i, wait=10, poll=0.005)
            with guard:
                events.append(("start", i))
            time.sleep(0.01)
            with guard:
                events.append(("end", i))
            assert sdp.release_lock(str(lock), token) is True
        except Exception as e:  # noqa: BLE001 - surfaced via `errors` below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert len(events) == 2 * n
    stack = []
    for kind, i in events:
        if kind == "start":
            assert not stack, f"interleave: {i} started while {stack} in flight"
            stack.append(i)
        else:
            assert stack and stack[-1] == i, f"bad end for {i}, stack={stack}"
            stack.pop()
    assert not stack
    assert not lock.exists()


def test_fresh_lock_not_broken_even_with_wait_zero(tmp_path):
    lock = tmp_path / "dolt-push.lock"
    clk = FakeClock(start=6000.0)
    sdp.acquire_lock(str(lock), "holder", pid=1, now_fn=clk.now, sleep_fn=clk.sleep)
    now_clk = FakeClock(start=6000.0 + 60)     # only 60s old — NOT stale
    with pytest.raises(sdp.LockContention):
        sdp.acquire_lock(str(lock), "latecomer", pid=2, ttl=600, wait=0.0,
                         now_fn=now_clk.now, sleep_fn=now_clk.sleep)


# --- dangling-chunk signature ------------------------------------------------

def test_is_dangling_error_matches():
    assert sdp.is_dangling_error("error: dangling chunk reference abc123")
    assert sdp.is_dangling_error("DANGLING REF detected during push")
    assert sdp.is_dangling_error("fatal: dangling ref while uploading")


def test_is_dangling_error_negative():
    assert not sdp.is_dangling_error("everything up to date")
    assert not sdp.is_dangling_error("permission denied (publickey)")
    assert not sdp.is_dangling_error("")


# --- do_push: pull-before-push, bounce-on-dangling, fail-loudly --------------

class FakeRun:
    """Records commands and returns canned CompletedProcess results.

    `results` maps a match-substring (first token joined) -> a list of
    (returncode, stdout) consumed in order, so a command can fail then succeed.
    """

    def __init__(self, results):
        self.results = {k: list(v) for k, v in results.items()}
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        key = self._key(cmd)
        rc, out = self.results[key].pop(0)
        return subprocess.CompletedProcess(cmd, rc, out, "")

    @staticmethod
    def _key(cmd):
        joined = " ".join(cmd)
        if "pull" in joined:
            return "pull"
        if "stop" in joined:
            return "bounce"
        if "push" in joined:
            return "push"
        return joined

    def count(self, key):
        return sum(1 for c in self.calls if self._key(c) == key)


PULL = ["bd", "dolt", "pull"]
PUSH = ["bd", "dolt", "push"]
BOUNCE = ["bd", "dolt", "stop"]


def test_do_push_happy_path():
    run = FakeRun({"pull": [(0, "")], "push": [(0, "Everything up-to-date")]})
    rc = sdp.do_push(pull_cmd=PULL, push_cmd=PUSH, bounce_cmd=BOUNCE, run=run)
    assert rc == sdp.EXIT_OK
    assert run.count("pull") == 1
    assert run.count("push") == 1
    assert run.count("bounce") == 0            # no bounce on a clean push


def test_do_push_pull_failure_aborts_before_push():
    run = FakeRun({"pull": [(1, "could not connect to remote")]})
    rc = sdp.do_push(pull_cmd=PULL, push_cmd=PUSH, bounce_cmd=BOUNCE, run=run)
    assert rc == sdp.EXIT_PULL_FAIL
    assert run.count("push") == 0              # never push on a failed pull


def test_do_push_dangling_bounces_and_retries_once():
    run = FakeRun({
        "pull": [(0, "")],
        "push": [(1, "error: dangling chunk reference deadbeef"), (0, "pushed")],
        "bounce": [(0, "server stopped")],
    })
    rc = sdp.do_push(pull_cmd=PULL, push_cmd=PUSH, bounce_cmd=BOUNCE, run=run)
    assert rc == sdp.EXIT_OK
    assert run.count("push") == 2              # original + one retry
    assert run.count("bounce") == 1            # bounced exactly once


def test_do_push_dangling_retry_still_fails_loudly():
    run = FakeRun({
        "pull": [(0, "")],
        "push": [(1, "dangling chunk ref"), (1, "dangling chunk ref still")],
        "bounce": [(0, "")],
    })
    rc = sdp.do_push(pull_cmd=PULL, push_cmd=PUSH, bounce_cmd=BOUNCE, run=run)
    assert rc == sdp.EXIT_PUSH_FAIL
    assert run.count("push") == 2              # exactly one retry, no infinite loop
    assert run.count("bounce") == 1


def test_do_push_non_dangling_failure_does_not_retry_or_bounce():
    run = FakeRun({
        "pull": [(0, "")],
        "push": [(1, "permission denied (publickey)")],
        "bounce": [(0, "")],
    })
    rc = sdp.do_push(pull_cmd=PULL, push_cmd=PUSH, bounce_cmd=BOUNCE, run=run)
    assert rc == sdp.EXIT_PUSH_FAIL
    assert run.count("push") == 1              # no retry on a non-dangling error
    assert run.count("bounce") == 0
