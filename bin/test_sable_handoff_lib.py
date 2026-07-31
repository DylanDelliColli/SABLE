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
            "prerequisites": [],
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


# --- the plan's own prerequisites (SABLE-slip0.6) -----------------------------
#
# Both polarities are load-bearing here.  The gate previously hashed
# framing.json without reading it, so the wedge contract was satisfied by the
# single character "x"; the fix has to catch y4nom's real failure WITHOUT
# reproducing the false refusal that prose extraction measured on tz7h.


class WatchedMapping(dict):
    """A mapping that records which keys were read.

    The point of the structured field is that the checker reads ONE key.  A
    behavioural assertion alone cannot prove that — a checker could scan
    ``wedge`` and merely happen to agree on the fixtures — so this records the
    access set directly.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads: set = set()

    def get(self, key, default=None):
        self.reads.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.reads.add(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.reads.add(key)
        return super().__contains__(key)


# y4nom's real shape: the framing named SABLE-9qqrv as prerequisite number one
# and the decomposition depended on none of it (measured 2026-07-30 — the six
# children's deps were only [], [SABLE-79zjm], [SABLE-5lli.5], [SABLE-y4nom.2]).
Y4NOM_WEDGE = (
    "The three prerequisites, in order: SABLE-9qqrv (workers can write the "
    "tracker), then SABLE-1el7e (delivery completes without a human pump), "
    "then SABLE-1dmfc/SABLE-1urtf. Make the fleet able to work at all BEFORE "
    "making the pool smaller."
)

# tz7h's real shape: SABLE-s0c3 (measured OPEN) is named in the wedge as a
# CARRIER and a sequencing note, not a hard blocker.  A naive prose rule
# refuses this, and that refusal would be wrong.
TZ7H_WEDGE = (
    "Producer panes ride the SABLE-s0c3 carrier; s0c3 is the sequencing note, "
    "not a blocker for this epic."
)


def _framing(prerequisites, *, wedge="durable transition proof", **extra):
    document = {
        "stories": [{"id": "S1", "title": "a worker can record its own work"}],
        "success_metric": "approved work is the only work that starts",
        "wedge": wedge,
        "prerequisites": prerequisites,
    }
    document.update(extra)
    return document


def _decomposition(dep_lists):
    return {
        "children": [
            {
                "id": f"SABLE-y4nom.{index + 1}",
                "title": f"child {index + 1}",
                "type": "task",
                "deps": list(deps),
                "ready": True,
            }
            for index, deps in enumerate(dep_lists)
        ],
        "swarm_validate": {"ok": True, "output": "PASS"},
        "victor_summary": "fresh",
    }


Y4NOM_DEPS = ([], ["SABLE-79zjm"], [], [], ["SABLE-5lli.5"], ["SABLE-y4nom.2"])


def _resolver(statuses):
    """A bd-backed resolver: id -> status, or None when bd cannot resolve it."""

    return lambda issue_id: statuses.get(issue_id)


OPEN_WORLD = _resolver(
    {
        "SABLE-9qqrv": "open",
        "SABLE-1el7e": "open",
        "SABLE-s0c3": "open",
        "SABLE-79zjm": "open",
        "SABLE-5lli.5": "open",
        "SABLE-y4nom.2": "open",
        "SABLE-y4nom.6": "open",
    }
)


def test_declared_prerequisite_missing_from_dep_closure_is_refused():
    """TRUE POSITIVE — y4nom's real shape, the failure that stood a wave down."""

    verdict = handoff.prerequisite_verdict(
        _framing(["SABLE-9qqrv", "SABLE-1el7e"], wedge=Y4NOM_WEDGE),
        _decomposition(Y4NOM_DEPS),
        resolve=OPEN_WORLD,
    )

    assert verdict["status"] == handoff.PREREQUISITE_REFUSE
    assert verdict["missing"] == ["SABLE-1el7e", "SABLE-9qqrv"]
    assert "SABLE-9qqrv" in verdict["reason"]


