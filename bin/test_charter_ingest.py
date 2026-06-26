#!/usr/bin/env python3
"""Unit tests for Full's charter-ingestion seam (SABLE-7v1r.3).

find_charter_for_epic scans .claude/sable/charters/ for a charter whose
epic_intention matches the target epic; framing_fields maps a charter onto the
FRAMING substage outputs. Given no match, detection returns None so Full falls
back to generating framing cold.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_charter_lib as lib  # noqa: E402


def _charter(slug, epic):
    return lib.Charter(
        slug=slug, title=slug.title(), epic_intention=epic,
        problem_statement="prob-" + slug,
        demand_evidence="dem-" + slug,
        target_user_and_wedge="wedge-" + slug,
        success_metric="metric-" + slug,
        non_goals="nogoal-" + slug,
    )


def test_find_charter_for_epic_hit(monkeypatch, tmp_path):
    monkeypatch.setenv("SABLE_CHARTERS_DIR", str(tmp_path))
    lib.write_charter(_charter("alerts", "SABLE-e1"))
    lib.write_charter(_charter("export", "SABLE-e2"))
    got = lib.find_charter_for_epic("SABLE-e2")
    assert got is not None
    assert got.slug == "export"


def test_find_charter_for_epic_miss(monkeypatch, tmp_path):
    monkeypatch.setenv("SABLE_CHARTERS_DIR", str(tmp_path))
    lib.write_charter(_charter("alerts", "SABLE-e1"))
    assert lib.find_charter_for_epic("SABLE-nope") is None


def test_find_charter_for_epic_empty_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("SABLE_CHARTERS_DIR", str(tmp_path / "empty"))
    assert lib.find_charter_for_epic("SABLE-e1") is None


def test_framing_fields_mapping():
    f = lib.framing_fields(_charter("alerts", "SABLE-e1"))
    assert f["wedge"] == "wedge-alerts"
    assert f["success_metric"] == "metric-alerts"
    assert f["non_goals"] == "nogoal-alerts"
    assert "prob-alerts" in f["user_story_context"]
    assert "dem-alerts" in f["user_story_context"]
    assert f["charter_slug"] == "alerts"
    assert f["epic_intention"] == "SABLE-e1"
