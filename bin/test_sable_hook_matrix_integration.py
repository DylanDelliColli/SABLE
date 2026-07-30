#!/usr/bin/env python3
"""Integration tests for `sable-hook-matrix --wiring` (SABLE-31b5l).

*** THE POINT OF THIS FILE IS THAT IT MEASURES INVOCATION INSTEAD OF READING
ABOUT IT. *** The bead exists because four people reasoned to a confident,
shared, wrong conclusion from hook SOURCE — reading a hook's body tells you
what it would do, never that it runs — so the checker built to prevent that is
itself checked against real pushes, real settings files and real installs, not
against fixtures that agree with it by construction.

Real composition throughout: real `git init` / `git push` over a real file://
bare remote, real git-hook dispatch via a real core.hooksPath, real JSON
settings files on disk, real symlinks and real copies, and the real CLI in a
real subprocess. Nothing here is mocked; a mocked registry would prove only
that the mock matches the code that reads it.

HERMETIC AND SAFE BY CONSTRUCTION (dispatch §0):
  * Every git operation happens inside pytest's tmp_path. The only remote is a
    file:// bare repo created in that same tmp_path — never `origin`, never the
    shared checkout at /home/ddc/dev-env/SABLE.
  * HOME and GIT_CONFIG_GLOBAL are redirected to scratch paths for every
    subprocess, so nothing can read or write the live ~/.claude or ~/.gitconfig
    (SABLE-2avau: an installer silently targeting the live ~/.claude with no
    escape; SABLE-k0nvp: a suite polluting the real ~/.local twice).
  * No installs are run and no host load is manufactured.

WHY THE PUSH EXPERIMENT IS NOT THE PRIMARY CHECK. The bead's original spec made
a scratch-clone `git push` the deciding measurement. It cannot be: the hook in
question is dispatched by CLAUDE CODE, not by git, so a scratch clone carrying
no settings.json would observe nothing and appear to confirm the wrong answer —
a false negative wearing the clothes of an experiment. It is kept, in
test_push_rebase_wiring_observed, for what it CAN establish: that the
git-hooks registry really does or does not carry a given hook, measured by
whether a tree moved, in both polarities.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parent / "sable-hook-matrix"
_LOADER = SourceFileLoader("sable_hook_matrix_integration", str(_TOOL))
_SPEC = importlib.util.spec_from_loader("sable_hook_matrix_integration", _LOADER)
shm = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(shm)

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git not on PATH; this suite measures real git-hook dispatch",
)


# ---------------------------------------------------------------------------
# helpers — every subprocess gets a scratch HOME and a scratch global gitconfig
# ---------------------------------------------------------------------------

def _env(home: Path) -> dict:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["GIT_CONFIG_GLOBAL"] = str(home / ".gitconfig")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def _git(cwd: Path, *args, home: Path, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args], env=_env(home),
                          capture_output=True, text=True, check=check)


def _new_repo(root: Path, home: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], env=_env(home),
                   check=True, capture_output=True)
    _git(root, "checkout", "-q", "-b", "main", home=home)
    _git(root, "config", "user.email", "worker@example.invalid", home=home)
    _git(root, "config", "user.name", "SABLE test worker", home=home)
    return root


def _commit(repo: Path, home: Path, name: str, body: str = "x\n"):
    (repo / name).write_text(body)
    _git(repo, "add", name, home=home)
    _git(repo, "commit", "-q", "-m", f"add {name}", home=home)


def _write_hook(path: Path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


# ---------------------------------------------------------------------------
# 1. THE PRIMARY CHECK — a hook wired ONLY by settings.json, on real files.
#    This is the exact case the bead got wrong.
# ---------------------------------------------------------------------------

@requires_git
def test_settings_wired_hook_is_detected(tmp_path):
    """Real scratch HOME, real settings.json registering a marker hook on the
    Bash matcher, and NO git hook of that name anywhere. The matrix must report
    it WIRED and NAME claude-settings — proving it consults more than
    core.hooksPath, which is the whole failure this bead documents."""
    home = tmp_path / "home"
    repo = _new_repo(tmp_path / "repo", home)

    # The artifact, in the same layout this repo uses.
    _write_hook(repo / "hooks" / "multi-manager" / "marker-hook.sh",
                "#!/usr/bin/env bash\nexit 0\n")
    # A sibling that nothing wires — the negative control, scanned in the SAME
    # run so a tool hardcoded to answer ACTIVE cannot pass this test.
    _write_hook(repo / "hooks" / "multi-manager" / "unwired-hook.sh",
                "#!/usr/bin/env bash\nexit 0\n")

    # A real core.hooksPath that is silent about both — exactly this repo's
    # .beads/hooks shape: pure beads integration, no delegation.
    beads = repo / ".beads" / "hooks"
    _write_hook(beads / "pre-push",
                "#!/usr/bin/env sh\n"
                "# --- BEGIN BEADS INTEGRATION v1.0.0 ---\n"
                "if command -v bd >/dev/null 2>&1; then bd hooks run pre-push \"$@\"; fi\n"
                "# --- END BEADS INTEGRATION v1.0.0 ---\n")
    _git(repo, "config", "core.hooksPath", str(beads), home=home)

    # The real settings.json, and the real installed copy it points at.
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{
        "matcher": "Bash",
        "hooks": [{"type": "command",
                   "command": "bash ~/.claude/hooks/multi-manager/marker-hook.sh",
                   "timeout": 5000}],
    }]}}, indent=2))
    _write_hook(home / ".claude" / "hooks" / "multi-manager" / "marker-hook.sh",
                "#!/usr/bin/env bash\nexit 0\n")

    pathdir = tmp_path / "pathbin"
    pathdir.mkdir()
    reports = {r.name: r for r in shm.build_wiring_matrix(
        repo_root=repo, home=str(home), path_env=str(pathdir),
        settings_paths=[settings])}

    wired = reports["marker-hook.sh"]
    assert wired.status == shm.W_ACTIVE, wired.status
    assert shm.REG_CLAUDE_SETTINGS in wired.registry_names()
    assert shm.REG_GIT_HOOKS not in wired.registry_names()
    w = [x for x in wired.wirings if x.registry == shm.REG_CLAUDE_SETTINGS][0]
    assert w.detail == "PreToolUse:Bash"
    assert w.site == str(settings)
    assert w.target == str(home / ".claude/hooks/multi-manager/marker-hook.sh")
    assert w.target_exists is True

    # Every primary registry was really consulted, so the verdict has scope.
    for reg in shm.PRIMARY_REGISTRIES:
        assert reg in wired.registries_checked
    assert not wired.registries_skipped

    # NEGATIVE CONTROL — same repo, same settings file, same scan.
    other = reports["unwired-hook.sh"]
    assert other.status == shm.W_UNWIRED, other.status
    assert other.registry_names() == ()
    assert other.exists is True          # present, just not wired


# ---------------------------------------------------------------------------
# 2. THE EXPERIMENT — a REAL push over a REAL file:// remote, and whether a
#    tree actually moved. Measured, not read.
# ---------------------------------------------------------------------------

@requires_git
def test_push_rebase_wiring_observed(tmp_path):
    """Push for real and watch whether a working tree advances.

    Two scratch clones of one file:// bare remote. `pusher` pushes; `target` is
    deliberately left a commit behind and is the tree a push-time rebase hook
    would advance — the same shape as pre-push-rebase-test.sh, which operates on
    a `-C` target directory rather than the shell cwd.

    Polarity A: core.hooksPath does NOT chain to the multi-manager hook (this
    repo's actual .beads/hooks shape). Expect the target tree UNMOVED.
    Polarity B: it DOES chain. Expect the target tree ADVANCED.

    Both are asserted against the tool's own git-hooks verdict, so the
    instrument is pinned to an observed invocation in both directions rather
    than to a reading of the hook's body.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True)

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], env=_env(home),
                   check=True, capture_output=True)
    # A scratch GIT_CONFIG_GLOBAL means init.defaultBranch is unset, so the bare
    # repo's HEAD points at 'master' while we push 'main' — clones would then
    # check out nothing. Point HEAD at the branch that will actually exist.
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main", home=home)
    remote_url = f"file://{bare}"

    seed = _new_repo(tmp_path / "seed", home)
    _commit(seed, home, "seed.txt")
    _git(seed, "remote", "add", "origin", remote_url, home=home)
    _git(seed, "push", "-q", "origin", "main", home=home)

    def clone(name: str) -> Path:
        dest = tmp_path / name
        subprocess.run(["git", "clone", "-q", remote_url, str(dest)],
                       env=_env(home), check=True, capture_output=True)
        _git(dest, "config", "user.email", "worker@example.invalid", home=home)
        _git(dest, "config", "user.name", "SABLE test worker", home=home)
        return dest

    def run_polarity(name: str, chain: bool):
        pusher = clone(f"{name}-pusher")
        target = clone(f"{name}-target")     # the tree a rebase hook would move
        target_before = _git(target, "rev-parse", "HEAD", home=home).stdout.strip()

        # The multi-manager-style hook: it advances the TARGET tree, not its own
        # — the same shape as pre-push-rebase-test.sh, which operates on a `-C`
        # target directory rather than the shell cwd.
        #
        # It fetches from the PUSHER, not from origin, and that is not an
        # arbitrary choice: a pre-push hook runs BEFORE the remote ref moves, so
        # at this instant origin/main is still the old commit and a hook reading
        # the remote would advance the target to nothing. Measured here, not
        # assumed — the first version of this test fetched origin, watched the
        # hook fire, and still saw an unmoved tree.
        rebase_hook = pusher / "hooks" / "multi-manager" / "rebase-probe.sh"
        _write_hook(rebase_hook,
                    "#!/usr/bin/env bash\n"
                    f'git -C "{target}" fetch -q "{pusher}" main\n'
                    f'git -C "{target}" reset -q --hard FETCH_HEAD\n'
                    f'echo fired >> "{tmp_path}/{name}-fired.txt"\n')

        beads = pusher / ".beads" / "hooks"
        body = ("#!/usr/bin/env sh\n"
                "# --- BEGIN BEADS INTEGRATION v1.0.0 ---\n"
                "if command -v bd >/dev/null 2>&1; then true; fi\n"
                "# --- END BEADS INTEGRATION v1.0.0 ---\n")
        if chain:
            body += f'bash "{rebase_hook}" "$@"\n'
        _write_hook(beads / "pre-push", body)
        _git(pusher, "config", "core.hooksPath", str(beads), home=home)

        _commit(pusher, home, f"{name}.txt")
        push = _git(pusher, "push", "origin", "main", home=home)
        assert push.returncode == 0, push.stderr

        target_after = _git(target, "rev-parse", "HEAD", home=home).stdout.strip()
        fired = (tmp_path / f"{name}-fired.txt").exists()

        pathdir = tmp_path / f"{name}-pathbin"
        pathdir.mkdir()
        reports = {r.name: r for r in shm.build_wiring_matrix(
            repo_root=pusher, home=str(home), path_env=str(pathdir),
            settings_paths=[])}
        return target_before, target_after, fired, reports["rebase-probe.sh"]

    # --- Polarity A: no chain. This repo's real shape. -----------------------
    before_a, after_a, fired_a, report_a = run_polarity("nochain", chain=False)
    assert fired_a is False, "hook ran despite core.hooksPath not chaining to it"
    assert after_a == before_a, "target tree advanced with no wiring to advance it"
    assert shm.REG_GIT_HOOKS not in report_a.registry_names()
    assert shm.REG_GIT_HOOKS in report_a.registries_checked
    assert report_a.status == shm.W_UNWIRED

    # --- Polarity B: chained. The mandatory positive control. ---------------
    before_b, after_b, fired_b, report_b = run_polarity("chained", chain=True)
    assert fired_b is True, "chained pre-push did not run on a real push"
    assert after_b != before_b, "chained hook ran but the target tree did not move"
    assert shm.REG_GIT_HOOKS in report_b.registry_names()
    assert report_b.status == shm.W_ACTIVE
    assert any("chained from git hook 'pre-push'" in w.detail
               for w in report_b.wirings)

    # *** THE LESSON, ASSERTED. *** Polarity A is a TRUE negative about the
    # git-hooks registry and would be a FALSE negative about wiring in general:
    # the identical artifact, unchanged on disk, reads WIRED the moment a
    # settings.json names it. A registry-blind "no" is worth nothing.
    settings = tmp_path / "late-settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{
        "matcher": "Bash",
        "hooks": [{"type": "command",
                   "command": f"bash {tmp_path}/nochain-pusher/hooks/multi-manager/rebase-probe.sh"}],
    }]}}))
    pathdir = tmp_path / "late-pathbin"
    pathdir.mkdir()
    again = {r.name: r for r in shm.build_wiring_matrix(
        repo_root=tmp_path / "nochain-pusher", home=str(home),
        path_env=str(pathdir), settings_paths=[settings])}["rebase-probe.sh"]
    assert again.status == shm.W_ACTIVE
    assert again.registry_names() == (shm.REG_CLAUDE_SETTINGS,)


