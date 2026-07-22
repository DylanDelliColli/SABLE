#!/usr/bin/env python3
"""Unit tests for bin/sable-contained (SABLE-gdp05).

Covers the decision logic (verdict reconciliation from raw git exit codes),
the ref-resolution fallback, and — the point of the whole bead — that the
CLI interface makes the argument-order inversion UNREPRESENTABLE: there is
exactly one positional argument, so there is no second slot an inverted
`<integration> <sha>` call could occupy.

test_contained_and_uncontained_verdicts uses a REAL fixture git repo (a
merged branch and an unmerged branch) rather than stubbing git for that one
case: the whole bead is about a real git plumbing footgun, and a stub of
merge-base would just replay this file's own assumption about what it does.
"""
import importlib.util
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

_LOADER = SourceFileLoader(
    "sable_contained", str(Path(__file__).resolve().parent / "sable-contained")
)
_SPEC = importlib.util.spec_from_loader("sable_contained", _LOADER)
sc = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(sc)


def _run(*args, cwd):
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, text=True)


def _git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run("git", "init", "-q", cwd=str(repo))
    _run("git", "config", "user.email", "test@example.invalid", cwd=str(repo))
    _run("git", "config", "user.name", "SABLE Test", cwd=str(repo))
    return repo


# --- is_ancestor_verdict -----------------------------------------------------

def test_is_ancestor_verdict_zero_is_contained():
    assert sc.is_ancestor_verdict(0) == sc.CONTAINED


def test_is_ancestor_verdict_one_is_not_contained():
    assert sc.is_ancestor_verdict(1) == sc.NOT_CONTAINED


def test_is_ancestor_verdict_other_codes_are_unresolved_never_guessed():
    assert sc.is_ancestor_verdict(128) == sc.UNRESOLVED
    assert sc.is_ancestor_verdict(2) == sc.UNRESOLVED


# --- log_range_verdict --------------------------------------------------------

def test_log_range_verdict_empty_output_is_contained():
    assert sc.log_range_verdict(0, "") == sc.CONTAINED
    assert sc.log_range_verdict(0, "\n") == sc.CONTAINED


def test_log_range_verdict_nonempty_output_is_not_contained():
    assert sc.log_range_verdict(0, "abc123 some commit\n") == sc.NOT_CONTAINED


def test_log_range_verdict_nonzero_rc_is_unresolved():
    assert sc.log_range_verdict(128, "") == sc.UNRESOLVED


# --- combine_verdicts ---------------------------------------------------------

def test_combine_verdicts_agreeing_contained():
    assert sc.combine_verdicts(sc.CONTAINED, sc.CONTAINED) == (sc.CONTAINED, True)


def test_combine_verdicts_agreeing_not_contained():
    assert sc.combine_verdicts(sc.NOT_CONTAINED, sc.NOT_CONTAINED) == (sc.NOT_CONTAINED, True)


def test_combine_verdicts_disagreement_is_loud_not_a_guess():
    verdict, agree = sc.combine_verdicts(sc.CONTAINED, sc.NOT_CONTAINED)
    assert verdict == sc.DISAGREE
    assert agree is False


def test_combine_verdicts_either_side_unresolved_is_unresolved():
    assert sc.combine_verdicts(sc.UNRESOLVED, sc.CONTAINED) == (sc.UNRESOLVED, False)
    assert sc.combine_verdicts(sc.CONTAINED, sc.UNRESOLVED) == (sc.UNRESOLVED, False)
    verdict, agree = sc.combine_verdicts(sc.UNRESOLVED, sc.UNRESOLVED)
    assert verdict == sc.UNRESOLVED
    assert agree is True


# --- render_report -------------------------------------------------------------

def test_render_report_contained_names_both_refs():
    out = sc.render_report("deadbeef", "origin/tmux-only",
                            {"verdict": sc.CONTAINED, "method_a": sc.CONTAINED,
                             "method_b": sc.CONTAINED, "agree": True})
    assert out.startswith("CONTAINED:")
    assert "deadbeef" in out and "origin/tmux-only" in out


def test_render_report_not_contained():
    out = sc.render_report("deadbeef", "origin/tmux-only",
                            {"verdict": sc.NOT_CONTAINED, "method_a": sc.NOT_CONTAINED,
                             "method_b": sc.NOT_CONTAINED, "agree": True})
    assert out.startswith("NOT-CONTAINED:")


