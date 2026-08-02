"""Durable authority for SABLE's planning-to-execution handoff.

Planning approval is useful only if execution can prove what was approved.
This module turns the final tier-appropriate planning gate into a compact
receipt bound to the planning state, the current backlog, the integration
base, and (for full planning) the five dossier inputs.  The receipt is carried
inside the atomic mode state so there is one transition authority, not a
second state file that can drift from it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from sable_mode_store_lib import (
    ModeStateError,
    read_mode_state,
    set_planning_handoff,
)

RECEIPT_VERSION = 1
DOSSIER_NAMES = (
    "framing",
    "research",
    "architecture",
    "test-strategy",
    "decomposition",
)
Runner = Callable[..., object]

# --- the plan's own prerequisites (SABLE-slip0.6) -----------------------------
#
# The gate used to bind the plan's INTEGRITY and never read its CONTENT: it
# sha256'd framing.json and required `wedge` to be a nonblank string, after
# which the parsed document was never referenced again.  So a wedge naming
# SABLE-9qqrv as prerequisite number one, and a decomposition depending on none
# of it, was signed — a wave dispatched, two workers produced real code, and
# not one bead could be closed.
#
# Prose extraction was rejected (D6) because it was measured inadequate in BOTH
# directions over the six real framing.json files: bead ids appear in the wedge
# on only 2 of 6, so the check is a silent no-op on the other 4; and where one
# does appear it may be a CARRIER or a sequencing note rather than a blocker
# (tz7h names SABLE-s0c3 that way), which a naive rule refuses wrongly.  The
# distinction between "prerequisite" and "merely mentioned" is semantic, and
# prose cannot carry it — so the plan states it in a structured field instead.
PREREQUISITE_PASS = "PASS"
PREREQUISITE_REFUSE = "REFUSE"
PREREQUISITE_UNCHECKABLE = "UNCHECKABLE"
PREREQUISITE_STATUSES = (
    PREREQUISITE_PASS,
    PREREQUISITE_REFUSE,
    PREREQUISITE_UNCHECKABLE,
)

# Ported from .github/ci/shell-run-set.sh's parse_exclude_tag (D7) rather than
# invented here, including its bead-id shape.  Two properties carry over and
# both are load-bearing:
#
#   ANCHORED — matched with fullmatch, so a mention is never harvested.  "see
#   SABLE-9qqrv first" is a malformed ENTRY, not a declaration of SABLE-9qqrv;
#   the free-scan failure mode is structurally unavailable.
#
#   EXACTLY ONE — one entry states one rule.  "SABLE-9qqrv SABLE-1el7e" fails
#   the same fullmatch, so a reader never has to guess whether an entry is one
#   id or several.
_BEAD_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*-[A-Za-z0-9.]+")


class HandoffRefused(RuntimeError):
    """Planning evidence is absent, incomplete, stale, or otherwise unsafe."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _timestamp(now: datetime | None = None) -> str:
    instant = now or datetime.now(timezone.utc)
    return instant.astimezone(timezone.utc).isoformat()


def approval_actor() -> str:
    """Return the recorded operator identity without claiming authentication."""

    return next(
        (
            value.strip()
            for value in (
                os.environ.get("SABLE_APPROVAL_ACTOR"),
                os.environ.get("BEADS_ACTOR"),
                os.environ.get("USER"),
            )
            if value and value.strip()
        ),
        "unknown-operator",
    )


