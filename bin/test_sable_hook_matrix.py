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


# ===========================================================================
# UNIT — the REGISTRY-WIRING matrix (SABLE-31b5l)
#
# Every assertion here carries its own negative control. A checker that always
# answers UNWIRED passes any test that only ever asserts UNWIRED, and it is
# exactly as useless as one that always answers ACTIVE — worse, actually, since
# it fails in the direction of crying wolf until the tool gets ignored. So each
# case below forces its condition, asserts the intended outcome, AND flips one
# input to prove the check discriminates.
# ===========================================================================

import subprocess


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


def _fixture_repo(root, chain=False, extra_hooks=()):
    """A repo shaped like this one: hooks/multi-manager/<hook>.sh plus a
    core.hooksPath pointing at a beads-style hooks dir. `chain` decides whether
    that dir's pre-push delegates to the multi-manager hook — the single
    variable the git-hooks polarity turns on."""
    root.mkdir(parents=True, exist_ok=True)
    mm = root / "hooks" / "multi-manager"
    mm.mkdir(parents=True)
    for name in ("pre-push-rebase-test.sh",) + tuple(extra_hooks):
        (mm / name).write_text("#!/usr/bin/env bash\necho hook\n")

    hooksdir = root / ".beads" / "hooks"
    hooksdir.mkdir(parents=True)
    body = "#!/usr/bin/env sh\n# BEADS INTEGRATION\nbd hooks run pre-push \"$@\"\n"
    if chain:
        body += 'bash "$(dirname "$0")/../../hooks/multi-manager/pre-push-rebase-test.sh" "$@"\n'
    (hooksdir / "pre-push").write_text(body)

    subprocess.run(["git", "init", "-q", str(root)], check=True,
                   capture_output=True)
    _git(root, "config", "core.hooksPath", str(hooksdir))
    return root


def _scan(root, home, **kw):
    """Always hermetic: a scratch HOME and a scratch PATH dir, never the live
    ~/.claude (SABLE-2avau — an installer silently targeting the real one)."""
    home.mkdir(parents=True, exist_ok=True)
    pathdir = home / "pathbin"
    pathdir.mkdir(exist_ok=True)
    kw.setdefault("settings_paths", [])
    kw.setdefault("path_env", str(pathdir))
    return {r.name: r for r in shm.build_wiring_matrix(
        repo_root=root, home=str(home), **kw)}


# --- the pure classifier: totality + each rule, both polarities --------------

def test_classify_wiring_is_total_over_every_input_shape():
    w = (shm.Wiring(registry=shm.REG_PATH, site="/x", detail="d"),)
    for exists in (True, False):
        for wirings in ((), w):
            for checked in ((), (shm.REG_PATH,)):
                for skipped in ((), (("git-hooks", "why"),)):
                    got = shm.classify_wiring(exists, wirings, checked, skipped)
                    assert got in shm.ALL_WIRING_STATUSES


def test_unwired_requires_every_registry_checked():
    # THE RULE THE BEAD EXISTS FOR. No wiring found, but a registry went
    # unchecked -> we refuse to call it unwired.
    assert shm.classify_wiring(
        True, (), (shm.REG_PATH,), (("claude-settings", "unparseable"),)
    ) == shm.W_INDETERMINATE
    # CONTROL: same inputs with nothing skipped DO earn the negative verdict.
    assert shm.classify_wiring(
        True, (), (shm.REG_PATH, shm.REG_CLAUDE_SETTINGS), ()
    ) == shm.W_UNWIRED


def test_classify_wiring_separates_unwired_absent_and_wired_missing():
    w = (shm.Wiring(registry=shm.REG_CLAUDE_SETTINGS, site="s", detail="d",
                    target="/nope", target_exists=False),)
    # On disk, no registry wires it -> the loud fifth-activation-class outcome.
    assert shm.classify_wiring(True, (), (shm.REG_PATH,), ()) == shm.W_UNWIRED
    # Not on disk and nothing wires it -> plainly absent, a different fact.
    assert shm.classify_wiring(False, (), (shm.REG_PATH,), ()) == shm.W_ABSENT
    # A registry entry naming a file that is not there is louder than either.
    assert shm.classify_wiring(False, w, (shm.REG_PATH,), ()) == shm.W_WIRED_MISSING
    # CONTROL: the same wiring with the file present is ordinary ACTIVE.
    assert shm.classify_wiring(True, w, (shm.REG_PATH,), ()) == shm.W_ACTIVE


