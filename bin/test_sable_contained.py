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

PATH MODE (SABLE-4snb4) is covered from `--- path mode` down, and follows the
same no-stubbing rule for the same reason, only harder: the defect there IS
`git ls-tree`'s exit status for an absent path, so a mocked git would
reproduce the bug's blind spot exactly. ::test_bad_lstree_idiom_would_false_positive
pins the raw git behaviour itself, so the fix keeps its meaning after
everyone has forgotten why it exists.
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


# =============================================================================
# path mode (SABLE-4snb4)
# =============================================================================

def _two_commit_repo(tmp_path):
    """tmux-only with base.txt at commit one and late.txt added at commit two,
    published as origin/tmux-only. Returns (repo, first_sha, second_sha) — the
    shape every path-mode assertion below needs: a path that is in one ref's
    tree and genuinely absent from the other's."""
    repo = _git_repo(tmp_path)
    _run("git", "checkout", "-qb", "tmux-only", cwd=str(repo))
    (repo / "base.txt").write_text("base")
    _run("git", "add", "base.txt", cwd=str(repo))
    _run("git", "commit", "-qm", "one", cwd=str(repo))
    first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                            capture_output=True, text=True, check=True).stdout.strip()
    (repo / "late.txt").write_text("added in the second commit")
    _run("git", "add", "late.txt", cwd=str(repo))
    _run("git", "commit", "-qm", "two", cwd=str(repo))
    second = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                             capture_output=True, text=True, check=True).stdout.strip()
    _run("git", "update-ref", "refs/remotes/origin/tmux-only", second, cwd=str(repo))
    return repo, first, second


# --- the defect itself, pinned against real git -------------------------------

def test_bad_lstree_idiom_would_false_positive(tmp_path):
    """THE REGRESSION THAT KEEPS THIS FIX MEANINGFUL. `git ls-tree <ref>
    <absent-path>` exits 0 with EMPTY stdout, so the natural-looking probe

        git ls-tree <ref> <path> --name-only && echo "PRESENT"

    prints PRESENT for a file that is NOT in the ref — a FALSE POSITIVE, i.e.
    the hold-RELEASING direction (SABLE-4snb4, live 2026-07-22). Real git, no
    mock: this test asserts what git actually does, which is the entire
    premise of the wrapper. If a future git ever made this exit non-zero,
    this test failing is exactly the notification we would want."""
    repo, first, _second = _two_commit_repo(tmp_path)
    probe = subprocess.run(["git", "ls-tree", first, "late.txt", "--name-only"],
                            cwd=str(repo), capture_output=True, text=True)
    assert probe.returncode == 0, (
        "premise of the bead: ls-tree exits 0 even for an absent path")
    assert probe.stdout.strip() == "", (
        "premise of the bead: it reports absence by printing NOTHING, which is "
        "why the exit status carries no presence information at all")

    # The correct primitive disagrees with the bad idiom's exit status.
    correct = subprocess.run(["git", "cat-file", "-e", f"{first}:late.txt"],
                              cwd=str(repo), capture_output=True, text=True)
    assert correct.returncode != 0, "cat-file -e must report the absent path"


# --- cat_file_verdict / ls_tree_verdict ---------------------------------------

def test_cat_file_verdict_zero_is_contained():
    assert sc.cat_file_verdict(0) == sc.CONTAINED


def test_cat_file_verdict_nonzero_is_not_contained():
    assert sc.cat_file_verdict(1) == sc.NOT_CONTAINED
    assert sc.cat_file_verdict(128) == sc.NOT_CONTAINED


def test_ls_tree_verdict_is_judged_on_output_never_exit_status():
    """rc 0 with empty output is the defect's shape and must be NOT-CONTAINED
    — this assertion is the fix, stated directly."""
    assert sc.ls_tree_verdict(0, "") == sc.NOT_CONTAINED
    assert sc.ls_tree_verdict(0, "\n") == sc.NOT_CONTAINED
    assert sc.ls_tree_verdict(0, "late.txt\n") == sc.CONTAINED


def test_ls_tree_verdict_nonzero_rc_is_unresolved_not_absent():
    """A broken ref is not evidence of absence — it is no evidence at all."""
    assert sc.ls_tree_verdict(128, "") == sc.UNRESOLVED


