#!/usr/bin/env python3
"""test_sable_status — unit + integration tests for the sable-status dashboard.

Verifies the pure render functions (deterministic from a normalized snapshot),
the snapshot gatherer (hermetic via the SABLE_STATUS_FIXTURE injection point),
and the --once integration path (one rendered frame to stdout). The live
Textual loop is a thin wrapper over render(gather_snapshot()) and is exercised
through --once rather than a headless TUI driver.

Run with:

  python3 bin/test_sable_status.py

Exits 0 if all pass, 1 if any fail. No pytest dependency.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STATUS_PATH = SCRIPT_DIR / "sable-status"
MODE_BIN = SCRIPT_DIR / "sable-mode"

# sable-status has no .py extension, so force a source loader (spec_from_file_
# location returns None for an unrecognized suffix).
_loader = SourceFileLoader("sable_status", str(STATUS_PATH))
spec = importlib.util.spec_from_loader("sable_status", _loader)
ss = importlib.util.module_from_spec(spec)
sys.modules["sable_status"] = ss
_loader.exec_module(ss)

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"FAIL: {name}" + (f"\n  {detail}" if detail else ""))


def contains(name: str, haystack: str, needle: str):
    check(name, needle in haystack, f"expected to find '{needle}' in:\n{haystack}")


# ---------- fixtures ----------

PLANNING_SNAPSHOT = {
    "mode": "planning",
    "since": "2026-06-01T10:00:00-0400",
    "fleet": ["sherlock", "columbo", "gaudi", "victor"],
    "pool": {"ready": 14, "blocked": 3},
    "findings": {"sherlock": 6, "columbo": 4, "gaudi": 2},
    "merge_queue": 0,
    "inbox": 0,
    "agents": [],
}

EXECUTION_SNAPSHOT = {
    "mode": "execution",
    "since": "2026-06-01T11:00:00-0400",
    "fleet": ["optimus", "tarzan", "chuck"],
    "pool": {"ready": 9, "blocked": 0},
    "findings": {"sherlock": 0, "columbo": 0, "gaudi": 0},
    "merge_queue": 2,
    "inbox": 1,
    "agents": [
        {"name": "optimus", "state": "running", "bead": "SABLE-205", "workers": 2},
        {"name": "tarzan", "state": "running", "bead": "SABLE-198", "workers": 1},
        {"name": "chuck", "state": "idle", "bead": "", "workers": 0},
    ],
}


# ---------- render: planning ----------

p = ss.render_planning(PLANNING_SNAPSHOT)
contains("planning shows PLANNING banner", p, "PLANNING")
contains("planning shows ready count", p, "14")
contains("planning shows blocked count", p, "3")
contains("planning shows sherlock", p, "sherlock")
contains("planning shows sherlock finding count", p, "6")
contains("planning shows columbo", p, "columbo")
contains("planning shows gaudi", p, "gaudi")

# ---------- render: execution ----------

e = ss.render_execution(EXECUTION_SNAPSHOT)
contains("execution shows EXECUTION banner", e, "EXECUTION")
contains("execution shows ready burn-down", e, "9")
contains("execution shows optimus", e, "optimus")
contains("execution shows optimus bead", e, "SABLE-205")
contains("execution shows worker count", e, "2")
contains("execution shows merge queue", e, "2")
contains("execution shows inbox", e, "1")

# ---------- render dispatch ----------

contains("render() dispatches planning", ss.render(PLANNING_SNAPSHOT), "PLANNING")
contains("render() dispatches execution", ss.render(EXECUTION_SNAPSHOT), "EXECUTION")

# ---------- graceful degradation ----------

none_snap = {"mode": None, "since": "", "fleet": [], "pool": {"ready": 0, "blocked": 0},
             "findings": {"sherlock": 0, "columbo": 0, "gaudi": 0}, "merge_queue": 0, "inbox": 0, "agents": []}
contains("no-mode frame renders without crashing", ss.render(none_snap), "no mode")

empty_exec = dict(EXECUTION_SNAPSHOT, agents=[])
contains("execution with no managers degrades gracefully", ss.render_execution(empty_exec), "no managers")


# ---------- gather_snapshot via fixture injection ----------

def write(path: Path, obj) -> str:
    path.write_text(json.dumps(obj))
    return str(path)


with tempfile.TemporaryDirectory() as d:
    dd = Path(d)
    state = dd / "cockpit-mode.json"
    subprocess.run([str(MODE_BIN), "set", "planning", "--fleet", "sherlock,columbo"],
                   env=dict(os.environ, SABLE_COCKPIT_STATE=str(state)), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    fixture = write(dd / "fx.json", {
        "ready": [{"id": f"R{i}"} for i in range(14)],
        "blocked": [{"id": f"B{i}"} for i in range(3)],
        "counts": {"sherlock-finding": 6, "columbo-test-spec": 4, "gaudi-arch-gap": 2,
                   "for-chuck": 0, "for-cockpit": 0},
        "agents": [],
    })

    snap = ss.gather_snapshot(env=dict(os.environ, SABLE_COCKPIT_STATE=str(state),
                                       SABLE_STATUS_FIXTURE=fixture))
    check("gather reads mode from state", snap["mode"] == "planning", f"got {snap['mode']}")
    check("gather computes ready count", snap["pool"]["ready"] == 14, f"got {snap['pool']['ready']}")
    check("gather computes blocked count", snap["pool"]["blocked"] == 3, f"got {snap['pool']['blocked']}")
    check("gather maps sherlock findings", snap["findings"]["sherlock"] == 6, f"got {snap['findings']['sherlock']}")
    check("gather maps columbo specs", snap["findings"]["columbo"] == 4, f"got {snap['findings']['columbo']}")


# ---------- integration: --once renders a frame ----------

def run_once(state_path: Path, fixture_path: Path) -> str:
    res = subprocess.run([sys.executable, str(STATUS_PATH), "--once"],
                         env=dict(os.environ, SABLE_COCKPIT_STATE=str(state_path),
                                  SABLE_STATUS_FIXTURE=str(fixture_path)),
                         capture_output=True, text=True)
    return res.stdout + res.stderr


with tempfile.TemporaryDirectory() as d:
    dd = Path(d)
    state = dd / "cockpit-mode.json"

    # planning frame
    subprocess.run([str(MODE_BIN), "set", "planning"],
                   env=dict(os.environ, SABLE_COCKPIT_STATE=str(state)), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    fx_plan = Path(write(dd / "plan.json", {
        "ready": [{"id": f"R{i}"} for i in range(14)],
        "blocked": [{"id": f"B{i}"} for i in range(3)],
        "counts": {"sherlock-finding": 6, "columbo-test-spec": 4, "gaudi-arch-gap": 2},
        "agents": [],
    }))
    out = run_once(state, fx_plan)
    contains("--once planning frame shows PLANNING", out, "PLANNING")
    contains("--once planning frame shows ready count", out, "14")

    # execution frame
    subprocess.run([str(MODE_BIN), "set", "execution"],
                   env=dict(os.environ, SABLE_COCKPIT_STATE=str(state)), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    fx_exec = Path(write(dd / "exec.json", {
        "ready": [{"id": f"R{i}"} for i in range(9)],
        "blocked": [],
        "counts": {"for-chuck": 2, "for-cockpit": 1},
        "agents": [{"name": "optimus", "status": "running", "bead": "SABLE-205", "workers": 2}],
    }))
    out = run_once(state, fx_exec)
    contains("--once execution frame shows EXECUTION", out, "EXECUTION")
    contains("--once execution frame shows optimus", out, "optimus")


# ---------- summary ----------

print()
print("==========================================")
print(f"Tests: {PASS + FAIL} | Passed: {PASS} | Failed: {FAIL}")
print("==========================================")
if FAIL:
    print("Failed: " + ", ".join(FAILED))
    sys.exit(1)
sys.exit(0)
