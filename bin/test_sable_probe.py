#!/usr/bin/env python3
"""Unit tests for bin/sable-probe (SABLE-xhrt0).

Covers the decision logic (grep-exit-code -> match verdict, and match
verdicts -> probe verdict) and the CLI, using REAL fixture git repos rather
than stubbing git for the discriminating cases: the whole bead is about a
real git-grep footgun (a zero reading indistinguishable from a broken
term), and a stub of git grep would just replay this file's own assumption
about what it does.

test_probe_refuses_verdict_when_term_matches_nothing_on_known_positive and
test_probe_reports_absent_when_term_is_validated_and_genuinely_missing are
the both-polarities pair the bead's test spec calls out by name: a helper
that refuses everything and one that actually validates are
indistinguishable without both directions covered.
"""
import importlib.util
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

_LOADER = SourceFileLoader(
    "sable_probe", str(Path(__file__).resolve().parent / "sable-probe")
)
_SPEC = importlib.util.spec_from_loader("sable_probe", _LOADER)
sp = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(sp)


def _run(*args, cwd):
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, text=True)


def _git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run("git", "init", "-q", cwd=str(repo))
    _run("git", "config", "user.email", "test@example.invalid", cwd=str(repo))
    _run("git", "config", "user.name", "SABLE Test", cwd=str(repo))
    return repo


def _rev_parse(repo: Path, ref: str) -> str:
    return subprocess.run(["git", "rev-parse", ref], cwd=str(repo),
                           capture_output=True, text=True, check=True).stdout.strip()


def _two_commit_repo(tmp_path):
    """A base commit with base.txt, then a second commit that adds
    marker.txt containing the term 'REAL-MARKER'. Returns
    (repo, base_sha, marker_sha) -- base_sha is a genuine known-negative
    (the term did not exist yet), marker_sha a genuine known-positive."""
    repo = _git_repo(tmp_path)
    (repo / "base.txt").write_text("nothing interesting here")
    _run("git", "add", "base.txt", cwd=str(repo))
    _run("git", "commit", "-qm", "base", cwd=str(repo))
    base_sha = _rev_parse(repo, "HEAD")

    (repo / "marker.txt").write_text("this commit adds REAL-MARKER to the tree")
    _run("git", "add", "marker.txt", cwd=str(repo))
    _run("git", "commit", "-qm", "add marker", cwd=str(repo))
    marker_sha = _rev_parse(repo, "HEAD")
    return repo, base_sha, marker_sha


# --- grep_verdict --------------------------------------------------------------

def test_grep_verdict_zero_is_match_yes():
    assert sp.grep_verdict(0) == sp.MATCH_YES


def test_grep_verdict_one_is_match_no():
    assert sp.grep_verdict(1) == sp.MATCH_NO


def test_grep_verdict_other_codes_are_unresolved_never_guessed():
    assert sp.grep_verdict(128) == sp.MATCH_UNRESOLVED
    assert sp.grep_verdict(2) == sp.MATCH_UNRESOLVED


# --- combine_probe: the discriminating guard ------------------------------------

def test_probe_refuses_verdict_when_term_matches_nothing_on_known_positive():
    """THE CORE GUARD. A term guaranteed absent from the known-positive
    artifact must never be reported as ABSENT elsewhere -- that reading is
    unvalidated, not a genuine negative. Assert COULD-NOT-ASSESS, and assert
    the verdict is NOT 'absent', regardless of what the negative side shows."""
    verdict, reason = sp.combine_probe(sp.MATCH_NO, sp.MATCH_NO)
    assert verdict == sp.COULD_NOT_ASSESS
    assert verdict != sp.ABSENT
    assert "known-positive" in reason

    # Same guard even when the negative side happens to also read MATCH_YES
    # -- a term that never matched the positive is unvalidated no matter
    # what the negative side does.
    verdict2, _reason2 = sp.combine_probe(sp.MATCH_NO, sp.MATCH_YES)
    assert verdict2 == sp.COULD_NOT_ASSESS
    assert verdict2 != sp.ABSENT


def test_probe_reports_absent_when_term_is_validated_and_genuinely_missing():
    """OPPOSITE POLARITY, required so the guard above is not vacuous: when
    the term is validated (matches known-positive) and correctly reads zero
    on the target/known-negative, the helper must produce a clean ABSENT
    verdict rather than refusing forever."""
    verdict, reason = sp.combine_probe(sp.MATCH_YES, sp.MATCH_NO)
    assert verdict == sp.ABSENT
    assert "validated" in reason


def test_probe_reports_present_when_term_matches_both_sides():
    """The third reachable shape: term validated AND it also matches the
    supposed known-negative. Reported honestly as PRESENT (with a reason
    naming the broken assumption) rather than silently discarded."""
    verdict, reason = sp.combine_probe(sp.MATCH_YES, sp.MATCH_YES)
    assert verdict == sp.PRESENT
    assert "known-negative" in reason