# ---------------------------------------------------------------------------
# 3. INSTALL SHAPE against real files — the second finding (dispatch §4)
# ---------------------------------------------------------------------------

@requires_git
def test_copy_install_goes_stale_while_symlink_install_cannot(tmp_path):
    """A COPY is byte-identical only until the next landing edits the repo file,
    and nothing announces the moment it stops being. Real files, real symlink,
    real edit — the drift is produced, not asserted about."""
    home = tmp_path / "home"
    repo = _new_repo(tmp_path / "repo", home)
    copied = repo / "hooks" / "multi-manager" / "copied-hook.sh"
    linked = repo / "hooks" / "multi-manager" / "linked-hook.sh"
    _write_hook(copied, "#!/usr/bin/env bash\necho v1\n")
    _write_hook(linked, "#!/usr/bin/env bash\necho v1\n")

    inst = home / ".claude" / "hooks" / "multi-manager"
    inst.mkdir(parents=True)
    shutil.copy2(str(copied), str(inst / "copied-hook.sh"))     # install.sh shape
    (inst / "linked-hook.sh").symlink_to(linked)                # sable-bin shape

    pathdir = tmp_path / "pathbin"
    pathdir.mkdir()

    def scan():
        return {r.name: r for r in shm.build_wiring_matrix(
            repo_root=repo, home=str(home), path_env=str(pathdir),
            settings_paths=[])}

    before = scan()
    assert before["copied-hook.sh"].install.shape == shm.SHAPE_COPY
    assert before["copied-hook.sh"].install.content == shm.CONTENT_MATCHES
    assert before["copied-hook.sh"].install.tracks_repo is False
    assert before["linked-hook.sh"].install.shape == shm.SHAPE_SYMLINK
    assert before["linked-hook.sh"].install.tracks_repo is True

    # Land a change in the repo — the event the byte-identity does not survive.
    copied.write_text("#!/usr/bin/env bash\necho v2\n")
    linked.write_text("#!/usr/bin/env bash\necho v2\n")

    after = scan()
    assert after["copied-hook.sh"].install.content == shm.CONTENT_DRIFTED
    assert after["linked-hook.sh"].install.content == shm.CONTENT_MATCHES
    assert (inst / "linked-hook.sh").read_text() == "#!/usr/bin/env bash\necho v2\n"


