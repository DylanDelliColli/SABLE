#!/usr/bin/env python3
"""Integration tests for the handoff gate's prerequisite check (SABLE-slip0.6).

Real composition throughout — no mocked bead store, no injected runner:

  * a real git repo on disk,
  * real planning artifacts at the canonical
    ``<root>/.claude/sable/state/planning/<epic>/`` location,
  * a real ``bd`` EXECUTABLE on PATH (a fixture-backed script, so the gate
    shells out exactly as it does in production), and
  * the actual ``bin/sable-mode`` CLI and the actual fleet-start authority
    read, both run through subprocess.

The riskiest arm here is D8's UNCHECKABLE. Three distinct enum values prove
nothing if one renderer flattens them, so the plant-and-fail below asserts on
what a human or a script would actually READ — at the call site and in the
signed receipt body — rather than on the constants.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent
MODE = BIN / "sable-mode"
SPAWN_MANAGER = BIN / "sable-spawn-manager"

EPIC = "SABLE-prereq"
CHILD = f"{EPIC}.1"
PREREQ = "SABLE-9qqrv"

# y4nom's real wedge shape: the ordering was signed, correct, and inert.
WEDGE = (
    f"The three prerequisites, in order: {PREREQ} (workers can write the "
    "tracker), then SABLE-1el7e. Make the fleet able to work at all BEFORE "
    "making the pool smaller."
)


# --- fixture plumbing ---------------------------------------------------------


def _record(issue_id, *, issue_type="task", status="open"):
    return {
        "id": issue_id,
        "title": f"title {issue_id}",
        "description": "work",
        "status": status,
        "priority": 1,
        "issue_type": issue_type,
        "labels": [],
        "dependencies": [],
    }


FAKE_BD = '''#!/usr/bin/env python3
"""A real bd executable for the gate to shell out to, backed by a JSON file."""
import json, os, sys

records = json.load(open(os.environ["FAKE_BD_RECORDS"]))
argv = sys.argv[1:]

if argv[:1] == ["show"]:
    record = records["beads"].get(argv[1])
    if record is None:
        sys.stderr.write("not found\\n")
        raise SystemExit(1)
    print(json.dumps([record]))
elif argv[:2] == ["swarm", "validate"]:
    print(json.dumps({"schema_version": 1, "swarmable": True,
                      "errors": None, "warnings": []}))
elif argv[:1] == ["ready"]:
    print(json.dumps([records["beads"][i] for i in records["ready"]]))
elif argv[:1] == ["list"] and "open-question" in argv:
    print(json.dumps([]))
elif argv[:2] == ["list", "--parent"]:
    print(json.dumps([records["beads"][i] for i in records["children"]]))
else:
    sys.stderr.write(f"fake bd: unexpected {argv}\\n")
    raise SystemExit(2)
'''


def make_bd(bin_dir, beads, *, children=(), ready=()):
    bin_dir.mkdir(parents=True, exist_ok=True)
    fixture = bin_dir / "records.json"
    fixture.write_text(
        json.dumps(
            {
                "beads": {b["id"]: b for b in beads},
                "children": list(children),
                "ready": list(ready),
            }
        )
    )
    script = bin_dir / "bd"
    script.write_text(FAKE_BD)
    script.chmod(0o755)
    return fixture


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "seed").write_text("seed\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "seed"], check=True, env=env
    )
    return repo


def write_dossier(planning_root, epic, *, prerequisites, deps):
    """The five artifacts, valid except for whatever this test is varying."""

    directory = planning_root / epic
    directory.mkdir(parents=True, exist_ok=True)
    framing = {
        "stories": [{"id": "S1", "title": "a worker can record its own work"}],
        "success_metric": "approved work is the only work that starts",
        "wedge": WEDGE,
        "non_goals": [f"fixing {PREREQ} itself"],
    }
    if prerequisites is not None:
        framing["prerequisites"] = prerequisites
    documents = {
        "framing": framing,
        "research": {
            "findings": [
                {
                    "title": "the gate never read the plan",
                    "summary": "framing.json was hashed, never consulted",
                    "derisk_status": "resolved",
                }
            ],
            "recommendation": "bind the declaration, not just the bytes",
        },
        "architecture": {
            "status": "ready",
            "decisions": [
                {
                    "title": "structured field",
                    "contract": "framing.prerequisites is REQUIRED",
                    "rationale": "prose cannot carry the semantic distinction",
                }
            ],
        },
        "test-strategy": {
            "stories": [
                {
                    "id": "S1",
                    "cases": [
                        {
                            "name": "true positive",
                            "layer": "UNIT",
                            "status": "planned",
                        }
                    ],
                }
            ],
            "coverage": {"covered": 1, "total": 1},
        },
        "decomposition": {
            "children": [
                {
                    "id": CHILD,
                    "title": "implement the check",
                    "type": "task",
                    "deps": list(deps),
                    "ready": True,
                }
            ],
            "swarm_validate": {"ok": True, "output": "PASS"},
            "victor_summary": "fresh",
        },
    }
    for name, document in documents.items():
        (directory / f"{name}.json").write_text(json.dumps(document))
    return directory


def write_planning_state(path, *, tier="full", substage="decomposition"):
    path.write_text(
        json.dumps(
            {
                "mode": "planning",
                "since": "2026-07-31T00:00:00+0000",
                "fleet": [],
                "tier": tier,
                "substage": substage,
            }
        )
    )


def env_for(repo, state, planning_root, *, path):
    return {
        "PATH": path,
        "HOME": str(repo.parent),
        "SABLE_MODE_STATE": str(state),
        "SABLE_PLANNING_DIR": str(planning_root),
        "SABLE_HANDOFF_REPO": str(repo),
        "SABLE_APPROVAL_ACTOR": "integration-operator",
        "FAKE_BD_RECORDS": str(repo.parent / "fakebin" / "records.json"),
    }


def run_mode(args, repo, env):
    return subprocess.run(
        [sys.executable, str(MODE), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture()
def gate(tmp_path):
    """A real repo + real bd + real artifacts, ready for the real CLI."""

    repo = make_repo(tmp_path)
    fake_bin = tmp_path / "fakebin"
    make_bd(
        fake_bin,
        [
            _record(EPIC, issue_type="epic"),
            _record(CHILD),
            _record(PREREQ),
            _record("SABLE-79zjm"),
        ],
        children=[CHILD],
        ready=[CHILD],
    )
    state = tmp_path / "mode-state.json"
    planning_root = tmp_path / "planning"

    class Gate:
        def __init__(self):
            self.repo = repo
            self.state = state
            self.planning_root = planning_root
            self.bd_path = f"{fake_bin}:/usr/bin:/bin"
            self.no_bd_path = "/usr/bin:/bin"

        def env(self, *, with_bd=True):
            return env_for(
                repo,
                state,
                planning_root,
                path=self.bd_path if with_bd else self.no_bd_path,
            )

        def run(self, args, *, with_bd=True):
            return run_mode(args, repo, self.env(with_bd=with_bd))

    return Gate()


# --- the true positive, end to end through the real CLI ------------------------


def test_gate_refuses_when_the_scope_skips_a_declared_prerequisite(gate):
    """y4nom's exact failure: the plan named it first, the scope depended on
    none of it, and the gate signed anyway."""

    write_planning_state(gate.state)
    write_dossier(
        gate.planning_root, EPIC, prerequisites=[PREREQ], deps=["SABLE-79zjm"]
    )

    result = gate.run(["handoff", "approve", "--epic", EPIC])

    assert result.returncode != 0, result.stdout
    assert PREREQ in result.stderr
    # And the refusal is real: no receipt was attached to the planning state.
    assert "handoff" not in json.loads(gate.state.read_text())


def test_gate_approves_when_the_prerequisite_is_in_the_dependency_closure(gate):
    write_planning_state(gate.state)
    write_dossier(gate.planning_root, EPIC, prerequisites=[PREREQ], deps=[PREREQ])

    result = gate.run(["handoff", "approve", "--epic", EPIC])

    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["prerequisites"]["status"] == "PASS"
    assert receipt["prerequisites"]["declared"] == [PREREQ]
    assert "prerequisites: PASS" in result.stderr


def test_framing_without_the_prerequisites_field_is_schema_invalid(gate):
    """NEGATIVE CONTROL: absence must refuse, not silently skip the check."""

    write_planning_state(gate.state)
    write_dossier(
        gate.planning_root, EPIC, prerequisites=None, deps=["SABLE-79zjm"]
    )

    result = gate.run(["handoff", "approve", "--epic", EPIC])

    assert result.returncode != 0
    assert "prerequisites" in result.stderr
    assert "handoff" not in json.loads(gate.state.read_text())


def test_a_dep_naming_a_nonexistent_bead_does_not_silently_pass(gate):
    """The closure is only as sound as the dep ids it is built from."""

    write_planning_state(gate.state)
    write_dossier(
        gate.planning_root,
        EPIC,
        prerequisites=[PREREQ],
        deps=[PREREQ, "SABLE-typo0"],
    )

    result = gate.run(["handoff", "approve", "--epic", EPIC])

    assert result.returncode != 0
    assert "SABLE-typo0" in result.stderr


# --- placement: the handoff gate, NOT substage advance ------------------------


def test_substage_advance_does_not_run_the_prerequisite_check(gate):
    """Placement is pinned, and this is the assertion that pins it.

    advance_planning_substage reads no artifact and checks no file existence,
    so a prerequisite check placed there would run before decomposition.json
    exists and could only ever report UNCHECKABLE — enforcement-shaped and
    inert, the precise failure this bead is named for.
    """

    write_planning_state(gate.state, substage="test-strategy")
    write_dossier(
        gate.planning_root, EPIC, prerequisites=[PREREQ], deps=["SABLE-79zjm"]
    )

    advance = gate.run(["substage", "advance"])

    assert advance.returncode == 0, advance.stderr
    assert "prerequisite" not in advance.stderr.lower()
    assert json.loads(gate.state.read_text())["substage"] == "decomposition"

    # Same bytes on disk, same violation — the HANDOFF gate is where it bites.
    refused = gate.run(["handoff", "approve", "--epic", EPIC])
    assert refused.returncode != 0
    assert PREREQ in refused.stderr


# --- D8's riskiest arm: plant-and-fail with bd genuinely off PATH -------------


AUTHORITY_DRIVER = """
import sys
sys.path.insert(0, {bin!r})
import sable_handoff_lib as handoff
try:
    handoff.read_execution_authority(base={repo!r})
