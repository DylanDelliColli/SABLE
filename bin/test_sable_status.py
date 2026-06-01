#!/usr/bin/env python3
"""test_sable_status — unit + integration tests for the sable-status dashboard.

Pure render functions are tested from normalized snapshots; the gatherer is
tested hermetically via SABLE_STATUS_FIXTURE (which also injects per-pid /proc
identity, active-bead markers, and in-progress beads so no real /proc, bd, or
claude calls happen). The Textual live loop is exercised via --once.

Run with:  python3 bin/test_sable_status.py
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
    check(name, needle in haystack, f"expected '{needle}' in:\n{haystack}")


def absent(name: str, haystack: str, needle: str):
    check(name, needle not in haystack, f"did NOT expect '{needle}' in:\n{haystack}")


# ---------- fixtures ----------

PLANNING_SNAPSHOT = {
    "mode": "planning", "since": "2026-06-01T10:00:00-0400",
    "fleet": ["sherlock", "columbo", "gaudi", "victor"],
    "pool": {"ready": 14, "blocked": 3},
    "findings": {"sherlock": 6, "columbo": 4, "gaudi": 2},
    "merge_queue": 0, "inbox": 0, "agents": [], "wip": [],
}

EXECUTION_SNAPSHOT = {
    "mode": "execution", "since": "2026-06-01T11:00:00-0400",
    "fleet": ["optimus", "tarzan", "chuck"],
    "pool": {"ready": 9, "blocked": 0},
    "findings": {"sherlock": 0, "columbo": 0, "gaudi": 0},
    "merge_queue": 2, "inbox": 1,
    "agents": [
        {"name": "cockpit", "role": "manager", "status": "idle", "repo": "internal-analytics", "uptime": "2m", "bead": None, "is_sable": True},
        {"name": "optimus", "role": "manager", "status": "busy", "repo": "SABLE", "uptime": "5m", "bead": "SABLE-205", "is_sable": True},
        {"name": None, "role": None, "status": "busy", "repo": "SABLE", "uptime": "1h", "bead": None, "is_sable": False},
    ],
    "wip": [{"id": "SABLE-cav.5", "title": "Cockpit UI/UX iteration pass", "parent": "SABLE-cav"}],
}


# ---------- render: planning ----------

p = ss.render_planning(PLANNING_SNAPSHOT)
contains("planning shows PLANNING banner", p, "PLANNING")
contains("planning shows ready count", p, "14")
contains("planning shows sherlock finding count", p, "6")

# ---------- render: execution (identity + cross-ref + wip) ----------

e = ss.render_execution(EXECUTION_SNAPSHOT)
contains("execution banner", e, "EXECUTION")
contains("execution ready burn-down", e, "9")
contains("execution names the cockpit agent", e, "cockpit")
contains("execution names optimus", e, "optimus")
contains("execution shows optimus's bead (cross-ref)", e, "SABLE-205")
contains("execution shows idle/busy status", e, "busy")
contains("execution shows the worktree/repo", e, "internal-analytics")
contains("execution counts non-SABLE observer sessions", e, "other session")
contains("execution shows in-progress work", e, "SABLE-cav.5")
contains("execution shows wip title", e, "Cockpit UI/UX")
contains("execution shows merge queue", e, "2")
absent("no bogus '?' agent rows", e, "?")
absent("no bogus worker column", e, "0w")

# graceful
empty = dict(EXECUTION_SNAPSHOT, agents=[], wip=[])
contains("no SABLE agents → friendly message", ss.render_execution(empty), "no SABLE agents")
contains("nothing in progress → friendly message", ss.render_execution(empty), "nothing in progress")

none_snap = dict(PLANNING_SNAPSHOT, mode=None)
contains("no-mode frame", ss.render(none_snap), "no mode")

# dispatch
contains("render() dispatch planning", ss.render(PLANNING_SNAPSHOT), "PLANNING")
contains("render() dispatch execution", ss.render(EXECUTION_SNAPSHOT), "EXECUTION")


# ---------- gather via fixture (identity + active markers + wip) ----------

def write(path: Path, obj) -> str:
    path.write_text(json.dumps(obj))
    return str(path)


NOW = 1780334200
with tempfile.TemporaryDirectory() as d:
    dd = Path(d)
    state = dd / "cockpit-mode.json"
    subprocess.run([str(MODE_BIN), "set", "execution", "--fleet", "optimus,tarzan,chuck"],
                   env=dict(os.environ, SABLE_COCKPIT_STATE=str(state)), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    fixture = write(dd / "fx.json", {
        "ready": [{"id": f"R{i}"} for i in range(9)],
        "blocked": [],
        "counts": {"for-chuck": 2, "for-cockpit": 1},
        "agents": [
            {"pid": 1, "cwd": "/home/x/internal-analytics", "status": "idle", "startedAt": (NOW - 120) * 1000},
            {"pid": 2, "cwd": "/home/x/SABLE", "status": "busy", "startedAt": (NOW - 300) * 1000},
        ],
        "proc_identity": {"1": {"name": "cockpit", "role": "manager"}, "2": {}},
        "active": {"cockpit": {}},
        "in_progress": [{"id": "SABLE-cav.5", "title": "Cockpit UI/UX iteration pass", "parent": "SABLE-cav"}],
        "now": NOW,
    })

    snap = ss.gather_snapshot(env=dict(os.environ, SABLE_COCKPIT_STATE=str(state),
                                       SABLE_STATUS_FIXTURE=fixture))
    check("gather mode", snap["mode"] == "execution", f"got {snap['mode']}")
    check("gather ready", snap["pool"]["ready"] == 9, f"got {snap['pool']['ready']}")
    agents = snap["agents"]
    check("gather identifies cockpit", any(a["name"] == "cockpit" and a["is_sable"] for a in agents), str(agents))
    check("gather marks identity-less process non-SABLE", any(a["name"] is None and not a["is_sable"] for a in agents), str(agents))
    cockpit = next(a for a in agents if a["name"] == "cockpit")
    check("gather computes uptime", cockpit["uptime"] == "2m", f"got {cockpit['uptime']}")
    check("gather repo basename", cockpit["repo"] == "internal-analytics", f"got {cockpit['repo']}")
    check("gather pulls in-progress beads", any(w["id"] == "SABLE-cav.5" for w in snap["wip"]), str(snap["wip"]))


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
    subprocess.run([str(MODE_BIN), "set", "execution"],
                   env=dict(os.environ, SABLE_COCKPIT_STATE=str(state)), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    fx = Path(write(dd / "exec.json", {
        "ready": [{"id": f"R{i}"} for i in range(9)],
        "blocked": [],
        "counts": {"for-chuck": 2, "for-cockpit": 1},
        "agents": [{"pid": 1, "cwd": "/home/x/internal-analytics", "status": "idle", "startedAt": (NOW - 120) * 1000}],
        "proc_identity": {"1": {"name": "cockpit", "role": "manager"}},
        "active": {},
        "in_progress": [{"id": "SABLE-cav.5", "title": "Cockpit UI/UX", "parent": "SABLE-cav"}],
        "now": NOW,
    }))
    out = run_once(state, fx)
    contains("--once shows EXECUTION", out, "EXECUTION")
    contains("--once names the cockpit agent", out, "cockpit")
    contains("--once shows in-progress work", out, "SABLE-cav.5")


print()
print("==========================================")
print(f"Tests: {PASS + FAIL} | Passed: {PASS} | Failed: {FAIL}")
print("==========================================")
if FAIL:
    print("Failed: " + ", ".join(FAILED))
    sys.exit(1)
sys.exit(0)
