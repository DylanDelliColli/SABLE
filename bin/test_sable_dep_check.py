#!/usr/bin/env python3
"""Unit tests for bin/sable-dep-check (SABLE-d5iku).

Pure-function coverage of the decision logic: which dependency edges can
falsely release, how a blocker's branch is resolved, and how a git ancestry
result becomes (or does NOT become) a warning.

BOTH DIRECTIONS ARE TESTED DELIBERATELY. A check that only proves it fires on
the bad case has traded a false-go for a false-block — the bead names that
trade explicitly. So every "warns" case here has a paired "stays silent" case:
open blocker, non-blocking edge, merged branch, pruned branch, unresolvable
ancestry.
"""
import importlib.util
import json
import os
from importlib.machinery import SourceFileLoader
from pathlib import Path

_LOADER = SourceFileLoader(
    "sable_dep_check", str(Path(__file__).resolve().parent / "sable-dep-check")
)
_SPEC = importlib.util.spec_from_loader("sable_dep_check", _LOADER)
sdc = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(sdc)


# --- which edges can falsely release ----------------------------------------

def test_only_blocks_edges_can_falsely_release():
    """relates-to and parent-child never gate readiness, so neither can produce
    a false release however its partner is sequenced."""
    edges = sdc.normalize_dep_records([
        {"id": "A", "dependency_type": "blocks", "status": "closed"},
        {"id": "B", "dependency_type": "relates-to", "status": "closed"},
        {"id": "C", "dependency_type": "parent-child", "status": "closed"},
    ], ["D"])
    assert [e["blocker"] for e in sdc.released_edges(edges, {})] == ["A"]


# --- the two shapes bd emits ------------------------------------------------

def test_normalize_single_id_shape_backfills_the_dependent():
    """`bd dep list <one-id>` returns enriched bead records with no
    back-reference to the bead asked about — it has to come from the request."""
    deps = [{"id": "BLK", "dependency_type": "blocks", "status": "closed"}]
    out = sdc.normalize_dep_records(deps, ["DEP"])
    assert out == [{"dependent": "DEP", "blocker": "BLK",
                    "type": "blocks", "status": "closed"}]


def test_normalize_batched_shape_carries_provenance_but_no_status():
    """`bd dep list <many ids>` returns raw edges — different keys, and no
    status at all. Reading only the single-id shape would drop every edge in a
    --ready sweep."""
    deps = [{"issue_id": "DEP1", "depends_on_id": "BLK1", "type": "blocks"},
            {"issue_id": "DEP2", "depends_on_id": "BLK2", "type": "relates-to"}]
    out = sdc.normalize_dep_records(deps, ["DEP1", "DEP2"])
    assert out == [
        {"dependent": "DEP1", "blocker": "BLK1", "type": "blocks", "status": ""},
        {"dependent": "DEP2", "blocker": "BLK2", "type": "relates-to", "status": ""},
    ]


def test_normalize_drops_records_with_no_resolvable_dependent():
    """Batched shape + several requested ids: a record with no issue_id cannot
    be attributed, and guessing would put a warning on the wrong bead."""
    deps = [{"id": "BLK", "dependency_type": "blocks", "status": "closed"}]
    assert sdc.normalize_dep_records(deps, ["A", "B"]) == []


def test_normalize_tolerates_garbage():
    assert sdc.normalize_dep_records(None, ["A"]) == []
    assert sdc.normalize_dep_records(["junk"], ["A"]) == []


def test_released_edges_only_closed_blockers():
    edges = sdc.normalize_dep_records(
        [{"issue_id": "D", "depends_on_id": "A", "type": "blocks"},
         {"issue_id": "D", "depends_on_id": "B", "type": "blocks"},
         {"issue_id": "D", "depends_on_id": "C", "type": "blocks"}], ["D"])
    status = {"A": "closed", "B": "open", "C": "in_progress"}
    assert [e["blocker"] for e in sdc.released_edges(edges, status)] == ["A"]


