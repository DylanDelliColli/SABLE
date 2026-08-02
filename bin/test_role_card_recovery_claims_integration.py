#!/usr/bin/env python3
"""Store-backed recovery-claim checker for SABLE-slip0.5 / locked D11.

The role cards now tell every restarting manager to recompute with
``sable-recover``; they do not promise that fleet topology lives in beads.
This module verifies the underlying store claim rather than trusting prose:

* the live in-progress pool is read with an unlimited, read-only census and
  carries none of the topology keys the cards explicitly decline to promise;
* a hermetic real-bd store plants both a dispatch key (``branch``) and the
  exact topology class (``lane``), proving the census can find what it says is
  absent rather than passing on an empty or broken scanner; and
* Chuck's narrower true claim is exercised through the documented real
  ``bd update --set-metadata`` hold path, then read through both ``bd show``
  and the production branch-metadata resolver.

All writes are confined to ``tmp_path``.  The live store leg is list-only.
The clean-room CI intentionally has no bd/dolt, so this real-bd module reports
an explicit skip there; the always-running prose guard lives separately in
``test_role_card_invariants.py``.
"""

from __future__ import annotations

import json
import os
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest


BIN_DIR = Path(__file__).resolve().parent
REPO = BIN_DIR.parent
RECONCILER = BIN_DIR / "sable-reconcile-handoffs"
HAVE_BD = shutil.which("bd") is not None

pytestmark = pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd/dolt; real-store recovery checker self-skips",
)

TOPOLOGY_KEYS = frozenset(
    {"worktree", "pane", "window", "lane", "session", "dispatch"}
)
DISPATCH_KEYS = frozenset({"branch"})


def _checked(argv, *, cwd: Path, env: dict[str, str]) -> str:
    cp = subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
    )
    assert cp.returncode == 0, (
        f"command failed rc={cp.returncode}: {argv!r}\n{cp.stdout}"
    )
    return cp.stdout


