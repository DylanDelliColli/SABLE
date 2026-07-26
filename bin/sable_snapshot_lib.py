#!/usr/bin/env python3
"""sable_snapshot_lib — green snapshot + RETRY-AS-CLASSIFIER (SABLE-jd5fj.5).

The fleet-wide safety mechanism behind the merge pipeline: a scheduled
full-suite run on the integration branch AFTER promotions, whose job is not
"did the tests pass" but "WHICH KIND of red is this". Everything else here
follows from that one distinction.

── WHY A RE-RUN IS A CLASSIFIER, NOT A RETRY ────────────────────────────────
A bare retry hides information: run it again, if it passes, ship. That is how a
flaky suite and a real regression become the same event, and it is why fleets
end up either freezing on noise or ignoring genuine red. Here the SECOND RUN IS
THE MEASUREMENT. Exactly one automatic re-run, of ONLY the suites that failed,
and its result is read as a verdict about the FAILURE'S NATURE:

  red -> red    DETERMINISTIC. The integration branch is broken by something
                that landed. FREEZE promotion + file exactly one Chuck bisect
                bead. The freeze is the point: every additional promotion on
                top of a broken base makes the bisect harder, so the gate stops
                accepting work until a human resolves it.
  red -> green  FLAKY. NOT a freeze — freezing the fleet on noise is the
                failure mode this classification exists to prevent. The suite
                goes on the QUARANTINE LIST and exactly one flaky-fix bead is
                filed. Quarantined suites STILL RUN and their results are STILL
                RECORDED; what quarantine removes is only their ability to
                TRIGGER A FREEZE.

This extends an instinct the gate already had: SABLE-sc24 made a CANCELLED
Actions run exit 24 (retryable) instead of 20 (red), because a cancellation is
not a content defect. Same move, one level up — a flake is not a content defect
either, and the system should say so rather than make a human decide each time.

Quarantine is deliberately NOT a skip. A skipped suite is a coverage hole that
nothing measures; a quarantined suite keeps producing evidence (its pass/fail is
recorded on every snapshot) so the flaky-fix bead has data and so a quarantined
suite that turns PERMANENTLY red is visible instead of silent.

── WHY THE STATE IS ON DISK, PER REPO ───────────────────────────────────────
The freeze flag and quarantine list live in the per-repo state dir
.claude/sable/state/merge-gate/, resolved through git-common-dir exactly like
mode-state.json (bin/sable-mode, hooks/multi-manager/lib-mode-path.sh) so all of
a repo's worktrees share ONE freeze, and two repos never share one. They are
gitignored: a freeze is a fact about a moment, not about a commit, and a freeze
that travelled through a merge would freeze repos that were never broken.

The freeze read is FAIL-CLOSED. An unreadable/corrupt freeze file reads as
FROZEN, because "we cannot prove the fleet is unfrozen" and "the fleet is
unfrozen" are not the same claim, and only one of them is safe to act on.

── WHAT THIS MODULE MAY NOT DEPEND ON ───────────────────────────────────────
Nothing in the merge gate. sable_gate_promote_lib imports THIS module (for the
mechanical freeze check); the reverse import would be a cycle and is asserted
against in bin/test_snapshot_classifier.py.

Environment seams (defaults are the production commands, so the whole flow is
testable without a real bd or a real suite run):
  SABLE_MERGE_GATE_STATE   state DIR override (default: per-repo, see above)
  SABLE_SNAPSHOT_BD        bd command (default: bd; all WRITES pass --sandbox)
  SABLE_SNAPSHOT_RUNNER    suite runner override; invoked as <runner...> <suite>
                           with cwd=repo (default: bash hooks/test/<suite>)
  SABLE_SNAPSHOT_TIMEOUT   per-suite timeout secs (default: the full_snapshot
                           tier budget from .github/ci/test-tiers.sh, else 1800)
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Snapshot exit codes. 25 is shared with the merge gate's EXIT_FROZEN
# (sable_gate_classify_lib) on purpose: "the fleet is frozen" is ONE fact with
# one number, whether you learn it from the snapshot that set it or the promote
# it denies.
EXIT_OK = 0             # snapshot green (or green after a quarantined flake)
EXIT_USAGE = 2
EXIT_PRECONDITION = 3   # could not enumerate the tier / no suites to run
EXIT_FROZEN = 25        # deterministic red -> promotion is now frozen
EXIT_QUARANTINED = 26   # flake(s) quarantined; NOT frozen, work continues

FREEZE_FILE = "freeze.json"
QUARANTINE_FILE = "quarantine.json"

# Bead labels — also the idempotency search key, so filing is a query away from
# being idempotent rather than a guess.
LABEL_BISECT = "snapshot-freeze"
LABEL_FLAKE = "snapshot-flake"
KEY_PREFIX = "snapshot-key:"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# THE CLASSIFIER (pure)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SnapshotVerdict:
    """What one snapshot learned. Pure value: every field is derivable from the
    two result maps and the quarantine list, which is what makes the whole
    matrix enumerable in bin/test_snapshot_classifier.py without a repo."""
    ran: int                            # suites in the first pass
    green: bool                         # every suite passed on the FIRST pass
    deterministic: tuple[str, ...]      # red twice, not quarantined -> freeze
    quarantined_red: tuple[str, ...]    # red twice BUT quarantined -> recorded only
    flaky: tuple[str, ...]              # red then green -> quarantine
    freeze: bool
    reason: str

    @property
    def clears_freeze(self) -> bool:
        """A snapshot may LIFT an existing freeze only when it actually ran
        suites AND nothing came out deterministically red. A zero-suite run is
        not evidence of health — the same instinct as the merge gate's
        "impact tier selected no suites" being ERROR rather than green."""
        return self.ran > 0 and not self.freeze


def classify_snapshot(first_pass: dict[str, bool],
                      rerun: dict[str, bool],
                      quarantined: set[str] | frozenset[str] | None = None
                      ) -> SnapshotVerdict:
    """The whole classification, as a total function of three inputs.

    first_pass  suite -> passed?, for every suite the tier ran.
    rerun       suite -> passed?, for the failed subset ONLY (the one automatic
                re-run). A suite that failed the first pass and is MISSING here
                is treated as still-red: a re-run that did not happen (crashed,
                timed out, was skipped) told us nothing, and "told us nothing"
                must not read as "it passed". Fail-closed, same posture as the
                unreadable freeze file.
    quarantined suites excluded from the FREEZE TRIGGER only. Their results are
                still classified and reported — see quarantined_red.
    """
    q = set(quarantined or ())
    failed = [s for s, ok in first_pass.items() if not ok]

    flaky, deterministic, quarantined_red = [], [], []
    for suite in sorted(failed):
        if rerun.get(suite) is True:
            flaky.append(suite)
        elif suite in q:
            quarantined_red.append(suite)
        else:
            deterministic.append(suite)

    freeze = bool(deterministic)
    if not first_pass:
        reason = "no suites ran — a snapshot that measured nothing"
    elif not failed:
        reason = f"all {len(first_pass)} suite(s) green on the first pass"
    elif freeze:
        reason = ("deterministic red (failed twice, not quarantined): "
                  + ", ".join(deterministic))
    elif flaky and quarantined_red:
        reason = (f"flaky: {', '.join(flaky)}; quarantined-and-still-red: "
                  f"{', '.join(quarantined_red)} — no freeze")
    elif flaky:
        reason = f"flaky (red then green on the re-run): {', '.join(flaky)} — no freeze"
    else:
        reason = (f"only quarantined suites are red: {', '.join(quarantined_red)} "
                  f"— recorded, no freeze")

    return SnapshotVerdict(
        ran=len(first_pass),
        green=bool(first_pass) and not failed,
        deterministic=tuple(deterministic),
        quarantined_red=tuple(quarantined_red),
        flaky=tuple(flaky),
        freeze=freeze,
        reason=reason,
    )


def bisect_key(suites: tuple[str, ...] | list[str]) -> str:
    """Idempotency key for the Chuck bisect bead: a pure function of WHICH
    suites are deterministically red. The next snapshot that finds the same
    broken set computes the same key, finds the open bead, and files nothing —
    "exactly one bead" is a property of the key, not of a flag someone has to
    remember to clear."""
    digest = hashlib.sha1("\n".join(sorted(suites)).encode()).hexdigest()[:12]
    return f"bisect:{digest}"


def flake_key(suite: str) -> str:
    """Idempotency key for a flaky-fix bead: ONE bead per flaky suite, however
    many times that suite flakes."""
    return f"flaky:{suite}"


# ---------------------------------------------------------------------------
# State (freeze flag + quarantine list)
# ---------------------------------------------------------------------------

def state_dir(repo: str | os.PathLike = ".") -> Path:
    """The per-repo merge-gate state dir. MIRRORS sable-mode's resolve_state_path
    (git-common-dir -> <repo root>/.claude/sable/state/), one level deeper, so a
    freeze set in one worktree is seen by every other worktree of the same repo
    and by none of another repo's."""
    override = os.environ.get("SABLE_MERGE_GATE_STATE")
    if override:
        return Path(override)
    try:
        cp = subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True, timeout=30)
        common = cp.stdout.strip()
        if cp.returncode == 0 and common:
            cdir = Path(common)
            if not cdir.is_absolute():
                cdir = Path(repo) / cdir
            return cdir.resolve().parent / ".claude" / "sable" / "state" / "merge-gate"
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(os.path.expanduser("~")) / ".claude" / "sable" / "state" / "merge-gate"


