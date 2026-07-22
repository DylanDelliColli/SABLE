#!/usr/bin/env python3
"""Integration test for bin/sable-merge-report (SABLE-jd5fj.7).

Runs the report against a REAL git repository and REAL `git log` output (no
mocked git, no monkeypatched subprocess seam -- SABLE Prime Directive 2) plus
a fixture window of synthetic gate logs standing in for gh/bd, exactly as the
jd5fj S6 test-strategy case calls for: this repo does not yet have a live
disjoint-promotion-followed-by-a-red-run event to observe (SABLE-jd5fj.4 has
landed but no base-move-and-disjoint race has happened here yet), so the gh
and notify-log data are fixtures fed through the SAME env seams
sable-merge-gate's own tests use (SABLE_MR_GH / SABLE_MR_NOTIFY_LOG) -- a
real subprocess (a real fixture script) runs, only the DATA is synthetic.

Asserts: all metrics render (no exceptions, no silent None where a real
input exists) and the baseline-vs-after diff is mathematically consistent
(the reported speedup is exactly baseline_median / current_median from the
report's own numbers).
"""
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import sable_merge_report_lib as rl


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, text=True, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _commit(repo, subject, iso_date, filename="f.txt"):
    (Path(repo) / filename).write_text(subject)
    _git(repo, "add", "-A")
    env = dict(os.environ, GIT_AUTHOR_DATE=iso_date, GIT_COMMITTER_DATE=iso_date,
              GIT_AUTHOR_NAME="SABLE Test", GIT_AUTHOR_EMAIL="t@sable.invalid",
              GIT_COMMITTER_NAME="SABLE Test", GIT_COMMITTER_EMAIL="t@sable.invalid")
    subprocess.run(["git", "commit", "-q", "-m", subject], cwd=repo, check=True, env=env)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = str(tmp_path / "repo")
    os.makedirs(r)
    _git(r, "init", "-q", "-b", "tmux-only")
    _git(r, "config", "user.email", "t@sable.invalid")
    _git(r, "config", "user.name", "SABLE Test")
    _commit(r, "init", "2026-07-21T10:00:00+00:00")
    return r


_FAKE_GH_TEMPLATE = '''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
if "--branch={base_branch}" in args:
    print(json.dumps({base_runs}))
else:
    print(json.dumps({preview_runs}))
'''


def _install_fake_gh(tmp_path, base_branch, base_runs, preview_runs):
    script = tmp_path / "fake_gh.py"
    script.write_text(_FAKE_GH_TEMPLATE.format(
        base_branch=base_branch, base_runs=repr(base_runs), preview_runs=repr(preview_runs)))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return f"{sys.executable} {script}"