def _run_json(runner: Runner, argv: list[str], description: str) -> object:
    try:
        result = runner(argv, capture_output=True, text=True)
    except OSError as exc:
        raise HandoffRefused(f"{description} could not run: {exc}") from exc
    returncode = getattr(result, "returncode", 1)
    stdout = getattr(result, "stdout", "")
    stderr = getattr(result, "stderr", "")
    if returncode != 0:
        detail = (stderr or stdout or "no diagnostic").strip()
        raise HandoffRefused(f"{description} failed: {detail}")
    try:
        return json.loads(stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HandoffRefused(
            f"{description} returned malformed JSON"
        ) from exc


def _records(payload: object, description: str) -> list[dict]:
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or any(
        not isinstance(item, dict) for item in payload
    ):
        raise HandoffRefused(f"{description} returned an invalid record set")
    return [dict(item) for item in payload]


def _show(issue_id: str, runner: Runner) -> dict:
    result = _records(
        _run_json(
            runner,
            ["bd", "show", issue_id, "--json"],
            f"bd show {issue_id}",
        ),
        f"bd show {issue_id}",
    )
    if len(result) != 1 or result[0].get("id") != issue_id:
        raise HandoffRefused(f"bd show {issue_id} did not return that bead")
    return result[0]


def _unresolved_questions(runner: Runner) -> list[dict]:
    return _records(
        _run_json(
            runner,
            [
                "bd",
                "list",
                "--status",
                "open,in_progress,blocked,deferred",
                "-l",
                "open-question",
                "--json",
                "--limit",
                "0",
            ],
            "open-question query",
        ),
        "open-question query",
    )


def _ready_ids(runner: Runner) -> set[str]:
    rows = _records(
        _run_json(
            runner,
            ["bd", "ready", "--json", "--limit", "0"],
            "ready-front query",
        ),
        "ready-front query",
    )
    return {
        str(row["id"])
        for row in rows
        if isinstance(row.get("id"), str) and row["id"]
    }


def _assert_open_unclaimed(records: Sequence[Mapping[str, object]]) -> None:
    unavailable = [
        str(record.get("id") or "?")
        for record in records
        if record.get("status") != "open"
        or bool(record.get("assignee"))
    ]
    if unavailable:
        raise HandoffRefused(
            "handoff scope must be open and unclaimed; unavailable: "
            + ", ".join(unavailable)
        )


def _repo_root() -> Path:
    override = os.environ.get("SABLE_HANDOFF_REPO")
    if override:
        return Path(override)
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise HandoffRefused("cannot resolve repository for handoff base")
    return Path(result.stdout.strip())


def _integration_base() -> dict[str, str]:
    root = _repo_root()
    try:
        from sable_gate_git_lib import resolve_integration_branch

        branch = resolve_integration_branch(str(root))
    except (ImportError, OSError, subprocess.SubprocessError) as exc:
        raise HandoffRefused(
            f"cannot resolve integration branch: {exc}"
        ) from exc
    candidates = (f"origin/{branch}", branch, "HEAD")
    for ref in candidates:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return {"ref": ref, "sha": result.stdout.strip()}
    raise HandoffRefused(
        f"integration base {branch!r} does not resolve to a commit"
    )


def _planning_root(state_path: Path) -> Path:
    override = os.environ.get("SABLE_PLANNING_DIR")
    return Path(override) if override else state_path.parent / "planning"


def _load_dossier(state_path: Path, epic: str) -> tuple[dict, dict[str, str]]:
    directory = _planning_root(state_path) / epic
    documents: dict[str, dict] = {}
    hashes: dict[str, str] = {}
    for name in DOSSIER_NAMES:
        path = directory / f"{name}.json"
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise HandoffRefused(
                f"missing planning artifact: {path.name}"
            ) from exc
        except OSError as exc:
            raise HandoffRefused(
                f"cannot read planning artifact {path.name}: {exc}"
            ) from exc
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HandoffRefused(
                f"malformed planning artifact: {path.name}"
            ) from exc
        if not isinstance(document, dict) or not document:
            raise HandoffRefused(f"empty planning artifact: {path.name}")
        documents[name] = document
        hashes[f"{name}.json"] = hashlib.sha256(raw).hexdigest()
    _validate_dossier(documents)
    return documents, hashes


def _nonempty_list(document: Mapping[str, object], key: str, name: str) -> list:
    value = document.get(key)
    if not isinstance(value, list) or not value:
        raise HandoffRefused(f"{name}.json requires a non-empty {key}")
    return value


def _validate_dossier(documents: Mapping[str, Mapping[str, object]]) -> None:
    framing = documents["framing"]
    framing_stories = _nonempty_list(framing, "stories", "framing")
    if any(
        not isinstance(story, dict)
        or not isinstance(story.get("id"), str)
        or not story["id"].strip()
        or not isinstance(story.get("title"), str)
        or not story["title"].strip()
        for story in framing_stories
    ):
        raise HandoffRefused("framing.json stories require id and title")
    for field in ("success_metric", "wedge"):
        if not isinstance(framing.get(field), str) or not framing[field].strip():
            raise HandoffRefused(f"framing.json requires {field}")
    # REQUIRED, and an empty array is permitted AND meaningful: it is the
    # explicit assertion "this epic declares no prerequisite".  Making the
    # field optional would restore the silent no-op — a gate that looks
    # enforced and enforces nothing is the exact shape this check exists to
    # remove, so absence is schema-invalid rather than skipped.
    prerequisites = framing.get("prerequisites")
    if not isinstance(prerequisites, list):
        raise HandoffRefused(
            "framing.json requires a prerequisites array of bead ids "
            "(an empty array is permitted and asserts that the epic declares "
            "no prerequisite)"
        )

    research = documents["research"]
    findings = _nonempty_list(research, "findings", "research")
    if any(
        not isinstance(finding, dict)
        or not isinstance(finding.get("title"), str)
        or not finding["title"].strip()
        or not isinstance(finding.get("summary"), str)
        or not finding["summary"].strip()
        or finding.get("derisk_status") == "open"
        or finding.get("derisk_status") != "resolved"
        for finding in findings
    ):
        raise HandoffRefused(
            "research.json contains an open or malformed de-risk finding"
        )
    if not isinstance(research.get("recommendation"), str) or not research[
        "recommendation"
    ].strip():
        raise HandoffRefused("research.json requires recommendation")

    architecture = documents["architecture"]
    if architecture.get("status") != "ready":
        raise HandoffRefused("architecture.json status must be ready")
    decisions = _nonempty_list(architecture, "decisions", "architecture")
    if any(
        not isinstance(decision, dict)
        or any(
            not isinstance(decision.get(field), str)
            or not decision[field].strip()
            for field in ("title", "contract", "rationale")
        )
        for decision in decisions
    ):
        raise HandoffRefused(
            "architecture.json decisions require title, contract, and rationale"
        )

    strategy = documents["test-strategy"]
    stories = _nonempty_list(strategy, "stories", "test-strategy")
    if any(
        not isinstance(story, dict)
        or not isinstance(story.get("id"), str)
        or not story["id"].strip()
        or not isinstance(story.get("cases"), list)
        or not story["cases"]
        or any(
            not isinstance(case, dict)
            or not isinstance(case.get("name"), str)
            or not case["name"].strip()
            or case.get("layer") not in {"UNIT", "E2E", "EVAL"}
            or case.get("status") not in {"planned", "gap"}
            for case in story["cases"]
        )
        for story in stories
    ):
        raise HandoffRefused(
            "test-strategy.json stories require non-empty cases"
        )
    coverage = strategy.get("coverage")
    if (
        not isinstance(coverage, dict)
        or not isinstance(coverage.get("covered"), int)
        or not isinstance(coverage.get("total"), int)
        or coverage["total"] < 1
        or coverage["covered"] < 0
        or coverage["covered"] > coverage["total"]
    ):
        raise HandoffRefused("test-strategy.json requires coverage")

    decomposition = documents["decomposition"]
    children = _nonempty_list(decomposition, "children", "decomposition")
    if any(
        not isinstance(child, dict)
        or any(
            not isinstance(child.get(field), str) or not child[field].strip()
            for field in ("id", "title", "type")
        )
        or not isinstance(child.get("deps"), list)
        or not isinstance(child.get("ready"), bool)
        for child in children
    ):
        raise HandoffRefused(
            "decomposition.json children require id, title, type, deps, and ready"
        )
    swarm = decomposition.get("swarm_validate")
    if (
        not isinstance(swarm, dict)
        or swarm.get("ok") is not True
        or not isinstance(swarm.get("output"), str)
        or not swarm["output"].strip()
    ):
        raise HandoffRefused(
            "decomposition.json requires swarm_validate.ok=true"
        )
    if not isinstance(decomposition.get("victor_summary"), str) or not decomposition[
        "victor_summary"
    ].strip():
        raise HandoffRefused("decomposition.json requires victor_summary")


def _verdict(
    status: str,
    reason: str,
    *,
    declared: Sequence[str] = (),
    missing: Sequence[str] = (),
    malformed: Sequence[str] = (),
    unresolvable: Sequence[str] = (),
    stale: Sequence[str] = (),
) -> dict:
    """One verdict shape, always fully populated.

    It is digested into the receipt, so every field is sorted and every key is
    present whatever the outcome — a consumer never has to distinguish "absent"
    from "empty", and two runs over the same inputs produce the same bytes.
    """

    return {
        "status": status,
        "reason": reason,
        "declared": sorted(set(declared)),
        "missing": sorted(set(missing)),
        "malformed": sorted(set(malformed)),
        "unresolvable": sorted(set(unresolvable)),
        "stale": sorted(set(stale)),
    }


def _dependency_closure(decomposition: Mapping[str, object]) -> tuple[list, list]:
    """Every id the decomposition's children declare a dependency edge to.

    Returns (dep ids, malformed entries).  Cross-epic ids are terminal and
    within-epic ids resolve to siblings whose own deps are listed too, so the
    union over children IS the closure represented by the artifact. Merely
    being a child does not satisfy a prerequisite: without an incoming edge,
    that child is in the same dispatch front rather than ordered first.
    """

    children = decomposition.get("children")
    if not isinstance(children, list):
        return [], ["decomposition.json children is not a list"]
    closure: list[str] = []
    malformed: list[str] = []
    for child in children:
        if not isinstance(child, dict):
            malformed.append("decomposition.json child is not an object")
            continue
        deps = child.get("deps")
        if not isinstance(deps, list):
            malformed.append(
                f"decomposition.json child {child.get('id')!r} deps is not a list"
            )
            continue
        for dep in deps:
            if isinstance(dep, str) and _BEAD_ID_RE.fullmatch(dep):
                closure.append(dep)
            else:
                malformed.append(dep if isinstance(dep, str) else repr(dep))
    return closure, malformed


def prerequisite_verdict(
    framing: Mapping[str, object],
    decomposition: Mapping[str, object],
    *,
    resolve: Callable[[str], str | None] | None = None,
) -> dict:
    """Does the scope actually depend on what the plan said had to come first?

    Reads framing's ``prerequisites`` and NOTHING else from framing — no scan
    over ``wedge``, ``non_goals`` or ``stories``, which is why a mention like
    tz7h's SABLE-s0c3 carrier cannot become a refusal.

    Two halves, deliberately split by what each needs (D7):

      SHAPE + CLOSURE are bd-free, so they decide everywhere — including the
      clean room — and they REFUSE, because a malformed entry or a declared
      prerequisite absent from the dependency closure is mechanically
      unambiguous without asking the tracker anything.

      RESOLUTION needs bd.  It separates UNRESOLVABLE (the id does not exist —
      a typo or a deleted bead, so the claim is unfalsifiable) from STALE (the
      prerequisite is already closed — a claim that outlived its cause, which
      is reported, not refused: it landed).

    ``resolve`` is a callable id -> status, returning None when the tracker
    cannot resolve the id, or None itself when there is no tracker at all.  In
    that last case the answer is UNCHECKABLE — never PASS (D8).
    """

    declared_raw = framing.get("prerequisites")
    if not isinstance(declared_raw, list):
        return _verdict(
            PREREQUISITE_REFUSE,
            "framing.json declares no prerequisites array; the field is "
            "REQUIRED (an empty array asserts the epic has no prerequisite)",
        )

    declared: list[str] = []
    malformed: list[str] = []
    for entry in declared_raw:
        if isinstance(entry, str) and _BEAD_ID_RE.fullmatch(entry):
            declared.append(entry)
        else:
            malformed.append(entry if isinstance(entry, str) else repr(entry))

    closure, dep_malformed = _dependency_closure(decomposition)
    malformed.extend(dep_malformed)
    if malformed:
        return _verdict(
            PREREQUISITE_REFUSE,
            "prerequisite and dependency entries must each be exactly one "
            "bead id; malformed: " + ", ".join(sorted(set(malformed))),
            declared=declared,
            malformed=malformed,
        )

    declared_set = set(declared)
    closure_set = set(closure)
    missing = [item for item in declared if item not in closure_set]
    if missing:
        return _verdict(
            PREREQUISITE_REFUSE,
            "the plan declares prerequisite(s) that no child in the "
            "decomposition names through a dependency edge: "
            + ", ".join(sorted(set(missing)))
            + " — the scope would dispatch before its own stated blockers land",
            declared=declared,
            missing=missing,
        )

    if resolve is None:
        return _verdict(
            PREREQUISITE_UNCHECKABLE,
            "the shape and dependency-closure halves hold, but no bead store "
            "is reachable, so the declared ids were not resolved",
            declared=declared,
        )

    unresolvable: list[str] = []
    stale: list[str] = []
    for issue_id in sorted(declared_set | closure_set):
        status = resolve(issue_id)
        if status is None:
            unresolvable.append(issue_id)
        elif status == "closed" and issue_id in declared_set:
            stale.append(issue_id)
    if unresolvable:
        return _verdict(
            PREREQUISITE_REFUSE,
            "bead id(s) cited by the plan do not resolve in the bead store, so "
            "the dependency closure this check compares against is itself "
            "unsound: " + ", ".join(unresolvable),
            declared=declared,
            unresolvable=unresolvable,
            stale=stale,
        )
    return _verdict(
        PREREQUISITE_PASS,
        f"{len(declared)} declared prerequisite(s), each present in the "
        "decomposition dependency closure",
        declared=declared,
        stale=stale,
    )


def carried_prerequisite_verdict(proof: Mapping[str, object]) -> dict:
    """Read a receipt's verdict without a bead store or a planning artifact.

    Fleet start (read_execution_authority) has neither, so it cannot recompute
    this — it can only report what was signed.  A receipt carrying no verdict,
    or one carrying something unrecognized, therefore reads back UNCHECKABLE.
    That covers legacy receipts without breaking them, and it keeps the one
    property D8 insists on: the absence of an answer never renders as a clean
    one.
    """

    carried = proof.get("prerequisites")
    if isinstance(carried, dict) and carried.get("status") in PREREQUISITE_STATUSES:
        return dict(carried)
    return _verdict(
        PREREQUISITE_UNCHECKABLE,
        "this receipt carries no recognizable prerequisite verdict, so nothing "
        "is asserted about the plan's own stated blockers",
    )


def render_prerequisite_verdict(verdict: Mapping[str, object]) -> str:
    """One line per verdict, and the three never collapse into each other.

    Asserting three distinct enum values proves nothing if the renderer prints
    them the same way, so this is the single renderer every call site uses —
    the gate, the mode transition, and fleet start — rather than three that can
    drift apart.
    """

    status = verdict.get("status")
    reason = str(verdict.get("reason") or "").strip()
    if status == PREREQUISITE_PASS:
        return f"prerequisites: {PREREQUISITE_PASS} — {reason or 'checked'}"
    if status == PREREQUISITE_REFUSE:
        return f"prerequisites: {PREREQUISITE_REFUSE} — {reason or 'refused'}"
    if status == PREREQUISITE_UNCHECKABLE:
        return (
            f"prerequisites: {PREREQUISITE_UNCHECKABLE} — this check did NOT "
            "run to a verdict" + (f"; {reason}" if reason else "")
        )
    return (
        f"prerequisites: {PREREQUISITE_UNCHECKABLE} — unrecognized verdict, "
        "treated as unverified"
    )


def _announce_prerequisites(verdict: Mapping[str, object]) -> None:
    """Render the verdict at whichever call site is running.

    Deliberately emitted from here rather than from sable-mode,
    sable-spawn-manager and sable-spawn-worker separately: three renderers of
    one three-valued verdict is the Shotgun Surgery the architecture review
    named, and it is exactly how one of them ends up printing UNCHECKABLE the
    same way it prints PASS.
    """

    sys.stderr.write(render_prerequisite_verdict(verdict) + "\n")


class _ResolutionUnavailable(RuntimeError):
    """The bead store could not be executed at all (not: an id was missing)."""


def _bead_resolver(runner: Runner) -> Callable[[str], str | None]:
    """A tolerant id -> status lookup for the resolution half.

    Deliberately not _show: that raises on a missing bead, and here "the id
    does not resolve" is a finding to REPORT (None), not an error to abort on.
    The two unavailabilities stay separate — ONLY bd's explicit missing-issue
    response returns None; an unrunnable command, a different nonzero result,
    malformed JSON, or an unusable record raises _ResolutionUnavailable.
    Conflating those cases is how "I could not look" becomes "it is not
    there", falsely accusing the plan instead of reporting UNCHECKABLE.
    """

    def resolve(issue_id: str) -> str | None:
        try:
            result = runner(
                ["bd", "show", issue_id, "--json"],
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise _ResolutionUnavailable(str(exc)) from exc
        stdout = getattr(result, "stdout", "") or ""
        stderr = getattr(result, "stderr", "") or ""
        try:
            payload = json.loads(stdout)
        except (TypeError, json.JSONDecodeError):
            payload = None

        if getattr(result, "returncode", 1) != 0:
            # Current bd emits this structured envelope (and the matching
            # stderr phrase) when the command DID reach the store and proved
            # the id absent. Narrow matching is deliberate: a generic rc=1 is
            # also how a locked/unreachable store reports failure, and that is
            # UNCHECKABLE, not evidence that the bead does not exist.
            error = payload.get("error") if isinstance(payload, dict) else None
            diagnostic = " ".join(
                value.strip().lower()
                for value in (str(error or ""), str(stderr))
                if value and value.strip()
            )
            if (
                "no issue found matching" in diagnostic
                or "no issues found matching" in diagnostic
            ):
                return None
            detail = str(stderr or stdout or "no diagnostic").strip()
            raise _ResolutionUnavailable(
                f"bd show {issue_id} failed: {detail}"
            )

        if payload is None:
            raise _ResolutionUnavailable(
                f"bd show {issue_id} returned malformed JSON"
            )
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list) or len(payload) != 1:
            raise _ResolutionUnavailable(
                f"bd show {issue_id} returned an invalid record set"
            )
        record = payload[0]
        if not isinstance(record, dict) or record.get("id") != issue_id:
            raise _ResolutionUnavailable(
                f"bd show {issue_id} did not return that bead"
            )
        status = record.get("status")
        if not isinstance(status, str) or not status.strip():
            raise _ResolutionUnavailable(
                f"bd show {issue_id} returned no usable status"
            )
        return status.strip()

    return resolve


def _checked_prerequisites(
    framing: Mapping[str, object],
    decomposition: Mapping[str, object],
    runner: Runner,
) -> dict:
    """The gate's arm: refuse a REFUSE, carry a PASS or an UNCHECKABLE."""

    try:
        verdict = prerequisite_verdict(
            framing, decomposition, resolve=_bead_resolver(runner)
        )
    except _ResolutionUnavailable as exc:
        verdict = prerequisite_verdict(framing, decomposition, resolve=None)
        if verdict["status"] == PREREQUISITE_UNCHECKABLE:
            verdict["reason"] = (
                f"{verdict['reason']} (bead store unreachable: {exc})"
            )
    if verdict["status"] == PREREQUISITE_REFUSE:
        raise HandoffRefused(verdict["reason"])
    return verdict


def _swarm_validation(epic: str, runner: Runner) -> dict:
    payload = _run_json(
        runner,
        ["bd", "swarm", "validate", epic, "--json"],
        f"bd swarm validate {epic}",
    )
    if not isinstance(payload, dict):
        raise HandoffRefused("swarm validate returned an invalid result")
    errors = payload.get("errors")
    if payload.get("swarmable") is not True or errors not in (None, []):
        raise HandoffRefused(
            "swarm validate did not prove a swarmable, error-free backlog"
        )
    warnings = payload.get("warnings")
    if warnings is None:
        warnings = []
    if not isinstance(warnings, list):
        raise HandoffRefused("swarm validate warnings must be a list")
    return {
        "schema_version": payload.get("schema_version"),
        "swarmable": True,
        "warnings": sorted(warnings, key=lambda item: _canonical(item)),
    }


def _collect_evidence(
    state_path: Path,
    state: Mapping[str, object],
    *,
    beads: Sequence[str] = (),
    epic: str | None = None,
    runner: Runner,
) -> dict:
    if state.get("mode") != "planning":
        raise HandoffRefused("handoff approval requires planning mode")
    tier = state.get("tier", "full")
    if tier == "discovery":
        raise HandoffRefused("discovery planning cannot authorize execution")

    questions = _unresolved_questions(runner)
    if questions:
        ids = ", ".join(str(row.get("id") or "?") for row in questions)
        raise HandoffRefused(f"unresolved open-question bead(s): {ids}")

    ready = _ready_ids(runner)
    base = _integration_base()
    if tier == "quick":
        scope = tuple(dict.fromkeys(bead.strip() for bead in beads if bead.strip()))
        if epic:
            raise HandoffRefused("quick handoff uses --beads, not --epic")
        if not 1 <= len(scope) <= 3:
            raise HandoffRefused("quick handoff requires 1-3 explicit bead IDs")
        records = [_show(issue_id, runner) for issue_id in scope]
        _assert_open_unclaimed(records)
        ready_scope = sorted(set(scope) & ready)
        if not ready_scope:
            raise HandoffRefused("quick handoff has no ready front")
        return {
            "tier": "quick",
            "scope": list(scope),
            "records": records,
            "ready_scope": ready_scope,
            "unresolved_question_ids": [],
            "base": base,
            "artifacts": {},
            "swarm": None,
            # Quick tier has no framing artifact, so there is no declaration to
            # check.  Recorded explicitly rather than omitted: "not asserted"
            # must be readable off the receipt, not inferred from a missing key.
            "prerequisites": _verdict(
                PREREQUISITE_UNCHECKABLE,
                "quick-tier handoff has no framing artifact, so no "
                "prerequisite declaration was made or checked",
            ),
        }

    if tier != "full":
        raise HandoffRefused(f"unsupported planning tier: {tier!r}")
    if beads:
        raise HandoffRefused("full handoff uses --epic, not --beads")
    if not epic:
        raise HandoffRefused("full handoff requires --epic")
    if state.get("substage") != "decomposition":
        raise HandoffRefused(
            "full handoff requires planning substage decomposition"
        )
    epic_record = _show(epic, runner)
    if epic_record.get("issue_type") != "epic":
        raise HandoffRefused(f"{epic} is not an epic")
    children = _records(
        _run_json(
            runner,
            [
                "bd",
                "list",
                "--parent",
                epic,
                "--status",
                "open,in_progress",
                "--json",
                "--limit",
                "0",
            ],
            f"live children for {epic}",
        ),
        f"live children for {epic}",
    )
    if not children:
        raise HandoffRefused(f"{epic} has no live child backlog")
    children.sort(key=lambda child: str(child.get("id") or ""))
    _assert_open_unclaimed(children)
    child_ids = [str(child.get("id") or "") for child in children]
    if any(not issue_id for issue_id in child_ids):
        raise HandoffRefused("full handoff contains a child without an ID")
    ready_scope = sorted(set(child_ids) & ready)
    if not ready_scope:
        raise HandoffRefused("full handoff has no ready front")

    documents, artifact_hashes = _load_dossier(state_path, epic)
    declared_children = {
        str(item.get("id"))
        for item in documents["decomposition"]["children"]
        if isinstance(item, dict) and item.get("id")
    }
    if declared_children != set(child_ids):
        raise HandoffRefused(
            "decomposition artifact is stale relative to the live child backlog"
        )
    # Placed HERE, at the handoff gate, and not at substage advance:
    # advance_planning_substage reads no artifact and checks no file existence,
    # so this check would run before decomposition.json exists and could only
    # report UNCHECKABLE forever — enforcement-shaped and inert, which is the
    # precise failure this bead exists to remove.
    prerequisites = _checked_prerequisites(
        documents["framing"], documents["decomposition"], runner
    )
    swarm = _swarm_validation(epic, runner)
    return {
        "tier": "full",
        "scope": child_ids,
        "epic": epic,
        "epic_record": epic_record,
        "records": children,
        "ready_scope": ready_scope,
        "unresolved_question_ids": [],
        "base": base,
        "artifacts": artifact_hashes,
        "swarm": swarm,
        "prerequisites": prerequisites,
    }


def _receipt(
    state: Mapping[str, object],
    evidence: Mapping[str, object],
    *,
    approved_by: str,
    now: datetime | None,
) -> dict:
    actor = approved_by.strip()
    if not actor:
        raise HandoffRefused("handoff approval requires a recorded operator")
    payload: dict[str, object] = {
        "version": RECEIPT_VERSION,
        "kind": "approved",
        "tier": evidence["tier"],
        "scope": evidence["scope"],
        "planning_since": state.get("since"),
        "approved_at": _timestamp(now),
        "approved_by": actor,
        "base": evidence["base"],
        "artifacts": evidence["artifacts"],
        "swarm": evidence["swarm"],
        "prerequisites": evidence["prerequisites"],
        "evidence_sha256": _digest(evidence),
    }
    if evidence.get("epic"):
        payload["epic"] = evidence["epic"]
    payload["receipt_id"] = _digest(payload)
    return payload


def approve_handoff(
    state_path: str | os.PathLike[str],
    *,
    approved_by: str,
    beads: Sequence[str] = (),
    epic: str | None = None,
    runner: Runner = subprocess.run,
    now: datetime | None = None,
) -> dict:
    """Validate final planning evidence and atomically attach its receipt."""

    path = Path(state_path)
    try:
        state = read_mode_state(path)
    except ModeStateError as exc:
        raise HandoffRefused(str(exc)) from exc
    evidence = _collect_evidence(
        path, state, beads=beads, epic=epic, runner=runner
    )
    receipt = _receipt(state, evidence, approved_by=approved_by, now=now)
    try:
        set_planning_handoff(path, receipt, expected_state=state)
    except ModeStateError as exc:
        raise HandoffRefused(
            f"planning state changed during handoff approval: {exc}"
        ) from exc
    _announce_prerequisites(carried_prerequisite_verdict(receipt))
    return receipt


def validate_handoff_state(
    state_path: str | os.PathLike[str],
    state: Mapping[str, object],
    *,
    runner: Runner = subprocess.run,
) -> dict:
    """Revalidate an embedded receipt against current external evidence."""

    receipt = state.get("handoff")
    if not isinstance(receipt, dict) or receipt.get("kind") != "approved":
        raise HandoffRefused("planning state has no approved handoff receipt")
    tier = receipt.get("tier")
    evidence = _collect_evidence(
        Path(state_path),
        state,
        beads=tuple(receipt.get("scope") or ()) if tier == "quick" else (),
        epic=str(receipt.get("epic")) if tier == "full" else None,
        runner=runner,
    )
    if (
        receipt.get("planning_since") != state.get("since")
        or receipt.get("evidence_sha256") != _digest(evidence)
    ):
        raise HandoffRefused(
            "handoff receipt is stale relative to planning state or evidence"
        )
    validate_execution_authority({"mode": "execution", "handoff": receipt})
    _announce_prerequisites(carried_prerequisite_verdict(receipt))
    return dict(receipt)


def validate_handoff(
    state_path: str | os.PathLike[str],
    *,
    runner: Runner = subprocess.run,
) -> dict:
    path = Path(state_path)
    try:
        state = read_mode_state(path)
    except ModeStateError as exc:
        raise HandoffRefused(str(exc)) from exc
    return validate_handoff_state(path, state, runner=runner)


def break_glass_authority(
    *,
    approved_by: str,
    reason: str,
    now: datetime | None = None,
    failed_checks: Sequence[str] = (),
) -> dict:
    """Create an explicit bypass record; this never masquerades as approval."""

    actor = approved_by.strip()
    explanation = reason.strip()
    if not actor:
        raise HandoffRefused("break-glass requires a recorded operator")
    if not explanation:
        raise HandoffRefused("break-glass requires a nonblank reason")
    payload: dict[str, object] = {
        "version": RECEIPT_VERSION,
        "kind": "break-glass",
        "approved_by": actor,
        "approved_at": _timestamp(now),
        "reason": explanation,
        "base": _integration_base(),
        "failed_checks": [str(check) for check in failed_checks if str(check)],
    }
    payload["receipt_id"] = _digest(payload)
    return payload


def validate_execution_authority(state: Mapping[str, object]) -> dict:
    """Return a valid carried authority or refuse legacy/statusless state."""

    if state.get("mode") != "execution":
        raise HandoffRefused("execution handoff requires execution mode")
    proof = state.get("handoff")
    if not isinstance(proof, dict):
        raise HandoffRefused("execution state lacks durable handoff proof")
    if proof.get("version") != RECEIPT_VERSION:
        raise HandoffRefused("execution handoff has an unsupported version")
    if not all(
        isinstance(proof.get(field), str) and proof[field].strip()
        for field in ("approved_by", "approved_at", "receipt_id")
    ):
        raise HandoffRefused("execution handoff is missing its audit identity")
    unsigned = {key: value for key, value in proof.items() if key != "receipt_id"}
    if proof["receipt_id"] != _digest(unsigned):
        raise HandoffRefused("execution handoff receipt digest does not match")
    base = proof.get("base")
    if not isinstance(base, dict) or not all(
        isinstance(base.get(field), str) and base[field].strip()
        for field in ("ref", "sha")
    ):
        raise HandoffRefused("execution handoff is missing its base identity")
    if proof.get("kind") == "approved":
        if proof.get("tier") not in {"quick", "full"}:
            raise HandoffRefused("approved handoff has an invalid tier")
        if not isinstance(proof.get("scope"), list) or not proof["scope"]:
            raise HandoffRefused("approved handoff has no scope")
        if not isinstance(proof.get("evidence_sha256"), str):
            raise HandoffRefused("approved handoff has no evidence digest")
    elif proof.get("kind") == "break-glass":
        if not isinstance(proof.get("reason"), str) or not proof["reason"].strip():
            raise HandoffRefused("break-glass handoff has no reason")
        if not isinstance(proof.get("failed_checks"), list):
            raise HandoffRefused("break-glass handoff has invalid failed_checks")
    else:
        raise HandoffRefused("execution handoff kind is invalid")
    return dict(proof)


def read_execution_authority(
    *, base: str | os.PathLike[str] | None = None
) -> dict:
    """Read and validate the fleet-start authority for one repository."""

    from sable_mode_store_lib import resolve_mode_state_path

    path = resolve_mode_state_path(base)
    try:
        state = read_mode_state(path)
    except ModeStateError as exc:
        raise HandoffRefused(str(exc)) from exc
    authority = validate_execution_authority(state)
    # Fleet start has no bead store and no planning artifact, so it cannot
    # recompute the verdict — it reports the one that was signed.  A receipt
    # that asserts nothing here says so out loud instead of starting quietly.
    _announce_prerequisites(carried_prerequisite_verdict(authority))
    return authority
