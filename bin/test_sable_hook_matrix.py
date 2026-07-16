#!/usr/bin/env python3
"""Tests for bin/sable-hook-matrix (SABLE-jfg6.6 / contract D6).

Two layers, per the epic test rule (S4-U2/U3 unit, S4-E1/E2 integration):

UNIT — the classifier `classify()` is a pure function; drive each research-F6
decision rule over synthetic cell inputs, plus the totality property (every cell
maps to exactly one of in-our-control | upstream, no unclassified cell). No hooks
invoked here.

INTEGRATION — the harness really invokes the D1 (lib-hook-trace) and D2
(control-trace) hooks in an isolated scratch dir across the two real session
types and emits the committed doc with one row per driven cell; the concurrency
cell records per-fire STDIN_BYTES diffable against the transcript count. Real
composition: real bash, real hook scripts, real log files — mocks would defeat
the point (a matrix that mocks the hook cannot prove the hook fires).

Hermetic/headless/sandbox discipline (SABLE-6cf9): every log path is redirected
into a pytest tmp_path; nothing touches the developer's real ~/.claude logs.
"""
import importlib.util
import json
import os
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader(
    "sable_hook_matrix", str(Path(__file__).resolve().parent / "sable-hook-matrix")
)
_SPEC = importlib.util.spec_from_loader("sable_hook_matrix", _LOADER)
shm = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(shm)


# ===========================================================================
# UNIT — classifier decision rules (research F6)
# ===========================================================================

def _cell(**kw):
    kw.setdefault("session_type", "warm-worker")
    kw.setdefault("hook_surface", "tdd-evidence")
    return shm.Cell(**kw)


def test_rule_empty_stdin_1to1_zero_bytes():
    # C == T and every fire recorded STDIN_BYTES=0 -> #16047 empty-stdin, upstream.
    c = _cell(transcript_tool_uses=3, control_fires=3, stdin_bytes=(0, 0, 0))
    r = shm.classify(c)
    assert r.verdict is shm.EMPTY_STDIN
    assert r.verdict.issue == "#16047"
    assert r.verdict.bucket == shm.UPSTREAM


def test_rule_genuine_non_dispatch_miss():
    # C < T in a warm-worker, not scaling -> genuine non-dispatch #6305/#15441.
    c = _cell(session_type="warm-worker", transcript_tool_uses=5, control_fires=2)
    r = shm.classify(c)
    assert r.verdict is shm.NON_DISPATCH
    assert r.verdict.issue == "#6305/#15441"
    assert r.verdict.bucket == shm.UPSTREAM


def test_rule_subagent_wontfix_miss_only_in_subagent():
    # C < T confined to Agent-subagent (not scaling) -> #34692 WONTFIX.
    c = _cell(session_type="Agent-subagent", transcript_tool_uses=3, control_fires=0)
    r = shm.classify(c)
    assert r.verdict is shm.SUBAGENT_WONTFIX
    assert r.verdict.issue == "#34692"
    assert r.verdict.bucket == shm.UPSTREAM


def test_rule_parallel_batch_scales_with_concurrency():
    # C < T and the miss scales with concurrency -> #64237 parallel-batch. This
    # signature wins even inside a subagent (scaling is the stronger evidence).
    c = _cell(session_type="Agent-subagent", load=8, transcript_tool_uses=8,
              control_fires=5, miss_scales_with_concurrency=True)
    r = shm.classify(c)
    assert r.verdict is shm.PARALLEL_BATCH
    assert r.verdict.issue == "#64237"
    assert r.verdict.bucket == shm.UPSTREAM


def test_rule_healthy_1to1_payload_present():
    # C == T with real payload bytes -> healthy, in-our-control.
    c = _cell(transcript_tool_uses=4, control_fires=4, stdin_bytes=(200, 200, 200, 200))
    r = shm.classify(c)
    assert r.verdict is shm.HEALTHY
    assert r.verdict.bucket == shm.IN_OUR_CONTROL


def test_healthy_when_control_surface_has_no_stdin_records():
    # The control surface never reads stdin; 1:1 with an empty stdin_bytes tuple
    # is healthy, NOT empty-stdin degradation.
    c = _cell(hook_surface="control", transcript_tool_uses=2, control_fires=2,
              stdin_bytes=())
    r = shm.classify(c)
    assert r.verdict is shm.HEALTHY
    assert r.verdict.bucket == shm.IN_OUR_CONTROL


def test_rule_overfire_maps_in_our_control():
    # C > T -> over-fire anomaly, in-our-control (totality-completeness bucket).
    c = _cell(hook_surface="control", transcript_tool_uses=2, control_fires=5)
    r = shm.classify(c)
    assert r.verdict is shm.OVERFIRE
    assert r.verdict.bucket == shm.IN_OUR_CONTROL


