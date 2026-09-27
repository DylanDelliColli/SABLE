"""Contracts for durable planning-to-execution handoff receipts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

import sable_handoff_lib as handoff
import sable_mode_store_lib as store


@dataclass
class Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeBd:
    def __init__(
        self,
        records: dict[str, dict],
        *,
        children: dict[str, list[str]] | None = None,
        ready: set[str] | None = None,
        questions: list[dict] | None = None,
        swarm_ok: bool = True,
    ):
        self.records = records
        self.children = children or {}
        self.ready = ready or set()
        self.questions = questions or []
        self.swarm_ok = swarm_ok

    def __call__(self, argv, **_kwargs):
        assert argv[0] == "bd"
        if argv[1] == "show":
            record = self.records.get(argv[2])
            return Result(
                returncode=0 if record else 1,
                stdout=json.dumps([record]) if record else "",
                stderr="" if record else "not found",
            )
        if argv[1:3] == ["list", "--parent"]:
            ids = self.children.get(argv[3], [])
            return Result(stdout=json.dumps([self.records[item] for item in ids]))
        if argv[1] == "list" and "open-question" in argv:
            return Result(stdout=json.dumps(self.questions))
        if argv[1] == "ready":
            return Result(
                stdout=json.dumps(
                    [self.records[item] for item in sorted(self.ready)]
                )
            )
        if argv[1:3] == ["swarm", "validate"]:
            payload = {
                "schema_version": 1,
                "swarmable": self.swarm_ok,
                "errors": None if self.swarm_ok else ["dependency graph invalid"],
                "warnings": ["one serial dependency"],
                "ready_fronts": [["SABLE-ready"]] if self.swarm_ok else [],
            }
            return Result(
                returncode=0 if self.swarm_ok else 1,
                stdout=json.dumps(payload),
                stderr="" if self.swarm_ok else "Swarmable: NO",
            )
        raise AssertionError(f"unexpected command: {argv}")


def _record(issue_id: str, *, issue_type: str = "task", description: str = "work"):
    return {
        "id": issue_id,
        "title": f"title {issue_id}",
        "description": description,
        "acceptance_criteria": "done",
        "status": "open",
        "priority": 1,
        "issue_type": issue_type,
        "labels": ["model:sonnet"],
        "dependencies": [],
    }


def _planning_state(path: Path, *, tier: str, substage: str = "framing"):
    path.write_text(
        json.dumps(
            {
                "mode": "planning",
                "since": "2026-07-28T12:00:00+0000",
                "fleet": [],
                "tier": tier,
                "substage": substage,
            }
        )
    )


def _full_dossier(root: Path, epic: str):
    state_dir = root / epic
    state_dir.mkdir(parents=True)
    documents = {
        "framing": {
            "stories": [
                {
                    "id": "story-1",
                    "title": "Operators can execute approved work",
                }
            ],
            "success_metric": "fleet starts only from approved work",
            "wedge": "durable transition proof",
        },
        "research": {
            "findings": [
                {
                    "title": "Advisory transition",
                    "summary": "the current transition is advisory",
                    "derisk_status": "resolved",
                }
            ],
            "recommendation": "bind approval to the transition",
        },
        "architecture": {
            "status": "ready",
            "decisions": [
                {
                    "title": "One authority",
                    "contract": "carry proof in mode state",
                    "rationale": "avoid drift",
                }
            ],
        },
        "test-strategy": {
            "stories": [
                {
                    "id": "story-1",
                    "cases": [
                        {
                            "name": "approved transition",
                            "layer": "UNIT",
                            "status": "planned",
                        },
                        {
                            "name": "stale refusal",
                            "layer": "UNIT",
                            "status": "planned",
                        },
                    ],
                }
            ],
            "coverage": {"covered": 1, "total": 1},
        },
    }
    for name, document in documents.items():
        (state_dir / f"{name}.json").write_text(json.dumps(document))
    (state_dir / "decomposition.json").write_text(
        json.dumps(
            {
                "children": [
                    {
                        "id": f"{epic}.1",
                        "title": "Implement authority",
                        "type": "task",
                        "deps": [],
                        "ready": True,
                    }
                ],
                "swarm_validate": {"ok": True, "output": "PASS"},
                "victor_summary": "fresh",
            }
        )
    )


NOW = datetime(2026, 7, 28, 13, 0, tzinfo=timezone.utc)


def test_quick_receipt_encodes_one_approval_without_full_dossier(
    tmp_path, monkeypatch
):
    state = tmp_path / "mode-state.json"
    _planning_state(state, tier="quick")
    bead = "SABLE-quick1"
    runner = FakeBd({bead: _record(bead)}, ready={bead})

    receipt = handoff.approve_handoff(
        state,
        approved_by="operator@example.com",
        beads=(bead,),
        runner=runner,
        now=NOW,
    )

    assert receipt["tier"] == "quick"
    assert receipt["scope"] == [bead]
    assert receipt["approved_by"] == "operator@example.com"
    assert handoff.read_mode_state(state)["handoff"] == receipt
    proof = handoff.validate_handoff(state, runner=runner)
    assert proof["kind"] == "approved"
    assert proof["receipt_id"] == receipt["receipt_id"]


def test_receipt_goes_stale_when_approved_bead_contract_changes(tmp_path):
    state = tmp_path / "mode-state.json"
    _planning_state(state, tier="quick")
    bead = "SABLE-quick2"
    runner = FakeBd({bead: _record(bead)}, ready={bead})
    handoff.approve_handoff(
        state, approved_by="human", beads=(bead,), runner=runner, now=NOW
    )
    runner.records[bead]["description"] = "mutated after approval"

    with pytest.raises(handoff.HandoffRefused, match="stale"):
        handoff.validate_handoff(state, runner=runner)


@pytest.mark.parametrize("status", ["open", "in_progress", "blocked", "deferred"])
def test_unresolved_question_in_any_live_status_refuses_approval(
    tmp_path, status
):
    state = tmp_path / "mode-state.json"
    _planning_state(state, tier="quick")
    bead = "SABLE-quick3"
    question = _record("SABLE-question")
    question["status"] = status
    runner = FakeBd(
        {bead: _record(bead)}, ready={bead}, questions=[question]
    )

    with pytest.raises(handoff.HandoffRefused, match="open-question"):
        handoff.approve_handoff(
            state, approved_by="human", beads=(bead,), runner=runner, now=NOW
        )


def test_scope_with_no_ready_front_refuses_as_undrainable(tmp_path):
    state = tmp_path / "mode-state.json"
    _planning_state(state, tier="quick")
    bead = "SABLE-quick4"
    runner = FakeBd({bead: _record(bead)}, ready=set())

    with pytest.raises(handoff.HandoffRefused, match="ready front"):
        handoff.approve_handoff(
            state, approved_by="human", beads=(bead,), runner=runner, now=NOW
        )


def test_full_receipt_requires_decomposition_and_complete_dossier(
    tmp_path, monkeypatch
):
    state = tmp_path / "mode-state.json"
    _planning_state(state, tier="full", substage="architecture")
    epic = "SABLE-epic1"
    child = f"{epic}.1"
    records = {
        epic: _record(epic, issue_type="epic"),
        child: _record(child),
    }
    runner = FakeBd(records, children={epic: [child]}, ready={child})
    monkeypatch.setenv("SABLE_PLANNING_DIR", str(tmp_path / "planning"))

    with pytest.raises(handoff.HandoffRefused, match="decomposition"):
        handoff.approve_handoff(
            state, approved_by="human", epic=epic, runner=runner, now=NOW
        )

    _planning_state(state, tier="full", substage="decomposition")
    with pytest.raises(handoff.HandoffRefused, match="missing"):
        handoff.approve_handoff(
            state, approved_by="human", epic=epic, runner=runner, now=NOW
        )


def test_full_receipt_binds_dossier_and_fresh_swarm_validation(
    tmp_path, monkeypatch
):
    state = tmp_path / "mode-state.json"
    _planning_state(state, tier="full", substage="decomposition")
    epic = "SABLE-epic2"
    child = f"{epic}.1"
    records = {
        epic: _record(epic, issue_type="epic"),
        child: _record(child),
    }
    runner = FakeBd(
        records, children={epic: [child]}, ready={child}, swarm_ok=True
    )
    planning_root = tmp_path / "planning"
    monkeypatch.setenv("SABLE_PLANNING_DIR", str(planning_root))
    _full_dossier(planning_root, epic)

    receipt = handoff.approve_handoff(
        state, approved_by="human", epic=epic, runner=runner, now=NOW
    )
    assert receipt["tier"] == "full"
    assert receipt["epic"] == epic
    assert sorted(receipt["artifacts"]) == [
        "architecture.json",
        "decomposition.json",
        "framing.json",
        "research.json",
        "test-strategy.json",
    ]

    runner.swarm_ok = False
    with pytest.raises(handoff.HandoffRefused, match="swarm validate"):
        handoff.validate_handoff(state, runner=runner)


@pytest.mark.parametrize(
    ("artifact", "mutation", "message"),
    [
        (
            "architecture",
            lambda value: value.update(status="needs-follow-up"),
            "status must be ready",
        ),
        (
            "research",
            lambda value: value["findings"][0].update(derisk_status="open"),
            "de-risk",
        ),
        (
            "decomposition",
            lambda value: value["swarm_validate"].update(ok=False),
            "swarm_validate",
        ),
    ],
)
def test_full_receipt_refuses_incomplete_dossier_contract(
    tmp_path, monkeypatch, artifact, mutation, message
):
    state = tmp_path / "mode-state.json"
    _planning_state(state, tier="full", substage="decomposition")
    epic = "SABLE-epic-invalid"
    child = f"{epic}.1"
    runner = FakeBd(
        {
            epic: _record(epic, issue_type="epic"),
            child: _record(child),
        },
        children={epic: [child]},
        ready={child},
    )
    planning_root = tmp_path / "planning"
    monkeypatch.setenv("SABLE_PLANNING_DIR", str(planning_root))
    _full_dossier(planning_root, epic)
    artifact_path = planning_root / epic / f"{artifact}.json"
    document = json.loads(artifact_path.read_text())
    mutation(document)
    artifact_path.write_text(json.dumps(document))

    with pytest.raises(handoff.HandoffRefused, match=message):
        handoff.approve_handoff(
            state, approved_by="human", epic=epic, runner=runner, now=NOW
        )


def test_stale_receipt_cannot_cross_atomic_execution_transition(tmp_path):
    state = tmp_path / "mode-state.json"
    _planning_state(state, tier="quick")
    bead = "SABLE-stale-transition"
    runner = FakeBd({bead: _record(bead)}, ready={bead})
    handoff.approve_handoff(
        state, approved_by="human", beads=(bead,), runner=runner, now=NOW
    )
    runner.records[bead]["description"] = "changed after final approval"
    before = state.read_bytes()

    with pytest.raises(store.ModeTransitionRefused, match="stale"):
        store.set_mode_state(
            state,
            "execution",
            handoff_validator=lambda locked: handoff.validate_handoff_state(
                state, locked, runner=runner
            ),
        )

    assert state.read_bytes() == before


def test_planning_substage_change_invalidates_prior_receipt(tmp_path):
    state = tmp_path / "mode-state.json"
    _planning_state(state, tier="quick")
    bead = "SABLE-invalidated"
    runner = FakeBd({bead: _record(bead)}, ready={bead})
    handoff.approve_handoff(
        state, approved_by="human", beads=(bead,), runner=runner, now=NOW
    )

    store.set_planning_substage(state, "research")

    assert "handoff" not in store.read_mode_state(state)
    with pytest.raises(handoff.HandoffRefused, match="no approved"):
        handoff.validate_handoff(state, runner=runner)


def test_break_glass_is_named_and_durable():
    proof = handoff.break_glass_authority(
        approved_by="operator@example.com",
        reason="recover a pre-receipt execution session",
        now=NOW,
    )
    assert proof["kind"] == "break-glass"
    assert proof["approved_by"] == "operator@example.com"
    assert proof["reason"].startswith("recover")
    assert handoff.validate_execution_authority(
        {"mode": "execution", "handoff": proof}
    ) == proof


def test_execution_authority_rejects_legacy_statusless_state():
    with pytest.raises(handoff.HandoffRefused, match="handoff"):
        handoff.validate_execution_authority({"mode": "execution"})


def test_execution_authority_rejects_tampered_break_glass():
    proof = handoff.break_glass_authority(
        approved_by="operator@example.com",
        reason="recover a pre-receipt execution session",
        now=NOW,
    )
    proof["reason"] = "quietly changed later"

    with pytest.raises(handoff.HandoffRefused, match="digest"):
        handoff.validate_execution_authority(
            {"mode": "execution", "handoff": proof}
        )
