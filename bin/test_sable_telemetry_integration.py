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
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_telemetry_bd_source as bd_source  # noqa: E402

BIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = BIN_DIR.parent
CLI = BIN_DIR / "sable-telemetry"
HOOK = REPO_ROOT / "hooks" / "bead-description-gate.sh"

# The ci-verify clean-room is tmux+pytest only -- no bd/dolt by design. The
# seeded-db test drives a REAL sandbox beads DB, so it self-skips when bd is
# absent, matching the bd/dolt-suites-self-skip contract in ci-verify.yml.
HAVE_BD = shutil.which("bd") is not None

_ENV_LEAKS = ("CLAUDE_AGENT_NAME", "TMUX_PANE", "SABLE_HOOK_TRACE_LOG")


def _run(cmd, cwd=None):
    return subprocess.run(cmd, capture_output=True, text=True, check=True,
                          stdin=subprocess.DEVNULL, cwd=cwd)


def _bd_env(home):
    env = {k: v for k, v in os.environ.items() if k not in _ENV_LEAKS}
    env["HOME"] = str(home)
    env["BD_NON_INTERACTIVE"] = "1"
    env["CI"] = "true"
    return env


def _bd_run(work, home, *args, check=True):
    cp = subprocess.run(["bd", *args], cwd=str(work), env=_bd_env(home), text=True,
                        stdin=subprocess.DEVNULL, capture_output=True, timeout=180)
    if check and cp.returncode != 0:
        raise AssertionError(f"bd {args} failed: {cp.stdout}{cp.stderr}")
    return cp


def _robust_bd_init(work, home):
    """`bd init` on the embedded-Dolt backend can leave a PARTIAL database on a
    first-run race (rc 0 but no .beads/config.yaml). A clean init always
    writes config.yaml, so gate success on that artifact and wipe+retry."""
    beads = work / ".beads"
    last = None
    for _ in range(4):
        if beads.exists():
            shutil.rmtree(beads)
        last = _bd_run(work, home, "init", "--non-interactive", check=False)
        if last.returncode == 0 and (beads / "config.yaml").is_file():
            return
    raise AssertionError(f"bd init never produced a clean DB: {last.stdout if last else '<none>'}")


@pytest.fixture
def seeded_bd_sandbox(tmp_path_factory):
    """A real sandbox beads DB with an open+closed mix: OPEN (never touched),
    CLOSED_DISPATCHED (claimed then closed -- has started_at), and
    CLOSED_MANAGER (closed with no claim -- started_at stays absent, the
    61%-missing case this adapter must surface rather than paper over)."""
    root = tmp_path_factory.mktemp("bdsource")
    work = root / "work"
    work.mkdir()
    home = root / "home"
    home.mkdir()
    _robust_bd_init(work, home)

    def create(title):
        cp = _bd_run(work, home, "create", "--sandbox", "--json",
                     "--title", title, "--type=task", "--priority=2",
                     "--description", f"seed fixture bead: {title}")
        return json.loads(cp.stdout)["id"]

    open_id = create("OPEN: never touched")
    dispatched_id = create("CLOSED_DISPATCHED: claimed then closed")
    _bd_run(work, home, "update", dispatched_id, "--sandbox", "--claim")
    _bd_run(work, home, "close", dispatched_id, "--sandbox",
           "--reason", "seed: dispatched close")

    manager_id = create("CLOSED_MANAGER: closed with no claim")
    _bd_run(work, home, "close", manager_id, "--sandbox",
           "--reason", "seed: manager close, no claim")

    return {"work": work, "open": open_id, "dispatched": dispatched_id,
            "manager": manager_id}


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


@pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd/dolt by design; real-bd integration self-skips",
)
def test_bd_source_returns_closed_beads_from_seeded_db(seeded_bd_sandbox):
    records = bd_source.fetch_bead_records(cwd=str(seeded_bd_sandbox["work"]))
    by_id = {r.id: r for r in records}

    ids = seeded_bd_sandbox
    # The core research trap: closed beads must actually surface, not just
    # the open one a naive (non---all) query would have returned.
    assert ids["open"] in by_id
    assert ids["dispatched"] in by_id
    assert ids["manager"] in by_id

    open_record = by_id[ids["open"]]
    assert open_record.status == "open"
    assert open_record.closed_at is None
    assert open_record.started_at is None

    dispatched_record = by_id[ids["dispatched"]]
    assert dispatched_record.status == "closed"
    assert dispatched_record.closed_at is not None
    assert dispatched_record.started_at is not None

    manager_record = by_id[ids["manager"]]
    assert manager_record.status == "closed"
    assert manager_record.closed_at is not None
    assert manager_record.started_at is None  # the 61%-missing case, surfaced explicitly


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