def ensure_state_dir(repo: str | os.PathLike = ".") -> Path:
    d = state_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_freeze(repo: str | os.PathLike = ".") -> dict | None:
    """The freeze record, or None if the fleet is not frozen.

    FAIL-CLOSED on anything unreadable: a freeze file that exists but cannot be
    parsed reads as FROZEN. The alternative — treating a corrupt file as "no
    freeze" — makes the safety mechanism fail in the direction of promoting onto
    a base we have no evidence about, which is the exact class of silent-green
    failure this epic exists to remove."""
    path = state_dir(repo) / FREEZE_FILE
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {"frozen": True, "since": "", "suites": [], "bead": "",
                "reason": f"freeze state at {path} is unreadable ({exc}) — "
                          f"fail-closed: treating the fleet as FROZEN"}
    if not isinstance(data, dict) or not data.get("frozen"):
        return None
    return data


def write_freeze(repo: str | os.PathLike, *, suites, reason: str,
                 bead: str = "", run_url: str = "") -> dict:
    record = {"frozen": True, "since": _now(), "suites": sorted(suites),
              "reason": reason, "bead": bead, "run_url": run_url}
    d = ensure_state_dir(repo)
    (d / FREEZE_FILE).write_text(json.dumps(record, indent=2) + "\n")
    return record