def test_render_report_disagreement_is_loud_and_names_both_methods():
    out = sc.render_report("deadbeef", "origin/tmux-only",
                            {"verdict": sc.DISAGREE, "method_a": sc.CONTAINED,
                             "method_b": sc.NOT_CONTAINED, "agree": False})
    assert "DISAGREEMENT" in out
    assert sc.CONTAINED in out and sc.NOT_CONTAINED in out


def test_render_report_unresolved_says_could_not_assess():
    out = sc.render_report("deadbeef", "origin/tmux-only",
                            {"verdict": sc.UNRESOLVED, "method_a": sc.UNRESOLVED,
                             "method_b": sc.UNRESOLVED, "agree": True})
    assert "COULD NOT ASSESS" in out


# --- resolve_integration_ref (real repo, no stubbing) -------------------------

def test_resolve_integration_ref_prefers_published_origin(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "f.txt").write_text("x")
    _run("git", "add", "f.txt", cwd=str(repo))
    _run("git", "commit", "-qm", "base", cwd=str(repo))
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True, check=True).stdout.strip()
    # A remote-tracking ref with no real remote configured — resolve_integration_ref
    # only needs the ref to exist, not a reachable network remote.
    _run("git", "update-ref", "refs/remotes/origin/tmux-only", sha, cwd=str(repo))
    assert sc.resolve_integration_ref(str(repo), "tmux-only") == "origin/tmux-only"


def test_resolve_integration_ref_falls_back_to_local_when_unpublished(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "f.txt").write_text("x")
    _run("git", "add", "f.txt", cwd=str(repo))
    _run("git", "checkout", "-qb", "tmux-only", cwd=str(repo))
    _run("git", "commit", "-qm", "base", cwd=str(repo))
    assert sc.resolve_integration_ref(str(repo), "tmux-only") == "tmux-only"


def test_resolve_integration_ref_empty_when_neither_resolves(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "f.txt").write_text("x")
    _run("git", "add", "f.txt", cwd=str(repo))
    _run("git", "commit", "-qm", "base", cwd=str(repo))
    assert sc.resolve_integration_ref(str(repo), "no-such-branch") == ""


# --- containment_verdict: real fixture repo, merged sha vs unmerged sha ------
#
# The bead's own spec: "given a fixture repo with a merged sha and an unmerged
# sha, assert CONTAINED and NOT-CONTAINED respectively". This also doubles as
# the false-green negative control (SABLE-5lli class): UNMERGED_SHA is forked
# from the CURRENT integration tip, which is exactly the shape where the
# INVERTED merge-base call ("--is-ancestor origin/<int> <sha>") would wrongly
# return 0/true (the integration tip trivially IS an ancestor of a branch
# freshly forked from it) — a spurious CONTAINED for something that is not.

def test_contained_and_uncontained_verdicts(tmp_path):
    repo = _git_repo(tmp_path)
    _run("git", "checkout", "-qb", "tmux-only", cwd=str(repo))
    (repo / "base.txt").write_text("base")
    _run("git", "add", "base.txt", cwd=str(repo))
    _run("git", "commit", "-qm", "base", cwd=str(repo))
    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                               capture_output=True, text=True, check=True).stdout.strip()

    # A branch that gets merged back into tmux-only.
    _run("git", "checkout", "-qb", "wk-merged", cwd=str(repo))
    (repo / "merged.txt").write_text("merged")
    _run("git", "add", "merged.txt", cwd=str(repo))
    _run("git", "commit", "-qm", "merged work", cwd=str(repo))
    merged_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                                 capture_output=True, text=True, check=True).stdout.strip()
    _run("git", "checkout", "-q", "tmux-only", cwd=str(repo))
    _run("git", "merge", "-q", "--no-ff", "--no-edit", "wk-merged", cwd=str(repo))

    # Publish tmux-only as a remote-tracking ref (no real remote needed).
    tip_sha = subprocess.run(["git", "rev-parse", "tmux-only"], cwd=str(repo),
                              capture_output=True, text=True, check=True).stdout.strip()
    _run("git", "update-ref", "refs/remotes/origin/tmux-only", tip_sha, cwd=str(repo))

    # A branch forked from the CURRENT (post-merge) tip that never merges back.
    _run("git", "checkout", "-qb", "wk-unmerged", cwd=str(repo))
    (repo / "unmerged.txt").write_text("unmerged")
    _run("git", "add", "unmerged.txt", cwd=str(repo))
    _run("git", "commit", "-qm", "unmerged work", cwd=str(repo))
    unmerged_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                                   capture_output=True, text=True, check=True).stdout.strip()

    # Sanity: the false-green shape actually holds in this fixture — the
    # inverted call reports the unmerged branch as a false ancestor.
    inverted = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "origin/tmux-only", unmerged_sha],
        cwd=str(repo))
    assert inverted.returncode == 0, (
        "fixture: unmerged branch must be a descendant of tmux-only's tip so "
        "the inverted merge-base call would (wrongly) say CONTAINED"
    )

    merged_result = sc.containment_verdict(str(repo), merged_sha, "origin/tmux-only")
    assert merged_result["verdict"] == sc.CONTAINED
    assert merged_result["agree"] is True

    unmerged_result = sc.containment_verdict(str(repo), unmerged_sha, "origin/tmux-only")
    assert unmerged_result["verdict"] == sc.NOT_CONTAINED
    assert unmerged_result["agree"] is True

    # base_sha is an ancestor of both branches, correctly contained.
    base_result = sc.containment_verdict(str(repo), base_sha, "origin/tmux-only")
    assert base_result["verdict"] == sc.CONTAINED


