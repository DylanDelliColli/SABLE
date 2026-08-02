#!/usr/bin/env python3
"""Fleet-contract smoke test (SABLE-slip0.3).

This is deliberately a plumbing test, not an agent-quality claim.  The worker
is a deterministic bash/Python stand-in, while the sandbox, tmux registry,
worker dispatcher, beads store, evidence writer/reader, git push, preview
construction, and promotion are real.  Therefore this suite is licensed to
prove the SABLE-9qqrv sandbox-grant class and the round-trip plumbing.  It is
not licensed to claim that a real LLM completed a task or that GitHub Actions
ran where ``SABLE_MG_GH`` supplies the verdict.

The full leg needs ``bd``, ``tmux``, and Linux ``bwrap``.  Missing any of them
is UNCHECKABLE, never a clean substitute.  The smaller preview-authority and
vacuity-control cases remain runnable without those external primitives.
"""

from __future__ import annotations

import enum
import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
import uuid
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BIN = REPO_ROOT / "bin"
SPAWN_MANAGER = BIN / "sable-spawn-manager"
SPAWN_WORKER = BIN / "sable-spawn-worker"
SABLE_TMUX = BIN / "sable-tmux"
SABLE_TEST = BIN / "sable-test"
TDD_GATE = REPO_ROOT / "hooks" / "tdd-gate.sh"
MERGE_GATE = BIN / "sable-merge-gate"
BASE_BRANCH = "trunk"
WORKER_BRANCH = "wk-smoke"

_WORKER_LOADER = SourceFileLoader("sable_fleet_smoke_spawn_worker", str(SPAWN_WORKER))
_WORKER_SPEC = importlib.util.spec_from_loader(
    "sable_fleet_smoke_spawn_worker", _WORKER_LOADER
)
spawn_worker = importlib.util.module_from_spec(_WORKER_SPEC)
_WORKER_LOADER.exec_module(spawn_worker)


class SandboxPosture(enum.Enum):
    ESTABLISHED = "established"
    UNCHECKABLE = "uncheckable"


def _classify_sandbox_probe(*, worktree_write: bool, checkout_write: bool) -> SandboxPosture:
    assert worktree_write, (
        "sandbox positive control could not write inside the worker worktree; "
        "an outside-write denial is not interpretable"
    )
    return (
        SandboxPosture.UNCHECKABLE
        if checkout_write
        else SandboxPosture.ESTABLISHED
    )


def _guard_live_store(run, count):
    before = count()
    result = run()
    after = count()
    assert after == before, (
        f"live beads store changed during smoke: before={before}, after={after}"
    )
    return result


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def _checked(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 60,
) -> str:
    cp = _run(argv, cwd=cwd, env=env, timeout=timeout)
    assert cp.returncode == 0, (
        f"command failed ({cp.returncode}): {shlex.join(argv)}\n{cp.stdout}"
    )
    return cp.stdout.strip()


def _git(cwd: Path | None, *args: str) -> str:
    return _checked(
        [
            "git",
            "-c",
            "user.name=SABLE smoke worker",
            "-c",
            "user.email=smoke@example.invalid",
            *args,
        ],
        cwd=cwd,
    )


def _setup_git_round_trip(tmp_path: Path, *, worker_commit: bool) -> dict[str, Path]:
    primary = tmp_path / "primary"
    worker = tmp_path / "worker"
    primary.mkdir()
    _git(primary, "init", "-b", BASE_BRANCH)
    (primary / "README.md").write_text("fleet smoke base\n")
    _git(primary, "add", "README.md")
    _git(primary, "commit", "-m", "base")

    # The scratch bare remote deliberately lives below the shared git common
    # directory.  A real network remote needs no extra filesystem grant; this
    # placement gives the local stand-in the same property when bwrap grants
    # only the production git-common root.
    origin = primary / ".git" / "smoke-origin.git"
    _git(None, "init", "--bare", "-b", BASE_BRANCH, str(origin))
    _git(primary, "remote", "add", "origin", str(origin))
    _git(primary, "push", "-u", "origin", BASE_BRANCH)
    _git(primary, "worktree", "add", "-b", WORKER_BRANCH, str(worker), BASE_BRANCH)
    if worker_commit:
        (worker / "feature.txt").write_text("preview authority plant\n")
        _git(worker, "add", "feature.txt")
        _git(worker, "commit", "-m", "worker feature")
        _git(worker, "push", "-u", "origin", WORKER_BRANCH)
    return {"primary": primary, "worker": worker, "origin": origin}