def test_detection_only_never_asserts_green_without_sample():
    # No live >2h sample (C=0/T=0/no stdin): must NOT collapse to healthy; it is
    # recorded as an unsampled age-degradation attributed upstream.
    c = _cell(detection_only=True, transcript_tool_uses=0, control_fires=0,
              stdin_bytes=())
    r = shm.classify(c)
    assert r.verdict is shm.AGE_UNSAMPLED
    assert r.verdict.issue == "#16047"
    assert r.verdict.bucket == shm.UPSTREAM
    assert "DETECTION-ONLY" in r.outcome


def test_detection_only_with_live_empty_stdin_sample():
    # A real >2h fire showing STDIN_BYTES=0 attributes to #16047 empty-stdin.
    c = _cell(detection_only=True, transcript_tool_uses=1, control_fires=1,
              stdin_bytes=(0,))
    r = shm.classify(c)
    assert r.verdict is shm.EMPTY_STDIN
    assert "DETECTION-ONLY" in r.outcome


def test_totality_over_synthetic_grid():
    # THE totality property: over a broad synthetic grid, classify() never
    # raises, always returns a verdict, and every verdict maps to exactly one of
    # the two buckets. No unclassified cell exists.
    buckets = {shm.IN_OUR_CONTROL, shm.UPSTREAM}
    seen = set()
    for detection_only in (False, True):
        for session_type in ("warm-worker", "Agent-subagent", "gc-managed", "cockpit"):
            for T in (0, 1, 3, 8):
                for C in (0, 1, 3, 5, 8, 12):
                    for stdin_bytes in ((), (0,), (0, 0), (200,), (0, 200)):
                        for scales in (False, True):
                            for load in (1, 5, 8):
                                c = shm.Cell(
                                    session_type=session_type,
                                    hook_surface="tdd-evidence",
                                    load=load,
                                    transcript_tool_uses=T,
                                    control_fires=C,
                                    stdin_bytes=stdin_bytes,
                                    miss_scales_with_concurrency=scales,
                                    detection_only=detection_only,
                                )
                                r = shm.classify(c)
                                assert r.verdict.bucket in buckets, c
                                assert r.verdict in shm.ALL_VERDICTS
                                seen.add(r.verdict.bucket)
    # Both buckets are actually reachable (the classification is not degenerate).
    assert seen == buckets


def test_every_named_verdict_maps_to_a_valid_bucket():
    # Structural totality: each declared verdict is one of the two buckets.
    for v in shm.ALL_VERDICTS:
        assert v.bucket in (shm.IN_OUR_CONTROL, shm.UPSTREAM)


# --- ground-truth parsers ---------------------------------------------------

def test_count_transcript_tool_uses_filters_by_name(tmp_path):
    tx = tmp_path / "t.jsonl"
    recs = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash"},
            {"type": "text", "text": "hi"},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read"},
            {"type": "tool_use", "name": "Bash"},
        ]}},
        {"type": "system", "content": "noise"},
        "not json",
    ]
    with tx.open("w") as f:
        for r in recs:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
    assert shm.count_transcript_tool_uses(str(tx)) == 3          # all tool_use
    assert shm.count_transcript_tool_uses(str(tx), "Bash") == 2  # Bash only


def test_parse_trace_log_extracts_bytes_and_ages(tmp_path):
    log = tmp_path / "hook-trace.log"
    log.write_text(
        "ENTRY 2026-07-16T00:00:00Z pid=1 hook=tdd-evidence session-age=0s\n"
        "STDIN_BYTES=200\n"
        "ENTRY 2026-07-16T03:00:00Z pid=2 hook=tdd-evidence session-age=10800s\n"
        "STDIN_BYTES=0\n"
    )
    entries, stdin_bytes, ages = shm.parse_trace_log(str(log))
    assert entries == 2
    assert stdin_bytes == [200, 0]
    assert ages == [0, 10800]


def test_parse_control_log_counts_fire_lines(tmp_path):
    log = tmp_path / "control-trace.log"
    log.write_text("2026-07-16T00:00:00Z 111 Bash\n2026-07-16T00:00:01Z 112 Bash\n")
    assert shm.parse_control_log(str(log)) == 2


# ===========================================================================
# INTEGRATION — real hook invocations + doc emission
# ===========================================================================

def test_drive_real_cell_warm_worker_tdd_evidence_healthy(tmp_path, monkeypatch):
    # Really invoke the D1+D2 hooks for a warm-worker tdd-evidence cell. The hooks
    # actually run: control fires 1:1 and the trace lib records a non-empty
    # STDIN_BYTES -> healthy, in-our-control.
    monkeypatch.setenv("SABLE_HOOK_TRACE", "1")
    monkeypatch.delenv("SABLE_HOOK_TRACE_LOG", raising=False)
    monkeypatch.delenv("SABLE_CONTROL_TRACE_LOG", raising=False)
    cell = shm.Cell(session_type="warm-worker", hook_surface="tdd-evidence", load=1)
    shm.drive_real_cell(cell, tmp_path)
    assert cell.provenance == "real"
    assert cell.control_fires == 1
    assert cell.transcript_tool_uses == 1
    assert cell.stdin_bytes and all(b > 0 for b in cell.stdin_bytes)
    r = shm.classify(cell)
    assert r.verdict is shm.HEALTHY


