#!/usr/bin/env python3
"""tripwire-watcher — auto-file P0 bead when a watched workflow goes stale or red-streaked.

The merge-readiness signal that managers depend on (smoke tests on the
integration branch) can rot silently. This watcher polls GitHub Actions
workflow state and files a P0 bead when:

  - The most recent SUCCESS is older than $SABLE_TRIPWIRE_STALE_HOURS, OR
  - The last $SABLE_TRIPWIRE_RED_STREAK runs are all failure / cancelled /
    startup_failure / action_required

Idempotent: queries existing open tripwire beads before creating; if one
already exists for the same workflow URL it exits cleanly without filing
a duplicate.

Usage:
  tripwire-watcher.py                       # one-shot scan, default config
  tripwire-watcher.py --workflow smoke.yml  # override which workflow to watch
  tripwire-watcher.py --dry-run             # report what would be filed, don't file
  tripwire-watcher.py --json                # machine-readable output

Environment (all optional):
  SABLE_TRIPWIRE_WORKFLOW       Workflow filename or name (default: smoke.yml)
  SABLE_TRIPWIRE_BRANCH         Branch to watch (default: auto-detect integration branch)
  SABLE_TRIPWIRE_STALE_HOURS    Max hours since last green run (default: 24)
  SABLE_TRIPWIRE_RED_STREAK     Consecutive red runs that trigger (default: 3)
  SABLE_TRIPWIRE_LOOKBACK       Number of runs to fetch (default: 10)
  SABLE_TRIPWIRE_ADDRESS_LABEL  Addressing label on filed beads (default: for-tarzan)

Schedule via:
  /loop 1h tripwire-watcher.py            # every hour from a manager session
  bd cron add tripwire-watcher.py "0 * * * *"   # via bd's cron, if supported
  GitHub Actions cron in .github/workflows/    # native cron, no agent needed
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

# Run conclusions that count as "red" for streak detection.
RED_CONCLUSIONS = {"failure", "cancelled", "startup_failure", "action_required", "timed_out"}
# Run conclusions that count as "successful" for staleness detection.
GREEN_CONCLUSIONS = {"success"}


# ---------------------------------------------------------------------------
# Pure functions — all classification logic lives here for testability.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    """Subset of a GitHub Actions workflow run we care about."""

    conclusion: str
    created_at: datetime
    head_sha: str
    url: str
    display_title: str = ""

    @classmethod
    def from_gh_json(cls, obj: dict) -> "Run":
        # gh returns ISO 8601 with trailing Z; Python 3.11+ handles it natively
        # but we normalize for older Python compatibility.
        created = obj.get("createdAt", "")
        if created.endswith("Z"):
            created = created[:-1] + "+00:00"
        return cls(
            conclusion=obj.get("conclusion") or "in_progress",
            created_at=datetime.fromisoformat(created) if created else datetime.now(timezone.utc),
            head_sha=obj.get("headSha", ""),
            url=obj.get("url", ""),
            display_title=obj.get("displayTitle", ""),
        )


@dataclass(frozen=True)
class Classification:
    """Outcome of evaluating a sequence of workflow runs."""

    state: str  # "healthy" | "stale" | "red-streak"
    last_green: Optional[Run]
    recent_runs: List[Run]
    reason: str


def classify_runs(
    runs: Iterable[Run],
    *,
    now: datetime,
    stale_hours: int,
    red_streak: int,
) -> Classification:
    """Classify the recent runs as healthy / stale / red-streak.

    A workflow is `red-streak` if the most recent N runs all have a conclusion
    in RED_CONCLUSIONS. red-streak takes priority over stale because it's
    typically a more urgent signal.

    Otherwise it's `stale` if the most recent green run is older than
    stale_hours (or there is no green run in the window).

    Otherwise it's `healthy`.
    """

    runs_list = list(runs)
    if not runs_list:
        return Classification(
            state="stale",
            last_green=None,
            recent_runs=[],
            reason="No runs found in lookback window",
        )

    # Red-streak check
    last_n = runs_list[:red_streak]
    if len(last_n) >= red_streak and all(r.conclusion in RED_CONCLUSIONS for r in last_n):
        return Classification(
            state="red-streak",
            last_green=_find_last_green(runs_list),
            recent_runs=runs_list,
            reason=f"Last {red_streak} runs all red: " + ", ".join(r.conclusion for r in last_n),
        )

    # Staleness check
    last_green = _find_last_green(runs_list)
    if last_green is None:
        return Classification(
            state="stale",
            last_green=None,
            recent_runs=runs_list,
            reason=f"No successful run in lookback window of {len(runs_list)} runs",
        )

    age = now - last_green.created_at
    if age > timedelta(hours=stale_hours):
        return Classification(
            state="stale",
            last_green=last_green,
            recent_runs=runs_list,
            reason=f"Last green run {_humanize(age)} ago (threshold: {stale_hours}h)",
        )

    return Classification(
        state="healthy",
        last_green=last_green,
        recent_runs=runs_list,
        reason=f"Last green {_humanize(age)} ago",
    )


def _find_last_green(runs: List[Run]) -> Optional[Run]:
    for r in runs:
        if r.conclusion in GREEN_CONCLUSIONS:
            return r
    return None


def _humanize(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        return f"{total // 3600}h"
    return f"{total // 86400}d"


def build_bead_description(
    *,
    workflow: str,
    branch: str,
    classification: Classification,
) -> str:
    """Build the markdown description for the auto-filed P0 bead."""

    runs_md = "\n".join(
        f"  - {r.conclusion} · {r.created_at.isoformat()} · {r.head_sha[:7]} · {r.url}"
        for r in classification.recent_runs[:5]
    ) or "  - (none)"

    last_green_md = (
        f"{classification.last_green.head_sha[:7]} at {classification.last_green.created_at.isoformat()} ({classification.last_green.url})"
        if classification.last_green
        else "(none in lookback window)"
    )

    return f"""## Tripwire signal

