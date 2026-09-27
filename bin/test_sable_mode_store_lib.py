"""Adversarial contracts for the atomic orchestration mode store."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
from pathlib import Path

import pytest

import sable_mode_store_lib as store


def _break_glass() -> dict:
    proof = {
        "version": 1,
        "kind": "break-glass",
        "approved_by": "test-operator",
        "approved_at": "2026-07-28T00:00:00+00:00",
        "reason": "synthetic authority for mode-store isolation",
        "base": {"ref": "HEAD", "sha": "a" * 40},
        "failed_checks": ["test fixture"],
    }
    proof["receipt_id"] = hashlib.sha256(
        json.dumps(
            proof, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()
    return proof


def _advance_substage(path: str, start) -> None:
    start.wait()
    store.advance_planning_substage(Path(path))


def _set_execution_provider(path: str, worker: str, start, outcomes) -> None:
    providers = {
        "optimus": "claude",
        "tarzan": "claude",
        "chuck": "claude",
        "worker": worker,
    }
    start.wait()
    try:
        store.set_mode_state(
            Path(path), "execution", providers=providers,
            since="2026-07-28T00:00:00+0000",
            handoff=_break_glass(),
        )
    except store.ModeTransitionRefused:
        outcomes.put("refused")
    else:
        outcomes.put("written")


def test_missing_and_corrupt_state_are_distinct(tmp_path):
    path = tmp_path / "mode-state.json"
    with pytest.raises(store.ModeStateMissing):
        store.read_mode_state(path)

    path.write_text("{broken json", encoding="utf-8")
    with pytest.raises(store.ModeStateCorrupt) as exc:
        store.read_mode_state(path)
    assert str(path) in str(exc.value)


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        "{}",
        '{"mode":"bogus"}',
        '{"mode":"execution","providers":{"worker":"unsupported"}}',
    ],
)
def test_valid_json_with_invalid_schema_is_corrupt(tmp_path, payload):
    path = tmp_path / "mode-state.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(store.ModeStateCorrupt):
        store.read_mode_state(path)


def test_replace_failure_preserves_the_last_valid_state(tmp_path, monkeypatch):
    path = tmp_path / "mode-state.json"
    store.set_mode_state(
        path, "planning", fleet=("sherlock",),
        since="2026-07-28T00:00:00+0000",
    )
    before = path.read_bytes()

    def fail_replace(*_args):
        raise OSError("planted replace failure")

    monkeypatch.setattr(store.os, "replace", fail_replace)
    with pytest.raises(OSError, match="planted"):
        store.set_mode_state(
            path, "planning", fleet=("victor",),
            since="2026-07-28T00:00:01+0000",
        )

    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".mode-state.json.*.tmp"))


def test_concurrent_read_modify_write_transitions_lose_no_updates(tmp_path):
    path = tmp_path / "mode-state.json"
    store.set_mode_state(
        path, "planning", since="2026-07-28T00:00:00+0000",
    )

    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()
    workers = [
        ctx.Process(target=_advance_substage, args=(str(path), start))
        for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0

    assert store.read_mode_state(path)["substage"] == "decomposition"


def test_provider_guard_and_write_share_one_lock(tmp_path):
    """Two conflicting first writers cannot both authorize against absence."""
    path = tmp_path / "mode-state.json"
    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()
    outcomes = ctx.Queue()
    workers = [
        ctx.Process(
            target=_set_execution_provider,
            args=(str(path), provider, start, outcomes),
        )
        for provider in ("claude", "codex")
    ]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0

    assert sorted(outcomes.get(timeout=2) for _ in workers) == [
        "refused", "written"]
    assert store.read_mode_state(path)["providers"]["worker"] in {
        "claude", "codex"}


def test_execution_without_authority_refuses_and_preserves_absence(tmp_path):
    path = tmp_path / "mode-state.json"

    with pytest.raises(store.ModeTransitionRefused, match="handoff proof"):
        store.set_mode_state(path, "execution")

    assert not path.exists()


def test_execution_without_authority_preserves_planning_bytes(tmp_path):
    path = tmp_path / "mode-state.json"
    store.set_mode_state(
        path, "planning", since="2026-07-28T00:00:00+0000"
    )
    before = path.read_bytes()

    with pytest.raises(store.ModeTransitionRefused, match="handoff proof"):
        store.set_mode_state(path, "execution")

    assert path.read_bytes() == before


def test_atomic_write_publishes_only_complete_json(tmp_path):
    path = tmp_path / "mode-state.json"
    store.set_mode_state(
        path, "planning", fleet=("initial",),
        since="2026-07-28T00:00:00+0000",
    )
    stop = multiprocessing.get_context("fork").Event()

    def writer() -> None:
        for index in range(100):
            store.set_mode_state(
                path, "planning", fleet=(str(index),),
                since=f"2026-07-28T00:00:{index % 60:02d}+0000",
            )
        stop.set()

    ctx = multiprocessing.get_context("fork")
    process = ctx.Process(target=writer)
    process.start()
    observations = 0
    while not stop.is_set():
        state = store.read_mode_state(path)
        observations += 1
        assert state["mode"] == "planning"
        assert len(state["fleet"]) == 1
    process.join(10)
    assert process.exitcode == 0
    assert observations > 0
