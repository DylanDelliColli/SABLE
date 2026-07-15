#!/usr/bin/env python3
"""Unit tests for bin/sable-docker-preflight (SABLE-n5rb).

Pure logic against synthetic strings/records — no subprocess, no docker, no
filesystem. Covers: JSON/listing parsing, phantom-container detection (check
a), ghost-cgroup-task detection with the self-exclusion + persistence
debounce (check b), and dual-postmaster comparison (check c). Regression
cases model both the real 2026-07-07 incident (market-brief-package-9scm —
ghost postgres alive, holding a real cgroup and real host ports, invisible to
`docker ps`; dual postmaster on one pgdata volume) and the false-positive
noise sources found live while building this tool (foreign-daemon cgroups
that never resolve a PID in our namespace, and the probe's own container).
"""
import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader(
    "sable_docker_preflight", str(Path(__file__).resolve().parent / "sable-docker-preflight")
)
_SPEC = importlib.util.spec_from_loader("sable_docker_preflight", _LOADER)
preflight = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(preflight)


# --- parse_docker_inspect -------------------------------------------------

def test_parse_docker_inspect_valid_array():
    raw = json.dumps([{"Id": "abc", "State": {"Running": True, "Pid": 100}}])
    records = preflight.parse_docker_inspect(raw)
    assert records == [{"Id": "abc", "State": {"Running": True, "Pid": 100}}]


# --- parse_proc_pid_listing ------------------------------------------------

def test_parse_proc_pid_listing_only_keeps_digits():
    raw = "1\n2\n856\nself\ncpuinfo\n"
    assert preflight.parse_proc_pid_listing(raw) == {1, 2, 856}


def test_parse_proc_pid_listing_empty():
    assert preflight.parse_proc_pid_listing("") == set()


def test_parse_proc_pid_listing_ignores_blank_lines():
    assert preflight.parse_proc_pid_listing("1\n\n\n2\n") == {1, 2}


# --- parse_cgroup_pid_listing -----------------------------------------------

_ID_A = "a" * 64
_ID_B = "b" * 64


def test_parse_cgroup_pid_listing_basic():
    raw = f"{_ID_A} 856\n{_ID_B} 0\n"
    assert preflight.parse_cgroup_pid_listing(raw) == {_ID_A: 856, _ID_B: 0}


def test_parse_cgroup_pid_listing_ignores_non_hex64_names():
    # models the real "buildx" non-container cgroup found under
    # /sys/fs/cgroup/docker/ alongside real container IDs
    raw = f"{_ID_A} 856\nbuildx 0\n"
    assert preflight.parse_cgroup_pid_listing(raw) == {_ID_A: 856}


def test_parse_cgroup_pid_listing_ignores_malformed_lines():
    raw = f"{_ID_A} 856\nnot-two-tokens\n{_ID_B}\n"
    assert preflight.parse_cgroup_pid_listing(raw) == {_ID_A: 856}


def test_parse_cgroup_pid_listing_empty():
    assert preflight.parse_cgroup_pid_listing("") == {}


# --- find_phantom_containers (check a) --------------------------------------

def _rec(id_, running, pid, name="/foo"):
    return {"Id": id_, "Name": name, "State": {"Running": running, "Pid": pid}}


def test_find_phantom_containers_healthy_pid_alive():
    rec = _rec(_ID_A, True, 100)
    phantoms = preflight.find_phantom_containers([rec], live_pids={100}, cgroup_pids={})
    assert phantoms == []


def test_find_phantom_containers_healthy_via_cgroup_only():
    # pid missing from /proc (e.g. stale State.Pid) but a live cgroup still
    # backs the container -> not a phantom (only both-missing is phantom).
    rec = _rec(_ID_A, True, 999)
    phantoms = preflight.find_phantom_containers([rec], live_pids=set(), cgroup_pids={_ID_A: 100})
    assert phantoms == []


def test_find_phantom_containers_not_running_never_flagged():
    # a correctly-stopped container has neither a live pid nor a cgroup —
    # that is expected, not a phantom.
    rec = _rec(_ID_A, False, None)
    phantoms = preflight.find_phantom_containers([rec], live_pids=set(), cgroup_pids={})
    assert phantoms == []


def test_find_phantom_containers_detects_phantom():
    # the 9scm-shaped failure: dockerd's own record claims Running, but
    # neither a live process nor a live cgroup backs it.
    rec = _rec(_ID_A, True, 12345, name="/supabase_db_dev-environment")
    phantoms = preflight.find_phantom_containers([rec], live_pids={1, 2}, cgroup_pids={_ID_A: 0})
    assert len(phantoms) == 1
    assert phantoms[0]["id"] == _ID_A
    assert phantoms[0]["name"] == "supabase_db_dev-environment"
    assert phantoms[0]["pid"] == 12345


def test_find_phantom_containers_ignores_unrelated_running_container():
    healthy = _rec(_ID_A, True, 100)
    phantom = _rec(_ID_B, True, 999)
    phantoms = preflight.find_phantom_containers(
        [healthy, phantom], live_pids={100}, cgroup_pids={_ID_A: 100}
    )
    assert [p["id"] for p in phantoms] == [_ID_B]


# --- find_ghost_tasks (check b) ---------------------------------------------