def clear_freeze(repo: str | os.PathLike, *, reason: str = "") -> bool:
    """Lift the freeze. Returns True iff a freeze was actually lifted. Deletes
    the file rather than writing frozen:false — absence is the unambiguous
    representation, and it keeps read_freeze's fail-closed rule simple."""
    path = state_dir(repo) / FREEZE_FILE
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def read_quarantine(repo: str | os.PathLike = ".") -> dict[str, dict]:
    """suite -> record. An unreadable list reads as EMPTY, which is the
    conservative direction here: nothing is excluded from the freeze trigger, so
    the failure mode is an over-eager freeze, never a missed one."""
    path = state_dir(repo) / QUARANTINE_FILE
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    suites = data.get("suites") if isinstance(data, dict) else None
    return suites if isinstance(suites, dict) else {}


def _write_quarantine(repo: str | os.PathLike, suites: dict[str, dict]) -> None:
    d = ensure_state_dir(repo)
    (d / QUARANTINE_FILE).write_text(
        json.dumps({"suites": dict(sorted(suites.items()))}, indent=2) + "\n")


def add_quarantine(repo: str | os.PathLike, suite: str, *, reason: str = "",
                   bead: str = "") -> bool:
    """Idempotent. Returns True iff the suite was newly quarantined."""
    suites = read_quarantine(repo)
    if suite in suites:
        return False
    suites[suite] = {"since": _now(), "reason": reason, "bead": bead}
    _write_quarantine(repo, suites)
    return True


