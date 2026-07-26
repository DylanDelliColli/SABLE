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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_telemetry_bd_source as bd_source  # noqa: E402
import sable_telemetry_git_source as git_source  # noqa: E402
import sable_telemetry_gh_source as gh_source  # noqa: E402
import sable_telemetry_lib as lib  # noqa: E402

BIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = BIN_DIR.parent
CLI = BIN_DIR / "sable-telemetry"
HOOK = REPO_ROOT / "hooks" / "bead-description-gate.sh"

# The ci-verify clean-room is tmux+pytest only -- no bd/dolt by design. The
# seeded-db test drives a REAL sandbox beads DB, so it self-skips when bd is
# absent, matching the bd/dolt-suites-self-skip contract in ci-verify.yml.
HAVE_BD = shutil.which("bd") is not None
# The multiday-backfill fixture backdates seed beads via direct Dolt SQL (bd's
# own CLI has no created_at/closed_at override flag) -- needs the standalone
# `dolt` binary in addition to `bd` itself, so it gets its own skip guard.
HAVE_DOLT = shutil.which("dolt") is not None

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


def _embedded_dolt_dir(work: Path) -> Path:
    """The sandbox's real, standalone Dolt data directory. bd's "embedded"
    mode runs the Dolt engine in-process for the duration of one bd
    invocation and holds no lingering server/lock afterward, so the
    standalone `dolt` CLI can open the same directory directly once that bd
    subprocess has exited (verified live: `bd create` then `dolt sql -q`
    against the same path in immediate sequence)."""
    beads_dolt = work / ".beads" / "embeddeddolt"
    candidates = [p for p in beads_dolt.iterdir() if p.is_dir()]
    assert len(candidates) == 1, (
        f"expected exactly one embedded Dolt db dir under {beads_dolt}, "
        f"found {candidates}"
    )
    return candidates[0]


def _dolt_sql(work: Path, sql: str) -> None:
    """Run one SQL statement directly against the sandbox's real Dolt
    storage. Used ONLY to backdate created_at/closed_at for a multi-day
    fixture -- `bd create`/`bd close` have no timestamp-override flag, and
    real historical dates can't otherwise be produced without waiting real
    days. Every assertion still reads back through bd's own real query path
    (bd_source.fetch_bead_records -> `bd list --json`), not through this
    helper -- this only seeds state, per Prime Directive 2 (no mocked DB)."""
    subprocess.run(
        ["dolt", "sql", "-q", sql], cwd=str(_embedded_dolt_dir(work)),
        capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
    )


@pytest.fixture
def multiday_bd_sandbox(tmp_path_factory):
    """A real sandbox beads DB backdated to a 3-day, non-contiguous corpus
    (verified UTC storage: dolt's `datetime` columns hold the same wall-clock
    value `bd list --json` renders with a trailing 'Z', confirmed by probing
    a freshly created bead's stored value against its JSON rendering):
    DAY1 (one close), DAY2 is a deliberate GAP -- no created/closed activity
    at all, the zero-fill case S4 exists to prove -- and DAY3 (one close plus
    a same-day shift-report bead, left open, for the overlay-marker case)."""
    root = tmp_path_factory.mktemp("multiday")
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

    day1_id = create("DAY1: closes")
    _bd_run(work, home, "close", day1_id, "--sandbox", "--reason", "seed: day1 close")

    day3_id = create("DAY3: closes")
    _bd_run(work, home, "close", day3_id, "--sandbox", "--reason", "seed: day3 close")

    report_id = create("[SHIFT REPORT] seed 2026-07-23")

    # Backdated to noon UTC -- far from any midnight boundary in every real
    # timezone, so bucketing with tz=timezone.utc in the assertions is
    # unambiguous regardless of what zone the test runner itself is in.
    _dolt_sql(work, "UPDATE issues SET created_at='2026-07-21 12:00:00', "
                    f"closed_at='2026-07-21 12:30:00' WHERE id='{day1_id}'")
    _dolt_sql(work, "UPDATE issues SET created_at='2026-07-23 12:00:00', "
                    f"closed_at='2026-07-23 12:30:00' WHERE id='{day3_id}'")
    _dolt_sql(work, "UPDATE issues SET created_at='2026-07-23 13:00:00' "
                    f"WHERE id='{report_id}'")

    return {"work": work, "day1": day1_id, "day3": day3_id, "report": report_id}


@pytest.fixture
def shift_report_bd_sandbox(tmp_path_factory):
    """A real sandbox beads DB with two ordinary closed beads plus one
    shift-report-titled bead (left open, matching the live corpus pattern —
    see SABLE-75dz5 et al.), all created at real, current, same-day
    timestamps -- no backdating, exercising the true host-local (tz=None)
    default path end to end."""
    root = tmp_path_factory.mktemp("shiftreport")
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

    close_ids = []
    for n in (1, 2):
        bead_id = create(f"CLOSE {n}: ordinary work")
        _bd_run(work, home, "close", bead_id, "--sandbox",
               "--reason", f"seed: ordinary close {n}")
        close_ids.append(bead_id)

    report_id = create("SHIFT REPORT seed-fixture: today's summary")

    return {"work": work, "closes": close_ids, "report": report_id}