def test_declared_prerequisite_present_in_dep_closure_passes():
    verdict = handoff.prerequisite_verdict(
        _framing(["SABLE-9qqrv"], wedge=Y4NOM_WEDGE),
        _decomposition(([], ["SABLE-9qqrv"], ["SABLE-y4nom.2"])),
        resolve=_resolver(
            {
                "SABLE-9qqrv": "open",
                "SABLE-y4nom.2": "open",
            }
        ),
    )

    assert verdict["status"] == handoff.PREREQUISITE_PASS
    assert verdict["missing"] == []


def test_prose_mention_with_an_empty_declaration_is_not_a_refusal():
    """FALSE-REFUSAL GUARD — tz7h's real shape.

    An empty array is a real assertion ("this epic declares no prerequisite"),
    not an absent one, so it must PASS even though a bead id appears in the
    prose and is nowhere in the dep graph.
    """

    verdict = handoff.prerequisite_verdict(
        _framing(
            [],
            wedge=TZ7H_WEDGE,
            non_goals=["not SABLE-s0c3's own delivery"],
        ),
        _decomposition(([], ["SABLE-y4nom.2"])),
        resolve=OPEN_WORLD,
    )

    assert verdict["status"] == handoff.PREREQUISITE_PASS
    assert verdict["declared"] == []
    assert verdict["missing"] == []


def test_checker_reads_only_the_prerequisites_field():
    """FORBID THE FREE SCAN.

    A scan over wedge/non_goals/stories is strictly noisier — measured, ids
    appear in non_goals on 5 of 6 real epics and in stories on 6 of 6.
    """

    framing = WatchedMapping(
        _framing(
            [],
            wedge=Y4NOM_WEDGE,
            non_goals=["SABLE-9qqrv stays out of scope"],
        )
    )

    verdict = handoff.prerequisite_verdict(
        framing, _decomposition(Y4NOM_DEPS), resolve=OPEN_WORLD
    )

    assert verdict["status"] == handoff.PREREQUISITE_PASS
    assert framing.reads == {"prerequisites"}


def test_prose_in_a_prerequisite_entry_is_refused_and_never_harvested():
    """The ANCHORED form ported from shell-run-set.sh: a mention is not an id.

    The entry is malformed, so it refuses — it does NOT quietly extract
    SABLE-9qqrv from the sentence and check that instead.
    """

    verdict = handoff.prerequisite_verdict(
        _framing(["see SABLE-9qqrv first"]),
        _decomposition(([], ["SABLE-9qqrv"])),
        resolve=OPEN_WORLD,
    )

    assert verdict["status"] == handoff.PREREQUISITE_REFUSE
    assert verdict["malformed"] == ["see SABLE-9qqrv first"]
    assert verdict["declared"] == []


def test_two_ids_in_one_entry_is_refused_so_an_entry_states_one_rule():
    verdict = handoff.prerequisite_verdict(
        _framing(["SABLE-9qqrv SABLE-1el7e"]),
        _decomposition(([], ["SABLE-9qqrv"], ["SABLE-1el7e"])),
        resolve=OPEN_WORLD,
    )

    assert verdict["status"] == handoff.PREREQUISITE_REFUSE
    assert verdict["malformed"] == ["SABLE-9qqrv SABLE-1el7e"]


def test_decomposition_dep_of_a_nonexistent_bead_does_not_silently_pass():
    """The closure is only as sound as the dep ids it is built from.

    ``children[].deps`` was validated only as ``isinstance(list)``, so a typo
    made the closure silently wrong and this check silently PASSED.
    """

    verdict = handoff.prerequisite_verdict(
        _framing(["SABLE-9qqrv"]),
        _decomposition(([], ["SABLE-9qqrv"], ["SABLE-typo0"])),
        resolve=OPEN_WORLD,
    )

    assert verdict["status"] == handoff.PREREQUISITE_REFUSE
    assert verdict["unresolvable"] == ["SABLE-typo0"]


def test_malformed_dep_id_is_refused_by_the_bd_free_shape_half():
    verdict = handoff.prerequisite_verdict(
        _framing([]),
        _decomposition(([], ["blocked on SABLE-9qqrv"])),
        resolve=None,
    )

    assert verdict["status"] == handoff.PREREQUISITE_REFUSE
    assert verdict["malformed"] == ["blocked on SABLE-9qqrv"]