def test_no_registry_checked_at_all_is_never_a_clean_negative():
    assert shm.classify_wiring(True, (), (), ()) == shm.W_INDETERMINATE


# --- git-hooks registry: the bead's ORIGINAL spec, both polarities -----------

def test_reports_unwired_repo_hooks(tmp_path):
    """core.hooksPath pointing at a dir that does NOT chain to
    hooks/multi-manager/* must report the hook PRESENT-BUT-UNWIRED — never
    absent (the file is right there) and never active."""
    root = _fixture_repo(tmp_path / "norepo", chain=False)
    r = _scan(root, tmp_path / "home1")["pre-push-rebase-test.sh"]
    assert r.status == shm.W_UNWIRED, r.status
    assert r.exists is True                    # not absent: it is on disk
    assert shm.REG_GIT_HOOKS not in r.registry_names()
    assert shm.REG_GIT_HOOKS in r.registries_checked   # the negative was earned

    # POSITIVE CONTROL — mandatory. Change the ONE variable that matters (the
    # hooks dir now chains) and the same tool must say ACTIVE, so this test
    # cannot pass by always answering UNWIRED.
    root2 = _fixture_repo(tmp_path / "chained", chain=True)
    r2 = _scan(root2, tmp_path / "home2")["pre-push-rebase-test.sh"]
    assert r2.status == shm.W_ACTIVE, r2.status
    assert shm.REG_GIT_HOOKS in r2.registry_names()
    assert any("chained from git hook 'pre-push'" in w.detail for w in r2.wirings)


def test_git_registry_unavailable_blocks_the_negative_verdict(tmp_path):
    """A non-git directory cannot answer the git-hooks question, so no verdict
    of UNWIRED may be issued from it."""
    root = tmp_path / "plain"
    (root / "hooks" / "multi-manager").mkdir(parents=True)
    (root / "hooks" / "multi-manager" / "x.sh").write_text("#!/bin/bash\n")
    r = _scan(root, tmp_path / "home")["x.sh"]
    assert r.status == shm.W_INDETERMINATE
    assert any(reg == shm.REG_GIT_HOOKS for reg, _ in r.registries_skipped)
    assert "not a git work tree" in r.scope_line()


# --- claude-settings registry: the case the bead got WRONG -------------------

def test_enumerates_all_registries(tmp_path):
    """Per hook, report WHICH registry wires it. The positive control is the
    exact case four people got wrong: a hook wired ONLY via settings.json, with
    no git hook of that name anywhere, must read WIRED — and must name
    claude-settings as the reason, not merely say 'yes'."""
    root = _fixture_repo(tmp_path / "repo", chain=False)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    settings = home / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{
        "matcher": "Bash",
        "hooks": [{"type": "command",
                   "command": "bash ~/.claude/hooks/multi-manager/pre-push-rebase-test.sh"}],
    }]}}))
    # The installed copy the settings entry actually points at.
    inst = home / ".claude" / "hooks" / "multi-manager"
    inst.mkdir(parents=True)
    (inst / "pre-push-rebase-test.sh").write_text("#!/usr/bin/env bash\necho hook\n")

    reports = _scan(root, home, settings_paths=[settings])
    r = reports["pre-push-rebase-test.sh"]

    assert r.status == shm.W_ACTIVE, r.status
    assert r.registry_names() == (shm.REG_CLAUDE_SETTINGS,)
    assert shm.REG_GIT_HOOKS not in r.registry_names()   # core.hooksPath is silent
    w = r.wirings[0]
    assert w.detail == "PreToolUse:Bash"
    assert w.site == str(settings)

    # EVERY primary registry must appear as checked — the whole defect was a
    # verdict from one registry passing as a verdict about wiring.
    for reg in shm.PRIMARY_REGISTRIES:
        assert reg in r.registries_checked
    assert not r.registries_skipped

    # NEGATIVE CONTROL — a sibling hook in the same repo, same scan, absent
    # from that same settings.json, must come back UNWIRED. Without this the
    # test would pass against a tool hardcoded to answer ACTIVE.
    root2 = _fixture_repo(tmp_path / "repo2", chain=False, extra_hooks=("lonely.sh",))
    other = _scan(root2, tmp_path / "home2", settings_paths=[settings])["lonely.sh"]
    assert other.status == shm.W_UNWIRED


