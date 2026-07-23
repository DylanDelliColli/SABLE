#!/usr/bin/env python3
"""Unit tests for sable-discover-emit (SABLE-7v1r.2).

The deterministic Discovery fan-out: write one charter per survivor (carrying its
epic_intention linkage) + a decision record with EVERY candidate verdict and the
no-go rationale verbatim. no-go candidates get NO charter file.
"""
import importlib.util
import subprocess
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


# --- gitignore check-ignore branch behavior (SABLE-lavb) --------------------
# sable_charter_lib.write_charter/write_decision_record now run every emitted
# path through ensure_charter_committable; emit() re-checks each written path
# and surfaces any un-cleared warning in the result JSON (not just stderr),
# since callers of this script parse stdout, not stderr.

def test_emit_charter_committable_when_dot_claude_gitignored(monkeypatch, tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".claude/\n")
    charters = tmp_path / ".claude" / "sable" / "charters"
    monkeypatch.setenv("SABLE_CHARTERS_DIR", str(charters))

    out = sde.emit(_triage())

    assert "warnings" not in out  # auto-carve cleared the ignore
    charter_path = charters / "real-time-alerts.md"
    assert charter_path.exists()
    ci = subprocess.run(
        ["git", "-C", str(tmp_path), "check-ignore", "-q",
         str(charter_path.relative_to(tmp_path))])
    assert ci.returncode == 1  # not ignored -> committable
    gi_text = (tmp_path / ".gitignore").read_text()
    assert "!.claude/sable/charters/**" in gi_text


def test_emit_surfaces_warning_when_autofix_cannot_clear(monkeypatch, tmp_path):
    monkeypatch.setenv("SABLE_CHARTERS_DIR", str(tmp_path))
    monkeypatch.setattr(
        sde.lib, "ensure_charter_committable",
        lambda path: f"sable-charter: WARNING - {path} still ignored")

    out = sde.emit(_triage())

    # 2 survivor charters + 1 decision record == 3 writes, all "still ignored"
    assert len(out["warnings"]) == 3
    assert all("WARNING" in w for w in out["warnings"])