def _write_fake_gh(
    path: Path,
    *,
    origin: Path,
    conclusion: str,
    query_log: Path,
) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, subprocess, sys\n"
        f"origin = {str(origin)!r}\n"
        f"conclusion = {conclusion!r}\n"
        f"query_log = {str(query_log)!r}\n"
        "args = sys.argv[1:]\n"
        "ref = args[args.index('--branch') + 1]\n"
        "sha = subprocess.run(\n"
        "    ['git', '--git-dir=' + origin, 'rev-parse', 'refs/heads/' + ref],\n"
        "    check=True, text=True, capture_output=True).stdout.strip()\n"
        "with open(query_log, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(sha + '\\n')\n"
        "print(json.dumps([{'databaseId': 1, 'headSha': sha,\n"
        "                   'status': 'completed', 'conclusion': conclusion,\n"
        "                   'url': 'https://invalid.example/smoke'}]))\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _write_fake_bd(path: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"${1:-}\" = show ]; then\n"
        "  printf '%s\\n' '## File reads' 'none'\n"
        "fi\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _merge_gate_env(gh: Path, fake_bd: Path) -> dict[str, str]:
    return {
        **os.environ,
        "SABLE_MG_GH": str(gh),
        "SABLE_MG_BD": str(fake_bd),
        "SABLE_MG_NOTIFY": "true",
        "SABLE_MG_POLL": "0",
        "SABLE_MG_GRACE": "0",
        "SABLE_MG_TIMEOUT": "0",
    }


def _promote(primary: Path, gh: Path, fake_bd: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            sys.executable,
            str(MERGE_GATE),
            "promote",
            "--bead",
            "SMK-1",
            "--branch",
            WORKER_BRANCH,
            "--base",
            BASE_BRANCH,
            "--repo",
            str(primary),
            "--remote",
            "origin",
        ],
        cwd=primary,
        env=_merge_gate_env(gh, fake_bd),
        timeout=60,
    )


def _exercise_preview_authority(tmp_path: Path):
    repo = _setup_git_round_trip(tmp_path, worker_commit=True)
    primary, origin = repo["primary"], repo["origin"]
    pre_push = tmp_path / "pre-push-green.json"
    pre_push.write_text('{"conclusion":"success"}\n')
    query_log = tmp_path / "red-preview-queries"
    gh = _write_fake_gh(
        tmp_path / "gh-red",
        origin=origin,
        conclusion="failure",
        query_log=query_log,
    )
    fake_bd = _write_fake_bd(tmp_path / "bd-no-reads")
    before = _git(
        None,
        "--git-dir=" + str(origin),
        "rev-parse",
        f"refs/heads/{BASE_BRANCH}",
    )
    gate = _promote(primary, gh, fake_bd)
    after = _git(
        None,
        "--git-dir=" + str(origin),
        "rev-parse",
        f"refs/heads/{BASE_BRANCH}",
    )
    assert query_log.read_text().strip(), "the gate never queried a preview SHA"
    return {
        "gate_rc": gate.returncode,
        "base_before": before,
        "base_after": after,
        "pre_push_green": json.loads(pre_push.read_text())["conclusion"] == "success",
        "preview_conclusion": "failure",
    }


def _execution_authority() -> dict[str, object]:
    proof: dict[str, object] = {
        "version": 1,
        "kind": "break-glass",
        "approved_by": "fleet-smoke-fixture",
        "approved_at": "2026-08-01T00:00:00+00:00",
        "reason": "isolated execution authority for the fleet smoke fixture",
        "base": {"ref": "HEAD", "sha": "a" * 40},
        "failed_checks": ["synthetic fixture"],
    }
    proof["receipt_id"] = hashlib.sha256(
        json.dumps(
            proof,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()
    return proof


def _write_execution_state(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "mode": "execution",
                "handoff": _execution_authority(),
                "providers": {"worker": "codex"},
            }
        )
    )