def remove_quarantine(repo: str | os.PathLike, suite: str) -> bool:
    suites = read_quarantine(repo)
    if suite not in suites:
        return False
    del suites[suite]
    _write_quarantine(repo, suites)
    return True


# ---------------------------------------------------------------------------
# Idempotent bead filing
# ---------------------------------------------------------------------------

def _bd_cmd() -> list[str]:
    return shlex.split(os.environ.get("SABLE_SNAPSHOT_BD", "bd"))


def find_open_bead(repo: str | os.PathLike, label: str, key: str) -> str | None:
    """The bead id of an OPEN bead carrying <label> whose description carries
    <key>, else None. The prior art is bin/tripwire-watcher.py's
    existing_open_tripwire_for(): query, don't remember."""
    cmd = _bd_cmd() + ["list", "--status=open", f"--label={label}", "--json"]
    try:
        cp = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, timeout=120)
        if cp.returncode != 0:
            return None
        items = json.loads(cp.stdout or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    if not isinstance(items, list):
        return None
    marker = f"{KEY_PREFIX} {key}"
    for item in items:
        if not isinstance(item, dict):
            continue
        if marker in (item.get("description") or ""):
            return item.get("id")
    return None


def file_bead_once(repo: str | os.PathLike, *, label: str, key: str, title: str,
                   description: str, bead_type: str = "bug", priority: int = 0,
                   extra_labels: tuple[str, ...] = ()) -> tuple[str | None, bool]:
    """File EXACTLY ONE bead for <key>. Returns (bead_id, created).

    The dedup is a query against the open pool, so it survives a re-run, a
    crashed snapshot, a second runner, and a restarted schedule — none of which
    a local "already filed" flag would survive. Every write passes --sandbox
    (SABLE-r8ibx contract)."""
    existing = find_open_bead(repo, label, key)
    if existing:
        return existing, False
    body = f"{description.rstrip()}\n\n{KEY_PREFIX} {key}\n"
    labels = ",".join((label, "coord") + tuple(extra_labels))
    cmd = _bd_cmd() + ["create", "--sandbox", "--title", title,
                       f"--type={bead_type}", f"--priority={priority}",
                       f"--labels={labels}", "--description", body]
    try:
        cp = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"sable-snapshot: bd create failed: {exc}", file=sys.stderr)
        return None, False
    if cp.returncode != 0:
        print(f"sable-snapshot: bd create failed: {cp.stderr.strip()}", file=sys.stderr)
        return None, False
    for line in cp.stdout.splitlines():
        if "Created issue:" in line:
            parts = line.split("Created issue:", 1)[1].strip().split()
            if parts:
                return parts[0], True
    return None, True


# ---------------------------------------------------------------------------
# Running the tier
# ---------------------------------------------------------------------------

def _tiers_sh(repo: str | os.PathLike) -> Path:
    return Path(repo) / ".github" / "ci" / "test-tiers.sh"


def tier_suites(repo: str | os.PathLike, tier: str = "full_snapshot") -> list[str]:
    """Suite membership from the cmar4.1 SSOT — never a list of our own. If
    test-tiers.sh cannot answer, this raises rather than substituting a guess:
    a snapshot that silently ran a different set than the tier declares is worse
    than no snapshot."""
    tiers = _tiers_sh(repo)
    if not tiers.is_file():
        raise RuntimeError(f"no tier SSOT at {tiers} — cannot enumerate tier {tier!r}")
    cp = subprocess.run(["bash", str(tiers), "--list", tier], cwd=str(repo),
                        capture_output=True, text=True, timeout=120)
    if cp.returncode != 0:
        raise RuntimeError(f"tier SSOT refused tier {tier!r}: {cp.stderr.strip()}")
    return [ln.strip() for ln in cp.stdout.splitlines() if ln.strip()]


