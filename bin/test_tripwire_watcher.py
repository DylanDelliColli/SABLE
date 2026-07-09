"""test_tripwire_watcher — unit tests for tripwire-watcher classification logic.

Tests the pure functions (no gh / bd / network needed). Run with:

  python3 -m pytest bin/test_tripwire_watcher.py

or as part of the full bin/ suite:

  python3 -m pytest bin/ -q
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

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


def make_run(conclusion: str, hours_ago: float, *, sha: str = "abc1234", url: str = "https://gh/run/1") -> tw.Run:
    return tw.Run(
        conclusion=conclusion,
        created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        head_sha=sha,
        url=url,
    )


NOW = datetime.now(timezone.utc)


# ---------- classify_runs ----------


def test_empty_runs_is_stale():
    c = tw.classify_runs([], now=NOW, stale_hours=24, red_streak=3)
    assert c.state == "stale"


def test_recent_green_is_healthy():
    c = tw.classify_runs([make_run("success", 2)], now=NOW, stale_hours=24, red_streak=3)
    assert c.state == "healthy"


def test_old_green_is_stale():
    c = tw.classify_runs([make_run("success", 30)], now=NOW, stale_hours=24, red_streak=3)
    assert c.state == "stale"


def test_no_green_at_all_is_stale():
    c = tw.classify_runs(
        [make_run("failure", 1), make_run("failure", 2)],
        now=NOW,
        stale_hours=24,
        red_streak=10,  # so red-streak doesn't fire on a 2-run case
    )
    assert c.state == "stale"


def test_three_consecutive_failures_is_red_streak():
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
    assert c.state == "red-streak"


def test_red_streak_takes_priority_over_staleness():
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
    assert c.state == "red-streak"


def test_streak_broken_by_green_is_healthy():
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
    assert c.state == "healthy"


def test_cancelled_and_startup_failure_count_as_red():
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
    assert c.state == "red-streak"


def test_in_progress_does_not_count_as_red():
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
    assert c.state == "stale"


def test_insufficient_runs_for_streak_threshold():
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
    assert c.state == "stale"


# ---------- last_green_url in classification ----------


def test_last_green_found_even_on_red_streak():
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
    assert c.last_green.url == "https://gh/run/green"


# ---------- build_bead_description content ----------


@pytest.fixture(scope="module")
def red_streak_classification():
    return tw.classify_runs(
        [make_run("failure", 1), make_run("failure", 2), make_run("failure", 3)],
        now=NOW,
        stale_hours=24,
        red_streak=3,
    )


@pytest.fixture(scope="module")
def red_streak_description(red_streak_classification):
    return tw.build_bead_description(workflow="smoke.yml", branch="dev", classification=red_streak_classification)


def test_description_includes_workflow_name(red_streak_description):
    assert "smoke.yml" in red_streak_description


def test_description_includes_branch(red_streak_description):
    assert "dev" in red_streak_description


def test_description_includes_state(red_streak_description):
    assert "red-streak" in red_streak_description


def test_description_includes_reason(red_streak_description):
    assert "all red" in red_streak_description


def test_description_includes_acceptance_criteria(red_streak_description):
    assert "Acceptance criteria" in red_streak_description


def test_description_includes_test_spec_marker(red_streak_description):
    assert "[no-test]" in red_streak_description


# ---------- build_bead_title ----------


def test_build_bead_title_format():
    assert tw.build_bead_title("smoke.yml", "dev", "stale") == "Tripwire: smoke.yml on dev is stale"


# ---------- _humanize ----------


def test_humanize_seconds():
    assert tw._humanize(timedelta(seconds=42)) == "42s"


def test_humanize_minutes():
    assert tw._humanize(timedelta(seconds=120)) == "2m"


def test_humanize_hours():
    assert tw._humanize(timedelta(hours=5)) == "5h"


def test_humanize_days():
    assert tw._humanize(timedelta(days=3)) == "3d"