# --- CLI: the inversion is unrepresentable ------------------------------------

def test_inverted_usage_cannot_be_expressed():
    """There is exactly one positional argument. An inverted call that tries
    to pass BOTH the integration ref and the sha as positionals (the shape of
    the live incident: `--is-ancestor origin/tmux-only <sha>`) is a usage
    error, not a silently-accepted swap."""
    parser = sc.build_parser()
    try:
        parser.parse_args(["origin/tmux-only", "deadbeef"])
    except SystemExit as exc:
        assert exc.code == sc.EXIT_USAGE
    else:
        raise AssertionError("parser accepted two positionals — the inversion "
                              "is representable, which is the exact defect")


def test_parser_accepts_exactly_one_positional_sha():
    args = sc.build_parser().parse_args(["deadbeef"])
    assert args.sha == "deadbeef"
    assert args.integration_branch is None
    assert args.format == "text"


def test_parser_has_no_flag_to_name_the_base_ref_positionally():
    """--integration-branch is a named override of the BRANCH NAME, not a
    second positional — it cannot be used to supply an inverted ref/sha pair
    in argument-order form."""
    args = sc.build_parser().parse_args(["deadbeef", "--integration-branch", "main"])
    assert args.sha == "deadbeef"
    assert args.integration_branch == "main"


# --- main(): exit codes and could-not-assess path -----------------------------

def test_main_exit_code_contained(tmp_path, capsys):
    repo = _git_repo(tmp_path)
    _run("git", "checkout", "-qb", "tmux-only", cwd=str(repo))
    (repo / "f.txt").write_text("x")
    _run("git", "add", "f.txt", cwd=str(repo))
    _run("git", "commit", "-qm", "base", cwd=str(repo))
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True, check=True).stdout.strip()
    rc = sc.main([sha, "--repo", str(repo), "--integration-branch", "tmux-only"])
    out = capsys.readouterr().out
    assert rc == sc.EXIT_CONTAINED
    assert "CONTAINED" in out


def test_main_exit_code_unresolved_when_integration_branch_absent(tmp_path, capsys):
    repo = _git_repo(tmp_path)
    (repo / "f.txt").write_text("x")
    _run("git", "add", "f.txt", cwd=str(repo))
    _run("git", "commit", "-qm", "base", cwd=str(repo))
    rc = sc.main(["HEAD", "--repo", str(repo), "--integration-branch", "no-such-branch"])
    out = capsys.readouterr().out
    assert rc == sc.EXIT_UNRESOLVED
    assert "COULD NOT ASSESS" in out


def test_main_json_format_is_valid_json(tmp_path, capsys):
    import json
    repo = _git_repo(tmp_path)
    _run("git", "checkout", "-qb", "tmux-only", cwd=str(repo))
    (repo / "f.txt").write_text("x")
    _run("git", "add", "f.txt", cwd=str(repo))
    _run("git", "commit", "-qm", "base", cwd=str(repo))
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True, check=True).stdout.strip()
    rc = sc.main([sha, "--repo", str(repo), "--integration-branch", "tmux-only",
                  "--format", "json"])
    parsed = json.loads(capsys.readouterr().out)
    assert rc == sc.EXIT_CONTAINED
    assert parsed["verdict"] == sc.CONTAINED
    assert parsed["agree"] is True
