#!/usr/bin/env python3
"""Integration tests for bin/sable-docker-preflight (SABLE-n5rb).

Real composition: the actual `sable-docker-preflight` binary run as a
subprocess against the real docker engine — real `docker ps`/`docker
inspect`, a real `--pid=host --cgroupns=host` probe container, real cgroup
filesystem reads, and (where a Supabase db container is up) a real `docker
exec` + real TCP connection to postgres. No mocked docker, no synthetic
filesystem — this proves the tool against the exact environment it exists to
protect (the 9scm ghost-container class of desync), including the noise
sources (foreign-daemon cgroups, transient buildkit/snapshotter churn, the
probe's own container) discovered live while building it.

Two tiers, gated independently:
  - HAVE_DOCKER: any test that only needs a working docker engine (spins up
    its own throwaway containers — portable to a bare CI runner).
  - HAVE_SUPABASE_DB: the full end-to-end run against a live supabase_db_*
    container (check c's real exec-vs-TCP comparison) — only present on a
    machine with the dev stack up; skips honestly elsewhere.
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import uuid
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PREFLIGHT = REPO / "bin" / "sable-docker-preflight"

_LOADER = SourceFileLoader("sable_docker_preflight_integration_target", str(PREFLIGHT))
_SPEC = importlib.util.spec_from_loader("sable_docker_preflight_integration_target", _LOADER)
preflight = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(preflight)


def _docker_daemon_reachable():
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


HAVE_DOCKER = _docker_daemon_reachable()
pytestmark = pytest.mark.skipif(not HAVE_DOCKER, reason="docker engine not reachable in this environment")


def _live_supabase_db_container():
    if not HAVE_DOCKER:
        return None
    r = subprocess.run(
        ["docker", "ps", "--filter", "name=supabase_db_", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=10,
    )
    names = [n.strip() for n in r.stdout.splitlines() if n.strip()]
    return names[0] if names else None


HAVE_SUPABASE_DB = _live_supabase_db_container() is not None


def run_preflight(*extra_args, timeout=60):
    return subprocess.run(
        [sys.executable, str(PREFLIGHT), *extra_args],
        capture_output=True, text=True, timeout=timeout,
    )


# --- own-container plumbing: real docker, no dependency on the dev stack ---

@pytest.fixture()
def throwaway_container():
    name = f"sable-docker-preflight-test-{uuid.uuid4().hex[:10]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "alpine", "sleep", "60"],
                    capture_output=True, text=True, timeout=30, check=True)
    try:
        yield name
    finally:
        subprocess.run(["docker", "stop", "-t", "1", name], capture_output=True, timeout=15)


def test_real_own_container_is_not_flagged_phantom_or_ghost(throwaway_container):
    # Real end-to-end proof of checks (a)/(b)'s plumbing: a container we just
    # started for real must NOT be flagged, using the actual docker
    # inspect/ps + --pid=host/--cgroupns=host cgroup probe — a "true negative"
    # against real infrastructure, not synthetic fixtures.
    result = run_preflight("--skip-pg-check", "--json")
    assert result.returncode in (0, preflight_error_bit()), result.stdout + result.stderr
    payload = json.loads(result.stdout)
    full_id = subprocess.run(
        ["docker", "inspect", throwaway_container, "--format", "{{.Id}}"],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    flagged_ids = {p["id"] for p in payload["phantoms"]} | {g["id"] for g in payload["ghosts"]}
    assert full_id not in flagged_ids


def preflight_error_bit():
    return 8  # EXIT_ERROR — tolerated here since this assertion only cares about our own container


# --- phantom project-scoping (SABLE-h5czc) ----------------------------------
#
# The observed live failure: a full `pytest bin/` run REDed a doc-only branch
# on test_healthy_dev_stack_is_clean_end_to_end because container
# 'elastic_buck' — host noise unrelated to any SABLE-managed stack, left by
# something else entirely — was flagged as a phantom. "there are no phantoms
# on this host" is the wrong assertion for a check about ONE specific dev
# stack; the fix scopes check (a) to the target stack's
# `com.docker.compose.project` label. These tests exercise that scoping
# against REAL `docker inspect` data (not fully synthetic) — only the
# liveness signal is forced into phantom shape, since genuinely producing a
# real dockerd/containerd desync would mean killing the container's shim out
# from under dockerd, unsafe to do to a real host in an automated test.

def test_foreign_phantom_scoped_away_but_own_stacks_phantom_still_caught(throwaway_container):
    real_record = json.loads(subprocess.run(
        ["docker", "inspect", throwaway_container],
        capture_output=True, text=True, timeout=10, check=True,
    ).stdout)[0]

    def _phantom_shaped(labels):
        rec = json.loads(json.dumps(real_record))  # deep copy
        rec["State"]["Running"] = True
        rec["State"]["Pid"] = 999999999  # cannot possibly be a live pid
        rec.setdefault("Config", {})["Labels"] = dict(labels)
        return rec

    foreign = _phantom_shaped({"com.docker.compose.project": "some-other-stack"})
    own = _phantom_shaped({"com.docker.compose.project": "sable-dev-stack"})

    # Regression control: a phantom-shaped record belonging to an unrelated
    # project must not fail a check scoped to a different stack.
    assert preflight.find_phantom_containers(
        [foreign], live_pids=set(), cgroup_pids={}, project="sable-dev-stack"
    ) == []

    # Opposite control: the exact same phantom shape, attributed to the
    # target stack itself, must still be caught — scoping cannot go blind to
    # a real desync of the stack it exists to protect.
    caught = preflight.find_phantom_containers(
        [own], live_pids=set(), cgroup_pids={}, project="sable-dev-stack"
    )
    assert [p["id"] for p in caught] == [own["Id"]]


@pytest.mark.skipif(not HAVE_SUPABASE_DB, reason="no live supabase_db_* container in this environment")
def test_target_project_resolves_the_real_dev_stacks_compose_project():
    # Proves the wiring (not just the pure filtering logic): _target_project
    # must actually resolve the live dev stack's real compose-project label,
    # the same one check (a) then scopes to inside run_checks.
    db_container = _live_supabase_db_container()
    expected = subprocess.run(
        ["docker", "inspect", db_container, "--format",
         '{{index .Config.Labels "com.docker.compose.project"}}'],
        capture_output=True, text=True, timeout=10, check=True,
    ).stdout.strip()
    assert expected, "expected a real compose-project label on the live dev stack container"
    resolved = preflight._target_project(None, skip_pg_check=False)
    assert resolved == expected


def test_probe_never_flags_itself_as_a_ghost():
    # Regression for the self-flagging bug found live while building this:
    # the probe container is itself running for the duration of both cgroup
    # samples. Run several times — the probe spins up a fresh container ID
    # each time, so this would be flaky (not just wrong) if unfixed.
    for _ in range(3):
        result = run_preflight("--skip-pg-check", "--json")
        payload = json.loads(result.stdout)
        assert payload["ghosts"] == [], f"probe flagged itself: {payload}"


def test_missing_explicit_db_container_reports_a_specific_error():
    result = run_preflight("--db-container", "sable-preflight-does-not-exist-xyz", "--json")
    payload = json.loads(result.stdout)
    assert result.returncode & 8  # EXIT_ERROR
    assert payload["clean"] is False
    assert any("sable-preflight-does-not-exist-xyz" in e for e in payload["errors"])


def test_zero_debounce_still_returns_well_formed_report():
    # fast-path smoke test (no --ghost-debounce-seconds wait) — proves the
    # gather/parse plumbing works even without the noise-filtering delay.
    result = run_preflight("--skip-pg-check", "--ghost-debounce-seconds", "0", "--json")
    payload = json.loads(result.stdout)
    assert isinstance(payload["phantoms"], list)
    assert isinstance(payload["ghosts"], list)


# --- full end-to-end against the live Supabase dev stack -------------------

pytestmark_db = pytest.mark.skipif(not HAVE_SUPABASE_DB, reason="no live supabase_db_* container in this environment")


@pytestmark_db
def test_healthy_dev_stack_is_clean_end_to_end():
    # The exact real-world case this tool exists to certify: a healthy,
    # single-postmaster Supabase stack must report clean. Real docker exec,
    # real TCP connection to the published port, real cgroup probe.
    result = run_preflight("--json")
    payload = json.loads(result.stdout)
    assert payload["phantoms"] == [], payload
    assert payload["errors"] == [], payload
    dp = payload["dual_postmaster"]
    assert dp is not None
    assert dp["mismatch"] is False
    assert dp["exec_start_time"] == dp["tcp_start_time"]
    assert result.returncode == 0, result.stdout + result.stderr


@pytestmark_db
def test_healthy_dev_stack_text_output_says_clean():
    result = run_preflight()
    assert result.returncode == 0
    assert "clean" in result.stdout
    assert "STOP THE FLEET" not in result.stdout


@pytestmark_db
def test_dual_postmaster_reading_is_stable_across_runs():
    # pg_postmaster_start_time() must not drift between two independent
    # invocations against the SAME running postmaster (a sanity check on the
    # exec/TCP plumbing itself, independent of the mismatch-detection logic
    # already covered by unit tests).
    first = json.loads(run_preflight("--json").stdout)
    second = json.loads(run_preflight("--json").stdout)
    assert first["dual_postmaster"]["exec_start_time"] == second["dual_postmaster"]["exec_start_time"]
