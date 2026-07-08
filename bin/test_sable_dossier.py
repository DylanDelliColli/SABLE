#!/usr/bin/env python3
"""Unit tests for the planning-dossier renderer (SABLE-lykc.1).

Covers: planning-dir resolution (env override / in-repo via git common-dir /
HOME fallback), tolerant per-substage loading (missing / malformed), and the
HTML rendering contract the /sable-plan gates depend on: all-sections render,
partial render, error boxes, highlight marking, gap classes, escaping, and
self-containment (no external requests — Artifact CSP blocks them).
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_dossier_lib as lib  # noqa: E402


# --- fixtures ----------------------------------------------------------------

FRAMING = {
    "stories": [
        {"id": "S1", "title": "user can resize login cards"},
        {"id": "S2", "title": "settings persist across sessions"},
    ],
    "non_goals": ["mobile layout"],
    "success_metric": "resize round-trips in under 200ms",
    "wedge": "desktop settings page only",
}

RESEARCH = {
    "findings": [
        {
            "title": "prior art: css resize property",
            "kind": "prior_art",
            "summary": "native resize handles cover 80% of the need",
            "sources": [],
            "derisk_status": "resolved",
        }
    ],
    "recommendation": "build on native resize",
}

ARCHITECTURE = {
    "decisions": [
        {
            "title": "persist size as enum not pixels",
            "contract": "size: 'S'|'M'|'L'|'XL' on the settings record",
            "rationale": "pixel values drift across breakpoints",
            "alternatives_rejected": ["raw pixel persistence"],
        }
    ],
    "smell_risks": ["settings record growing into a god object"],
    "deferred": [],
    "status": "ready",
}

TEST_STRATEGY = {
    "epic": "EPIC-1",
    "sha": "abc1234",
    "stories": [
        {
            "id": "S1",
            "title": "user can resize login cards",
            "impl_beads": [{"id": "EPIC-1.1", "title": "resize handler"}],
            "cases": [
                {
                    "name": "rejects invalid size enum",
                    "layer": "UNIT",
                    "status": "planned",
                    "bead": "EPIC-1.2",
                    "category": 3,
                },
                {
                    "name": "full flow: click to persist",
                    "layer": "E2E",
                    "status": "gap",
                    "bead": None,
                    "category": 1,
                },
            ],
        }
    ],
    "unmapped_beads": [{"id": "EPIC-1.9", "title": "stray infra bead"}],
    "findings": {
        "resolved": ["filed regression bead EPIC-1.8"],
        "deferred": ["eval layer skipped: no LLM surface"],
    },
    "layer_mix": {"unit": 1, "e2e": 1, "eval": 0},
    "coverage": {"covered": 1, "total": 2},
}

DECOMPOSITION = {
    "children": [
        {
            "id": "EPIC-1.1",
            "title": "resize handler",
            "type": "task",
            "deps": [],
            "ready": True,
        }
    ],
    "swarm_validate": {"ok": True, "output": "swarm validate: PASS"},
    "victor_summary": "all fingerprints fresh at abc1234",
}


def write_state(state_dir: Path, **files):
    state_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        fname = name.replace("_", "-") + ".json"
        (state_dir / fname).write_text(
            payload if isinstance(payload, str) else json.dumps(payload)
        )
    return state_dir


# --- planning_dir resolution -------------------------------------------------

def test_planning_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SABLE_PLANNING_DIR", str(tmp_path / "custom"))
    assert lib.planning_dir("EPIC-1") == tmp_path / "custom" / "EPIC-1"


def test_planning_dir_in_git_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("SABLE_PLANNING_DIR", raising=False)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    got = lib.planning_dir("EPIC-1", base=str(tmp_path))
    want = tmp_path.resolve() / ".claude" / "sable" / "state" / "planning" / "EPIC-1"
    assert got == want


def test_planning_dir_home_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("SABLE_PLANNING_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    got = lib.planning_dir("EPIC-1", base=str(tmp_path))  # not a git repo
    assert got == tmp_path / ".claude" / "sable" / "state" / "planning" / "EPIC-1"


# --- load_state ----------------------------------------------------------------

def test_load_state_missing_files_are_none(tmp_path):
    state = lib.load_state(write_state(tmp_path / "s", framing=FRAMING))
    assert state["framing"]["success_metric"] == FRAMING["success_metric"]
    for name in ("research", "architecture", "test-strategy", "decomposition"):
        assert state[name] is None


def test_load_state_malformed_json_is_error(tmp_path):
    state = lib.load_state(
        write_state(tmp_path / "s", test_strategy="{not json", framing=FRAMING)
    )
    assert isinstance(state["test-strategy"], lib.LoadError)
    assert state["framing"] is not None


# --- render: sections ---------------------------------------------------------

def full_state():
    return {
        "framing": FRAMING,
        "research": RESEARCH,
        "architecture": ARCHITECTURE,
        "test-strategy": TEST_STRATEGY,
        "decomposition": DECOMPOSITION,
    }


def test_render_all_sections_present():
    html = lib.render("EPIC-1", full_state())
    for heading in ("Framing", "Research", "Architecture", "Test strategy", "Decomposition"):
        assert heading in html
    assert "user can resize login cards" in html
    assert "rejects invalid size enum" in html
    assert "persist size as enum not pixels" in html
    assert "swarm validate: PASS" in html
    assert "stray infra bead" in html


def test_render_partial_marks_missing():
    state = {k: None for k in lib.SUBSTAGES}
    state["framing"] = FRAMING
    html = lib.render("EPIC-1", state)
    assert "user can resize login cards" in html
    assert html.count("not yet produced") == 4


def test_render_malformed_section_gets_error_box_not_crash():
    state = full_state()
    state["test-strategy"] = lib.LoadError("test-strategy.json: invalid JSON")
    html = lib.render("EPIC-1", state)
    assert "invalid JSON" in html
    assert "load-error" in html
    assert "persist size as enum not pixels" in html  # other sections intact


def test_render_highlight_marks_exactly_one_section():
    html = lib.render("EPIC-1", full_state(), highlight="test-strategy")
    assert html.count("awaiting signoff") == 1
    assert 'id="test-strategy"' in html


def test_render_gap_case_has_gap_class():
    html = lib.render("EPIC-1", full_state())
    assert 'class="case gap"' in html
    assert "full flow: click to persist" in html


def test_render_coverage_and_layer_mix_summary():
    html = lib.render("EPIC-1", full_state())
    assert "1/2" in html
    assert "UNIT" in html and "E2E" in html


def test_render_escapes_html():
    state = full_state()
    state["framing"] = {
        "stories": [{"id": "S1", "title": "<script>alert(1)</script>"}]
    }
    html = lib.render("EPIC-1", state)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_is_self_contained():
    html = lib.render("EPIC-1", full_state())
    assert "http://" not in html
    assert "https://" not in html
    assert "<title>" in html and "<style>" in html


def test_render_tolerates_unknown_and_missing_keys():
    state = full_state()
    state["test-strategy"] = {"stories": [{"title": "bare story", "surprise": 42}]}
    state["research"] = {"unexpected": True}
    html = lib.render("EPIC-1", state)
    assert "bare story" in html


# --- write_dossier -------------------------------------------------------------

def test_write_dossier_end_to_end(tmp_path):
    state_dir = write_state(
        tmp_path / "s", framing=FRAMING, test_strategy=TEST_STRATEGY
    )
    out = lib.write_dossier("EPIC-1", state_dir=str(state_dir), highlight="test-strategy")
    assert out == state_dir / "dossier.html"
    text = out.read_text()
    assert "rejects invalid size enum" in text
    assert "awaiting signoff" in text
