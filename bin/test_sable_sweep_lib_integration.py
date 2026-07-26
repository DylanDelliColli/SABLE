#!/usr/bin/env python3
"""Integration tests for bin/sable_sweep_lib.py (SABLE-tz7h.4).

Real composition, no mocking of the library itself: export_snapshot() runs a
REAL subprocess `bd list --json` call against a PATH-shim fake `bd` executable
(a call-counting stand-in, not a DI override) that serves a fixture bead
export. A mini sweep then runs slice() -> stub read-only shards -> merge() ->
write_plan(), with the PATH-shim call log proving two properties end to end:
shards make ZERO bd invocations, and every `bd update` call happens strictly
AFTER merge (post-merge-only writes). A second test proves snapshot
consistency: a bead that lands in the fixture after export() has already run
is invisible to anything working from that snapshot. A third test proves
merge()'s count-reconciliation cross-check (SABLE-5s97) end to end: a real
slice()->stub-shards->merge() pipeline where one shard silently drops its
slice's boundary bead raises ShardUnderReportError, and — critically — no
`bd update` call ever reaches the shim, because the parent never gets a
merged report to build a write plan from.
"""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_sweep_lib as lib  # noqa: E402


FAKE_BD = r"""#!/usr/bin/env python3
import os
import sys

log_path = os.environ["BD_CALL_LOG"]
with open(log_path, "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")

args = sys.argv[1:]
if args[:1] == ["list"]:
    fixture = os.environ["BD_FIXTURE_FILE"]
    with open(fixture) as fh:
        sys.stdout.write(fh.read())
sys.exit(0)
"""