def test_report_renders_all_metrics_against_real_git_log_and_synthetic_gate_logs(
        repo, tmp_path, monkeypatch):
    # --- real git history: two disjoint promotions (jd5fj.4 shape), one
    # ordinary promotion, and one non-promotion commit in between.
    clean_sha = _commit(repo, "ci-verify merge-preview: wk-clean onto tmux-only (SABLE-clean1)",
                        "2026-07-21T11:00:00+00:00")
    _commit(repo, "fix(unrelated): not a promotion at all", "2026-07-21T11:05:00+00:00")
    broken_sha = _commit(
        repo, "ci-verify merge-preview: wk-broken onto tmux-only (SABLE-broken1, disjoint re-verify)",
        "2026-07-21T12:00:00+00:00")
    safe_sha = _commit(
        repo, "ci-verify merge-preview: wk-safe onto tmux-only (SABLE-safe1, disjoint re-verify)",
        "2026-07-21T13:00:00+00:00")

    # --- fixture gate logs: gh run list, two shapes distinguished by --branch.
    # Also doubles as the PRIMARY metric's fast-forward signal (earliest
    # base-branch run per landed sha, SABLE-jd5fj.11) -- one entry per landed
    # sha, none of them a discard (this test is about the semantic-break
    # flagship metric, not the discarded-preview one; that gets its own test).
    base_runs = [
        {"headSha": clean_sha, "createdAt": "2026-07-21T11:00:07Z", "conclusion": "success"},
        # broken_sha: a LATER run against the exact same SHA comes back red
        # within the window -- this is the semantic-break case.
        {"headSha": broken_sha, "createdAt": "2026-07-21T12:00:07Z", "conclusion": "success"},
        {"headSha": broken_sha, "createdAt": "2026-07-21T12:10:00Z", "conclusion": "failure"},
        # safe_sha: no later run reds -- not a break.
        {"headSha": safe_sha, "createdAt": "2026-07-21T13:00:07Z", "conclusion": "success"},
    ]
    preview_runs = [
        {"headBranch": "ci-verify/wk-clean-abc1234", "headSha": clean_sha,
         "createdAt": "2026-07-21T11:00:05Z",
         "updatedAt": "2026-07-21T11:05:00Z", "conclusion": "success", "status": "completed"},
        {"headBranch": "ci-verify/wk-broken-def5678", "headSha": broken_sha,
         "createdAt": "2026-07-21T12:00:05Z",
         "updatedAt": "2026-07-21T12:05:00Z", "conclusion": "success", "status": "completed"},
        {"headBranch": "ci-verify/wk-safe-9990000", "headSha": safe_sha,
         "createdAt": "2026-07-21T13:00:05Z",
         "updatedAt": "2026-07-21T13:03:00Z", "conclusion": "success", "status": "completed"},
    ]
    fake_gh = _install_fake_gh(tmp_path, "tmux-only", base_runs, preview_runs)

    notify_log = tmp_path / "notify.log"
    notify_log.write_text(
        "2026-07-21T11:00:00Z pid=1 name=chuck branch=wk-clean | "
        "CONFIRMED local=aaa remote=aaa attempts=1\n"
        "2026-07-21T12:00:00Z pid=1 name=chuck branch=wk-broken | "
        "CONFIRMED local=bbb remote=bbb attempts=1\n"
        "2026-07-21T13:00:00Z pid=1 name=chuck branch=wk-safe | "
        "CONFIRMED local=ccc remote=ccc attempts=1\n"
    )

    monkeypatch.setenv("SABLE_MR_GH", fake_gh)
    monkeypatch.setenv("SABLE_MR_NOTIFY_LOG", str(notify_log))

    report = rl.build_report(repo, "tmux-only", "tmux-only", since=None,
                             window_hours=1.0, check_snapshot_status=False)

    # --- all metrics render (no exceptions above just getting here; assert
    # none of the headline fields silently came back None where real input exists)
    assert report["promotions_total"] == 3  # init is not a promotion subject
    assert report["promotions_disjoint"] == 2
    assert report["push_to_ci_done"]["n"] == 3
    assert report["push_to_ci_done"]["median"] is not None
    assert report["red_rate"]["n"] == 3
    assert report["snapshot_backstop"] == "skipped"

    # --- the PRIMARY bar (SABLE-jd5fj.11): all three promotions have an
    # observed fast-forward and none discarded a preview in this scenario.
    assert report["push_to_landed"]["n"] == 3
    assert report["push_to_landed"]["median"] is not None
    assert report["discarded_preview_count"] == 0

    # --- the flagship metric: exactly one of the two disjoint promotions is
    # a semantic break (broken_sha, red within the 1h window); safe_sha is not.
    sb = report["semantic_break"]
    assert sb["disjoint_promotions"] == 2
    assert sb["breaks"] == 1
    assert sb["rate"] == 0.5
    assert sb["break_details"][0]["sha"] == broken_sha

    # --- baseline-vs-after diff is mathematically consistent: the reported
    # speedup is exactly baseline_median / current_median, recomputed here
    # independently from the report's own numbers. The success metric is now
    # evaluated against push-to-LANDED (SABLE-jd5fj.11), not push-to-CI-done.
    sm = report["success_metric"]
    expected_speedup = report["baseline"]["push_to_ci_done"]["median"] / report["push_to_landed"]["median"]
    assert sm["speedup"] == pytest.approx(expected_speedup)
    # nueh3's doc has no headline red-rate figure to compare against, so the
    # bar is honestly UNDECIDABLE on that axis rather than a fabricated pass.
    assert sm["meets_bar"] is None

    # clean_sha participates in git history but is not a disjoint promotion
    assert clean_sha in [p for p in _git(repo, "log", "--format=%H").stdout.splitlines()]


def test_report_handles_a_repo_with_zero_promotions(repo, tmp_path, monkeypatch):
    fake_gh = _install_fake_gh(tmp_path, "tmux-only", [], [])
    notify_log = tmp_path / "empty.log"
    notify_log.write_text("")
    monkeypatch.setenv("SABLE_MR_GH", fake_gh)
    monkeypatch.setenv("SABLE_MR_NOTIFY_LOG", str(notify_log))

    report = rl.build_report(repo, "tmux-only", "tmux-only", since=None,
                             check_snapshot_status=False)

    assert report["promotions_total"] == 0
    assert report["semantic_break"] == {
        "disjoint_promotions": 0, "breaks": 0, "rate": None,
        "rule_of_three_bound": None, "break_details": [],
    }
    assert report["push_to_ci_done"]["n"] == 0
    assert report["success_metric"]["meets_bar"] is None
    # renders as text without raising, even with nothing to report
    text = rl.format_report_text(report)
    assert "promotions observed: 0" in text