def _suite_timeout(repo: str | os.PathLike) -> float:
    override = os.environ.get("SABLE_SNAPSHOT_TIMEOUT")
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    tiers = _tiers_sh(repo)
    if tiers.is_file():
        try:
            cp = subprocess.run(["bash", str(tiers), "--budget", "full_snapshot"],
                                cwd=str(repo), capture_output=True, text=True, timeout=60)
            if cp.returncode == 0:
                return float(cp.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return 1800.0


def run_suite(repo: str | os.PathLike, suite: str) -> tuple[bool, str]:
    """Run one suite; return (passed, tail-of-output). A TIMEOUT is a FAILURE,
    not an error to swallow — a suite that cannot finish is not a suite that
    passed."""
    runner = os.environ.get("SABLE_SNAPSHOT_RUNNER")
    cmd = (shlex.split(runner) + [suite] if runner
           else ["bash", str(Path(repo) / "hooks" / "test" / suite)])
    try:
        cp = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True,
                            timeout=_suite_timeout(repo))
    except subprocess.TimeoutExpired:
        return False, f"{suite}: TIMED OUT after {_suite_timeout(repo)}s"
    except OSError as exc:
        return False, f"{suite}: could not run ({exc})"
    out = ((cp.stdout or "") + (cp.stderr or "")).strip()
    return cp.returncode == 0, out[-2000:]


def run_suites(repo: str | os.PathLike, suites, *, banner: str = "") -> tuple[dict[str, bool], dict[str, str]]:
    results: dict[str, bool] = {}
    logs: dict[str, str] = {}
    for suite in suites:
        if banner:
            print(f"sable-snapshot: [{banner}] {suite}")
        ok, out = run_suite(repo, suite)
        results[suite] = ok
        if not ok:
            logs[suite] = out
    return results, logs


# ---------------------------------------------------------------------------
# The snapshot itself
# ---------------------------------------------------------------------------

@dataclass
class SnapshotResult:
    verdict: SnapshotVerdict
    exit_code: int
    bisect_bead: str = ""
    flake_beads: dict[str, str] = field(default_factory=dict)
    unfroze: bool = False
    logs: dict[str, str] = field(default_factory=dict)


def _bisect_description(verdict: SnapshotVerdict, repo_branch: str,
                        logs: dict[str, str], run_url: str) -> str:
    tails = "\n\n".join(f"### {s}\n```\n{logs.get(s, '(no output captured)')}\n```"
                        for s in verdict.deterministic)
    return f"""The scheduled green snapshot on the integration branch ({repo_branch}) found
DETERMINISTIC red: the suite(s) below failed, were automatically re-run, and
failed AGAIN. That is not flake — something that landed broke them.

PROMOTION IS FROZEN. bin/sable_gate_promote_lib refuses every promote with exit
25 while the freeze flag is set, so the base cannot accumulate more commits on
top of the break (which is what makes a bisect cheap now and expensive later).

Deterministically red suites:
{chr(10).join('  - ' + s for s in verdict.deterministic)}

## What to do
1. `sable-snapshot status` — confirm the freeze and read the recorded suites.
2. Bisect the integration branch over the promotions since the last GREEN
   snapshot; the failing suite names above are the bisect predicate.
3. Land the fix (or revert the culprit promotion).
4. The next GREEN snapshot clears the freeze automatically — do not hand-clear
   it unless you are overriding deliberately (`sable-snapshot unfreeze
   --reason "..."`, which is recorded).

## Test spec
[no-test] operational bisect bead auto-filed by the snapshot runner
(SABLE-jd5fj.5); the fix that closes it carries its own tests.

Snapshot run: {run_url or 'local'}

## Failure output
{tails}
"""


def _flake_description(suite: str, repo_branch: str, log: str, run_url: str) -> str:
    return f"""The scheduled green snapshot on the integration branch ({repo_branch}) found
hooks/test/{suite} FLAKY: it failed, was automatically re-run, and PASSED. A
red-then-green suite is not a content defect, so the fleet was NOT frozen.

{suite} is now QUARANTINED. Quarantine is not a skip: the suite still runs on
every snapshot and its result is still recorded — what quarantine removes is
only its ability to TRIGGER A FREEZE. A flaky suite that can freeze the fleet
trains everyone to ignore freezes, which costs more than the flake.

## What to do
1. Reproduce: run `bash hooks/test/{suite}` in a loop and find the nondeterminism
   (shared temp paths, wall-clock/timing assumptions, ambient tmux/git state,
   ordering dependence on another suite).
2. Fix the suite (or the product defect it is intermittently catching — a flake
   is sometimes a real race that only sometimes loses).
3. `sable-snapshot quarantine remove {suite}` to put it back under the freeze
   trigger, and close this bead.

## Test spec
The fix must make the nondeterminism impossible, not rarer: assert the invariant
the flake violates (unit), and re-run {suite} N>=20 times green (integration).

Snapshot run: {run_url or 'local'}

## First-pass failure output
```
{log or '(no output captured)'}
```
"""


