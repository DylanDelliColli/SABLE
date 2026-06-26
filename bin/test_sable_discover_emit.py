#!/usr/bin/env python3
"""Unit tests for sable-discover-emit (SABLE-7v1r.2).

The deterministic Discovery fan-out: write one charter per survivor (carrying its
epic_intention linkage) + a decision record with EVERY candidate verdict and the
no-go rationale verbatim. no-go candidates get NO charter file.
"""
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_charter_lib as lib  # noqa: E402

_LOADER = SourceFileLoader(
    "sable_discover_emit", str(Path(__file__).resolve().parent / "sable-discover-emit"))
_SPEC = importlib.util.spec_from_loader("sable_discover_emit", _LOADER)
sde = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(sde)


def _triage():
    return {
        "session": "2026-06-26 triage",
        "title": "Q3 portfolio",
        "candidates": [
            {"title": "Real-Time Alerts", "verdict": "go", "rationale": "clear demand",
             "epic_intention": "SABLE-e1",
             "charter": {"target_user_and_wedge": "ops wedge", "success_metric": "ack"}},
            {"title": "Bulk Export", "verdict": "reshape", "rationale": "scope wide",
             "epic_intention": "SABLE-e2",
             "charter": {"problem_statement": "manual export"}},
            {"title": "Themes", "verdict": "no-go", "rationale": "zero pull, high cost"},
        ],
    }


def test_emit_writes_charter_per_survivor_only(monkeypatch, tmp_path):
    monkeypatch.setenv("SABLE_CHARTERS_DIR", str(tmp_path))
    out = sde.emit(_triage())
    assert set(out["survivors"]) == {"real-time-alerts", "bulk-export"}
    assert (tmp_path / "real-time-alerts.md").exists()
    assert (tmp_path / "bulk-export.md").exists()
    assert not (tmp_path / "themes.md").exists()   # no-go gets no charter
    assert out["nogos"] == ["Themes"]


def test_emit_charter_carries_epic_linkage(monkeypatch, tmp_path):
    monkeypatch.setenv("SABLE_CHARTERS_DIR", str(tmp_path))
    sde.emit(_triage())
    c = lib.Charter.from_markdown((tmp_path / "real-time-alerts.md").read_text())
    assert c.epic_intention == "SABLE-e1"
    assert c.target_user_and_wedge == "ops wedge"
    assert c.decision_record == "2026-06-26-triage-decisions"


def test_emit_decision_record_all_candidates_nogo_verbatim(monkeypatch, tmp_path):
    monkeypatch.setenv("SABLE_CHARTERS_DIR", str(tmp_path))
    out = sde.emit(_triage())
    rec = lib.DecisionRecord.from_markdown(Path(out["decision"]).read_text())
    by_title = {c.title: c for c in rec.candidates}
    assert set(by_title) == {"Real-Time Alerts", "Bulk Export", "Themes"}
    assert by_title["Themes"].verdict == "no-go"
    assert by_title["Themes"].rationale == "zero pull, high cost"
    assert by_title["Themes"].charter is None
    assert by_title["Real-Time Alerts"].charter == "real-time-alerts"


def test_emit_survivor_requires_epic_intention(monkeypatch, tmp_path):
    monkeypatch.setenv("SABLE_CHARTERS_DIR", str(tmp_path))
    bad = {"session": "s", "candidates": [{"title": "X", "verdict": "go", "rationale": "r"}]}
    with pytest.raises(Exception):
        sde.emit(bad)
