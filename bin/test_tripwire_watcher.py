#!/usr/bin/env python3
"""test_tripwire_watcher — unit tests for tripwire-watcher classification logic.

Tests the pure functions (no gh / bd / network needed). Run with:

  python3 bin/test_tripwire_watcher.py

Exits 0 if all pass, 1 if any fail. No pytest dependency.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

# Load the watcher module by file path (it has a hyphen in its name, which
# makes it un-importable via normal import statements).
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHER_PATH = os.path.join(SCRIPT_DIR, "tripwire-watcher.py")
spec = importlib.util.spec_from_file_location("tripwire_watcher", WATCHER_PATH)
tw = importlib.util.module_from_spec(spec)
# Register before exec so dataclass introspection (Python 3.14+) can find the
# module via cls.__module__ → sys.modules lookup.
sys.modules["tripwire_watcher"] = tw
spec.loader.exec_module(tw)


PASS = 0
FAIL = 0
FAILED_NAMES: list[str] = []


def assert_eq(name: str, got, expected):
    global PASS, FAIL
    if got == expected:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        FAILED_NAMES.append(name)
        print(f"FAIL: {name}")
        print(f"  expected: {expected!r}")
        print(f"  got:      {got!r}")


def assert_in(name: str, needle, haystack):
    global PASS, FAIL
    if needle in haystack:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        FAILED_NAMES.append(name)
        print(f"FAIL: {name}")
        print(f"  expected substring: {needle!r}")
        print(f"  in:                 {haystack!r}")


def make_run(conclusion: str, hours_ago: float, *, sha: str = "abc1234", url: str = "https://gh/run/1") -> tw.Run:
    return tw.Run(
        conclusion=conclusion,
        created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        head_sha=sha,
        url=url,
    )


NOW = datetime.now(timezone.utc)


# ---------- classify_runs ----------

# Test 1: empty runs → stale (no data)
c = tw.classify_runs([], now=NOW, stale_hours=24, red_streak=3)
assert_eq("empty runs → stale", c.state, "stale")

# Test 2: most recent green within window → healthy
c = tw.classify_runs(
    [make_run("success", 2)],
    now=NOW,
    stale_hours=24,
    red_streak=3,
)
assert_eq("recent green → healthy", c.state, "healthy")

# Test 3: most recent green > 24h ago → stale
c = tw.classify_runs(
    [make_run("success", 30)],
    now=NOW,
    stale_hours=24,
    red_streak=3,
)
assert_eq("old green → stale", c.state, "stale")

# Test 4: no green at all in lookback → stale
c = tw.classify_runs(
    [make_run("failure", 1), make_run("failure", 2)],
    now=NOW,
    stale_hours=24,
    red_streak=10,  # so red-streak doesn't fire on a 2-run case
)
assert_eq("no green → stale", c.state, "stale")

# Test 5: 3 consecutive failures → red-streak (priority over stale)
c = tw.classify_runs(
    [
        make_run("failure", 1),
        make_run("failure", 2),
        make_run("failure", 3),
        make_run("success", 50),  # green exists but old
    ],
    now=NOW,
    stale_hours=24,
    red_streak=3,
)
assert_eq("3 reds → red-streak", c.state, "red-streak")

# Test 6: red-streak takes priority over staleness
c = tw.classify_runs(
    [
        make_run("failure", 1),
        make_run("failure", 2),
        make_run("failure", 3),
        make_run("success", 1),  # very recent green!
    ],
    now=NOW,
    stale_hours=24,
    red_streak=3,
)
# Red-streak fires because the LAST 3 runs are all red (green is 4th).
assert_eq("red-streak fires before staleness check", c.state, "red-streak")

# Test 7: 2 reds + 1 green → not red-streak (streak broken)
c = tw.classify_runs(
    [
        make_run("failure", 1),
        make_run("failure", 2),
        make_run("success", 3),
    ],
    now=NOW,
    stale_hours=24,
    red_streak=3,
)
assert_eq("streak broken by green → healthy", c.state, "healthy")

# Test 8: cancelled counts as red
c = tw.classify_runs(
    [
        make_run("cancelled", 1),
        make_run("failure", 2),
        make_run("startup_failure", 3),
    ],
    now=NOW,
    stale_hours=24,
    red_streak=3,
)
assert_eq("cancelled/startup_failure count as red", c.state, "red-streak")

# Test 9: in_progress runs don't break the streak (treated as not-green)
c = tw.classify_runs(
    [
        make_run("in_progress", 0.1),
        make_run("failure", 1),
        make_run("failure", 2),
        make_run("failure", 3),
    ],
    now=NOW,
    stale_hours=24,
    red_streak=3,
)
# The last 3 in the list are 3 failures... but the FIRST run is in_progress,
# so the most-recent-3 are [in_progress, failure, failure] which are not all red.
assert_eq("in_progress doesn't count as red", c.state, "stale")

# Test 10: red_streak threshold of 5, only 3 runs → cannot fire red-streak
c = tw.classify_runs(
    [
        make_run("failure", 1),
        make_run("failure", 2),
        make_run("failure", 3),
    ],
    now=NOW,
    stale_hours=24,
    red_streak=5,
)
assert_eq("insufficient runs for streak threshold", c.state, "stale")


# ---------- last_green_url in classification ----------

c = tw.classify_runs(
    [
        make_run("failure", 1),
        make_run("failure", 2),
        make_run("failure", 3),
        make_run("success", 50, url="https://gh/run/green"),
    ],
    now=NOW,
    stale_hours=24,
    red_streak=3,
)
assert_eq("last_green found even on red-streak", c.last_green.url, "https://gh/run/green")


# ---------- build_bead_description content ----------

c = tw.classify_runs(
    [make_run("failure", 1), make_run("failure", 2), make_run("failure", 3)],
    now=NOW,
    stale_hours=24,
    red_streak=3,
)
desc = tw.build_bead_description(workflow="smoke.yml", branch="dev", classification=c)
assert_in("description includes workflow name", "smoke.yml", desc)
assert_in("description includes branch", "dev", desc)
assert_in("description includes state", "red-streak", desc)
assert_in("description includes reason", "all red", desc)
assert_in("description includes acceptance criteria", "Acceptance criteria", desc)
assert_in("description includes test spec marker", "[no-test]", desc)


# ---------- build_bead_title ----------

assert_eq(
    "title format",
    tw.build_bead_title("smoke.yml", "dev", "stale"),
    "Tripwire: smoke.yml on dev is stale",
)


# ---------- _humanize ----------

assert_eq("humanize seconds", tw._humanize(timedelta(seconds=42)), "42s")
assert_eq("humanize minutes", tw._humanize(timedelta(seconds=120)), "2m")
assert_eq("humanize hours", tw._humanize(timedelta(hours=5)), "5h")
assert_eq("humanize days", tw._humanize(timedelta(days=3)), "3d")


# ---------- summary ----------

print()
print("=" * 50)
print(f"Tests: {PASS + FAIL} | Passed: {PASS} | Failed: {FAIL}")
print("=" * 50)

if FAIL > 0:
    print("Failed tests:")
    for n in FAILED_NAMES:
        print(f"  - {n}")
    sys.exit(1)
sys.exit(0)