def test_released_edges_ignores_closed_relates_to():
    """A closed relates-to partner with an unmerged branch is NOT a false
    release — that edge never gated readiness in the first place."""
    edges = sdc.normalize_dep_records(
        [{"issue_id": "D", "depends_on_id": "B", "type": "relates-to"}], ["D"])
    assert sdc.released_edges(edges, {"B": "closed"}) == []


def test_released_edges_falls_back_to_edge_status():
    """Single-id shape carries status on the edge itself; a lookup-only rule
    would find nothing when the bead fetch came up empty."""
    edges = sdc.normalize_dep_records(
        [{"id": "A", "dependency_type": "blocks", "status": "closed"}], ["D"])
    assert [e["blocker"] for e in sdc.released_edges(edges, {})] == ["A"]


# --- blocker -> branch resolution -------------------------------------------

def test_metadata_branch_is_read_exactly():
    assert sdc.metadata_branch({"metadata": {"branch": "wk-pin-refresh"}}) == "wk-pin-refresh"
    assert sdc.metadata_branch({"metadata": {"branch": "  wk-x  "}}) == "wk-x"


def test_metadata_branch_absent_forms():
    assert sdc.metadata_branch({}) == ""
    assert sdc.metadata_branch({"metadata": None}) == ""
    assert sdc.metadata_branch({"metadata": {}}) == ""
    assert sdc.metadata_branch(None) == ""


def test_prose_branch_candidates_finds_branch_in_a_sentence():
    """The exact live shape: SABLE-9boz4's close_reason names its branch in
    prose because it predates the `branch` metadata tag."""
    text = "Pushed to origin/wk-pin-refresh at 09295c5."
    assert sdc.prose_branch_candidates(text) == ["wk-pin-refresh"]


def test_prose_branch_candidates_strips_trailing_punctuation():
    assert sdc.prose_branch_candidates("see wk-foo, wk-bar) and wk-baz.") == [
        "wk-foo", "wk-bar", "wk-baz"]


def test_prose_branch_candidates_dedupes_preserving_order():
    assert sdc.prose_branch_candidates("wk-b then wk-a then wk-b") == ["wk-b", "wk-a"]


def test_prose_branch_candidates_ignores_non_wk_tokens():
    assert sdc.prose_branch_candidates("merged tmux-only into main via feature-x") == []


def test_bead_prose_spans_description_notes_and_close_reason():
    bead = {"description": "d wk-one", "notes": "n wk-two", "close_reason": "c wk-three"}
    assert sdc.prose_branch_candidates(sdc.bead_prose(bead)) == ["wk-one", "wk-two", "wk-three"]


# --- ancestry verdict --------------------------------------------------------

def test_ancestry_verdict_zero_is_merged():
    assert sdc.ancestry_verdict(0) == "merged"


def test_ancestry_verdict_one_is_unmerged():
    assert sdc.ancestry_verdict(1) == "unmerged"


def test_ancestry_verdict_other_codes_are_unresolved_never_unmerged():
    """git exits 128 for a bad ref. Reading that as "unmerged" would
    manufacture warnings out of every deleted branch."""
    for rc in (2, 127, 128, 129):
        assert sdc.ancestry_verdict(rc) == "unresolved"


# --- report rendering --------------------------------------------------------

def _finding():
    return {"dependent": "SABLE-78kxu", "blocker": "SABLE-9boz4",
            "branch": "wk-pin-refresh", "integration_branch": "tmux-only"}


def test_format_warning_names_every_actor():
    line = sdc.format_warning("SABLE-78kxu", "SABLE-9boz4", "wk-pin-refresh", "tmux-only")
    for token in ("SABLE-78kxu", "SABLE-9boz4", "wk-pin-refresh", "tmux-only"):
        assert token in line


def test_render_report_is_empty_with_no_findings():
    """Silence is the clean signal — the hook's whole emit decision is whether
    this string is empty, so notes alone must never produce output."""
    assert sdc.render_report([], []) == ""
    assert sdc.render_report([], ["a note", "another"]) == ""


