#!/usr/bin/env python3
"""Unit tests for the Discovery artifact layer (SABLE-7v1r.1).

Pure-function + schema coverage: slug generation, charters-dir path resolution
(env override / in-repo via git common-dir / HOME fallback), and round-trip for
both the charter and the decision-record schemas.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_charter_lib as lib  # noqa: E402


# --- slugify ---------------------------------------------------------------

def test_slugify_basic():
    assert lib.slugify("Real-Time Alerts") == "real-time-alerts"


def test_slugify_strips_punct_and_edges():
    assert lib.slugify("  Foo!! Bar??  ") == "foo-bar"


def test_slugify_empty_is_untitled():
    assert lib.slugify("***") == "untitled"


# --- charters_dir resolution (mirrors sable-mode resolve_state_path) --------

def test_charters_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SABLE_CHARTERS_DIR", str(tmp_path / "x"))
    assert lib.charters_dir() == tmp_path / "x"


def test_charters_dir_in_repo(monkeypatch, tmp_path):
    monkeypatch.delenv("SABLE_CHARTERS_DIR", raising=False)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    got = lib.charters_dir(str(tmp_path))
    assert got == tmp_path.resolve() / ".claude" / "sable" / "charters"


def test_charters_dir_home_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("SABLE_CHARTERS_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    non_repo = tmp_path / "plain"
    non_repo.mkdir()
    got = lib.charters_dir(str(non_repo))
    assert got == tmp_path / ".claude" / "sable" / "charters"


# --- charter schema round-trip ---------------------------------------------

def test_charter_round_trip_full():
    c = lib.Charter(
        slug="alerts",
        title="Real-Time Alerts",
        decision_record="2026-06-26-triage-decisions",
        epic_intention="SABLE-abc1",
        created="2026-06-26T10:00:00-0400",
        problem_statement="Users miss time-critical events.",
        demand_evidence="12 support tickets this quarter.",
        status_quo="They poll the dashboard manually.",
        target_user_and_wedge="On-call ops leads; the incident wedge.",
        why_now="Webhook infra is finally stable.",
        product_approaches="A: live push.\nB: batched digest.",
        recommended_shape="Live push with a digest fallback.",
        success_metric="50% faster incident ack.",
        non_goals="No native mobile app in v1.",
        open_questions="Which channels ship first?",
    )
    again = lib.Charter.from_markdown(c.to_markdown())
    assert again == c


def test_charter_round_trip_with_none_and_empty():
    c = lib.Charter(slug="x", title="X")
    again = lib.Charter.from_markdown(c.to_markdown())
    assert again == c
    assert again.decision_record is None
    assert again.problem_statement == ""


# --- decision record schema round-trip -------------------------------------

def test_decision_record_round_trip():
    rec = lib.DecisionRecord(
        session="2026-06-26 portfolio",
        title="Q3 feature triage",
        created="2026-06-26T10:00:00-0400",
        candidates=[
            lib.Candidate("Alerts", "go", "Clear demand.", charter="alerts"),
            lib.Candidate("Themes", "no-go", "No evidence; revisit after GA."),
            lib.Candidate("Export", "reshape", "Good but scope too wide.", charter="export"),
        ],
    )
    again = lib.DecisionRecord.from_markdown(rec.to_markdown())
    assert again == rec


def test_decision_record_keeps_nogo_rationale_verbatim():
    rec = lib.DecisionRecord(
        session="s",
        candidates=[lib.Candidate("Themes", "no-go", "Killed: zero pull, high cost.")],
    )
    md = rec.to_markdown()
    assert "Killed: zero pull, high cost." in md
    again = lib.DecisionRecord.from_markdown(md)
    assert again.candidates[0].rationale == "Killed: zero pull, high cost."
    assert again.candidates[0].verdict == "no-go"
    assert again.candidates[0].charter is None