@pytest.fixture
def shift_ledger_bd_sandbox(tmp_path_factory):
    """A real sandbox beads DB, rooted under its own tmp_path (no shared
    ancestor with REPO_ROOT, so bd's cwd-based auto-discovery can never
    resolve into the live production DB) -- proves file_shift_ledger_bead's
    real `bd create` lands only in the sandbox (SABLE-j0vr: a prior test
    suite's real bd invocation filed durable P1 beads into the production
    pool by skipping this isolation)."""
    root = tmp_path_factory.mktemp("shiftledger")
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

    close_id = create("SEED: ordinary close for ledger shift metrics")
    _bd_run(work, home, "close", close_id, "--sandbox",
           "--reason", "seed: ledger shift close")

    return {"work": work, "home": home, "closed": close_id}


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


@pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd/dolt by design; real-bd integration self-skips",
)
def test_cycle_split_denominator_against_seeded_bd_db_mixed_dispatch(seeded_bd_sandbox):
    """S2 denominator invariant + dispatched-subset scoping proven against a
    REAL seeded bd DB with a genuinely mixed started_at population (open,
    claimed+closed, and manager-closed with no claim) -- not asserted
    against hand-authored BeadRecord fixtures (test-strategy.json S2)."""
    records = bd_source.fetch_bead_records(cwd=str(seeded_bd_sandbox["work"]))
    ids = seeded_bd_sandbox

    dispatched_record = next(r for r in records if r.id == ids["dispatched"])
    assert dispatched_record.started_at is not None

    # git_source's own regex/fixture behavior is covered by
    # test_git_source_regex_against_real_git_fixture_repo_log; this test's
    # job is the S2 join + denominator against real bd corpus state, so the
    # merge event only needs to be real-shaped, timestamped after the real
    # closed_at this seeded DB actually produced.
    closed_dt = datetime.fromisoformat(dispatched_record.closed_at.replace("Z", "+00:00"))
    merged_at = (closed_dt + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    merges = [git_source.MergeEvent(bead_id=ids["dispatched"], sha="deadbee",
                                    committed_at=merged_at)]

    report = lib.build_cycle_split_report(records, merges)

    # Real corpus: 2 closed beads (dispatched + manager), 1 open (excluded
    # from closed_count entirely, per the "closed beads" scoping).
    assert report.closed_count == 2
    assert report.dispatched_count == 1
    assert report.denominator_note == "1 of 2 closed beads had dispatch timestamps"
    assert "had dispatch timestamps" in report.denominator_note

    # The manager-closed bead never produces an entry, even though it is
    # closed -- the 61%-missing case this bead exists to surface, not paper
    # over.
    assert {e.bead_id for e in report.entries} == {ids["dispatched"]}
    entry = report.entries[0]
    assert entry.merge_queue_wait_seconds == pytest.approx(300.0)
    assert report.merge_queue_wait_share == pytest.approx(
        entry.merge_queue_wait_seconds / entry.total_seconds
    )


@pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd/dolt by design; real-bd integration self-skips",
)
def test_shift_report_closes_per_day_matches_seeded_bd_db(shift_report_bd_sandbox):
    """S1: closes/day and intake/day computed from a REAL seeded bd DB, with
    a real shift-report-titled bead overlaid as a marker on its day --
    proving the overlay never gets miscounted as a close itself and never
    perturbs the ordinary beads' own numbers."""
    ids = shift_report_bd_sandbox
    records = bd_source.fetch_bead_records(cwd=str(ids["work"]))

    series = lib.build_daily_burn_series(records)
    assert len(series) == 1  # every seed bead was created today, host-local
    today = series[0]

    today_local = datetime.now().astimezone().date().isoformat()
    assert today.date == today_local

    assert today.closes == len(ids["closes"]) == 2
    assert today.intake == 3  # 2 ordinary closes + 1 shift-report bead
    assert today.net_burn == today.closes - today.intake
    assert today.shift_report_ids == (ids["report"],)


@pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd/dolt by design; real-bd integration self-skips",
)
def test_shift_ledger_auto_file_writes_real_bead_to_sandbox_only(shift_ledger_bd_sandbox):
    """SABLE-8b41.8 AUTO-FILE, via the library function directly: a real
    `bd create` lands a shift-telemetry ledger bead in the sandbox DB with
    the right fields, and the SAME real, captured signature (never a
    hand-written guess at the expected title/label shape -- SABLE-5lli.7)
    is then proven absent from the live production db, attributed by that
    exact signature rather than a before/after global count (SABLE-3mrv3: a
    bare count can't attribute a delta; a concurrent filer elsewhere would
    false-RED it)."""
    ids = shift_ledger_bd_sandbox
    work, home = str(ids["work"]), ids["home"]
    env = _bd_env(home)

    records = bd_source.fetch_bead_records(cwd=work)
    report = lib.build_shift_report(records, tz=timezone.utc)

    ledger_id = lib.file_shift_ledger_bead(report, cwd=work, env=env)

    show = subprocess.run(
        ["bd", "show", ledger_id, "--json"], cwd=work, env=env,
        capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
    )
    filed = json.loads(show.stdout)[0]

    assert lib.SHIFT_TELEMETRY_LABEL in filed["labels"]
    assert "origin:operator" in filed["labels"]
    assert filed["title"].startswith(lib.SHIFT_TELEMETRY_TITLE_PREFIX)
    assert "closes" in filed["description"]

    signature = filed["title"]  # the real captured ledger signature

    live = subprocess.run(
        ["bd", "list", "--all", "--json", "--limit", "0"], cwd=str(REPO_ROOT),
        capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
    )
    live_titles = {r.get("title") for r in json.loads(live.stdout)}
    assert signature not in live_titles


@pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd/dolt by design; real-bd integration self-skips",
)
def test_cli_shift_file_flag_writes_real_ledger_bead(shift_ledger_bd_sandbox):
    """Same AUTO-FILE contract, end to end through the real `sable-telemetry
    --shift --file` CLI subprocess rather than calling the library function
    directly -- proves the CLI wiring (not just the lib function) lands in
    the sandbox only."""
    ids = shift_ledger_bd_sandbox
    work, home = str(ids["work"]), ids["home"]
    env = _bd_env(home)

    result = subprocess.run(
        [str(CLI), "--shift", "--file"], cwd=work, env=env,
        capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
    )
    assert "filed shift-telemetry ledger bead" in result.stderr
    ledger_id = result.stderr.strip().rsplit(" ", 1)[-1]

    show = subprocess.run(
        ["bd", "show", ledger_id, "--json"], cwd=work, env=env,
        capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
    )
    filed = json.loads(show.stdout)[0]
    assert lib.SHIFT_TELEMETRY_LABEL in filed["labels"]

    live = subprocess.run(
        ["bd", "list", "--all", "--json", "--limit", "0"], cwd=str(REPO_ROOT),
        capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
    )
    live_titles = {r.get("title") for r in json.loads(live.stdout)}
    assert filed["title"] not in live_titles


@pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd/dolt by design; real-bd integration self-skips",
)
def test_trend_output_reproducible_across_two_runs_same_seeded_state(seeded_bd_sandbox):
    """S4: the derive-at-read burn series is durable/reproducible from bd
    alone -- two independent real `bd list --json` subprocess calls against
    the SAME unchanged seeded state must compute byte-for-byte identical
    daily series, proving there is no hidden mutation or non-determinism in
    the read-and-aggregate path."""
    work = str(seeded_bd_sandbox["work"])

    records_run1 = bd_source.fetch_bead_records(cwd=work)
    series_run1 = lib.build_daily_burn_series(records_run1, tz=timezone.utc)

    records_run2 = bd_source.fetch_bead_records(cwd=work)
    series_run2 = lib.build_daily_burn_series(records_run2, tz=timezone.utc)

    assert series_run1 == series_run2
    assert series_run1  # non-vacuous: the seeded corpus actually produced days


@pytest.mark.skipif(
    not (HAVE_BD and HAVE_DOLT),
    reason="needs both bd and the standalone dolt CLI to backdate the multi-day fixture",
)
def test_backfill_full_history_from_seeded_multiday_corpus(multiday_bd_sandbox):
    """S4: a real seeded bd DB backdated across a 3-day, non-contiguous
    corpus backfills to a full spine -- including the deliberate zero-activity
    gap day -- purely from bd history, with the shift-report bead correctly
    overlaid on its own day rather than the gap."""
    ids = multiday_bd_sandbox
    records = bd_source.fetch_bead_records(cwd=str(ids["work"]))

    series = lib.build_daily_burn_series(records, tz=timezone.utc)
    by_date = {d.date: d for d in series}

    assert [d.date for d in series] == ["2026-07-21", "2026-07-22", "2026-07-23"]

    day1 = by_date["2026-07-21"]
    assert day1.closes == 1
    assert day1.intake == 1
    assert day1.shift_report_ids == ()

    gap_day = by_date["2026-07-22"]
    assert gap_day.closes == 0
    assert gap_day.intake == 0
    assert gap_day.net_burn == 0
    assert gap_day.shift_report_ids == ()

    day3 = by_date["2026-07-23"]
    assert day3.closes == 1  # the shift-report bead itself was never closed
    assert day3.intake == 2  # DAY3 close-bead + the shift-report bead
    assert day3.shift_report_ids == (ids["report"],)


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