except handoff.HandoffRefused as exc:
    sys.stderr.write("refused: %s\\n" % exc)
    raise SystemExit(3)
"""


def run_fleet_start(gate, *, with_bd=False):
    """The real fleet-start authority read — the same call sable-spawn-manager
    (:463) and sable-spawn-worker make before standing a pane up."""

    return subprocess.run(
        [
            sys.executable,
            "-c",
            AUTHORITY_DRIVER.format(bin=str(BIN), repo=str(gate.repo)),
        ],
        cwd=str(gate.repo),
        capture_output=True,
        text=True,
        env=gate.env(with_bd=with_bd),
    )


def sign_quick_receipt(gate):
    """A REAL signed receipt, produced by the real CLI over the real bd."""

    write_planning_state(gate.state, tier="quick", substage="framing")
    result = gate.run(["handoff", "approve", "--beads", CHILD])
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def as_execution_state(gate, receipt):
    gate.state.write_text(json.dumps({"mode": "execution", "handoff": receipt}))


def test_bd_off_path_reports_uncheckable_and_never_reads_as_clean(gate):
    """PLANT-AND-FAIL, both required surfaces.

    Quick tier has no framing artifact, so its receipt honestly asserts
    nothing about prerequisites. That receipt is then read back at fleet start
    with bd genuinely absent from PATH — the environment in which a gate is
    most tempted to shrug and continue.
    """

    receipt = sign_quick_receipt(gate)

    # 1. THE SIGNED RECEIPT BODY.
    assert receipt["prerequisites"]["status"] == "UNCHECKABLE"
    body = json.dumps(receipt)
    assert "UNCHECKABLE" in body
    assert "clean" not in body.lower()

    # 2. THE CALL SITE, with bd off PATH for real.
    as_execution_state(gate, receipt)
    assert shutil.which("bd", path=gate.no_bd_path) is None
    started = run_fleet_start(gate)

    assert started.returncode == 0, started.stderr
    assert "UNCHECKABLE" in started.stderr
    assert "clean" not in started.stderr.lower()
    assert "PASS" not in started.stderr


def test_uncheckable_and_pass_do_not_render_the_same_way(gate):
    """A renderer that prints the three verdicts identically would satisfy an
    enum assertion and defeat the entire check, so compare the OUTPUT."""

    receipt = sign_quick_receipt(gate)
    as_execution_state(gate, receipt)
    uncheckable = run_fleet_start(gate).stderr

    passing = dict(receipt)
    passing["prerequisites"] = {
        "status": "PASS",
        "reason": "1 declared prerequisite(s), each present in the "
        "decomposition dependency closure",
        "declared": [PREREQ],
        "missing": [],
        "malformed": [],
        "unresolvable": [],
        "stale": [],
    }
    passing.pop("receipt_id")
    passing["receipt_id"] = _digest(passing)
    as_execution_state(gate, passing)
    cleared = run_fleet_start(gate).stderr

    assert uncheckable != cleared
    assert "UNCHECKABLE" in uncheckable and "UNCHECKABLE" not in cleared
    assert "PASS" in cleared and "PASS" not in uncheckable


def test_receipt_carrying_no_verdict_at_all_reads_back_uncheckable(gate):
    """The legacy shape: a receipt signed before this field existed asserts
    nothing, so fleet start must say so rather than start quietly."""

    receipt = sign_quick_receipt(gate)
    legacy = {k: v for k, v in receipt.items() if k != "prerequisites"}
    legacy.pop("receipt_id")
    legacy["receipt_id"] = _digest(legacy)
    as_execution_state(gate, legacy)

    started = run_fleet_start(gate)

    assert started.returncode == 0, started.stderr
    assert "UNCHECKABLE" in started.stderr
    assert "PASS" not in started.stderr


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_real_spawn_manager_renders_the_verdict_before_fleet_start(gate, tmp_path):
    """The actual CLI call site (sable-spawn-manager:463), with a PATH that has
    tmux but genuinely no bd."""

    receipt = sign_quick_receipt(gate)
    as_execution_state(gate, receipt)
    tmux_only = tmp_path / "tmuxbin"
    tmux_only.mkdir()
    os.symlink(shutil.which("tmux"), tmux_only / "tmux")
    socket = f"sable-prereq-{os.getpid()}"
    env = gate.env(with_bd=False)
    env["PATH"] = f"{tmux_only}:{gate.no_bd_path}"
    env["SABLE_REGISTRY"] = str(tmp_path / "registry.json")
    env["SABLE_TMUX_SOCKET"] = socket
    env["SABLE_TMUX_SESSION"] = "prereq-int"

    try:
        result = subprocess.run(
            [sys.executable, str(SPAWN_MANAGER), "chuck"],
            cwd=str(gate.repo),
            capture_output=True,
            text=True,
            env=env,
        )
    finally:
        subprocess.run(
            ["tmux", "-L", socket, "kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    assert "UNCHECKABLE" in result.stderr, result.stderr
    assert "clean" not in result.stderr.lower()


def _digest(value):
    """The receipt's own signing digest, so re-signed fixtures stay valid."""

    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    ).hexdigest()
