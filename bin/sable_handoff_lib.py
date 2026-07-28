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
import subprocess
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
    return validate_execution_authority(state)
