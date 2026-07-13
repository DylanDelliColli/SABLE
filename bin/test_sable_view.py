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


def test_live_worker_count_excludes_done_and_nonworkers():
    # SABLE-mmdt: the count backing the cockpit's count-vs-cap line — running
    # (or not-yet-tagged) worker panes only; done panes and role panes excluded.
    listing = LISTING + "1 %6 worker SABLE-def done\n"
    panes = sv.parse_panes(listing)
    assert sv.live_worker_count(panes) == 1


def test_cap_summary_shows_count_vs_cap_and_names_the_knob():
    panes = sv.parse_panes(LISTING)
    line = sv.cap_summary(panes, 4)
    assert "1/4" in line
    assert "SABLE_MAX_WORKERS" in line


def test_view_session_name_is_grouped_and_unique():
    assert sv.view_session_name("sable", 123) == "sable-view-123"


def test_grouped_view_commands_shape():
    """The pre-attach sequence creates a grouped session and selects the target
    window/pane in the VIEW session only. destroy-unattached must NOT appear
    here: the grouped session is still DETACHED, and tmux reaps a detached
    session the moment that option turns on — before select-window ever runs
    (SABLE-d8tl). The option rides the attach exec instead (attach_argv)."""
    target = {"window": "1", "pane": "%4", "role": "worker", "bead": "", "status": ""}
    cmds = sv.grouped_view_commands(["tmux"], "sable", "sable-view-9", target)
    flat = [" ".join(c) for c in cmds]
    assert any("new-session" in c and "-t sable" in c and "-s sable-view-9" in c
               for c in flat)
    assert any("select-window" in c and "sable-view-9:1" in c for c in flat)
    assert any("select-pane" in c and "%4" in c for c in flat)
    assert not any("destroy-unattached" in c for c in flat)


def test_attach_argv_enables_destroy_unattached_only_after_attach():
    """SABLE-d8tl: self-destruct-on-detach is enabled in the SAME tmux command
    sequence as the attach, AFTER the attach command — the only safe moment."""
    argv = sv.attach_argv(["tmux"], "sable-view-9",
                          destroy_on_detach=True, switch_client=False)
    assert argv[:4] == ["tmux", "attach", "-t", "sable-view-9"]
    tail = argv[argv.index(";") + 1:]
    assert "set-option" in tail and "destroy-unattached" in tail and "on" in tail


def test_attach_argv_switch_client_inside_tmux():
    argv = sv.attach_argv(["tmux"], "sable-view-9",
                          destroy_on_detach=True, switch_client=True)
    assert argv[:4] == ["tmux", "switch-client", "-t", "sable-view-9"]
    assert "destroy-unattached" in argv[argv.index(";"):]


def test_attach_argv_without_destroy_has_no_chained_command():
    argv = sv.attach_argv(["tmux"], "sable-view-9",
                          destroy_on_detach=False, switch_client=False)
    assert ";" not in argv
    assert "destroy-unattached" not in argv


if __name__ == "__main__":
    import sys
    import pytest as _p
    sys.exit(_p.main([__file__, "-q"]))