def _new_repo(tmp_path, name):
    r = str(tmp_path / name)
    os.makedirs(r)
    _git(r, "init", "-q", "-b", "tmux-only")
    _git(r, "config", "user.email", "t@sable.invalid")
    _git(r, "config", "user.name", "SABLE Test")
    _commit(r, "init", "2026-07-21T10:00:00+00:00")
    return r


def test_discarded_preview_cost_is_visible_in_the_primary_metric(tmp_path, monkeypatch):
    """SABLE-jd5fj.11: a serial queue where one merge invalidates a LATER
    branch's push-time preview must show up in the PRIMARY (push-to-landed)
    metric -- real git log for the promotions, fixture gate logs for the
    gh/notify data, same pattern as the other tests in this file. A second,
    otherwise-identical window with no invalidation is the POSITIVE CONTROL:
    the report must NOT claim a discarded-preview cost there, proving it
    distinguishes the two situations instead of always reporting one."""

    # === scenario A: wk-blocker lands first. wk-queued's push-time preview
    # tested a DIFFERENT object (its push-time preview is discarded when
    # wk-blocker's landing moves the base) -- a rebuilt preview, waited on
    # separately, is what actually lands (SABLE-kzi1a's shape).
    queue_repo = _new_repo(tmp_path, "queue-repo")
    blocker_sha = _commit(
        queue_repo, "ci-verify merge-preview: wk-blocker onto tmux-only (SABLE-blocker)",
        "2026-07-21T11:00:00+00:00")
    queued_sha = _commit(
        queue_repo, "ci-verify merge-preview: wk-queued onto tmux-only (SABLE-queued)",
        "2026-07-21T11:10:00+00:00")

    queue_base_runs = [
        {"headSha": blocker_sha, "createdAt": "2026-07-21T11:00:06Z", "conclusion": "success"},
        {"headSha": queued_sha, "createdAt": "2026-07-21T11:29:05Z", "conclusion": "success"},
    ]
    queue_preview_runs = [
        {"headBranch": "ci-verify/wk-blocker-blk0001", "headSha": blocker_sha,
         "createdAt": "2026-07-21T11:00:02Z", "updatedAt": "2026-07-21T11:00:04Z",
         "conclusion": "success", "status": "completed"},
        # the push-time preview -- goes green, then gets DISCARDED
        {"headBranch": "ci-verify/wk-queued-orig0001", "headSha": "deadbeefdiscardedsha",
         "createdAt": "2026-07-21T11:05:02Z", "updatedAt": "2026-07-21T11:08:00Z",
         "conclusion": "success", "status": "completed"},
        # the rebuilt preview -- this is the sha that actually lands
        {"headBranch": "ci-verify/wk-queued-rebuild1", "headSha": queued_sha,
         "createdAt": "2026-07-21T11:20:00Z", "updatedAt": "2026-07-21T11:29:00Z",
         "conclusion": "success", "status": "completed"},
    ]
    queue_fake_gh = _install_fake_gh(tmp_path, "tmux-only", queue_base_runs, queue_preview_runs)
    queue_notify_log = tmp_path / "queue-notify.log"
    queue_notify_log.write_text(
        "2026-07-21T11:00:00Z pid=1 name=chuck branch=wk-blocker | "
        "CONFIRMED local=aaa remote=aaa attempts=1\n"
        "2026-07-21T11:05:00Z pid=1 name=chuck branch=wk-queued | "
        "CONFIRMED local=bbb remote=bbb attempts=1\n"
    )
    monkeypatch.setenv("SABLE_MR_GH", queue_fake_gh)
    monkeypatch.setenv("SABLE_MR_NOTIFY_LOG", str(queue_notify_log))

    queued_report = rl.build_report(queue_repo, "tmux-only", "tmux-only", since=None,
                                    check_snapshot_status=False)

    assert queued_report["promotions_total"] == 2
    assert queued_report["discarded_preview_count"] == 1
    by_branch = {r["branch"]: r for r in queued_report["landed_records"]}
    assert by_branch["wk-queued"]["discarded_preview"] is True
    assert by_branch["wk-blocker"]["discarded_preview"] is False
    assert (by_branch["wk-queued"]["push_to_landed_seconds"]
           > by_branch["wk-queued"]["push_to_ci_done_seconds"])
    assert queued_report["push_to_landed"]["n"] == 2
    text = rl.format_report_text(queued_report)
    assert "discarded-preview cost: 1/2" in text

    # === POSITIVE CONTROL: the SAME window shape, but every push-time
    # preview's sha is exactly what lands -- no invalidation anywhere.
    clean_repo = _new_repo(tmp_path, "clean-repo")
    clean_blocker_sha = _commit(
        clean_repo, "ci-verify merge-preview: wk-blocker onto tmux-only (SABLE-blocker)",
        "2026-07-21T11:00:00+00:00")
    clean_queued_sha = _commit(
        clean_repo, "ci-verify merge-preview: wk-queued onto tmux-only (SABLE-queued)",
        "2026-07-21T11:10:00+00:00")

    clean_base_runs = [
        {"headSha": clean_blocker_sha, "createdAt": "2026-07-21T11:00:06Z", "conclusion": "success"},
        {"headSha": clean_queued_sha, "createdAt": "2026-07-21T11:10:09Z", "conclusion": "success"},
    ]
    clean_preview_runs = [
        {"headBranch": "ci-verify/wk-blocker-blk0001", "headSha": clean_blocker_sha,
         "createdAt": "2026-07-21T11:00:02Z", "updatedAt": "2026-07-21T11:00:04Z",
         "conclusion": "success", "status": "completed"},
        {"headBranch": "ci-verify/wk-queued-orig0001", "headSha": clean_queued_sha,
         "createdAt": "2026-07-21T11:05:02Z", "updatedAt": "2026-07-21T11:10:03Z",
         "conclusion": "success", "status": "completed"},
    ]
    clean_fake_gh = _install_fake_gh(tmp_path, "tmux-only", clean_base_runs, clean_preview_runs)
    clean_notify_log = tmp_path / "clean-notify.log"
    clean_notify_log.write_text(
        "2026-07-21T11:00:00Z pid=1 name=chuck branch=wk-blocker | "
        "CONFIRMED local=aaa remote=aaa attempts=1\n"
        "2026-07-21T11:05:00Z pid=1 name=chuck branch=wk-queued | "
        "CONFIRMED local=bbb remote=bbb attempts=1\n"
    )
    monkeypatch.setenv("SABLE_MR_GH", clean_fake_gh)
    monkeypatch.setenv("SABLE_MR_NOTIFY_LOG", str(clean_notify_log))

    clean_report = rl.build_report(clean_repo, "tmux-only", "tmux-only", since=None,
                                   check_snapshot_status=False)

    assert clean_report["promotions_total"] == 2
    assert clean_report["discarded_preview_count"] == 0
    for record in clean_report["landed_records"]:
        assert record["discarded_preview"] is False
    clean_text = rl.format_report_text(clean_report)
    assert "discarded-preview cost: 0/2" in clean_text


