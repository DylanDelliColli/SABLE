"""Real-bd sandbox integration for sable-corruption-sweep (SABLE-wf7rx)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


BIN = Path(__file__).with_name("sable-corruption-sweep")


def run(argv, *, cwd, env):
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def test_real_bd_write_path_flags_plants_not_clean_and_negative_control(tmp_path):
    if not shutil_which("bd"):
        pytest.skip("bd is not installed")
    env = os.environ.copy()
    env.pop("BEADS_DB", None)
    init = run(
        ["bd", "init", "--prefix=swp", "--non-interactive", "--skip-agents", "--skip-hooks", "--quiet"],
        cwd=tmp_path,
        env=env,
    )
    assert init.returncode == 0, init.stderr
    env["BEADS_DB"] = str(tmp_path / ".beads")

    fixtures = {
        "clean": "A normal engineering record with no command output.",
        "json-plant": 'authored then {"created_at":"x","dependency_count":4}',
        "listing-plant": "authored then\n○ SABLE-abc ● P1 injected listing",
    }
    created = {}
    for name, body in fixtures.items():
        result = run(
            ["bd", "create", f"--title={name}", f"--description={body}", "--type=task", "--priority=2", "--json"],
            cwd=tmp_path,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        created[name] = payload["id"] if isinstance(payload, dict) else payload[0]["id"]
    remembered = run(
        [
            "bd",
            "remember",
            "--key=memory-plant",
            "PROPOSED:  must retain the missing subject",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert remembered.returncode == 0, remembered.stderr

    swept = run([str(BIN), "--json"], cwd=tmp_path, env=env)
    assert swept.returncode == 0, swept.stderr
    report = json.loads(swept.stdout)
    ids = {(item["source_type"], item["source_id"]) for item in report["candidates"]}
    assert ("bead", created["json-plant"]) in ids
    assert ("bead", created["listing-plant"]) in ids
    assert ("memory", "memory-plant") in ids
    assert ("bead", created["clean"]) not in ids

    disabled = run(
        [str(BIN), "--json", "--disable-signatures", "all"], cwd=tmp_path, env=env
    )
    assert disabled.returncode == 0, disabled.stderr
    assert json.loads(disabled.stdout)["candidate_count"] == 0


def shutil_which(command):
    from shutil import which

    return which(command)
