#!/usr/bin/env python3
"""Integration tests for the sable-charter bin (SABLE-7v1r.1).

Real subprocess invocation in a temp git repo: write a decision record + two
charters under .claude/sable/charters/, locate them back, and assert they are
DURABLE (git-addable, NOT gitignored) — the come-back-to record, unlike the
ephemeral .claude/sable/state.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-charter"


def _run(args, cwd):
    env = dict(os.environ)
    env.pop("SABLE_CHARTERS_DIR", None)  # force real per-repo resolution from cwd
    return subprocess.run(
        [sys.executable, str(BIN), *args],
        cwd=str(cwd), capture_output=True, text=True, env=env,
    )


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    return tmp_path


def test_write_locate_and_durable(repo):
    # two survivor charters
    for slug, title in [("alerts", "Real-Time Alerts"), ("export", "Bulk Export")]:
        f = repo / f"{slug}.json"
        f.write_text(json.dumps({"slug": slug, "title": title,
                                 "problem_statement": "problem-" + slug}))
        r = _run(["write-charter", "--json", str(f)], cwd=repo)
        assert r.returncode == 0, r.stderr

    # the session decision record (one no-go kept verbatim)
    dec = {
        "session": "2026-06-26 triage",
        "candidates": [
            {"title": "Real-Time Alerts", "verdict": "go", "rationale": "demand", "charter": "alerts"},
            {"title": "Bulk Export", "verdict": "reshape", "rationale": "scope wide", "charter": "export"},
            {"title": "Themes", "verdict": "no-go", "rationale": "zero pull, high cost"},
        ],
    }
    df = repo / "dec.json"
    df.write_text(json.dumps(dec))
    r = _run(["write-decision", "--json", str(df)], cwd=repo)
    assert r.returncode == 0, r.stderr

    cdir = repo / ".claude" / "sable" / "charters"
    assert (cdir / "alerts.md").exists()
    assert (cdir / "export.md").exists()
    decisions = list(cdir.glob("*-decisions.md"))
    assert len(decisions) == 1
    assert "zero pull, high cost" in decisions[0].read_text()

    # locate round-trips (hit + miss)
    r = _run(["locate", "alerts"], cwd=repo)
    assert r.returncode == 0
    assert r.stdout.strip().endswith("charters/alerts.md")
    assert _run(["locate", "nope"], cwd=repo).returncode == 1

    # DURABLE: git add picks the charters up, and check-ignore says NOT ignored
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    staged = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
        capture_output=True, text=True, check=True).stdout
    assert ".claude/sable/charters/alerts.md" in staged
    ci = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", ".claude/sable/charters/alerts.md"],
        capture_output=True, text=True)
    assert ci.returncode != 0  # nonzero exit == path is NOT ignored