def test_find_ghost_tasks_none_when_all_tracked():
    s1 = {_ID_A: 100}
    s2 = {_ID_A: 100}
    assert preflight.find_ghost_tasks(s1, s2, docker_ps_ids=[_ID_A]) == []


def test_find_ghost_tasks_detects_persistent_untracked_cgroup():
    # the real 9scm shape: a live cgroup with a real pid in BOTH samples,
    # absent from `docker ps` — containerd kept it running behind dockerd.
    s1 = {_ID_A: 2135}
    s2 = {_ID_A: 2135}
    ghosts = preflight.find_ghost_tasks(s1, s2, docker_ps_ids=[])
    assert ghosts == [{"id": _ID_A, "pid": 2135}]


def test_find_ghost_tasks_ignores_unresolvable_pid_zero():
    # models the foreign-daemon noise found live: a cgroup exists but its
    # process is not resolvable in our own pid namespace (kernel reports 0).
    s1 = {_ID_A: 0}
    s2 = {_ID_A: 0}
    assert preflight.find_ghost_tasks(s1, s2, docker_ps_ids=[]) == []


def test_find_ghost_tasks_ignores_transient_churn_not_in_second_sample():
    # models the short-lived buildkit/snapshotter cgroup churn found live:
    # resolvable in sample 1, already gone (or a different id) by sample 2.
    s1 = {_ID_A: 500}
    s2 = {}
    assert preflight.find_ghost_tasks(s1, s2, docker_ps_ids=[]) == []


def test_find_ghost_tasks_ignores_id_that_only_appears_in_second_sample():
    s1 = {}
    s2 = {_ID_A: 500}
    assert preflight.find_ghost_tasks(s1, s2, docker_ps_ids=[]) == []


def test_find_ghost_tasks_excludes_the_probes_own_id():
    # the probe container is itself alive across both samples and would
    # otherwise flag itself — the caller must fold self_id into docker_ps_ids
    # (docker_ps_ids alone never lists it, captured before the probe starts).
    s1 = {_ID_A: 42}
    s2 = {_ID_A: 42}
    assert preflight.find_ghost_tasks(s1, s2, docker_ps_ids=[_ID_A]) == []


def test_find_ghost_tasks_sorted_and_multiple():
    s1 = {_ID_B: 10, _ID_A: 20}
    s2 = {_ID_B: 11, _ID_A: 21}
    ghosts = preflight.find_ghost_tasks(s1, s2, docker_ps_ids=[])
    assert [g["id"] for g in ghosts] == [_ID_A, _ID_B]
    # pid reported is the SECOND (more recent) sample's reading
    assert ghosts[0]["pid"] == 21


# --- check_dual_postmaster (check c) ----------------------------------------

def test_check_dual_postmaster_matching_is_healthy():
    assert preflight.check_dual_postmaster(
        "2026-07-14 19:13:21.873446+00", "2026-07-14 19:13:21.873446+00"
    ) is False


def test_check_dual_postmaster_microsecond_mismatch_is_dual():
    # the real 9scm numbers: visible twin 17:43:31Z vs ghost 17:42:58Z.
    assert preflight.check_dual_postmaster(
        "2026-07-07 17:43:31.000000+00", "2026-07-07 17:42:58.000000+00"
    ) is True


def test_check_dual_postmaster_whitespace_normalized():
    assert preflight.check_dual_postmaster(
        "  2026-07-14 19:13:21.873446+00  \n", "2026-07-14 19:13:21.873446+00"
    ) is False


def test_check_dual_postmaster_raises_on_empty_reading():
    with pytest.raises(preflight.PreflightError):
        preflight.check_dual_postmaster("", "2026-07-14 19:13:21.873446+00")


def test_check_dual_postmaster_raises_on_both_empty():
    with pytest.raises(preflight.PreflightError):
        preflight.check_dual_postmaster("", "")


# --- render_text / main exit-code composition (still pure, no subprocess) --

def test_render_text_clean(capsys):
    preflight.render_text({"phantoms": [], "ghosts": [], "dual_postmaster": None, "errors": []}, 0)
    out = capsys.readouterr().out
    assert "clean" in out


def test_render_text_reports_each_check_specifically(capsys):
    diagnosis = {
        "phantoms": [{"id": _ID_A, "name": "ghost_db", "pid": 999, "reason": "stale"}],
        "ghosts": [{"id": _ID_B, "pid": 42}],
        "dual_postmaster": {
            "container": "supabase_db_dev-environment",
            "exec_start_time": "T1",
            "tcp_start_time": "T2",
            "mismatch": True,
        },
        "errors": [],
    }
    exit_code = preflight.EXIT_PHANTOM | preflight.EXIT_GHOST | preflight.EXIT_DUAL_PG
    preflight.render_text(diagnosis, exit_code)
    out = capsys.readouterr().out
    assert "STOP THE FLEET" in out
    assert "ghost_db" in out
    assert _ID_B in out
    assert "T1" in out and "T2" in out
    assert "market-brief-package-9scm" in out


def test_exit_code_bits_are_distinct_and_summable():
    assert preflight.EXIT_PHANTOM == 1
    assert preflight.EXIT_GHOST == 2
    assert preflight.EXIT_DUAL_PG == 4
    assert preflight.EXIT_ERROR == 8
    combined = preflight.EXIT_PHANTOM | preflight.EXIT_GHOST | preflight.EXIT_DUAL_PG | preflight.EXIT_ERROR
    assert combined == 15