def test_probe_distinguishes_unreadable_ref_from_absent_term():
    """Instance 2 (SABLE-rhsuj), mechanized: a ref that cannot be resolved
    must never be read as a negative answer. Both directions covered --
    positive side unreadable, and negative side unreadable after the
    positive side validated -- since either could silently masquerade as
    ABSENT if the unresolved case were folded into MATCH_NO."""
    verdict, reason = sp.combine_probe(sp.MATCH_UNRESOLVED, sp.MATCH_NO)
    assert verdict == sp.COULD_NOT_ASSESS
    assert verdict != sp.ABSENT
    assert "known-positive" in reason

    verdict2, reason2 = sp.combine_probe(sp.MATCH_YES, sp.MATCH_UNRESOLVED)
    assert verdict2 == sp.COULD_NOT_ASSESS
    assert verdict2 != sp.ABSENT
    assert "known-negative" in reason2


# --- render_report ---------------------------------------------------------------

def test_render_report_absent_names_both_refs():
    out = sp.render_report("TERM", "origin/tmux-only~1", "origin/tmux-only",
                            sp.ABSENT, "validated: ...")
    assert out.startswith("ABSENT:")
    assert "origin/tmux-only~1" in out and "origin/tmux-only" in out


def test_render_report_present_names_reason():
    out = sp.render_report("TERM", "pos", "neg", sp.PRESENT, "found anyway")
    assert out.startswith("PRESENT:")
    assert "found anyway" in out


def test_render_report_could_not_assess_names_reason():
    out = sp.render_report("TERM", "pos", "neg", sp.COULD_NOT_ASSESS,
                            "known-positive ref/path is unreadable")
    assert out.startswith("COULD NOT ASSESS:")
    assert "unreadable" in out


# --- ref_resolves / path_exists: real repo, no stubbing ---------------------------

def test_ref_resolves_true_for_real_ref_false_for_typo(tmp_path):
    repo, base_sha, marker_sha = _two_commit_repo(tmp_path)
    assert sp.ref_resolves(str(repo), marker_sha) is True
    assert sp.ref_resolves(str(repo), base_sha) is True
    assert sp.ref_resolves(str(repo), "not-a-real-ref-at-all") is False


def test_path_exists_true_for_real_path_false_for_absent(tmp_path):
    repo, base_sha, marker_sha = _two_commit_repo(tmp_path)
    assert sp.path_exists(str(repo), marker_sha, "marker.txt") is True
    assert sp.path_exists(str(repo), base_sha, "marker.txt") is False


# --- grep_side: real repo, exercises the existence precondition -------------------

def test_grep_side_match_yes_on_real_positive(tmp_path):
    repo, _base_sha, marker_sha = _two_commit_repo(tmp_path)
    assert sp.grep_side(str(repo), "REAL-MARKER", marker_sha, None) == sp.MATCH_YES


def test_grep_side_match_no_on_real_negative(tmp_path):
    repo, base_sha, _marker_sha = _two_commit_repo(tmp_path)
    assert sp.grep_side(str(repo), "REAL-MARKER", base_sha, None) == sp.MATCH_NO


def test_grep_side_unresolved_for_nonexistent_ref(tmp_path):
    repo, _base_sha, _marker_sha = _two_commit_repo(tmp_path)
    assert sp.grep_side(str(repo), "REAL-MARKER", "no-such-ref", None) == sp.MATCH_UNRESOLVED


def test_grep_side_unresolved_for_nonexistent_path(tmp_path):
    """A path that doesn't exist in an otherwise-real ref must be
    UNRESOLVED, not a confident 'no match' -- the same rule sable-contained
    applies to a typo'd ref (SABLE-4snb4), applied to a scoped path here."""
    repo, _base_sha, marker_sha = _two_commit_repo(tmp_path)
    assert sp.grep_side(str(repo), "REAL-MARKER", marker_sha, "no-such-file.txt") \
        == sp.MATCH_UNRESOLVED


# --- probe(): end-to-end against a real fixture repo -------------------------------

def test_probe_end_to_end_absent(tmp_path):
    repo, base_sha, marker_sha = _two_commit_repo(tmp_path)
    result = sp.probe(str(repo), "REAL-MARKER", marker_sha, base_sha, None)
    assert result["verdict"] == sp.ABSENT
    assert result["known_positive_match"] == sp.MATCH_YES
    assert result["known_negative_match"] == sp.MATCH_NO


def test_probe_end_to_end_could_not_assess_with_wrong_term(tmp_path):
    """The class this bead is about, reproduced directly: a term that came
    from a paraphrase rather than the artifact reads zero on BOTH sides --
    and must never be reported as a clean ABSENT."""
    repo, base_sha, marker_sha = _two_commit_repo(tmp_path)
    result = sp.probe(str(repo), "WRONG-PARAPHRASED-TERM", marker_sha, base_sha, None)
    assert result["verdict"] == sp.COULD_NOT_ASSESS
    assert result["verdict"] != sp.ABSENT


def test_probe_end_to_end_could_not_assess_for_missing_ref(tmp_path):
    repo, base_sha, marker_sha = _two_commit_repo(tmp_path)
    result = sp.probe(str(repo), "REAL-MARKER", "no-such-ref", base_sha, None)
    assert result["verdict"] == sp.COULD_NOT_ASSESS
    assert result["verdict"] != sp.ABSENT