def test_unparseable_settings_file_is_skipped_not_read_as_empty(tmp_path):
    """An unreadable registry yields no hits, and 'no hits' is precisely what a
    genuinely-unwired hook looks like. Conflating them is the bug."""
    root = _fixture_repo(tmp_path / "repo", chain=False)
    bad = tmp_path / "broken-settings.json"
    bad.write_text("{ this is not json")
    r = _scan(root, tmp_path / "home", settings_paths=[bad])["pre-push-rebase-test.sh"]
    assert r.status == shm.W_INDETERMINATE
    assert any(reg == shm.REG_CLAUDE_SETTINGS for reg, _ in r.registries_skipped)

    # CONTROL: the same file, valid and simply not mentioning the hook, DOES
    # license the negative verdict.
    ok = tmp_path / "ok-settings.json"
    ok.write_text(json.dumps({"hooks": {}}))
    r2 = _scan(root, tmp_path / "home2", settings_paths=[ok])["pre-push-rebase-test.sh"]
    assert r2.status == shm.W_UNWIRED


def test_settings_entry_naming_a_missing_file_is_wired_but_missing(tmp_path):
    root = _fixture_repo(tmp_path / "repo", chain=False)
    home = tmp_path / "home"
    s = tmp_path / "s.json"
    s.write_text(json.dumps({"hooks": {"PreToolUse": [{
        "matcher": "Bash",
        "hooks": [{"type": "command",
                   "command": "bash ~/.claude/hooks/multi-manager/pre-push-rebase-test.sh"}],
    }]}}))
    # No installed copy exists at the referenced path.
    r = _scan(root, home, settings_paths=[s])["pre-push-rebase-test.sh"]
    assert r.wirings and r.wirings[0].target_exists is False
    assert r.status == shm.W_ACTIVE   # repo file exists; the miss is the target
    assert any("*** TARGET MISSING ***" in line
               for line in shm.render_wiring_report([r]).splitlines())


def test_all_four_settings_files_are_layered(tmp_path):
    """The overlooked registry must not be overlooked a second time by reading
    only the user file: project settings wire hooks too."""
    root = tmp_path / "repo"
    root.mkdir()
    home = tmp_path / "home"
    cands = [str(p) for p in shm.settings_candidates(str(home), root)]
    assert str(home / ".claude" / "settings.json") in cands
    assert str(home / ".claude" / "settings.local.json") in cands
    assert str(root / ".claude" / "settings.json") in cands
    assert str(root / ".claude" / "settings.local.json") in cands


# --- PATH registry ----------------------------------------------------------

def test_path_registry_finds_and_misses_correctly(tmp_path):
    root = _fixture_repo(tmp_path / "repo", chain=False)
    home = tmp_path / "home"
    home.mkdir()
    pdir = tmp_path / "pathbin"
    pdir.mkdir()
    (pdir / "pre-push-rebase-test.sh").write_text("#!/bin/bash\n")
    r = _scan(root, home, path_env=str(pdir))["pre-push-rebase-test.sh"]
    assert shm.REG_PATH in r.registry_names()
    assert r.status == shm.W_ACTIVE

    # CONTROL: an empty PATH dir is checked and finds nothing.
    empty = tmp_path / "emptybin"
    empty.mkdir()
    r2 = _scan(root, tmp_path / "home2", path_env=str(empty))["pre-push-rebase-test.sh"]
    assert shm.REG_PATH not in r2.registry_names()
    assert shm.REG_PATH in r2.registries_checked
    assert r2.status == shm.W_UNWIRED


def test_empty_path_is_skipped_not_treated_as_checked(tmp_path):
    dirs, skipped = shm.scan_path_dirs("")
    assert dirs == [] and skipped
    dirs2, skipped2 = shm.scan_path_dirs(str(tmp_path))
    assert dirs2 and not skipped2