def test_render_report_names_the_bead_and_the_branch():
    out = sdc.render_report([_finding()], [])
    assert "UNMERGED-BLOCKER WARNING" in out
    assert "wk-pin-refresh" in out
    assert "SABLE-9boz4" in out


def test_render_report_appends_notes_when_there_is_a_finding():
    out = sdc.render_report([_finding()], ["SABLE-x: blocker SABLE-y — no branch"])
    assert "Unresolved" in out
    assert "SABLE-y" in out


# --- bd DB location vs merge-state repo -------------------------------------

def test_has_beads_db_walks_up(tmp_path):
    (tmp_path / ".beads").mkdir()
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert sdc.has_beads_db(str(deep)) is True


def test_has_beads_db_false_when_absent(tmp_path):
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert sdc.has_beads_db(str(deep)) is False


def test_resolve_bd_dir_prefers_explicit_override(tmp_path):
    override = tmp_path / "elsewhere"
    override.mkdir()
    assert sdc.resolve_bd_dir(str(tmp_path), str(override)) == str(override)


def test_resolve_bd_dir_uses_repo_when_it_has_a_db(tmp_path):
    (tmp_path / ".beads").mkdir()
    assert sdc.resolve_bd_dir(str(tmp_path)) == str(tmp_path)


def test_resolve_bd_dir_falls_back_to_cwd_for_a_db_less_repo(tmp_path):
    """--repo names the git repo whose ancestry answers the question; a fixture
    repo under /tmp has no bead DB, and bd must not be run there."""
    assert sdc.resolve_bd_dir(str(tmp_path)) == os.getcwd()


def test_exit_codes_are_distinct():
    """3, not 1, for "found a warning": a crash must never be readable as a
    finding."""
    assert sdc.EXIT_CLEAN == 0
    assert sdc.EXIT_USAGE == 2
    assert sdc.EXIT_WARN == 3
    assert len({sdc.EXIT_CLEAN, sdc.EXIT_USAGE, sdc.EXIT_WARN}) == 3


# --- COULD NOT ASSESS vs CLEAN (SABLE-d5iku, second revision) ----------------
# A guard that goes quiet when its data source vanishes is a guard that fails
# silent. In ci-verify 29867402197 bd was absent from the clean-room, this tool
# raised out of subprocess.run, and --format json produced EMPTY STDOUT — so
# every consumer parsing it died, and "could not assess" was indistinguishable
# from "assessed, nothing wrong" (SABLE-2az2x's defect, SABLE-6sdpx's class).
#
# Every case below is paired with its healthy-path complement: the unknown
# state must be loud, and the clean state must stay byte-for-byte silent, or
# the fix has simply traded silence for noise on every dispatch.

def _no_bd_env(monkeypatch, tmp_path):
    """A PATH with no `bd` on it — the clean-room condition, reproduced rather
    than simulated. Nothing here stubs the tool's own code."""
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def test_run_returns_127_instead_of_raising_when_the_binary_is_missing(tmp_path):
    """The root cause. subprocess.run raises FileNotFoundError for a missing
    executable; that traceback is what emptied stdout in CI."""
    cp = sdc._run(["definitely-not-a-real-binary-xyz"], cwd=str(tmp_path))
    assert cp.returncode == 127
    assert cp.stdout == ""


def test_run_still_reports_a_real_exit_code_when_the_binary_exists(tmp_path):
    """Complement: hardening the missing-binary case must not swallow the
    ordinary failed-call case."""
    cp = sdc._run(["false"], cwd=str(tmp_path))
    assert cp.returncode == 1


def test_bd_unavailable_reason_names_a_missing_bd(monkeypatch, tmp_path):
    _no_bd_env(monkeypatch, tmp_path)
    assert "PATH" in sdc.bd_unavailable_reason(str(tmp_path))


