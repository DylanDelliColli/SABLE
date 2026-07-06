#!/usr/bin/env python3
"""Unit tests for bin/sable-spawn-manager (SABLE-dqhn.2).

Pure-logic layer: role validation, idempotent-skip planning from an existing
pane listing, and window-not-split command construction (the Lincoln window
must never be disturbed: new-window with -d). tmux behavior is covered by
test_sable_spawn_manager_integration.py.
"""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader(
    "sable_spawn_manager", str(Path(__file__).resolve().parent / "sable-spawn-manager")
)
_SPEC = importlib.util.spec_from_loader("sable_spawn_manager", _LOADER)
sm = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(sm)


def test_validate_roles_accepts_managers():
    assert sm.validate_roles(["optimus", "chuck"]) == ["optimus", "chuck"]


def test_validate_roles_rejects_unknown():
    with pytest.raises(ValueError) as e:
        sm.validate_roles(["optimus", "lincoln"])
    assert "lincoln" in str(e.value) and "optimus" in str(e.value)


def test_parse_existing_roles():
    out = "%0 lincoln\n%1 optimus\n%2 \n"
    assert sm.parse_existing_roles(out) == {"lincoln", "optimus"}


def test_plan_spawns_skips_existing():
    to_spawn, skipped = sm.plan_spawns(["optimus", "tarzan"], {"lincoln", "optimus"})
    assert to_spawn == ["tarzan"]
    assert skipped == ["optimus"]


def test_window_args_are_detached_named_windows():
    args = sm.window_args("sable", "optimus", "bash")
    assert "new-window" in args
    assert "-d" in args                      # never steal the Lincoln window
    assert "-n" in args and "optimus" in args
    i = args.index("-t")
    assert args[i + 1] == "sable"
    assert "split-window" not in args


if __name__ == "__main__":
    import sys
    import pytest as _p
    sys.exit(_p.main([__file__, "-q"]))