# --- install shape: SYMLINK tracks the repo, COPY does not ------------------

def test_install_shape_symlink_vs_copy_vs_absent(tmp_path):
    repo_file = tmp_path / "hook.sh"
    repo_file.write_text("original\n")

    missing = shm.inspect_install(repo_file, tmp_path / "nope.sh")
    assert missing.shape == shm.SHAPE_ABSENT

    link = tmp_path / "linked.sh"
    link.symlink_to(repo_file)
    li = shm.inspect_install(repo_file, link)
    assert li.shape == shm.SHAPE_SYMLINK
    assert li.tracks_repo is True

    copy = tmp_path / "copied.sh"
    copy.write_text("original\n")
    ci = shm.inspect_install(repo_file, copy)
    assert ci.shape == shm.SHAPE_COPY
    assert ci.tracks_repo is False          # byte-identity is not tracking
    assert ci.content == shm.CONTENT_MATCHES

    # The whole point of reporting shape: edit the repo file and the COPY goes
    # stale while the SYMLINK cannot. Both polarities, one assertion each.
    repo_file.write_text("landed a change\n")
    assert shm.inspect_install(repo_file, copy).content == shm.CONTENT_DRIFTED
    assert shm.inspect_install(repo_file, link).content == shm.CONTENT_MATCHES


def test_symlink_pointing_elsewhere_does_not_claim_to_track(tmp_path):
    repo_file = tmp_path / "hook.sh"
    repo_file.write_text("a\n")
    other = tmp_path / "other.sh"
    other.write_text("a\n")
    link = tmp_path / "l.sh"
    link.symlink_to(other)
    assert shm.inspect_install(repo_file, link).tracks_repo is False


# --- source-chain: HEURISTIC, and it must not cry wolf in either direction ---

def test_prose_in_an_executable_file_is_not_an_invocation(tmp_path):
    """A docstring paragraph recording that a hook was RETIRED must not read as
    evidence the hook is live. This exact shape promoted a de-wired hook to
    ACTIVE before the tokenizer fix.

    The docstring line is byte-identical to a real invocation, so the prose gate
    is the ONLY thing that can produce the expected answer — an earlier version
    of this test used prose that failed the command-boundary check anyway, and
    so passed with the prose gate ripped out entirely."""
    py = ('#!/usr/bin/env python3\n'
          '"""Usage:\n'
          'bash hooks/multi-manager/pre-dispatch-refresh.sh   # retired, do not use\n'
          '"""\n'
          'x = 1\n')
    prose = shm.prose_line_numbers(py)
    assert 3 in prose and 5 not in prose

    root = tmp_path / "repo"
    (root / "hooks" / "multi-manager").mkdir(parents=True)
    (root / "hooks" / "multi-manager" / "pre-dispatch-refresh.sh").write_text("#!/bin/bash\n")
    (root / "bin").mkdir()
    (root / "bin" / "tool").write_text(py)

    # Asserted through scan_references, not just the helper: the question is
    # whether the gate is WIRED INTO the scan, which is this bead's own lesson.
    refs = shm.scan_references(root, ["pre-dispatch-refresh.sh"])["pre-dispatch-refresh.sh"]
    assert refs, "the reference was not collected at all"
    assert all(not r.exec_shaped for r in refs)
    assert all(not r.is_invocation() for r in refs)

    # CONTROL: the SAME line outside the docstring IS an invocation, so this is
    # not passing by never detecting anything.
    (root / "bin" / "runner.sh").write_text(
        '#!/usr/bin/env bash\nbash hooks/multi-manager/pre-dispatch-refresh.sh\n')
    refs2 = shm.scan_references(root, ["pre-dispatch-refresh.sh"])["pre-dispatch-refresh.sh"]
    assert any(r.exec_shaped and r.is_invocation() for r in refs2)