def test_unresolvable_prerequisite_is_refused_as_an_unfalsifiable_claim():
    verdict = handoff.prerequisite_verdict(
        _framing(["SABLE-ghost"]),
        _decomposition(([], ["SABLE-ghost"])),
        resolve=OPEN_WORLD,
    )

    assert verdict["status"] == handoff.PREREQUISITE_REFUSE
    assert verdict["unresolvable"] == ["SABLE-ghost"]


def test_closed_prerequisite_is_reported_stale_rather_than_refused():
    """A claim that outlived its cause is reported, not refused.

    The prerequisite landed — refusing here would be the false-refusal arm
    this bead exists to avoid — but it stays visible so the declaration can be
    retired deliberately.
    """

    verdict = handoff.prerequisite_verdict(
        _framing(["SABLE-9qqrv"]),
        _decomposition(([], ["SABLE-9qqrv"])),
        resolve=_resolver({"SABLE-9qqrv": "closed"}),
    )

    assert verdict["status"] == handoff.PREREQUISITE_PASS
    assert verdict["stale"] == ["SABLE-9qqrv"]


def test_resolution_unavailable_is_uncheckable_never_pass():
    """D8: never render uncheckable as clean."""

    verdict = handoff.prerequisite_verdict(
        _framing(["SABLE-9qqrv"]),
        _decomposition(([], ["SABLE-9qqrv"])),
        resolve=None,
    )

    assert verdict["status"] == handoff.PREREQUISITE_UNCHECKABLE
    assert verdict["status"] != handoff.PREREQUISITE_PASS


def test_mechanically_unambiguous_refusal_does_not_need_bd():
    """The SHAPE and CLOSURE halves are bd-free, so they still refuse."""

    verdict = handoff.prerequisite_verdict(
        _framing(["SABLE-9qqrv"], wedge=Y4NOM_WEDGE),
        _decomposition(Y4NOM_DEPS),
        resolve=None,
    )

    assert verdict["status"] == handoff.PREREQUISITE_REFUSE
    assert "SABLE-9qqrv" in verdict["reason"]


def test_absent_prerequisites_field_is_schema_invalid_not_skipped():
    """NEGATIVE CONTROL: absence must refuse, not silently skip the check."""

    verdict = handoff.prerequisite_verdict(
        {"stories": [], "wedge": Y4NOM_WEDGE},
        _decomposition(Y4NOM_DEPS),
        resolve=OPEN_WORLD,
    )

    assert verdict["status"] == handoff.PREREQUISITE_REFUSE
    assert "prerequisites" in verdict["reason"]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (handoff.PREREQUISITE_PASS, handoff.PREREQUISITE_UNCHECKABLE),
        (handoff.PREREQUISITE_PASS, handoff.PREREQUISITE_REFUSE),
        (handoff.PREREQUISITE_UNCHECKABLE, handoff.PREREQUISITE_REFUSE),
    ],
)
def test_every_verdict_renders_differently_from_every_other(first, second):
    """Three distinct enum values prove nothing if one renderer flattens them."""

    rendered_first = handoff.render_prerequisite_verdict({"status": first})
    rendered_second = handoff.render_prerequisite_verdict({"status": second})

    assert rendered_first != rendered_second
    assert first in rendered_first
    assert second in rendered_second


def test_uncheckable_never_renders_as_clean():
    rendered = handoff.render_prerequisite_verdict(
        {"status": handoff.PREREQUISITE_UNCHECKABLE, "reason": "bd unavailable"}
    )

    assert handoff.PREREQUISITE_UNCHECKABLE in rendered
    assert "clean" not in rendered.lower()
    assert handoff.PREREQUISITE_PASS not in rendered


def test_receipt_without_a_verdict_reads_back_uncheckable_not_pass():
    """A legacy receipt asserts nothing about prerequisites, so it is not clean."""

    carried = handoff.carried_prerequisite_verdict(
        {"version": 1, "kind": "approved"}
    )

    assert carried["status"] == handoff.PREREQUISITE_UNCHECKABLE