# --- CLI: build_parser --------------------------------------------------------------

def test_parser_requires_term_and_both_refs():
    parser = sp.build_parser()
    args = parser.parse_args(["--term", "X", "--known-positive", "a",
                               "--known-negative", "b"])
    assert args.term == "X"
    assert args.known_positive == "a"
    assert args.known_negative == "b"
    assert args.path is None
    assert args.format == "text"


def test_parser_errors_without_required_flags():
    parser = sp.build_parser()
    try:
        parser.parse_args(["--term", "X"])
    except SystemExit as exc:
        assert exc.code == sp.EXIT_USAGE
    else:
        raise AssertionError("parser accepted a call missing --known-positive "
                              "and --known-negative")


# --- main(): exit codes -------------------------------------------------------------

def test_main_exit_code_absent(tmp_path, capsys):
    repo, base_sha, marker_sha = _two_commit_repo(tmp_path)
    rc = sp.main(["--term", "REAL-MARKER", "--known-positive", marker_sha,
                  "--known-negative", base_sha, "--repo", str(repo)])
    out = capsys.readouterr().out
    assert rc == sp.EXIT_ABSENT
    assert "ABSENT" in out


def test_main_exit_code_could_not_assess_never_reports_absent_text(tmp_path, capsys):
    repo, base_sha, marker_sha = _two_commit_repo(tmp_path)
    rc = sp.main(["--term", "WRONG-TERM", "--known-positive", marker_sha,
                  "--known-negative", base_sha, "--repo", str(repo)])
    out = capsys.readouterr().out
    assert rc == sp.EXIT_COULD_NOT_ASSESS
    assert "COULD NOT ASSESS" in out
    assert "ABSENT:" not in out


def test_main_exit_code_present(tmp_path, capsys):
    repo, base_sha, marker_sha = _two_commit_repo(tmp_path)
    # base.txt exists on both commits and contains no marker; use a term
    # that's present on both sides by pointing known-negative at marker_sha
    # too (term matches "positive" == marker_sha, and matches "negative"
    # when negative is also marker_sha).
    rc = sp.main(["--term", "REAL-MARKER", "--known-positive", marker_sha,
                  "--known-negative", marker_sha, "--repo", str(repo)])
    out = capsys.readouterr().out
    assert rc == sp.EXIT_PRESENT
    assert "PRESENT" in out


def test_main_json_format_is_valid_json(tmp_path, capsys):
    import json
    repo, base_sha, marker_sha = _two_commit_repo(tmp_path)
    rc = sp.main(["--term", "REAL-MARKER", "--known-positive", marker_sha,
                  "--known-negative", base_sha, "--repo", str(repo),
                  "--format", "json"])
    parsed = json.loads(capsys.readouterr().out)
    assert rc == sp.EXIT_ABSENT
    assert parsed["verdict"] == sp.ABSENT
    assert parsed["known_positive"] == marker_sha
    assert parsed["known_negative"] == base_sha


def _path_scoped_repo(tmp_path):
    """A single file (shared.txt) that starts without the term and is
    edited in a second commit to add it -- the shape a --path-scoped probe
    is meant for (validating a specific file's marker pre/post a change)."""
    repo = _git_repo(tmp_path)
    (repo / "shared.txt").write_text("nothing interesting here")
    _run("git", "add", "shared.txt", cwd=str(repo))
    _run("git", "commit", "-qm", "before", cwd=str(repo))
    before_sha = _rev_parse(repo, "HEAD")

    (repo / "shared.txt").write_text("this now carries REAL-MARKER inline")
    _run("git", "add", "shared.txt", cwd=str(repo))
    _run("git", "commit", "-qm", "after", cwd=str(repo))
    after_sha = _rev_parse(repo, "HEAD")
    return repo, before_sha, after_sha


def test_main_path_mode_validates_against_a_real_path_scoped_change(tmp_path, capsys):
    repo, before_sha, after_sha = _path_scoped_repo(tmp_path)
    rc = sp.main(["--term", "REAL-MARKER", "--known-positive", after_sha,
                  "--known-negative", before_sha, "--path", "shared.txt",
                  "--repo", str(repo)])
    out = capsys.readouterr().out
    assert rc == sp.EXIT_ABSENT
    assert "ABSENT" in out


def test_main_path_mode_missing_path_on_one_side_is_could_not_assess(tmp_path, capsys):
    """marker.txt does not exist at base_sha at all (added in the second
    commit) -- a probe scoped to it there must refuse to assess, never read
    the missing file as a confident absence."""
    repo, base_sha, marker_sha = _two_commit_repo(tmp_path)
    rc = sp.main(["--term", "REAL-MARKER", "--known-positive", marker_sha,
                  "--known-negative", base_sha, "--path", "marker.txt",
                  "--repo", str(repo)])
    out = capsys.readouterr().out
    assert rc == sp.EXIT_COULD_NOT_ASSESS
    assert "COULD NOT ASSESS" in out
    assert "unreadable" in out