def _bd_env(*, home: Path | None = None, store: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("BEADS_DB", "BEADS_DIR", "SABLE_RC_BD"):
        env.pop(key, None)
    if home is not None:
        env["HOME"] = str(home)
    if store is not None:
        env["BEADS_DIR"] = str(store)
    env["BD_NON_INTERACTIVE"] = "1"
    env["CI"] = "true"
    return env


def _json_records(raw: str) -> list[dict]:
    payload = json.loads(raw)
    if isinstance(payload, dict):
        payload = [payload]
    assert isinstance(payload, list), f"bd returned non-record JSON: {payload!r}"
    assert all(isinstance(row, dict) for row in payload), payload
    return payload


def _metadata_hits(records: list[dict], keys: frozenset[str]) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    for record in records:
        metadata = record.get("metadata") or {}
        assert isinstance(metadata, dict), (
            f"bead {record.get('id')} has non-object metadata: {metadata!r}"
        )
        for key in keys:
            if metadata.get(key) not in (None, ""):
                hits.setdefault(key, []).append(str(record.get("id")))
    return {key: sorted(ids) for key, ids in sorted(hits.items())}


def _init_store(root: Path) -> tuple[Path, Path, dict[str, str], str]:
    repo = root / "repo"
    home = root / "home"
    repo.mkdir()
    home.mkdir()
    base_env = _bd_env(home=home)
    _checked(["git", "init", "-b", "main", str(repo)], cwd=root, env=base_env)

    store = repo / ".beads"
    last_output = ""
    for _ in range(4):
        if store.exists():
            shutil.rmtree(store)
        cp = subprocess.run(
            [
                "bd",
                "init",
                "--prefix=RCR",
                "--non-interactive",
                "--skip-agents",
                "--skip-hooks",
                "--quiet",
            ],
            cwd=str(repo),
            env=base_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
        )
        last_output = cp.stdout
        if cp.returncode == 0 and (store / "config.yaml").is_file():
            break
    else:
        raise AssertionError(f"bd init never produced a complete store:\n{last_output}")

    env = _bd_env(home=home, store=store)
    created = _json_records(
        _checked(
            [
                "bd",
                "create",
                "--sandbox",
                "--json",
                "--title=Recovery-claim checker seed",
                "--description=Hermetic store evidence for SABLE-slip0.5.",
                "--type=task",
                "--priority=4",
            ],
            cwd=repo,
            env=env,
        )
    )
    assert len(created) == 1 and created[0].get("id"), created
    bead_id = str(created[0]["id"])
    _checked(
        [
            "bd",
            "update",
            bead_id,
            "--sandbox",
            "--status=in_progress",
            "--set-metadata",
            "branch=wk-seed",
            "--set-metadata",
            "lane=optimus",
            "--set-metadata",
            "hold=awaiting exact preview",
            "--set-metadata",
            "hold_by=chuck",
            "--set-metadata",
            "hold_since=2026-08-02T00:00:00Z",
            "--set-metadata",
            "hold_until=the exact preview is green",
        ],
        cwd=repo,
        env=env,
    )
    return repo, store, env, bead_id


@pytest.fixture(scope="module")
def seeded_store(tmp_path_factory):
    return _init_store(tmp_path_factory.mktemp("role-card-recovery-claims"))


def test_live_in_progress_pool_has_no_bead_backed_topology_state():
    """The live leg is deliberately read-only and never assumes zero metadata."""
    rows = _json_records(
        _checked(
            ["bd", "list", "--status", "in_progress", "--json", "--limit", "0"],
            cwd=REPO,
            env=_bd_env(),
        )
    )
    assert _metadata_hits(rows, TOPOLOGY_KEYS) == {}, (
        "live beads now carry topology state; the recovery-card claim and "
        "D11 contract must be revisited rather than silently drifting"
    )


def test_seeded_store_proves_census_detects_dispatch_and_topology_keys(seeded_store):
    repo, _store, env, bead_id = seeded_store
    rows = _json_records(
        _checked(
            ["bd", "list", "--status", "in_progress", "--json", "--limit", "0"],
            cwd=repo,
            env=env,
        )
    )
    assert _metadata_hits(rows, TOPOLOGY_KEYS | DISPATCH_KEYS) == {
        "branch": [bead_id],
        "lane": [bead_id],
    }, "the positive-control store did not exercise the census's exact key class"


def test_chuck_hold_round_trip_uses_durable_metadata_and_production_reader(
    seeded_store, monkeypatch
):
    repo, store, env, bead_id = seeded_store
    shown = _json_records(
        _checked(["bd", "show", bead_id, "--json"], cwd=repo, env=env)
    )
    assert len(shown) == 1, shown
    metadata = shown[0].get("metadata") or {}
    assert {
        key: metadata.get(key)
        for key in ("hold", "hold_by", "hold_since", "hold_until")
    } == {
        "hold": "awaiting exact preview",
        "hold_by": "chuck",
        "hold_since": "2026-08-02T00:00:00Z",
        "hold_until": "the exact preview is green",
    }

    # Pin the primary resolver's real query shape separately: the branch name
    # contains no bead id and the title/description do not mention it, so a
    # successful production read below cannot be laundered through either
    # fallback resolver.
    joined = _json_records(
        _checked(
            [
                "bd",
                "list",
                "--metadata-field",
                "branch=wk-seed",
                "--status",
                "all",
                "--json",
                "--limit",
                "0",
            ],
            cwd=repo,
            env=env,
        )
    )
    assert [row.get("id") for row in joined] == [bead_id], joined

    monkeypatch.delenv("BEADS_DB", raising=False)
    monkeypatch.delenv("SABLE_RC_BD", raising=False)
    monkeypatch.setenv("BEADS_DIR", str(store))
    monkeypatch.setenv("HOME", env["HOME"])
    namespace = runpy.run_path(
        str(RECONCILER), run_name="sable_reconcile_handoffs_role_claims"
    )
    assert namespace["hold_from_bead"](shown[0]) == {
        "bead": bead_id,
        "reason": "awaiting exact preview",
        "by": "chuck",
        "since": "2026-08-02T00:00:00Z",
        "until": "the exact preview is green",
    }
    assert namespace["find_work_bead_hold"](str(repo), "wk-seed") == {
        "bead": bead_id,
        "reason": "awaiting exact preview",
        "by": "chuck",
        "since": "2026-08-02T00:00:00Z",
        "until": "the exact preview is green",
    }