def test_drive_real_cell_empty_stdin_reproduces_16047(tmp_path):
    # Real repro of #16047: dispatch happens (control fires 1:1) but the payload
    # is empty (STDIN_BYTES=0). This is a genuine invocation, not a mock.
    cell = shm.Cell(session_type="warm-worker", hook_surface="tdd-evidence", load=1)
    shm.drive_real_cell(cell, tmp_path, empty_stdin=True)
    assert cell.control_fires == 1
    assert cell.stdin_bytes == (0,)
    r = shm.classify(cell)
    assert r.verdict is shm.EMPTY_STDIN


def test_concurrency_cell_records_per_fire_stdin_diffable(tmp_path):
    # S4-E2: the concurrency cell records a per-fire STDIN_BYTES value for each
    # of `load` concurrent invocations, diffable against the transcript count.
    cell = shm.Cell(session_type="warm-worker", hook_surface="tdd-evidence", load=5)
    shm.drive_real_cell(cell, tmp_path)
    assert cell.transcript_tool_uses == 5
    assert cell.control_fires == 5
    # One STDIN_BYTES record per concurrent fire, all non-empty, count == T.
    assert len(cell.stdin_bytes) == 5
    assert all(b > 0 for b in cell.stdin_bytes)
    assert len(cell.stdin_bytes) == cell.transcript_tool_uses


def test_control_only_cell_has_no_stdin_dependency(tmp_path):
    # The control surface must fire without ever reading stdin (D2 invariant):
    # C == T, zero STDIN_BYTES records -> healthy, in-our-control.
    cell = shm.Cell(session_type="warm-worker", hook_surface="control", load=1)
    shm.drive_real_cell(cell, tmp_path)
    assert cell.control_fires == 1
    assert cell.stdin_bytes == ()
    r = shm.classify(cell)
    assert r.verdict is shm.HEALTHY


def test_build_matrix_emits_row_per_cell_both_session_types(tmp_path):
    # S4-E1: the harness drives the matrix and emits a doc with one row per
    # driven cell, covering both real session types and all F6 buckets.
    results = shm.build_matrix(tmp_path)
    assert len(results) >= 4
    session_types = {r.cell.session_type for r in results}
    assert "warm-worker" in session_types
    assert "Agent-subagent" in session_types
    # Every cell classified into exactly one bucket (totality on the real matrix).
    for r in results:
        assert r.verdict.bucket in (shm.IN_OUR_CONTROL, shm.UPSTREAM)
    # All four F6 upstream issues are represented across the matrix.
    issues = {r.verdict.issue for r in results}
    for expected in ("#16047", "#34692", "#64237"):
        assert expected in issues
    # Both buckets appear (healthy in-our-control + upstream failures).
    buckets = {r.verdict.bucket for r in results}
    assert buckets == {shm.IN_OUR_CONTROL, shm.UPSTREAM}


def test_render_doc_has_one_table_row_per_cell(tmp_path):
    results = shm.build_matrix(tmp_path)
    doc = shm.render_doc(results)
    assert "# Hook-firing classification" in doc
    assert "## Totality" in doc
    assert "DETECTION-ONLY" in doc
    # One matrix row per cell in the "Matrix cells" table. Count rows that carry
    # a bucket cell (**in-our-control** / **upstream**), which only the per-cell
    # rows do.
    cell_rows = [ln for ln in doc.splitlines()
                 if ln.startswith("| ") and ("**in-our-control**" in ln or "**upstream**" in ln)]
    assert len(cell_rows) == len(results)


def test_emit_doc_writes_file(tmp_path):
    out = tmp_path / "docs" / "HOOK-FIRING-CLASSIFICATION.md"
    rc = shm.main(["--emit-doc", str(out), "--workdir", str(tmp_path / "wd")])
    assert rc == 0
    assert out.exists()
    text = out.read_text()
    assert "Hook-firing classification" in text
    assert "in-our-control" in text and "upstream" in text


def test_detection_only_cell_reads_live_trace(tmp_path, monkeypatch):
    # attribute_detection_only reads the live trace log; with an aged fire showing
    # empty stdin it attributes #16047, DETECTION-ONLY, never asserting green.
    live = tmp_path / "hook-trace.log"
    live.write_text(
        "ENTRY 2026-07-16T00:00:00Z pid=1 hook=tdd-evidence session-age=0s\n"
        "STDIN_BYTES=200\n"
        "ENTRY 2026-07-16T05:00:00Z pid=2 hook=tdd-evidence session-age=18000s\n"
        "STDIN_BYTES=0\n"
    )
    monkeypatch.setenv("SABLE_HOOK_TRACE_LOG", str(live))
    cell = shm.Cell(session_type="warm-worker", hook_surface="tdd-evidence")
    shm.attribute_detection_only(cell)
    assert cell.detection_only is True
    assert cell.provenance == "detection-only"
    r = shm.classify(cell)
    assert r.verdict is shm.EMPTY_STDIN
    assert "session-age>2h" in r.outcome


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