@pytest.mark.parametrize(
    "tampered",
    [None, "PASS", {"status": "clean"}, {"status": "pass"}, {}],
)
def test_unrecognized_carried_verdict_degrades_to_uncheckable(tampered):
    carried = handoff.carried_prerequisite_verdict(
        {"version": 1, "kind": "approved", "prerequisites": tampered}
    )

    assert carried["status"] == handoff.PREREQUISITE_UNCHECKABLE


def _full_gate(tmp_path, monkeypatch, epic, *, framing=None, deps=None):
    """Stand the real full-tier gate up over a real on-disk dossier."""

    state = tmp_path / "mode-state.json"
    _planning_state(state, tier="full", substage="decomposition")
    child = f"{epic}.1"
    records = {
        epic: _record(epic, issue_type="epic"),
        child: _record(child),
    }
    runner = FakeBd(records, children={epic: [child]}, ready={child})
    planning_root = tmp_path / "planning"
    monkeypatch.setenv("SABLE_PLANNING_DIR", str(planning_root))
    _full_dossier(planning_root, epic)
    if framing is not None:
        (planning_root / epic / "framing.json").write_text(json.dumps(framing))
    if deps is not None:
        path = planning_root / epic / "decomposition.json"
        document = json.loads(path.read_text())
        document["children"][0]["deps"] = list(deps)
        path.write_text(json.dumps(document))
    return state, runner


def test_full_gate_refuses_when_the_scope_skips_a_declared_prerequisite(
    tmp_path, monkeypatch
):
    epic = "SABLE-y4shape"
    state, runner = _full_gate(
        tmp_path,
        monkeypatch,
        epic,
        framing=_framing(["SABLE-9qqrv"], wedge=Y4NOM_WEDGE),
        deps=["SABLE-79zjm"],
    )
    runner.records["SABLE-9qqrv"] = _record("SABLE-9qqrv")
    runner.records["SABLE-79zjm"] = _record("SABLE-79zjm")

    with pytest.raises(handoff.HandoffRefused, match="SABLE-9qqrv"):
        handoff.approve_handoff(
            state, approved_by="human", epic=epic, runner=runner, now=NOW
        )


def test_full_gate_refuses_framing_without_the_prerequisites_field(
    tmp_path, monkeypatch
):
    """NEGATIVE CONTROL at the real gate: schema-invalid, not silently skipped."""

    epic = "SABLE-nofield"
    framing = _framing([], wedge=Y4NOM_WEDGE)
    framing.pop("prerequisites")
    state, runner = _full_gate(tmp_path, monkeypatch, epic, framing=framing)

    with pytest.raises(handoff.HandoffRefused, match="prerequisites"):
        handoff.approve_handoff(
            state, approved_by="human", epic=epic, runner=runner, now=NOW
        )


def test_full_receipt_body_carries_the_prerequisite_verdict(
    tmp_path, monkeypatch
):
    epic = "SABLE-carries"
    state, runner = _full_gate(
        tmp_path,
        monkeypatch,
        epic,
        framing=_framing(["SABLE-9qqrv"], wedge=Y4NOM_WEDGE),
        deps=["SABLE-9qqrv"],
    )
    runner.records["SABLE-9qqrv"] = _record("SABLE-9qqrv")

    receipt = handoff.approve_handoff(
        state, approved_by="human", epic=epic, runner=runner, now=NOW
    )

    assert receipt["prerequisites"]["status"] == handoff.PREREQUISITE_PASS
    assert receipt["prerequisites"]["declared"] == ["SABLE-9qqrv"]
    assert (
        handoff.carried_prerequisite_verdict(receipt)["status"]
        == handoff.PREREQUISITE_PASS
    )


def test_quick_receipt_records_uncheckable_rather_than_a_clean_result(tmp_path):
    """Quick tier has no framing artifact, so it asserts nothing here."""

    state = tmp_path / "mode-state.json"
    _planning_state(state, tier="quick")
    bead = "SABLE-quick-prereq"
    runner = FakeBd({bead: _record(bead)}, ready={bead})

    receipt = handoff.approve_handoff(
        state, approved_by="human", beads=(bead,), runner=runner, now=NOW
    )

    assert (
        receipt["prerequisites"]["status"] == handoff.PREREQUISITE_UNCHECKABLE
    )