def test_bd_unavailable_reason_is_empty_when_bd_looks_usable(monkeypatch, tmp_path):
    """Complement: a healthy environment must produce NO reason, or every run
    reports itself unassessed."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "bd").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "bd").chmod(0o755)
    (tmp_path / ".beads").mkdir()
    monkeypatch.setenv("PATH", str(fake_bin))
    assert sdc.bd_unavailable_reason(str(tmp_path)) == ""


def test_check_beads_reports_unknown_not_clean_when_bd_is_absent(monkeypatch, tmp_path):
    """THE REGRESSION. With bd gone the tool must not return a clean, empty
    result — findings empty AND unknown empty is the sentence "I checked and
    everything is fine", which would be a lie."""
    _no_bd_env(monkeypatch, tmp_path)
    findings, notes, unknown = sdc.check_beads(
        str(tmp_path), str(tmp_path), "origin", "tmux-only", ["SABLE-x"])
    assert findings == []
    assert unknown, "bd absent must produce an explicit could-not-assess state"
    assert "SABLE-x" in " ".join(unknown)


def test_render_unknown_block_is_loud_and_says_it_is_not_clean():
    block = sdc.render_unknown_block(["could not read dependencies for SABLE-x"])
    assert "COULD NOT ASSESS" in block
    assert "NOT a clean result" in block
    assert "SABLE-x" in block


def test_render_unknown_block_is_empty_when_nothing_is_unassessed():
    """Complement: the healthy path stays silent. An advisory that speaks on
    every dispatch is one that gets routed around."""
    assert sdc.render_unknown_block([]) == ""


def test_render_report_surfaces_unknown_even_with_no_findings():
    """Notes alone stay silent (assessed and dismissed); unknown alone does
    NOT (never assessed). That distinction is the whole fix."""
    assert sdc.render_report([], ["a note"]) == ""
    out = sdc.render_report([], [], ["could not read dependencies for SABLE-x"])
    assert "COULD NOT ASSESS" in out


def test_render_report_keeps_both_a_finding_and_an_unknown():
    """A partial run that found something must report BOTH — the finding is
    real, and the unassessed remainder is still unassessed."""
    out = sdc.render_report([_finding()], [], ["SABLE-z: ancestry check failed"])
    assert "UNMERGED-BLOCKER WARNING" in out
    assert "COULD NOT ASSESS" in out
    assert "SABLE-z" in out


def test_main_emits_valid_json_with_assessed_false_when_bd_is_absent(
        monkeypatch, tmp_path, capsys):
    """End to end through main(), because the CI failure was a CONSUMER of the
    json contract, not an internal state: `json.load` on empty stdout."""
    _no_bd_env(monkeypatch, tmp_path)
    rc = sdc.main(["--repo", str(tmp_path), "--bd-dir", str(tmp_path),
                   "--format=json", "SABLE-x"])
    payload = json.loads(capsys.readouterr().out)   # would raise on empty stdout
    assert payload["assessed"] is False
    assert payload["unknown"]
    assert payload["findings"] == []
    assert rc == sdc.EXIT_UNKNOWN


def test_main_hook_format_is_non_empty_when_bd_is_absent(monkeypatch, tmp_path, capsys):
    """The hook/dispatch surface decides purely on emptiness, so an unassessed
    run has to break the silence there too — otherwise the dispatch path reads
    "nothing wrong" from a check that never ran."""
    _no_bd_env(monkeypatch, tmp_path)
    sdc.main(["--repo", str(tmp_path), "--bd-dir", str(tmp_path),
              "--format=hook", "SABLE-x"])
    assert "COULD NOT ASSESS" in capsys.readouterr().out


def test_exit_unknown_is_distinct_from_clean_and_warn():
    assert sdc.EXIT_UNKNOWN == 4
    assert len({sdc.EXIT_CLEAN, sdc.EXIT_USAGE, sdc.EXIT_WARN,
                sdc.EXIT_UNKNOWN}) == 4