def test_markdown_and_json_never_promote_to_wired(tmp_path):
    root = tmp_path / "repo"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "h.sh").write_text("#!/bin/bash\n")
    # A fenced block whose line is byte-identical to a real invocation, so the
    # file-type gate is the ONLY thing standing between it and a WIRED verdict.
    (root / "doc.md").write_text("Run it:\n\n```\nbash hooks/h.sh\n```\n")
    refs = shm.scan_references(root, ["h.sh"])["h.sh"]
    md = [r for r in refs if r.path.endswith(".md")]
    assert md and md[0].exec_shaped is True       # it does look like a command
    assert md[0].executable_file is False         # ...in a file that cannot run
    assert md[0].is_invocation() is False

    # CONTROL: byte-identical line in a .sh file DOES count.
    (root / "caller.sh").write_text("#!/usr/bin/env bash\nbash hooks/h.sh\n")
    refs2 = shm.scan_references(root, ["h.sh"])["h.sh"]
    assert any(r.path.endswith(".sh") and r.is_invocation() for r in refs2)


def test_test_only_callers_do_not_count_as_production_wiring(tmp_path):
    root = tmp_path / "repo"
    (root / "hooks" / "test").mkdir(parents=True)
    (root / "hooks" / "h.sh").write_text("#!/bin/bash\n")
    (root / "hooks" / "test" / "test-h.sh").write_text(
        "#!/usr/bin/env bash\nbash hooks/h.sh\n")
    refs = shm.scan_references(root, ["h.sh"])["h.sh"]
    assert refs and all(r.is_test for r in refs)
    assert all(not r.is_invocation() for r in refs)

    # CONTROL: the identical invocation from a non-test file counts.
    (root / "prod.sh").write_text("#!/usr/bin/env bash\nbash hooks/h.sh\n")
    refs2 = shm.scan_references(root, ["h.sh"])["h.sh"]
    assert any(r.is_invocation() for r in refs2)


def test_sourcing_idiom_with_spaces_in_the_path_is_detected():
    """`. "$(dirname "${BASH_SOURCE[0]}")/lib-hook-trace.sh"` is this repo's
    standard sourcing line. A \\S* path pattern missed every one and reported
    three genuinely-sourced libraries as unwired."""
    line = '. "$(dirname "${BASH_SOURCE[0]}")/lib-hook-trace.sh"'
    assert shm._exec_shaped(line, "lib-hook-trace.sh") is True
    # CONTROL: a mention of the same file in prose is not an invocation.
    assert shm._exec_shaped("see lib-hook-trace.sh for details",
                            "lib-hook-trace.sh") is False


def test_sentence_period_is_not_the_source_shorthand():
    """The bare `.` alternation, unanchored, matched every sentence-ending
    period — turning documentation into evidence of invocation."""
    line = "notes are in `upgrade-notes.md`. (`pre-dispatch-refresh.sh` was retired)"
    assert shm._exec_shaped(line, "pre-dispatch-refresh.sh") is False
    # CONTROL: a real source at command position still matches.
    assert shm._exec_shaped(". ./pre-dispatch-refresh.sh",
                            "pre-dispatch-refresh.sh") is True


def test_variable_indirection_is_followed(tmp_path):
    """IMPL="$DIR/tree-claim-impl.sh" ... bash "$IMPL" — the executing line never
    names the file and the naming line never looks like a command."""
    text = ('#!/usr/bin/env bash\n'
            'IMPL="$SELF_DIR/tree-claim-impl.sh"\n'
            'OUT=$(printf x | bash "$IMPL")\n')
    assert shm.indirect_exec_lines(text, "tree-claim-impl.sh") == {2}

    # CONTROL: assigned but never executed -> not an invocation. Without this
    # the check would count every mention of a path in an assignment.
    text2 = ('#!/usr/bin/env bash\n'
             'IMPL="$SELF_DIR/tree-claim-impl.sh"\n'
             'echo "the impl lives at $IMPL"\n')
    assert shm.indirect_exec_lines(text2, "tree-claim-impl.sh") == set()

    # And prove the helper is actually WIRED INTO the scan rather than merely
    # correct in isolation — the distinction this whole bead is about. Without
    # this, breaking the call site in scan_references leaves the test green.
    root = tmp_path / "repo"
    mm = root / "hooks" / "multi-manager"
    mm.mkdir(parents=True)
    (mm / "tree-claim-impl.sh").write_text("#!/bin/bash\n")
    (mm / "tree-claim.sh").write_text(text)
    refs = shm.scan_references(root, ["tree-claim-impl.sh"])["tree-claim-impl.sh"]
    assert any(r.is_invocation() for r in refs), \
        "scan_references does not consult indirect_exec_lines"

    # CONTROL: the assign-but-never-execute variant stays a non-invocation
    # through the same scan.
    (mm / "tree-claim.sh").write_text(text2)
    refs2 = shm.scan_references(root, ["tree-claim-impl.sh"])["tree-claim-impl.sh"]
    assert not any(r.is_invocation() for r in refs2)