# ---------------------------------------------------------------------------
# 4. THE REAL CLI, in a real subprocess
# ---------------------------------------------------------------------------

@requires_git
def test_cli_reports_registry_per_hook_end_to_end(tmp_path):
    """The tool as an operator actually runs it: real argv, real process, real
    stdout — with a scratch HOME so it cannot read the live ~/.claude."""
    home = tmp_path / "home"
    repo = _new_repo(tmp_path / "repo", home)
    _write_hook(repo / "hooks" / "multi-manager" / "wired.sh", "#!/bin/bash\nexit 0\n")
    _write_hook(repo / "hooks" / "multi-manager" / "orphan.sh", "#!/bin/bash\nexit 0\n")

    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": {"PostToolUse": [{
        "matcher": "Bash",
        "hooks": [{"type": "command",
                   "command": "bash ~/.claude/hooks/multi-manager/wired.sh"}],
    }]}}))
    _write_hook(home / ".claude" / "hooks" / "multi-manager" / "wired.sh",
                "#!/bin/bash\nexit 0\n")
    pathdir = tmp_path / "pathbin"
    pathdir.mkdir()

    proc = subprocess.run(
        [sys.executable, str(_TOOL), "--wiring", "--json",
         "--repo", str(repo), "--home", str(home), "--path-env", str(pathdir)],
        env=_env(home), capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)

    reports = {r["name"]: r for r in payload["reports"]}
    assert reports["wired.sh"]["status"] == "ACTIVE"
    assert reports["wired.sh"]["registries_wiring_it"] == ["claude-settings"]
    assert reports["wired.sh"]["wirings"][0]["detail"] == "PostToolUse:Bash"
    assert reports["orphan.sh"]["status"] == "PRESENT-BUT-UNWIRED"
    assert reports["orphan.sh"]["registries_wiring_it"] == []

    # Scope is machine-readable, not just prose in the text renderer.
    for reg in shm.PRIMARY_REGISTRIES:
        assert reg in reports["orphan.sh"]["registries_checked"]
    assert reports["orphan.sh"]["registries_not_checked"] == []

    # And the human report says which registries it checked, in both modes.
    text = subprocess.run(
        [sys.executable, str(_TOOL), "--wiring",
         "--repo", str(repo), "--home", str(home), "--path-env", str(pathdir)],
        env=_env(home), capture_output=True, text=True, check=False)
    assert text.returncode == 0, text.stderr
    assert "PRESENT-BUT-UNWIRED" in text.stdout
    assert "claude-settings" in text.stdout
    assert "HEURISTIC" in text.stdout