def _install_fake_bd(bin_dir: Path) -> Path:
    script = bin_dir / "bd"
    script.write_text(FAKE_BD)
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _env_with_fake_bd(bin_dir: Path, fixture_path: Path, call_log: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["BD_FIXTURE_FILE"] = str(fixture_path)
    env["BD_CALL_LOG"] = str(call_log)
    return env


def _call_log_lines(call_log: Path) -> list:
    if not call_log.exists():
        return []
    return [line for line in call_log.read_text().splitlines() if line.strip()]


def _run_real_bd(cmd, env):
    return subprocess.run(cmd, capture_output=True, text=True, check=True, env=env).stdout


def stub_shard(assigned: list) -> dict:
    """A read-only shard: classifies each bead from its own fields only. Makes
    NO bd calls — real shard subagents get repo-grep rights, never bd."""
    findings = []
    for bead in assigned:
        classification = "stale-fixed" if bead.get("suspect_fixed") else "valid"
        findings.append({
            "bead_id": bead["id"],
            "classification": classification,
            "evidence": f"checked {bead['id']} against fixture",
        })
    return {"findings": findings}


def test_mini_sweep_shards_make_zero_bd_calls_and_writes_are_post_merge(tmp_path):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _install_fake_bd(bin_dir)

    fixture = tmp_path / "export.json"
    beads = [{"id": f"SWEEP-{i}", "suspect_fixed": i % 3 == 0} for i in range(7)]
    fixture.write_text(json.dumps(beads))

    call_log = tmp_path / "bd-calls.log"
    env = _env_with_fake_bd(bin_dir, fixture, call_log)

    # 1. export: ONE real `bd list --json` subprocess call through the shim.
    exported = lib.export_snapshot(run=lambda cmd: _run_real_bd(cmd, env))
    assert exported == beads
    assert _call_log_lines(call_log) == ["list --json --limit 0"]

    # 2. slice + run stub shards (the "shard context") — must add ZERO bd calls.
    slices = lib.slice(exported, 3)
    shard_reports = [stub_shard(s) for s in slices if s]
    assert _call_log_lines(call_log) == ["list --json --limit 0"], (
        "shard context made a bd call — shards must never touch bd")

    # 3. merge preserves every shard finding; merge/write_plan add no bd calls.
    # Passing slices= exercises the count-reconciliation cross-check too — a
    # correct set of shard reports merges clean (SABLE-5s97).
    merged = lib.merge(shard_reports, slices=[s for s in slices if s])
    assert merged["stats"]["candidates"] == len(beads)
    assert {f["bead_id"] for f in merged["findings"]} == {b["id"] for b in beads}

    plan = lib.write_plan(merged)
    assert _call_log_lines(call_log) == ["list --json --limit 0"], (
        "merge/write_plan must not call bd directly — only the executed plan does")

    # 4. the PARENT executes the plan — the ONLY bd writes, and only now.
    for cmd in plan:
        _run_real_bd(cmd, env)

    calls = _call_log_lines(call_log)
    assert calls[0] == "list --json --limit 0"
    assert len(calls) == 1 + len(plan)
    assert all(c.startswith("update ") for c in calls[1:])
    assert all("--append-notes" in c for c in calls[1:])


def test_snapshot_invisible_to_beads_added_after_export(tmp_path):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _install_fake_bd(bin_dir)

    fixture = tmp_path / "export.json"
    original = [{"id": "SWEEP-1"}, {"id": "SWEEP-2"}]
    fixture.write_text(json.dumps(original))

    call_log = tmp_path / "bd-calls.log"
    env = _env_with_fake_bd(bin_dir, fixture, call_log)

    exported = lib.export_snapshot(run=lambda cmd: _run_real_bd(cmd, env))
    assert {b["id"] for b in exported} == {"SWEEP-1", "SWEEP-2"}

    # a bead lands in the live db AFTER the snapshot was taken
    fixture.write_text(json.dumps(original + [{"id": "SWEEP-3-LATE"}]))

    # the already-captured snapshot is unaffected — it's a plain value, not a
    # live view — so shards slicing over it never see the late-arriving bead.
    assert {b["id"] for b in exported} == {"SWEEP-1", "SWEEP-2"}
    for s in lib.slice(exported, 2):
        assert all(b["id"] != "SWEEP-3-LATE" for b in s)

    # re-exporting (a NEW snapshot call) DOES see it — proving the fixture
    # itself changed and the first export's isolation wasn't a fixture bug.
    reexported = lib.export_snapshot(run=lambda cmd: _run_real_bd(cmd, env))
    assert "SWEEP-3-LATE" in {b["id"] for b in reexported}


def test_mini_sweep_deficient_shard_raises_before_any_bd_write(tmp_path):
    """End-to-end reproduction of the live SABLE-tz7h.5 failure: a shard that
    silently drops the boundary (last) bead of its own correctly-sized slice.
    merge() must raise instead of producing a mergeable report — and because
    it raises, write_plan() is never reached and zero `bd update` calls ever
    happen, proving the deficient shard's short report never reaches bd."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _install_fake_bd(bin_dir)

    fixture = tmp_path / "export.json"
    beads = [{"id": f"SWEEP-{i}"} for i in range(6)]
    fixture.write_text(json.dumps(beads))

    call_log = tmp_path / "bd-calls.log"
    env = _env_with_fake_bd(bin_dir, fixture, call_log)

    exported = lib.export_snapshot(run=lambda cmd: _run_real_bd(cmd, env))
    slices = lib.slice(exported, 2)

    # shard 0 behaves correctly; shard 1 silently drops the LAST bead of its
    # own slice — the exact boundary miss observed live.
    shard_reports = [
        stub_shard(slices[0]),
        stub_shard(slices[1][:-1]),
    ]

    try:
        lib.merge(shard_reports, slices=slices)
        raise AssertionError("expected ShardUnderReportError, merge() returned normally")
    except lib.ShardUnderReportError as exc:
        assert exc.shard_index == 1
        assert slices[1][-1]["id"] in exc.missing_ids

    # the deficient report never became a write plan, so bd saw only the
    # original export call — no `update` calls at all.
    assert _call_log_lines(call_log) == ["list --json --limit 0"]
