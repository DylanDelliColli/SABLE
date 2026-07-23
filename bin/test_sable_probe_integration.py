#!/usr/bin/env python3
"""Integration test for bin/sable-probe (SABLE-xhrt0).

Drives the actual CLI as a subprocess against a real on-disk repo -- no
mocked git. Builds a genuine "pre-merge base" commit and a genuine
"post-merge" commit that adds a real marker string, and asserts on the
specific refs and term this test created (never a global count -- SABLE-jd5fj.15
attributable-absence rule): the merge-preview shape from the bead's own
motivating incidents (SABLE-mn1da, SABLE-rhsuj, SABLE-wqe2e), reproduced end
to end through the CLI boundary rather than the library functions directly.
"""
import subprocess
from pathlib import Path

BIN = Path(__file__).resolve().parent / "sable-probe"


def _run(argv, cwd):
    return subprocess.run(argv, cwd=cwd, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _git(cwd, *args):
    cp = _run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd)
    if cp.returncode != 0:
        raise AssertionError(f"git {args} failed: {cp.stdout}")
    return cp.stdout.strip()


def _setup_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def test_probe_against_real_git_history(tmp_path):
    repo = _setup_repo(tmp_path)

    # Pre-merge base: a role-card-style file with no relation to the change.
    (repo / "role-card.md").write_text(
        "# Role card\n\nThe agent applies the standard escalation ladder.\n"
    )
    _git(repo, "add", "role-card.md")
    _git(repo, "commit", "-qm", "pre-merge base")
    pre_merge_sha = _git(repo, "rev-parse", "HEAD")

    # Post-merge: the real commit that adds a real marker string (the
    # SABLE-mn1da shape: the actual landed text, not a paraphrase of it).
    (repo / "role-card.md").write_text(
        "# Role card\n\nThe tool does NOT apply this ladder -- you do.\n"
    )
    _git(repo, "add", "role-card.md")
    _git(repo, "commit", "-qm", "land the corrected role-card wording")
    post_merge_sha = _git(repo, "rev-parse", "HEAD")

    real_term = "The tool does NOT apply this ladder"
    paraphrased_term = "does not GRADE"  # SABLE-mn1da's actual wrong term

    # --- the real term discriminates cleanly -------------------------------
    cp = _run(
        [str(BIN), "--term", real_term,
         "--known-positive", post_merge_sha,
         "--known-negative", pre_merge_sha,
         "--repo", str(repo), "--format", "json"],
        cwd=str(repo),
    )
    assert cp.returncode == 0, cp.stdout  # EXIT_ABSENT
    assert '"verdict": "absent"' in cp.stdout
    assert f'"known_positive": "{post_merge_sha}"' in cp.stdout
    assert f'"known_negative": "{pre_merge_sha}"' in cp.stdout

    # --- the paraphrased term (the live SABLE-mn1da mistake) is refused,
    # never reported as a clean absence, even though it also reads zero on
    # both refs -- the exact zero-reading confusion the bead is about. ------
    cp2 = _run(
        [str(BIN), "--term", paraphrased_term,
         "--known-positive", post_merge_sha,
         "--known-negative", pre_merge_sha,
         "--repo", str(repo), "--format", "json"],
        cwd=str(repo),
    )
    assert cp2.returncode == 3, cp2.stdout  # EXIT_COULD_NOT_ASSESS
    assert '"verdict": "could-not-assess"' in cp2.stdout
    assert '"verdict": "absent"' not in cp2.stdout

    # --- reversing the two refs (asking whether the wording pre-existed
    # pre-merge) reports PRESENT on pre_merge_sha as known-positive and
    # ABSENT would be wrong -- the real text must NOT be found in the
    # pre-merge commit at all, so pre_merge_sha cannot itself validate as a
    # known-positive for this term. -----------------------------------------
    cp3 = _run(
        [str(BIN), "--term", real_term,
         "--known-positive", pre_merge_sha,
         "--known-negative", post_merge_sha,
         "--repo", str(repo), "--format", "json"],
        cwd=str(repo),
    )
    assert cp3.returncode == 3, cp3.stdout  # EXIT_COULD_NOT_ASSESS
    assert '"verdict": "could-not-assess"' in cp3.stdout


def test_probe_containment_after_a_missing_ref_is_could_not_assess(tmp_path):
    """The SABLE-rhsuj shape, mechanized: probing against a ref that does
    not exist (a retired/deleted branch) must never read as ABSENT."""
    repo = _setup_repo(tmp_path)
    (repo / "f.txt").write_text("some content with MARKER-TERM inside\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "one")
    sha = _git(repo, "rev-parse", "HEAD")

    cp = _run(
        [str(BIN), "--term", "MARKER-TERM",
         "--known-positive", sha,
         "--known-negative", "origin/deleted-branch-that-never-existed",
         "--repo", str(repo), "--format", "json"],
        cwd=str(repo),
    )
    assert cp.returncode == 3, cp.stdout  # EXIT_COULD_NOT_ASSESS
    assert '"verdict": "could-not-assess"' in cp.stdout
    assert '"verdict": "absent"' not in cp.stdout