# ---------------------------------------------------------------------------
# 5. A REAL malformed settings file must not read as a clean negative
# ---------------------------------------------------------------------------

@requires_git
def test_real_unparseable_settings_forces_indeterminate(tmp_path):
    """Half-written JSON is a real state — an interrupted edit, a merge
    conflict. It yields no hits, and no-hits is indistinguishable from
    genuinely-unwired unless the tool refuses to guess."""
    home = tmp_path / "home"
    repo = _new_repo(tmp_path / "repo", home)
    _write_hook(repo / "hooks" / "multi-manager" / "h.sh", "#!/bin/bash\nexit 0\n")

    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('{"hooks": {"PreToolUse": [{"matcher": "Bash",')  # truncated

    pathdir = tmp_path / "pathbin"
    pathdir.mkdir()
    r = {x.name: x for x in shm.build_wiring_matrix(
        repo_root=repo, home=str(home), path_env=str(pathdir),
        settings_paths=[settings])}["h.sh"]

    assert r.status == shm.W_INDETERMINATE
    assert any(reg == shm.REG_CLAUDE_SETTINGS for reg, _ in r.registries_skipped)
    assert "NOT CHECKED" in r.scope_line()

    # CONTROL: repair the file and the same scan issues a real verdict.
    settings.write_text(json.dumps({"hooks": {}}))
    r2 = {x.name: x for x in shm.build_wiring_matrix(
        repo_root=repo, home=str(home), path_env=str(pathdir),
        settings_paths=[settings])}["h.sh"]
    assert r2.status == shm.W_UNWIRED
    assert not r2.registries_skipped


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