def test_strip_comment_removes_trailing_and_whole_line_comments():
    assert shm.strip_comment("bash x.sh # run it").strip() == "bash x.sh"
    assert shm.strip_comment("# bash x.sh").strip() == ""
    assert shm.strip_comment("bash x.sh").strip() == "bash x.sh"


def test_libraries_are_labelled_so_they_are_not_reported_as_dead(tmp_path):
    root = tmp_path / "repo"
    mm = root / "hooks" / "multi-manager"
    mm.mkdir(parents=True)
    (mm / "lib-thing.sh").write_text("#!/bin/bash\n")
    (mm / "hook.sh").write_text("#!/bin/bash\n")
    roles = {p.name: role for p, role in shm.enumerate_artifacts(root)}
    assert roles["lib-thing.sh"] == "library"
    assert roles["hook.sh"] == "hook"


# --- scratch-home discipline (SABLE-2avau) ----------------------------------

def test_tilde_expands_against_the_given_home_not_the_live_one():
    """The tests run against a scratch HOME and must not be able to reach the
    real ~/.claude by accident."""
    got = shm.expand_path_token("~/.claude/hooks/x.sh", "/scratch/home")
    assert got == "/scratch/home/.claude/hooks/x.sh"
    assert shm.expand_path_token("$HOME/.claude/x.sh", "/scratch/home") == \
        "/scratch/home/.claude/x.sh"
    # CONTROL: an absolute path is left alone.
    assert shm.expand_path_token("/abs/x.sh", "/scratch/home") == "/abs/x.sh"


def test_referenced_paths_pulls_the_installed_target_out_of_a_command():
    cmd = "bash ~/.claude/hooks/multi-manager/pre-push-rebase-test.sh"
    got = shm.referenced_paths(cmd, "/h")
    assert "/h/.claude/hooks/multi-manager/pre-push-rebase-test.sh" in got
    assert "bash" not in got


# --- report rendering -------------------------------------------------------

def test_report_states_its_scope_on_every_negative_verdict(tmp_path):
    """Silence about scope IS the defect: a NOT-WIRED with no statement of what
    was checked is the shape of the original mistake."""
    root = _fixture_repo(tmp_path / "repo", chain=False)
    r = _scan(root, tmp_path / "home")["pre-push-rebase-test.sh"]
    out = shm.render_wiring_report([r])
    assert shm.W_UNWIRED in out
    assert "scope" in out
    for reg in shm.PRIMARY_REGISTRIES:
        assert reg in out
    assert "HEURISTIC" in out    # source-chain's limits stated, not implied


def test_json_report_names_registries_checked_and_not_checked(tmp_path):
    root = _fixture_repo(tmp_path / "repo", chain=False)
    r = _scan(root, tmp_path / "home",
              settings_paths=[tmp_path / "missing.json"])["pre-push-rebase-test.sh"]
    d = shm.wiring_to_dict(r)
    assert set(d) >= {"status", "install", "registries_wiring_it",
                      "registries_checked", "registries_not_checked",
                      "wirings", "invocation_sites"}
    assert d["status"] == shm.W_UNWIRED
    assert shm.REG_CLAUDE_SETTINGS in d["registries_checked"]


def test_cli_wiring_mode_runs_and_emits_json(tmp_path, capsys):
    root = _fixture_repo(tmp_path / "repo", chain=True)
    home = tmp_path / "home"
    home.mkdir()
    rc = shm.main(["--wiring", "--json", "--repo", str(root),
                   "--home", str(home), "--path-env", str(home)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["registries"] == list(shm.REGISTRIES)
    names = {r["name"]: r for r in payload["reports"]}
    assert names["pre-push-rebase-test.sh"]["status"] == shm.W_ACTIVE


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