# --- path_containment_verdict, real repo --------------------------------------

def test_absent_path_reports_not_contained(tmp_path):
    repo, first, _second = _two_commit_repo(tmp_path)
    result = sc.path_containment_verdict(str(repo), first, "late.txt")
    assert result["verdict"] == sc.NOT_CONTAINED
    assert result["agree"] is True


def test_present_path_reports_contained(tmp_path):
    """NEGATIVE CONTROL for the test above: without it, a helper hardcoded to
    say NOT-CONTAINED would pass."""
    repo, _first, second = _two_commit_repo(tmp_path)
    result = sc.path_containment_verdict(str(repo), second, "late.txt")
    assert result["verdict"] == sc.CONTAINED
    assert result["agree"] is True


def test_path_mode_handles_directories_and_nested_paths(tmp_path):
    repo, _first, _second = _two_commit_repo(tmp_path)
    (repo / "hooks").mkdir()
    (repo / "hooks" / "deep.sh").write_text("#!/bin/sh\n")
    _run("git", "add", "hooks/deep.sh", cwd=str(repo))
    _run("git", "commit", "-qm", "nested", cwd=str(repo))
    assert sc.path_containment_verdict(str(repo), "HEAD", "hooks/deep.sh")["verdict"] \
        == sc.CONTAINED
    assert sc.path_containment_verdict(str(repo), "HEAD", "hooks")["verdict"] \
        == sc.CONTAINED
    assert sc.path_containment_verdict(str(repo), "HEAD", "hooks/absent.sh")["verdict"] \
        == sc.NOT_CONTAINED


# --- ref_resolves: a bad ref must never look like absence ---------------------

def test_ref_resolves_true_for_real_ref_false_for_typo(tmp_path):
    repo, _first, _second = _two_commit_repo(tmp_path)
    assert sc.ref_resolves(str(repo), "origin/tmux-only") is True
    assert sc.ref_resolves(str(repo), "origin/tmuxonly") is False


def test_main_path_mode_typo_ref_is_could_not_assess_not_not_contained(tmp_path, capsys):
    """The dangerous confusion in path mode: cat-file -e returns the same
    non-zero for "no such ref" as for "no such path". A typo'd ref must
    produce COULD NOT ASSESS (exit 4), never a confident verdict."""
    repo, _first, _second = _two_commit_repo(tmp_path)
    rc = sc.main(["--path", "late.txt", "--ref", "origin/tmuxonly",
                  "--repo", str(repo)])
    out = capsys.readouterr().out
    assert rc == sc.EXIT_UNRESOLVED
    assert "COULD NOT ASSESS" in out


# --- path-mode render ---------------------------------------------------------

def test_render_path_report_all_four_shapes():
    base = {"method_a": sc.CONTAINED, "method_b": sc.CONTAINED, "agree": True}
    assert sc.render_path_report("f.sh", "origin/tmux-only",
                                  {**base, "verdict": sc.CONTAINED}).startswith("CONTAINED:")
    assert sc.render_path_report("f.sh", "origin/tmux-only",
                                  {**base, "verdict": sc.NOT_CONTAINED}).startswith("NOT-CONTAINED:")
    disagree = sc.render_path_report("f.sh", "origin/tmux-only",
                                      {"verdict": sc.DISAGREE, "method_a": sc.CONTAINED,
                                       "method_b": sc.NOT_CONTAINED, "agree": False})
    assert "DISAGREEMENT" in disagree and "SABLE-4snb4" in disagree
    unresolved = sc.render_path_report("f.sh", "origin/tmux-only",
                                        {"verdict": sc.UNRESOLVED, "method_a": sc.UNRESOLVED,
                                         "method_b": sc.UNRESOLVED, "agree": True})
    assert "COULD NOT ASSESS" in unresolved


# --- CLI: mode selection fails closed -----------------------------------------

def test_parser_accepts_path_mode_without_a_sha():
    args = sc.build_parser().parse_args(["--path", "bin/x", "--ref", "origin/tmux-only"])
    assert args.sha is None
    assert args.path == "bin/x"
    assert args.ref == "origin/tmux-only"


