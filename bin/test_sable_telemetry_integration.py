#!/usr/bin/env python3
"""Integration tests for sable-telemetry foundation (SABLE-8b41.1).

Real composition, no mocks: invokes the actual `bin/sable-telemetry` CLI
subprocess AND the actual `hooks/bead-description-gate.sh` subprocess against
the same single-source origin: taxonomy, and diffs their output. This is the
Shotgun Surgery guard the architecture review flagged — a hardcoded second
copy of the taxonomy in the hook would silently drift from
bin/sable_telemetry_lib.py; only a real subprocess call through the hook's
own read path can catch that.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

BIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = BIN_DIR.parent
CLI = BIN_DIR / "sable-telemetry"
HOOK = REPO_ROOT / "hooks" / "bead-description-gate.sh"


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True,
                          stdin=subprocess.DEVNULL)


def test_cli_runs_and_prints_json():
    result = _run([str(CLI), "--shift", "--json"])
    payload = json.loads(result.stdout)
    assert "metrics" in payload


def test_origin_labels_resolve_identically_through_tool_and_hook_paths():
    tool_out = _run([str(CLI), "--print-origin-labels"]).stdout
    hook_out = _run(["bash", str(HOOK), "--print-origin-labels"]).stdout

    assert tool_out == hook_out

    labels = [line.strip() for line in tool_out.splitlines() if line.strip()]
    assert labels == [
        "planned", "dogfood", "recurrence", "cross-fleet", "operator", "followup",
    ]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