def test_cli_end_to_end_against_the_real_fixture_repo(repo, tmp_path, monkeypatch):
    """Exercises bin/sable-merge-report itself (not just the lib), as a real
    subprocess against the real fixture repo built above -- the full CLI ->
    lib -> git path, argv parsing included."""
    _commit(repo, "ci-verify merge-preview: wk-x onto tmux-only (SABLE-x1)",
            "2026-07-21T11:00:00+00:00")
    fake_gh = _install_fake_gh(tmp_path, "tmux-only", [], [])
    notify_log = tmp_path / "empty.log"
    notify_log.write_text("")

    env = dict(os.environ, SABLE_MR_GH=fake_gh, SABLE_MR_NOTIFY_LOG=str(notify_log))
    script = str(Path(__file__).resolve().parent / "sable-merge-report")
    cp = subprocess.run(
        [sys.executable, script, "--repo", repo, "--base", "tmux-only",
         "--remote", "origin", "--no-snapshot-check", "--json"],
        cwd=repo, text=True, capture_output=True, env=env,
    )
    # git_base_ref becomes "origin/tmux-only" (no remote configured in the
    # fixture) -- `git log` on a nonexistent remote ref returns nonzero and
    # collect_promotions degrades to an empty list rather than raising, so
    # the CLI still exits 0 with a (thin) report.
    assert cp.returncode == 0, cp.stdout
    assert '"promotions_total"' in cp.stdout
