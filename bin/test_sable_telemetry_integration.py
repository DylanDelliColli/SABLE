#!/usr/bin/env python3
"""Integration tests for sable-telemetry foundation (SABLE-8b41.1).

Real composition, no mocks: invokes the actual `bin/sable-telemetry` CLI
subprocess AND the actual `hooks/bead-description-gate.sh` subprocess against
the same single-source origin: taxonomy, and diffs their output. This is the
Shotgun Surgery guard the architecture review flagged — a hardcoded second
copy of the taxonomy in the hook would silently drift from
bin/sable_telemetry_lib.py; only a real subprocess call through the hook's
own read path can catch that.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_telemetry_bd_source as bd_source  # noqa: E402
import sable_telemetry_git_source as git_source  # noqa: E402
import sable_telemetry_gh_source as gh_source  # noqa: E402

BIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = BIN_DIR.parent
CLI = BIN_DIR / "sable-telemetry"
HOOK = REPO_ROOT / "hooks" / "bead-description-gate.sh"

# The ci-verify clean-room is tmux+pytest only -- no bd/dolt by design. The
# seeded-db test drives a REAL sandbox beads DB, so it self-skips when bd is
# absent, matching the bd/dolt-suites-self-skip contract in ci-verify.yml.
HAVE_BD = shutil.which("bd") is not None

_ENV_LEAKS = ("CLAUDE_AGENT_NAME", "TMUX_PANE", "SABLE_HOOK_TRACE_LOG")


def _run(cmd, cwd=None):
    return subprocess.run(cmd, capture_output=True, text=True, check=True,
                          stdin=subprocess.DEVNULL, cwd=cwd)


def _bd_env(home):
    env = {k: v for k, v in os.environ.items() if k not in _ENV_LEAKS}
    env["HOME"] = str(home)
    env["BD_NON_INTERACTIVE"] = "1"
    env["CI"] = "true"
    return env


def _bd_run(work, home, *args, check=True):
    cp = subprocess.run(["bd", *args], cwd=str(work), env=_bd_env(home), text=True,
                        stdin=subprocess.DEVNULL, capture_output=True, timeout=180)
    if check and cp.returncode != 0:
        raise AssertionError(f"bd {args} failed: {cp.stdout}{cp.stderr}")
    return cp


def _robust_bd_init(work, home):
    """`bd init` on the embedded-Dolt backend can leave a PARTIAL database on a
    first-run race (rc 0 but no .beads/config.yaml). A clean init always
    writes config.yaml, so gate success on that artifact and wipe+retry."""
    beads = work / ".beads"
    last = None
    for _ in range(4):
        if beads.exists():
            shutil.rmtree(beads)
        last = _bd_run(work, home, "init", "--non-interactive", check=False)
        if last.returncode == 0 and (beads / "config.yaml").is_file():
            return
    raise AssertionError(f"bd init never produced a clean DB: {last.stdout if last else '<none>'}")


@pytest.fixture
def seeded_bd_sandbox(tmp_path_factory):
    """A real sandbox beads DB with an open+closed mix: OPEN (never touched),
    CLOSED_DISPATCHED (claimed then closed -- has started_at), and
    CLOSED_MANAGER (closed with no claim -- started_at stays absent, the
    61%-missing case this adapter must surface rather than paper over)."""
    root = tmp_path_factory.mktemp("bdsource")
    work = root / "work"
    work.mkdir()
    home = root / "home"
    home.mkdir()
    _robust_bd_init(work, home)

    def create(title):
        cp = _bd_run(work, home, "create", "--sandbox", "--json",
                     "--title", title, "--type=task", "--priority=2",
                     "--description", f"seed fixture bead: {title}")
        return json.loads(cp.stdout)["id"]

    open_id = create("OPEN: never touched")
    dispatched_id = create("CLOSED_DISPATCHED: claimed then closed")
    _bd_run(work, home, "update", dispatched_id, "--sandbox", "--claim")
    _bd_run(work, home, "close", dispatched_id, "--sandbox",
           "--reason", "seed: dispatched close")

    manager_id = create("CLOSED_MANAGER: closed with no claim")
    _bd_run(work, home, "close", manager_id, "--sandbox",
           "--reason", "seed: manager close, no claim")

    return {"work": work, "open": open_id, "dispatched": dispatched_id,
            "manager": manager_id}


def test_cli_runs_and_prints_json():
    result = _run([str(CLI), "--shift", "--json"])
    payload = json.loads(result.stdout)
    assert "metrics" in payload


def test_origin_labels_resolve_identically_through_tool_and_hook_paths():
    tool_out = _run([str(CLI), "--print-origin-labels"]).stdout
    hook_out = _run(["bash", str(HOOK), "--print-origin-labels"]).stdout

    assert tool_out == hook_out

    labels = [line.strip() for line in tool_out.splitlines() if line.strip()]
    assert labels == [
        "planned", "dogfood", "recurrence", "cross-fleet", "operator", "followup",
    ]


@pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd/dolt by design; real-bd integration self-skips",
)
def test_bd_source_returns_closed_beads_from_seeded_db(seeded_bd_sandbox):
    records = bd_source.fetch_bead_records(cwd=str(seeded_bd_sandbox["work"]))
    by_id = {r.id: r for r in records}

    ids = seeded_bd_sandbox
    # The core research trap: closed beads must actually surface, not just
    # the open one a naive (non---all) query would have returned.
    assert ids["open"] in by_id
    assert ids["dispatched"] in by_id
    assert ids["manager"] in by_id

    open_record = by_id[ids["open"]]
    assert open_record.status == "open"
    assert open_record.closed_at is None
    assert open_record.started_at is None

    dispatched_record = by_id[ids["dispatched"]]
    assert dispatched_record.status == "closed"
    assert dispatched_record.closed_at is not None
    assert dispatched_record.started_at is not None

    manager_record = by_id[ids["manager"]]
    assert manager_record.status == "closed"
    assert manager_record.closed_at is not None
    assert manager_record.started_at is None  # the 61%-missing case, surfaced explicitly


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, text=True, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _git_commit(repo, subject, iso_date, filename="f.txt"):
    (Path(repo) / filename).write_text(subject)
    _git(repo, "add", "-A")
    env = dict(os.environ, GIT_AUTHOR_DATE=iso_date, GIT_COMMITTER_DATE=iso_date,
              GIT_AUTHOR_NAME="SABLE Test", GIT_AUTHOR_EMAIL="t@sable.invalid",
              GIT_COMMITTER_NAME="SABLE Test", GIT_COMMITTER_EMAIL="t@sable.invalid")
    subprocess.run(["git", "commit", "-q", "-m", subject], cwd=repo, check=True, env=env)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_git_source_regex_against_real_git_fixture_repo_log(tmp_path):
    """Real temp git repo, real `git log` subprocess output, no mocked git
    (Prime Directive 2). Mixes real merge-preview subjects (plain bead,
    disjoint re-verify, push-time kick) with a decoy human commit that
    carries its own trailing (SABLE-xxxx) reference but is NOT a
    merge-preview event -- the negative case the S2 test-strategy calls out
    by name."""
    repo = str(tmp_path / "gitsource_repo")
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "tmux-only")
    _git(repo, "config", "user.email", "t@sable.invalid")
    _git(repo, "config", "user.name", "SABLE Test")
    _git_commit(repo, "init", "2026-07-21T10:00:00+00:00")

    clean_sha = _git_commit(
        repo, "ci-verify merge-preview: wk-clean onto tmux-only (SABLE-clean1)",
        "2026-07-21T11:00:00+00:00")
    _git_commit(
        repo, "fix(unrelated): patch something (SABLE-decoy1)",
        "2026-07-21T11:05:00+00:00")
    disjoint_sha = _git_commit(
        repo,
        "ci-verify merge-preview: wk-broken onto tmux-only (SABLE-broken1, disjoint re-verify)",
        "2026-07-21T12:00:00+00:00")
    kick_sha = _git_commit(
        repo, "ci-verify merge-preview: wk-thing onto tmux-only (push-time kick)",
        "2026-07-21T13:00:00+00:00")

    events = git_source.fetch_merge_events(base_ref="tmux-only", cwd=repo)
    by_sha = {e.sha: e for e in events}

    assert by_sha[clean_sha].bead_id == "SABLE-clean1"
    assert by_sha[disjoint_sha].bead_id == "SABLE-broken1"
    # push-time kick carries no bead id -- not counted as a bead merge event
    assert kick_sha not in by_sha
    # the decoy human commit (its own trailing bead ref) must not appear
    assert "SABLE-decoy1" not in {e.bead_id for e in events}
    assert len(events) == 2

    # oldest-first ordering (git log itself is newest-first)
    assert [e.sha for e in events] == [clean_sha, disjoint_sha]


# Real recorded `gh run list` / `gh run view --json jobs` payloads for
# SABLE-c008 (research.json's two-run example), fetched from
# DylanDelliColli/SABLE via `gh run view 29596385380`/`29596766036` --json
# databaseId,createdAt,updatedAt,conclusion,status,displayTitle,jobs. Using
# the REAL recorded shape (not a hand-authored stub) means drift in gh's
# actual run/job JSON shape breaks this test, not just a bead-id regex typo
# (test-strategy.json findings.deferred).
GH_C008_PREVIEW_RUN = {
    "databaseId": 29596385380,
    "headBranch": "ci-verify/SABLE-c008-16d94ed",
    "createdAt": "2026-07-17T16:29:52Z",
    "updatedAt": "2026-07-17T16:35:14Z",
    "conclusion": "success",
    "status": "completed",
    "displayTitle": "ci-verify merge-preview: wk-reap-superseded onto tmux-only (SABLE-c008)",
}
GH_C008_PREVIEW_JOBS = [
    {
        "databaseId": 87937697846,
        "name": "verify",
        "status": "completed",
        "conclusion": "success",
        "startedAt": "2026-07-17T16:29:55Z",
        "completedAt": "2026-07-17T16:35:14Z",
        "steps": [
            {"number": 3, "name": "Dedup guard — skip tmux-only re-verify of an "
                                   "already-verified preview SHA (SABLE-r3i6)",
             "status": "completed", "conclusion": "skipped"},
            {"number": 8, "name": "pytest — full bin/ suite (unit + integration; "
                                   "bd/dolt suites self-skip)",
             "status": "completed", "conclusion": "success"},
        ],
    }
]

GH_C008_TMUX_ONLY_RUN = {
    "databaseId": 29596766036,
    "headBranch": "tmux-only",
    "createdAt": "2026-07-17T16:35:35Z",
    "updatedAt": "2026-07-17T16:35:43Z",
    "conclusion": "success",
    "status": "completed",
    "displayTitle": "ci-verify merge-preview: wk-reap-superseded onto tmux-only (SABLE-c008)",
}
GH_C008_TMUX_ONLY_JOBS = [
    {
        "databaseId": 87938950599,
        "name": "verify",
        "status": "completed",
        "conclusion": "success",
        "startedAt": "2026-07-17T16:35:38Z",
        "completedAt": "2026-07-17T16:35:42Z",
        "steps": [
            {"number": 3, "name": "Dedup guard — skip tmux-only re-verify of an "
                                   "already-verified preview SHA (SABLE-r3i6)",
             "status": "completed", "conclusion": "success"},
            {"number": 8, "name": "pytest — full bin/ suite (unit + integration; "
                                   "bd/dolt suites self-skip)",
             "status": "completed", "conclusion": "skipped"},
        ],
    }
]


def test_gh_source_dedup_against_recorded_two_run_fixture():
    raw_runs = [GH_C008_PREVIEW_RUN, GH_C008_TMUX_ONLY_RUN]

    selected = gh_source.select_preview_runs(raw_runs)

    # The redundant post-merge tmux-only run is dropped by construction --
    # only the ci-verify/<bead>-<sha7> preview-ref run survives.
    assert [r["databaseId"] for r in selected] == [29596385380]

    event = gh_source.build_ci_run_event(selected[0], GH_C008_PREVIEW_JOBS)

    assert event.bead_id == "SABLE-c008"
    assert event.sha7 == "16d94ed"
    assert event.run_id == 29596385380
    # job-level startedAt (16:29:55Z) -> completedAt (16:35:14Z) = 5m19s,
    # NOT the near-instant tmux-only run's ~4s, and NOT the run-level
    # createdAt->updatedAt span which includes Actions queue time.
    assert event.duration_seconds == 319.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