def _usage_exit(argv):
    try:
        sc.main(argv)
    except SystemExit as exc:
        return exc.code
    raise AssertionError(f"expected a usage error for {argv}")


def test_main_rejects_both_modes_at_once(capsys):
    assert _usage_exit(["deadbeef", "--path", "bin/x"]) == sc.EXIT_USAGE


def test_main_rejects_neither_mode(capsys):
    assert _usage_exit([]) == sc.EXIT_USAGE


def test_main_rejects_ref_in_commit_mode(capsys):
    """--ref must not become the second positional-in-disguise that
    reintroduces the SABLE-gdp05 inversion into commit mode."""
    assert _usage_exit(["deadbeef", "--ref", "origin/tmux-only"]) == sc.EXIT_USAGE


# --- main(): path-mode exit codes and json ------------------------------------

def test_main_path_mode_exit_codes_against_both_refs(tmp_path, capsys):
    repo, first, second = _two_commit_repo(tmp_path)
    assert sc.main(["--path", "late.txt", "--ref", first, "--repo", str(repo)]) \
        == sc.EXIT_NOT_CONTAINED
    assert "NOT-CONTAINED" in capsys.readouterr().out
    assert sc.main(["--path", "late.txt", "--ref", second, "--repo", str(repo)]) \
        == sc.EXIT_CONTAINED
    assert "CONTAINED" in capsys.readouterr().out


def test_main_path_mode_defaults_ref_to_the_integration_ref(tmp_path, capsys):
    """"Is this file on the spine yet" is the question that motivated the
    mode, so it must not require naming the ref."""
    repo, _first, _second = _two_commit_repo(tmp_path)
    rc = sc.main(["--path", "late.txt", "--repo", str(repo),
                  "--integration-branch", "tmux-only"])
    out = capsys.readouterr().out
    assert rc == sc.EXIT_CONTAINED
    assert "origin/tmux-only" in out


def test_main_path_mode_json_carries_mode_and_ref(tmp_path, capsys):
    import json
    repo, first, _second = _two_commit_repo(tmp_path)
    rc = sc.main(["--path", "late.txt", "--ref", first, "--repo", str(repo),
                  "--format", "json"])
    parsed = json.loads(capsys.readouterr().out)
    assert rc == sc.EXIT_NOT_CONTAINED
    assert parsed["mode"] == "path"
    assert parsed["verdict"] == sc.NOT_CONTAINED
    assert parsed["path"] == "late.txt" and parsed["ref"] == first


def test_main_path_mode_disagreement_refuses_to_answer(tmp_path, capsys, monkeypatch):
    """Real git can never make the two legs disagree, so the only way to
    exercise the cross-check at all is to inject a faulty git (the same
    SABLE_CONTAINED_GIT seam commit mode's integration test uses). Here the
    fake makes ls-tree print a spurious entry for a path that cat-file
    correctly reports absent: the tool must return DISAGREEMENT (exit 3), not
    pick the CONTAINED side."""
    repo, first, _second = _two_commit_repo(tmp_path)
    fake = tmp_path / "faulty-git"
    fake.write_text(
        "#!/bin/sh\n"
        "for a in \"$@\"; do\n"
        "  if [ \"$a\" = ls-tree ]; then echo late.txt; exit 0; fi\n"
        "done\n"
        "exec git \"$@\"\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("SABLE_CONTAINED_GIT", str(fake))
    rc = sc.main(["--path", "late.txt", "--ref", first, "--repo", str(repo)])
    out = capsys.readouterr().out
    assert rc == sc.EXIT_DISAGREE
    assert "DISAGREEMENT" in out


def test_main_path_mode_no_disagreement_without_fault_injection(tmp_path, capsys):
    """The COMPLEMENT of the test above, without which it proves nothing: the
    same call with real git agrees cleanly."""
    repo, first, _second = _two_commit_repo(tmp_path)
    rc = sc.main(["--path", "late.txt", "--ref", first, "--repo", str(repo)])
    assert rc == sc.EXIT_NOT_CONTAINED
    assert "DISAGREEMENT" not in capsys.readouterr().out
