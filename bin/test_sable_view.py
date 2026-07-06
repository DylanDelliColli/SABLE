#!/usr/bin/env python3
"""Unit tests for bin/sable-view (SABLE-ssws.3).

Pure-logic layer: pane-listing parse, role resolution (exact match, worker
prefix group), last-activity extraction, and table/JSON rendering. The tmux
side is covered by test_sable_view_integration.py.
"""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader(
    "sable_view", str(Path(__file__).resolve().parent / "sable-view")
)
_SPEC = importlib.util.spec_from_loader("sable_view", _LOADER)
sv = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(sv)

LISTING = (
    "0 %0 lincoln \n"
    "0 %1 optimus \n"
    "0 %2 tarzan \n"
    "0 %3 chuck \n"
    "1 %4 worker SABLE-abc running\n"
    "2 %5  \n"          # role-less pane: skipped
)


def test_parse_panes_basic():
    panes = sv.parse_panes(LISTING)
    assert [p["role"] for p in panes] == ["lincoln", "optimus", "tarzan", "chuck", "worker"]
    assert panes[0] == {"window": "0", "pane": "%0", "role": "lincoln", "bead": "", "status": ""}
    assert panes[4]["bead"] == "SABLE-abc"
    assert panes[4]["status"] == "running"


def test_parse_panes_skips_roleless():
    assert all(p["pane"] != "%5" for p in sv.parse_panes(LISTING))


def test_resolve_target_exact_role():
    panes = sv.parse_panes(LISTING)
    assert sv.resolve_target(panes, "optimus")["pane"] == "%1"


def test_resolve_target_unknown_role_lists_known():
    panes = sv.parse_panes(LISTING)
    with pytest.raises(ValueError) as e:
        sv.resolve_target(panes, "bogus")
    msg = str(e.value)
    assert "optimus" in msg and "worker" in msg


def test_last_line_picks_last_nonempty_and_truncates():
    cap = "first\n\nsecond line\n   \n"
    assert sv.last_line(cap) == "second line"
    long = "x" * 200
    assert len(sv.last_line(long)) <= sv.ACTIVITY_WIDTH
    assert sv.last_line("") == ""


def test_render_table_contains_roles_and_status():
    panes = sv.parse_panes(LISTING)
    table = sv.render_table(panes, {"%4": "npm test ok"})
    assert "lincoln" in table and "worker" in table
    assert "SABLE-abc" in table and "running" in table
    assert "npm test ok" in table


def test_view_session_name_is_grouped_and_unique():
    assert sv.view_session_name("sable", 123) == "sable-view-123"


def test_grouped_view_commands_shape():
    """The attach path creates a grouped session, self-destructs on detach,
    and selects the target window/pane in the VIEW session only."""
    target = {"window": "1", "pane": "%4", "role": "worker", "bead": "", "status": ""}
    cmds = sv.grouped_view_commands(["tmux"], "sable", "sable-view-9", target,
                                    destroy_on_detach=True)
    flat = [" ".join(c) for c in cmds]
    assert any("new-session" in c and "-t sable" in c and "-s sable-view-9" in c
               for c in flat)
    assert any("destroy-unattached" in c for c in flat)
    assert any("select-window" in c and "sable-view-9:1" in c for c in flat)
    cmds_na = sv.grouped_view_commands(["tmux"], "sable", "sable-view-9", target,
                                       destroy_on_detach=False)
    assert not any("destroy-unattached" in " ".join(c) for c in cmds_na)


if __name__ == "__main__":
    import sys
    import pytest as _p
    sys.exit(_p.main([__file__, "-q"]))