Workflow `{workflow}` on `{branch}` is **{classification.state}**.

**Reason:** {classification.reason}

## Last green run

{last_green_md}

## Recent runs

{runs_md}

## Suggested action

Investigate the failing/stale workflow. If a fix is in flight, link the fixing PR here and close this tripwire bead. If not, treat as a P0 swarm-blocker — see MULTI-MANAGER-PATTERN.md §Tarzan's emergency mode.

## Acceptance criteria

- Workflow returns to healthy (last green within {os.environ.get('SABLE_TRIPWIRE_STALE_HOURS', '24')}h, no red streak)
- Or root cause is documented and a fix is tracked in another bead

## Test spec

[no-test] — this bead is a runtime-emitted alert, not a code change. The fix shipped by the agent who claims it must include tests for the underlying issue.
"""


def build_bead_title(workflow: str, branch: str, state: str) -> str:
    return f"Tripwire: {workflow} on {branch} is {state}"


# ---------------------------------------------------------------------------
# Subprocess wrappers — kept thin so tests can monkey-patch.
# ---------------------------------------------------------------------------


def fetch_runs(workflow: str, branch: str, lookback: int) -> List[Run]:
    """Query gh for recent workflow runs. Returns most-recent-first."""

    cmd = [
        "gh",
        "run",
        "list",
        "-w",
        workflow,
        "-b",
        branch,
        "-L",
        str(lookback),
        "--json",
        "conclusion,createdAt,headSha,url,displayTitle",
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"tripwire-watcher: gh failed: {e.stderr}\n")
        return []
    data = json.loads(out)
    return [Run.from_gh_json(o) for o in data]


def existing_open_tripwire_for(workflow_url_substr: str) -> Optional[str]:
    """Return the bead ID of an existing open tripwire bead for this workflow,
    or None if no such bead exists. Idempotency check before filing."""

    cmd = ["bd", "list", "--status=open", "--label=tripwire", "--json", "--limit", "0"]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"tripwire-watcher: bd list failed: {e.stderr}\n")
        return None
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not isinstance(items, list):
        return None
    for item in items:
        desc = item.get("description", "") or ""
        if workflow_url_substr and workflow_url_substr in desc:
            return item.get("id")
    return None


def create_tripwire_bead(
    *,
    title: str,
    description: str,
    address_label: str,
    dry_run: bool,
) -> Optional[str]:
    if dry_run:
        sys.stdout.write(f"[dry-run] would file bead:\n  title: {title}\n  labels: {address_label},coord,tripwire\n")
        return "DRY-RUN"
    cmd = [
        "bd",
        "create",
        "--title",
        title,
        "--type=bug",
        "--priority=0",
        f"--labels={address_label},coord,tripwire",
        "--description",
        description,
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"tripwire-watcher: bd create failed: {e.stderr}\n")
        return None
    # bd create outputs "✓ Created issue: SABLE-xxx — ..."
    for line in out.splitlines():
        if "Created issue:" in line:
            parts = line.split("Created issue:", 1)[1].strip().split()
            if parts:
                return parts[0]
    return None


def detect_integration_branch() -> str:
    """Best-effort detection of the integration branch.

    Order: $SABLE_TRIPWIRE_BRANCH → $SABLE_QA_INTEGRATION_BRANCH →
    $SABLE_BASE_BRANCH (stripping origin/) → "dev" → "main".
    """

    for env_key in ("SABLE_TRIPWIRE_BRANCH", "SABLE_QA_INTEGRATION_BRANCH"):
        v = os.environ.get(env_key)
        if v:
            return v
    base = os.environ.get("SABLE_BASE_BRANCH", "")
    if base:
        return base.split("/", 1)[1] if "/" in base else base
    # Fallback: try git default
    try:
        out = subprocess.check_output(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if "/" in out:
            return out.split("/", 1)[1]
    except subprocess.CalledProcessError:
        pass
    return "dev"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Tripwire watcher for stale or red-streaked CI workflows.")
    parser.add_argument("--workflow", default=os.environ.get("SABLE_TRIPWIRE_WORKFLOW", "smoke.yml"))
    parser.add_argument("--branch", default=None, help="Branch to watch (default: auto-detect integration branch)")
    parser.add_argument("--stale-hours", type=int, default=int(os.environ.get("SABLE_TRIPWIRE_STALE_HOURS", "24")))
    parser.add_argument("--red-streak", type=int, default=int(os.environ.get("SABLE_TRIPWIRE_RED_STREAK", "3")))
    parser.add_argument("--lookback", type=int, default=int(os.environ.get("SABLE_TRIPWIRE_LOOKBACK", "10")))
    parser.add_argument("--address-label", default=os.environ.get("SABLE_TRIPWIRE_ADDRESS_LABEL", "for-tarzan"))
    parser.add_argument("--dry-run", action="store_true", help="Report what would be filed, do not file")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON to stdout")
    args = parser.parse_args(argv)

    branch = args.branch or detect_integration_branch()
    runs = fetch_runs(args.workflow, branch, args.lookback)
    classification = classify_runs(
        runs,
        now=datetime.now(timezone.utc),
        stale_hours=args.stale_hours,
        red_streak=args.red_streak,
    )

    output = {
        "workflow": args.workflow,
        "branch": branch,
        "state": classification.state,
        "reason": classification.reason,
        "last_green_url": classification.last_green.url if classification.last_green else None,
        "filed_bead": None,
        "skipped_existing_bead": None,
    }

    if classification.state == "healthy":
        if args.json:
            print(json.dumps(output))
        else:
            print(f"healthy: {classification.reason}")
        return 0

    # Idempotency: check for existing open tripwire bead for this workflow
    workflow_marker = f"`{args.workflow}` on `{branch}`"
    existing_id = existing_open_tripwire_for(workflow_marker)
    if existing_id:
        output["skipped_existing_bead"] = existing_id
        if args.json:
            print(json.dumps(output))
        else:
            print(f"{classification.state}: existing tripwire bead {existing_id} already open — not filing duplicate")
        return 0

    title = build_bead_title(args.workflow, branch, classification.state)
    description = build_bead_description(
        workflow=args.workflow,
        branch=branch,
        classification=classification,
    )
    bead_id = create_tripwire_bead(
        title=title,
        description=description,
        address_label=args.address_label,
        dry_run=args.dry_run,
    )
    output["filed_bead"] = bead_id

    if args.json:
        print(json.dumps(output))
    else:
        if bead_id:
            print(f"{classification.state}: filed P0 tripwire bead {bead_id}")
            print(f"  reason: {classification.reason}")
        else:
            print(f"{classification.state}: failed to file bead — see stderr")
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
