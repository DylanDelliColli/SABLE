#!/usr/bin/env python3
"""Unit tests for bin/sable-worker-status (SABLE-bldh.4).

Done-signal detection + reaping-decision logic. A worker pane carries three
tmux user-options set at spawn / completion: @sable_role=worker, @sable_bead=<id>,
@sable_status=running|done. Reaping is driven by the pane's own done-flag (pure
tmux); the manager separately watches the bead pool for the actual result.
"""
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader(
    "sable_worker_status", str(Path(__file__).resolve().parent / "sable-worker-status")
)
_SPEC = importlib.util.spec_from_loader("sable_worker_status", _LOADER)
sws = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(sws)


def test_parse_worker_panes_basic():
    out = "%1 worker abc-1 running\n%2 worker abc-2 done\n"
    panes = sws.parse_worker_panes(out)
    assert panes == [
        {"pane": "%1", "bead": "abc-1", "status": "running"},
        {"pane": "%2", "bead": "abc-2", "status": "done"},
    ]


def test_parse_worker_panes_filters_non_workers():
    # lincoln/optimus panes (role != worker) and a role-less pane are skipped
    out = "%1 lincoln  \n%2 optimus  \n%3 worker abc-1 running\n%4\n"
    panes = sws.parse_worker_panes(out)
    assert [p["pane"] for p in panes] == ["%3"]


def test_parse_worker_panes_missing_status_defaults_running():
    # a freshly spawned worker may not have set @sable_status yet
    out = "%5 worker abc-9 \n"
    panes = sws.parse_worker_panes(out)
    assert panes == [{"pane": "%5", "bead": "abc-9", "status": "running"}]


def test_is_done():
    assert sws.is_done("done") is True
    assert sws.is_done("running") is False
    assert sws.is_done("") is False


def test_reaping_decision_only_done_panes():
    workers = [
        {"pane": "%1", "bead": "a", "status": "running"},
        {"pane": "%2", "bead": "b", "status": "done"},
        {"pane": "%3", "bead": "c", "status": "done"},
    ]
    assert sws.reaping_decision(workers) == ["%2", "%3"]


def test_reaping_decision_empty_when_none_done():
    workers = [{"pane": "%1", "bead": "a", "status": "running"}]
    assert sws.reaping_decision(workers) == []


def test_tmux_base_socket():
    assert sws.tmux_base("sk") == ["tmux", "-L", "sk"]
    assert sws.tmux_base(None) == ["tmux"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