def _write_registry(path: Path) -> None:
    path.write_text(
        "version: 1\n"
        "agents:\n"
        "  lincoln:\n"
        "    type: cockpit\n"
        "  optimus:\n"
        "    type: epic_manager\n"
        "  tarzan:\n"
        "    type: one_off_manager\n"
        "  chuck:\n"
        "    type: integrator\n"
    )


def _clean_bd_env(*, store: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("BEADS_DB", None)
    if store is None:
        env.pop("BEADS_DIR", None)
    else:
        env["BEADS_DIR"] = str(store)
    env["BD_NON_INTERACTIVE"] = "1"
    return env


def _init_bead_store(primary: Path) -> tuple[Path, str]:
    _checked(
        [
            "bd",
            "init",
            "--prefix=SMK",
            "--non-interactive",
            "--skip-agents",
            "--skip-hooks",
            "--quiet",
        ],
        cwd=primary,
        env=_clean_bd_env(),
    )
    store = primary / ".beads"
    created = _checked(
        [
            "bd",
            "create",
            "--title=Fleet smoke throwaway",
            "--description=Implement feature.txt and prove the worker-owned close.",
            "--type=task",
            "--json",
        ],
        cwd=primary,
        env=_clean_bd_env(store=store),
    )
    payload = json.loads(created)
    if isinstance(payload, list):
        payload = payload[0]
    assert isinstance(payload, dict) and payload.get("id"), created
    return store, str(payload["id"])


def _manager_tui_command() -> str:
    # This deterministic stand-in models the one provider-owned fact the
    # manager trichotomy requires: SessionStart wrote a boot epoch.  It does not
    # pretend that a provider hook actually fired; the suite header constrains
    # the claim accordingly.
    return (
        "bash --noprofile --norc -c '"
        "tmux set-option -p -t \"$TMUX_PANE\" @sable_boot_epoch "
        "\"smoke-$BASHPID\"; "
        "while true; do printf \"❯ \"; "
        "IFS= read -r line || break; printf \"%s\\n\" \"$line\"; done'"
    )


def _write_worker_standin(path: Path) -> Path:
    path.write_text(
        r'''#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
from pathlib import Path

outcome_path = Path(os.environ["SMOKE_OUTCOME"])
worktree = Path.cwd()
forbidden = Path(os.environ["SMOKE_FORBIDDEN"])
release = Path(os.environ["SMOKE_RELEASE"])
bead = os.environ["SMOKE_BEAD"]
branch = os.environ["SMOKE_BRANCH"]
session_id = os.environ["SMOKE_SESSION_ID"]
evidence = Path(os.environ["SMOKE_EVIDENCE"])
sable_test = os.environ["SMOKE_SABLE_TEST"]
tdd_gate = os.environ["SMOKE_TDD_GATE"]


def run(argv, *, input_text=None):
    cp = subprocess.run(
        argv,
        text=True,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if cp.returncode != 0:
        raise RuntimeError(
            f"command failed ({cp.returncode}): {argv!r}\n{cp.stdout}"
        )
    return cp.stdout


def finish(payload, rc):
    outcome_path.write_text(json.dumps(payload, sort_keys=True))
    raise SystemExit(rc)


try:
    print("› ", end="", flush=True)
    instruction = sys.stdin.readline().strip()
    print(instruction, flush=True)
    # Re-render an empty composer so the real dispatch verifier can prove the
    # instruction left the input box before this deterministic turn continues.
    print("› ", end="", flush=True)

    match = re.match(r"^Read (.+) in full and execute ", instruction)
    if not match:
        raise RuntimeError(f"dispatch instruction was not delivered: {instruction!r}")
    dispatch_path = Path(match.group(1))
    dispatch = dispatch_path.read_text()
    if bead not in dispatch or str(worktree) not in dispatch:
        raise RuntimeError("dispatch file did not bind bead and worktree")

    inside_probe = worktree / ".smoke-worktree-probe"
    inside_probe.write_text("inside\n")
    worktree_write = inside_probe.exists()
    inside_probe.unlink()

    checkout_write = False
    try:
        forbidden.write_text("sandbox escaped\n")
        checkout_write = True
    except OSError:
        checkout_write = False
    if checkout_write:
        forbidden.unlink(missing_ok=True)
        finish(
            {
                "status": "UNCHECKABLE",
                "sandbox": "uncheckable",
                "worktree_write": worktree_write,
                "checkout_write": True,
            },
            86,
        )
    if not worktree_write:
        raise RuntimeError("sandbox positive control could not write the worktree")

    # The parent releases this only after reading the scratch bead back as
    # OPEN.  That makes the no-manager-owned-tracker-mutation claim observable:
    # both spawn tools have completed, but only this process may cross the
    # release and perform claim/close.
    import time
    deadline = time.monotonic() + 15
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not release.exists():
        raise RuntimeError("parent never released worker tracker mutations")

    run(["bd", "update", bead, "--claim"])
    feature = worktree / "feature.txt"
    feature.write_text("implemented by the fleet smoke worker\n")
    run([sable_test, "bash", "-c", "test -f feature.txt && grep -q fleet feature.txt"])
    if not evidence.is_file() or not evidence.read_text().strip():
        raise RuntimeError("sable-test wrote no evidence")

    gate_input = json.dumps(
        {"session_id": session_id, "tool_input": {"command": f"bd close {bead}"}}
    )
    gate = subprocess.run(
        ["bash", tdd_gate],
        text=True,
        input=gate_input,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if gate.returncode != 0 or gate.stdout.strip():
        raise RuntimeError(
            f"real tdd-gate did not allow the close: rc={gate.returncode} "
            f"output={gate.stdout!r}"
        )

    run(["bd", "close", bead, "--reason", "fleet smoke worker completed with evidence"])
    run(["git", "add", "feature.txt"])
    run([
        "git", "-c", "user.name=SABLE smoke worker",
        "-c", "user.email=smoke@example.invalid",
        "commit", "-m", "feat: fleet smoke worker implementation",
    ])
    run(["git", "push", "-u", "origin", branch])
    sha = run(["git", "rev-parse", "HEAD"]).strip()
    finish(
        {
            "status": "ok",
            "sandbox": "established",
            "worktree_write": True,
            "checkout_write": False,
            "bead": bead,
            "branch": branch,
            "sha": sha,
            "dispatch": str(dispatch_path),
            "tdd_gate_allowed": True,
            "tracker_writes": ["claim", "close"],
        },
        0,
    )
except BaseException as exc:
    if isinstance(exc, SystemExit):
        raise
    finish({"status": "error", "error": repr(exc)}, 1)
'''
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _workspace_roots(worker: Path, env: dict[str, str]) -> list[str]:
    with mock.patch.dict(os.environ, env, clear=False):
        roots = spawn_worker.codex_writable_roots(str(worker))
    return roots


def _bwrap_command(
    *,
    worker: Path,
    roots: list[str],
    standin: Path,
    evidence: Path,
    env: dict[str, str],
) -> str:
    argv = [
        "bwrap",
        "--die-with-parent",
        "--ro-bind",
        "/",
        "/",
        "--dev-bind",
        "/dev",
        "/dev",
        "--proc",
        "/proc",
    ]
    for item in [str(worker), *roots, str(evidence)]:
        argv += ["--bind", item, item]
    argv += ["--chdir", str(worker)]
    for name in (
        "BEADS_DIR",
        "CLAUDE_SESSION_ID",
        "SMOKE_BEAD",
        "SMOKE_BRANCH",
        "SMOKE_EVIDENCE",
        "SMOKE_FORBIDDEN",
        "SABLE_HOOK_TRACE",
        "SMOKE_OUTCOME",
        "SMOKE_RELEASE",
        "SMOKE_SABLE_TEST",
        "SMOKE_SESSION_ID",
        "SMOKE_TDD_GATE",
        "TMPDIR",
    ):
        argv += ["--setenv", name, env[name]]
    argv += ["--", sys.executable, str(standin)]
    return shlex.join(argv)


def _live_store_count() -> int:
    raw = _checked(["bd", "count"], cwd=REPO_ROOT, env=_clean_bd_env())
    return int(raw.splitlines()[-1])


def _wait_for_path(path: Path, *, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def _exercise_full_round_trip_unchecked(tmp_path: Path) -> dict[str, object]:
    repo = _setup_git_round_trip(tmp_path, worker_commit=False)
    primary, worker, origin = repo["primary"], repo["worker"], repo["origin"]
    store, bead = _init_bead_store(primary)
    feedback = tmp_path / "feedback"
    feedback.mkdir()
    dispatch = tmp_path / "dispatch"
    dispatch.mkdir()
    scratch = worker / ".tmp"
    scratch.mkdir()
    forbidden = primary / "sandbox-escape-probe"
    outcome = worker / ".smoke-outcome.json"
    release = worker / ".smoke-release"
    mode_state = tmp_path / "mode-state.json"
    registry = tmp_path / "agents.yaml"
    standin = _write_worker_standin(tmp_path / "worker-standin.py")
    _write_execution_state(mode_state)
    _write_registry(registry)

    session_id = f"fleet-smoke-{uuid.uuid4().hex}"
    evidence = Path("/tmp") / f"tdd-evidence-{session_id}"
    evidence.write_text("")
    socket = f"fleet-smoke-{uuid.uuid4().hex[:10]}"
    session = "fleet-smoke"
    env = {
        **os.environ,
        "BEADS_DIR": str(store),
        "BD_NON_INTERACTIVE": "1",
        "CLAUDE_AGENT_NAME": "optimus",
        "CLAUDE_SESSION_ID": session_id,
        "SABLE_AGENT_NAME": "optimus",
        "SABLE_AGENT_ROLE": "manager",
        "SABLE_AGENTS_YAML": str(registry),
        "SABLE_DISPATCH_DIR": str(dispatch),
        "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
        "SABLE_DISPATCH_READY_TIMEOUT": "3",
        "SABLE_DISPATCH_SUBMIT_TRIES": "3",
        "SABLE_FEEDBACK_DIR": str(feedback),
        "SABLE_HOOK_TRACE": "0",
        "SABLE_MANAGER_REKICK_TRIES": "2",
        "SABLE_MAX_LOAD_PER_CORE": "1000",
        "SABLE_MODE_STATE": str(mode_state),
        "SABLE_TMUX_PANE_CMD": _manager_tui_command(),
        "SABLE_TMUX_SESSION": session,
        "SABLE_TMUX_SOCKET": socket,
        "SMOKE_BEAD": bead,
        "SMOKE_BRANCH": WORKER_BRANCH,
        "SMOKE_EVIDENCE": str(evidence),
        "SMOKE_FORBIDDEN": str(forbidden),
        "SMOKE_OUTCOME": str(outcome),
        "SMOKE_RELEASE": str(release),
        "SMOKE_SABLE_TEST": str(SABLE_TEST),
        "SMOKE_SESSION_ID": session_id,
        "SMOKE_TDD_GATE": str(TDD_GATE),
        "TMPDIR": str(scratch),
    }

    try:
        roots = _workspace_roots(worker, env)
        common = str((primary / ".git").resolve())
        assert common in roots, roots
        assert str(store.resolve()) in roots, roots
        assert str(feedback.resolve()) in roots, roots
        assert str(primary.resolve()) not in roots, roots
        env["SABLE_WORKER_CMD"] = _bwrap_command(
            worker=worker,
            roots=roots,
            standin=standin,
            evidence=evidence,
            env=env,
        )

        _checked(
            [
                sys.executable,
                str(SABLE_TMUX),
                "--session",
                session,
                "--roles",
                "lincoln",
            ],
            cwd=primary,
            env=env,
        )
        _checked(
            [sys.executable, str(SPAWN_MANAGER), "--all"],
            cwd=primary,
            env=env,
            timeout=30,
        )
        role_rows = _checked(
            [
                "tmux",
                "-L",
                socket,
                "list-panes",
                "-s",
                "-t",
                session,
                "-F",
                ("#{@sable_role}\t#{@sable_status}\t#{@sable_boot_epoch}"
                 "\t#{@sable_kicked_epoch}"),
            ]
        ).splitlines()
        role_evidence = {
            fields[0]: fields[1:]
            for fields in (row.split("\t") for row in role_rows)
        }
        roles = list(role_evidence)
        assert sorted(roles) == ["chuck", "lincoln", "optimus", "tarzan"], roles
        for role in ("optimus", "tarzan", "chuck"):
            status, boot_epoch, kicked_epoch = role_evidence[role]
            assert status == "running", role_evidence
            assert boot_epoch and kicked_epoch == boot_epoch, role_evidence

        spawned = _run(
            [
                sys.executable,
                str(SPAWN_WORKER),
                bead,
                "--worktree",
                str(worker),
                "--model",
                "gpt-5.6-sol",
                "--lane",
                "optimus",
                "--session",
                session,
                "--skip-governance",
            ],
            cwd=primary,
            env=env,
            timeout=30,
        )
        assert spawned.returncode == 0, spawned.stdout
        before_worker = json.loads(
            _checked(
                ["bd", "show", bead, "--json"],
                cwd=primary,
                env=_clean_bd_env(store=store),
            )
        )[0]
        assert before_worker["status"] == "open", before_worker
        release.touch()
        _wait_for_path(outcome)
        worker_result = json.loads(outcome.read_text())
        assert worker_result.get("status") == "ok", worker_result
        posture = _classify_sandbox_probe(
            worktree_write=worker_result["worktree_write"],
            checkout_write=worker_result["checkout_write"],
        )
        assert posture is SandboxPosture.ESTABLISHED
        assert worker_result["tracker_writes"] == ["claim", "close"]
        assert evidence.is_file() and f"REPO={worker}" in evidence.read_text()
        # The stand-in publishes its outcome immediately before SystemExit;
        # the lifecycle wrapper can only write `done` after that process exits.
        # Poll the pane-owned verdict rather than racing those two events.
        deadline = time.monotonic() + 3
        worker_rows: list[list[str]] = []
        while time.monotonic() < deadline:
            pane_rows = _checked(
                [
                    "tmux", "-L", socket, "list-panes", "-s", "-t", session,
                    "-F",
                    ("#{@sable_role}\t#{@sable_provider}\t#{@sable_bead}"
                     "\t#{@sable_status}"),
                ]
            ).splitlines()
            worker_rows = [
                row.split("\t") for row in pane_rows if row.startswith("worker\t")
            ]
            if worker_rows == [["worker", "codex", bead, "done"]]:
                break
            time.sleep(0.02)
        assert worker_rows == [["worker", "codex", bead, "done"]], worker_rows

        remote_worker = _git(
            None,
            "--git-dir=" + str(origin),
            "rev-parse",
            f"refs/heads/{WORKER_BRANCH}",
        )
        assert remote_worker == worker_result["sha"]
        shown = json.loads(
            _checked(
                ["bd", "show", bead, "--json"],
                cwd=primary,
                env=_clean_bd_env(store=store),
            )
        )[0]
        assert shown["status"] == "closed", shown

        query_log = tmp_path / "green-preview-queries"
        gh = _write_fake_gh(
            tmp_path / "gh-green",
            origin=origin,
            conclusion="success",
            query_log=query_log,
        )
        fake_bd = _write_fake_bd(tmp_path / "bd-no-reads-green")
        promoted = _promote(primary, gh, fake_bd)
        assert promoted.returncode == 0, promoted.stdout
        queried = query_log.read_text().splitlines()
        assert queried, "merge gate never queried the preview verdict"
        base_tip = _git(
            None,
            "--git-dir=" + str(origin),
            "rev-parse",
            f"refs/heads/{BASE_BRANCH}",
        )
        assert base_tip == queried[-1]

        return {
            "sandbox": posture.value,
            "worker_pushed": remote_worker == worker_result["sha"],
            "bead_closed": shown["status"] == "closed",
            "tdd_gate_allowed": worker_result["tdd_gate_allowed"],
            "bead_open_before_worker_release": before_worker["status"] == "open",
            "promoted_preview": queried[-1],
            "base_tip": base_tip,
            "live_store_unchanged": True,
        }
    finally:
        _run(["tmux", "-L", socket, "kill-server"], timeout=10)
        evidence.unlink(missing_ok=True)
        forbidden.unlink(missing_ok=True)
        release.unlink(missing_ok=True)


def _exercise_full_round_trip(tmp_path: Path):
    return _guard_live_store(
        lambda: _exercise_full_round_trip_unchecked(tmp_path),
        _live_store_count,
    )


def test_bare_bash_override_is_uncheckable_not_green(tmp_path):
    primary = tmp_path / "primary"
    worker = tmp_path / "worker"
    primary.mkdir()
    worker.mkdir()
    dispatch = tmp_path / "dispatch.md"
    bead = "SMK-probe"
    dispatch.write_text(f"bead={bead}\nworktree={worker}\n")
    outcome = worker / "outcome.json"
    forbidden = primary / "escape-probe"
    standin = _write_worker_standin(tmp_path / "standin.py")
    env = {
        **os.environ,
        "SMOKE_BEAD": bead,
        "SMOKE_BRANCH": WORKER_BRANCH,
        "SMOKE_EVIDENCE": str(tmp_path / "unused-evidence"),
        "SMOKE_FORBIDDEN": str(forbidden),
        "SMOKE_OUTCOME": str(outcome),
        "SMOKE_RELEASE": str(worker / "never-released"),
        "SMOKE_SABLE_TEST": str(SABLE_TEST),
        "SMOKE_SESSION_ID": "bare-probe",
        "SMOKE_TDD_GATE": str(TDD_GATE),
    }
    bare = subprocess.run(
        [sys.executable, str(standin)],
        cwd=worker,
        env=env,
        text=True,
        input=f"Read {dispatch} in full and execute the smoke dispatch.\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
    )
    assert bare.returncode == 86, bare.stdout
    observed = json.loads(outcome.read_text())
    assert _classify_sandbox_probe(
        worktree_write=observed["worktree_write"],
        checkout_write=observed["checkout_write"],
    ) is SandboxPosture.UNCHECKABLE
    assert observed["status"] == "UNCHECKABLE"
    assert not forbidden.exists(), "bare negative probe must clean its plant"


def test_broken_sandbox_probe_cannot_masquerade_as_a_denial():
    with pytest.raises(AssertionError, match="positive control"):
        _classify_sandbox_probe(worktree_write=False, checkout_write=False)


def test_live_store_pollution_plant_is_detected():
    observed = iter((41, 42))
    with pytest.raises(AssertionError, match="live beads store changed"):
        _guard_live_store(lambda: None, lambda: next(observed))


def test_pre_push_green_cannot_replace_the_preview_verdict(tmp_path):
    result = _exercise_preview_authority(tmp_path)
    assert result["gate_rc"] != 0
    assert result["base_before"] == result["base_after"]
    assert result["pre_push_green"] is True
    assert result["preview_conclusion"] == "failure"


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bd", "tmux", "bwrap")),
    reason="UNCHECKABLE: full fleet smoke needs bd, tmux, and bwrap",
)
def test_full_worker_push_close_and_chuck_promotion_round_trip(tmp_path):
    result = _exercise_full_round_trip(tmp_path)
    assert result["sandbox"] == SandboxPosture.ESTABLISHED.value
    assert result["worker_pushed"] is True
    assert result["bead_closed"] is True
    assert result["tdd_gate_allowed"] is True
    assert result["bead_open_before_worker_release"] is True
    assert result["promoted_preview"] == result["base_tip"]
    assert result["live_store_unchanged"] is True