def snapshot(repo: str | os.PathLike = ".", *, tier: str = "full_snapshot",
             branch: str = "", run_url: str = "", dry_run: bool = False
             ) -> SnapshotResult:
    """One full snapshot: run the tier, classify, persist, file beads.

    Order is load-bearing. Quarantine additions land BEFORE the freeze decision
    is persisted so that a suite quarantined by THIS run is already excluded
    from the freeze trigger of this run's own record; and the freeze is written
    BEFORE the bisect bead is filed so that a crash between them leaves the
    fleet frozen-without-a-bead (recoverable, and the next snapshot files the
    bead) rather than bead-without-a-freeze (a bisect nobody is protected
    during)."""
    suites = tier_suites(repo, tier)
    if not suites:
        raise RuntimeError(f"tier {tier!r} declares no suites — nothing to snapshot")

    first, logs = run_suites(repo, suites, banner="pass 1")
    failed = sorted(s for s, ok in first.items() if not ok)

    rerun: dict[str, bool] = {}
    if failed:
        print(f"sable-snapshot: {len(failed)} suite(s) red — ONE automatic re-run of "
              f"only those, as the classifier: {', '.join(failed)}")
        rerun, rerun_logs = run_suites(repo, failed, banner="re-run")
        logs.update({k: v for k, v in rerun_logs.items() if k not in logs})

    verdict = classify_snapshot(first, rerun, set(read_quarantine(repo)))
    print(f"sable-snapshot: verdict — {verdict.reason}")
    result = SnapshotResult(verdict=verdict, exit_code=EXIT_OK, logs=logs)
    if dry_run:
        result.exit_code = (EXIT_FROZEN if verdict.freeze else
                            EXIT_QUARANTINED if verdict.flaky else EXIT_OK)
        return result

    for suite in verdict.flaky:
        bead, created = file_bead_once(
            repo, label=LABEL_FLAKE, key=flake_key(suite),
            title=f"flaky suite: hooks/test/{suite} (quarantined by the green snapshot)",
            description=_flake_description(suite, branch or "integration",
                                           logs.get(suite, ""), run_url),
            bead_type="bug", priority=1)
        if bead:
            result.flake_beads[suite] = bead
        add_quarantine(repo, suite, reason="red then green on the snapshot re-run",
                       bead=bead or "")
        print(f"sable-snapshot: quarantined {suite} "
              f"({'filed ' + bead if created and bead else 'existing bead ' + (bead or 'n/a')})")

    if verdict.freeze:
        write_freeze(repo, suites=verdict.deterministic, reason=verdict.reason,
                     run_url=run_url)
        bead, created = file_bead_once(
            repo, label=LABEL_BISECT, key=bisect_key(verdict.deterministic),
            title=("integration branch RED (deterministic) — bisect and unfreeze: "
                   + ", ".join(verdict.deterministic)),
            description=_bisect_description(verdict, branch or "integration", logs, run_url),
            bead_type="bug", priority=0, extra_labels=("for-chuck",))
        if bead:
            result.bisect_bead = bead
            write_freeze(repo, suites=verdict.deterministic, reason=verdict.reason,
                         bead=bead, run_url=run_url)
        print(f"sable-snapshot: FROZEN — promotion denied until this is resolved "
              f"({'filed ' + bead if created and bead else 'existing bead ' + (bead or 'n/a')})")
        result.exit_code = EXIT_FROZEN
        return result

    if verdict.clears_freeze and read_freeze(repo) is not None:
        clear_freeze(repo, reason=verdict.reason)
        result.unfroze = True
        print("sable-snapshot: green snapshot — freeze CLEARED, promotion is open again")

    result.exit_code = EXIT_QUARANTINED if verdict.flaky else EXIT_OK
    return result
