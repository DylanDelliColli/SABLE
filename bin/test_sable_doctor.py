#!/usr/bin/env python3
"""Unit tests for bin/sable-doctor (SABLE-1i6m).

Pure logic against synthetic repo/claude-dir fixture trees under tmp_path —
no dependency on the real SABLE repo or a real ~/.claude install (that's the
integration variant, which runs the real installer). Covers: manifest
construction mirrors what install.sh copies, skill-name resolution by
frontmatter, and the clean/drift/missing classification — including fixtures
modeled on the two real drift incidents this bead was filed from (a stale
installed hook missing a fix, and an installed role file missing a block that
landed in the repo).
"""
import importlib.util
import json
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader(
    "sable_doctor", str(Path(__file__).resolve().parent / "sable-doctor")
)
_SPEC = importlib.util.spec_from_loader("sable_doctor", _LOADER)
doctor = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(doctor)

BIN = Path(__file__).resolve().parent / "sable-doctor"


# --- fixture builder ----------------------------------------------------------

def make_repo(tmp_path, *, mutate=None):
    """Build a minimal repo tree matching what install.sh copies, then an
    installed ~/.claude tree that starts as an exact mirror. `mutate` is an
    optional callback(repo, claude_dir) to tamper with the installed copy
    after the mirror is built."""
    repo = tmp_path / "repo"
    claude_dir = tmp_path / "claude"

    (repo / "hooks" / "multi-manager").mkdir(parents=True)
    (repo / "hooks" / "tdd-gate.sh").write_text("#!/bin/sh\necho gate\n")
    (repo / "hooks" / "multi-manager" / "post-push-merge-notify.sh").write_text("#!/bin/sh\necho notify\n")

    (repo / "templates" / "agents").mkdir(parents=True)
    (repo / "templates" / "agents" / "sherlock.md").write_text("# sherlock agent\n")

    (repo / "templates" / "multi-manager" / "roles").mkdir(parents=True)
    (repo / "templates" / "multi-manager" / "agents.yaml").write_text("agents: []\n")
    (repo / "templates" / "multi-manager" / "roles" / "tarzan.md").write_text("# tarzan role\nblock A\n")
    (repo / "templates" / "multi-manager" / "roles" / "lincoln.md").write_text("# lincoln role\n")

    skill_dir = repo / "skills" / "columbo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: columbo\ndescription: test skill\n---\nbody\n")

    # mirror into claude_dir exactly as install.sh would
    for category, src, dst in doctor.build_manifest(repo, claude_dir):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())

    if mutate:
        mutate(repo, claude_dir)

    return repo, claude_dir


# --- build_manifest ------------------------------------------------------------

def test_build_manifest_covers_every_category(tmp_path):
    repo, claude_dir = make_repo(tmp_path)
    entries = doctor.build_manifest(repo, claude_dir)
    categories = {c for c, _, _ in entries}
    assert categories == {"hooks", "multi-manager hooks", "agent definitions",
                          "registry", "manager roles", "skill:columbo"}


def test_build_manifest_installed_paths_match_install_layout(tmp_path):
    repo, claude_dir = make_repo(tmp_path)
    entries = doctor.build_manifest(repo, claude_dir)
    by_category = {c: dst for c, _, dst in entries}
    assert by_category["hooks"] == claude_dir / "hooks" / "tdd-gate.sh"
    assert by_category["multi-manager hooks"] == claude_dir / "hooks" / "multi-manager" / "post-push-merge-notify.sh"
    assert by_category["agent definitions"] == claude_dir / "agents" / "sherlock.md"
    assert by_category["registry"] == claude_dir / "sable" / "agents.yaml"
    assert by_category["manager roles"] == claude_dir / "sable" / "roles" / "tarzan.md"
    assert by_category["skill:columbo"] == claude_dir / "skills" / "columbo" / "SKILL.md"


def test_build_manifest_only_installs_the_four_manager_roles(tmp_path):
    # a producer role fragment sitting alongside the manager roles (e.g.
    # sherlock.md) must NOT be treated as an installed manager role — those
    # feed templates/agents/*.md via sable-build-agents instead.
    repo, claude_dir = make_repo(tmp_path)
    (repo / "templates" / "multi-manager" / "roles" / "sherlock.md").write_text("# producer fragment\n")
    entries = doctor.build_manifest(repo, claude_dir)
    role_srcs = {src.name for c, src, _ in entries if c == "manager roles"}
    assert role_srcs == {"tarzan.md", "lincoln.md"}


def test_skill_install_name_prefers_frontmatter_over_dirname(tmp_path):
    skill_dir = tmp_path / "on-disk-dirname"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: real-skill-name\n---\nbody\n")
    assert doctor.skill_install_name(skill_dir) == "real-skill-name"


def test_skill_install_name_falls_back_to_dirname_without_frontmatter(tmp_path):
    skill_dir = tmp_path / "plain-dir"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("no frontmatter here\n")
    assert doctor.skill_install_name(skill_dir) == "plain-dir"


# --- check_manifest classification --------------------------------------------

def test_check_manifest_all_clean(tmp_path):
    repo, claude_dir = make_repo(tmp_path)
    results = doctor.check_manifest(doctor.build_manifest(repo, claude_dir))
    assert all(r["status"] == "clean" for r in results)
    assert len(results) == 7  # 1 hook, 1 mm hook, 1 agent def, 1 registry, 2 roles, 1 skill file


def test_check_manifest_detects_stale_hook_missing_a_fix(tmp_path):
    # models the real SABLE-4ba / f6aw incident: the installed hook is missing
    # a fix that landed in the repo copy after install — content differs, both
    # files exist.
    def mutate(repo, claude_dir):
        (claude_dir / "hooks" / "tdd-gate.sh").write_text("#!/bin/sh\necho gate\n")  # pre-fix content
        (repo / "hooks" / "tdd-gate.sh").write_text("#!/bin/sh\necho gate\necho post-fix-line\n")

    repo, claude_dir = make_repo(tmp_path, mutate=mutate)
    results = doctor.check_manifest(doctor.build_manifest(repo, claude_dir))
    hook_result = next(r for r in results if r["category"] == "hooks")
    assert hook_result["status"] == "drift"


def test_check_manifest_detects_role_file_missing_a_block(tmp_path):
    # models the real tarzan-role incident: the repo role file grew a new
    # block (SABLE-mmdt worker-cap) that the installed copy never picked up.
    def mutate(repo, claude_dir):
        (repo / "templates" / "multi-manager" / "roles" / "tarzan.md").write_text(
            "# tarzan role\nblock A\nblock B (worker-cap)\n"
        )

    repo, claude_dir = make_repo(tmp_path, mutate=mutate)
    results = doctor.check_manifest(doctor.build_manifest(repo, claude_dir))
    role_result = next(r for r in results if r["category"] == "manager roles" and "tarzan" in r["repo_path"])
    assert role_result["status"] == "drift"


def test_check_manifest_detects_missing_installed_file(tmp_path):
    def mutate(repo, claude_dir):
        (claude_dir / "agents" / "sherlock.md").unlink()

    repo, claude_dir = make_repo(tmp_path, mutate=mutate)
    results = doctor.check_manifest(doctor.build_manifest(repo, claude_dir))
    agent_result = next(r for r in results if r["category"] == "agent definitions")
    assert agent_result["status"] == "missing"


def test_check_manifest_unrelated_files_stay_clean_when_one_drifts(tmp_path):
    def mutate(repo, claude_dir):
        (claude_dir / "hooks" / "tdd-gate.sh").write_text("tampered\n")

    repo, claude_dir = make_repo(tmp_path, mutate=mutate)
    results = doctor.check_manifest(doctor.build_manifest(repo, claude_dir))
    statuses = {r["category"]: r["status"] for r in results}
    assert statuses["hooks"] == "drift"
    assert statuses["multi-manager hooks"] == "clean"
    assert statuses["agent definitions"] == "clean"


# --- pinned bins (SABLE-mkj6k) --------------------------------------------------
# Defect 2 was that install.sh silently re-symlinked a deliberately pinned
# (regular-file) spine bin, restoring the y6ik3 hot-swap hazard the pin was
# authorized to remove — with nothing warning. These cover the "pinned bins"
# manifest category: opt-in only when bin_dir is given, "unpinned" detected
# via the .sable-pinned marker (a symlink whose CONTENT matches repo HEAD
# would otherwise sha256-compare "clean", hiding exactly this defect), and
# that a drifted pinned bin's report never recommends `bash install.sh`
# (SABLE-mkj6k's guarded-remedy acceptance criterion).

def make_pinned_repo(tmp_path, *, bin_mutate=None):
    """A repo/bin fixture for the three pinned spine bins, plus a bin_dir that
    starts as an exact regular-file mirror — the pinned state an
    operator-approved de-hazard window leaves behind."""
    repo = tmp_path / "repo"
    bin_dir = tmp_path / "local-bin"
    (repo / "bin").mkdir(parents=True)
    bin_dir.mkdir()
    for name in doctor.PINNED_BIN_NAMES:
        (repo / "bin" / name).write_text(f"#!/bin/sh\necho {name}\n")
        (bin_dir / name).write_bytes((repo / "bin" / name).read_bytes())
    (bin_dir / ".sable-pinned").write_text("\n".join(doctor.PINNED_BIN_NAMES) + "\n")
    if bin_mutate:
        bin_mutate(repo, bin_dir)
    return repo, bin_dir


def test_build_manifest_includes_pinned_bins_category_when_bin_dir_given(tmp_path):
    repo, bin_dir = make_pinned_repo(tmp_path)
    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir)
    pinned = {src.name for c, src, _ in entries if c == "pinned bins"}
    assert pinned == set(doctor.PINNED_BIN_NAMES)


def test_build_manifest_omits_pinned_bins_without_bin_dir(tmp_path):
    repo, _ = make_pinned_repo(tmp_path)
    entries = doctor.build_manifest(repo, tmp_path / "claude")
    assert not any(c == "pinned bins" for c, _, _ in entries)


def test_check_manifest_pinned_bin_clean_when_regular_file_matches(tmp_path):
    repo, bin_dir = make_pinned_repo(tmp_path)
    results = doctor.check_manifest(doctor.build_manifest(repo, tmp_path / "claude", bin_dir))
    pinned_results = [r for r in results if r["category"] == "pinned bins"]
    assert len(pinned_results) == len(doctor.PINNED_BIN_NAMES)
    assert all(r["status"] == "clean" for r in pinned_results)


def test_check_manifest_pinned_bin_symlink_is_unpinned_even_with_matching_content(tmp_path):
    # The exact DEFECT 2 failure mode: install re-symlinks a pinned bin. The
    # symlink's RESOLVED content is byte-identical to repo HEAD (a symlink
    # always resolves live), so a naive sha256 compare would call this
    # "clean" — the violation is the file TYPE reverting, caught via the
    # .sable-pinned marker independent of content.
    target_name = doctor.PINNED_BIN_NAMES[0]

    def mutate(repo, bin_dir):
        target = bin_dir / target_name
        target.unlink()
        target.symlink_to(repo / "bin" / target_name)

    repo, bin_dir = make_pinned_repo(tmp_path, bin_mutate=mutate)
    results = doctor.check_manifest(doctor.build_manifest(repo, tmp_path / "claude", bin_dir))
    r = next(x for x in results if x["category"] == "pinned bins" and Path(x["repo_path"]).name == target_name)
    assert r["status"] == "unpinned"


def test_check_manifest_pinned_bin_drift_when_content_differs(tmp_path):
    target_name = doctor.PINNED_BIN_NAMES[0]

    def mutate(repo, bin_dir):
        (bin_dir / target_name).write_text("tampered\n")

    repo, bin_dir = make_pinned_repo(tmp_path, bin_mutate=mutate)
    results = doctor.check_manifest(doctor.build_manifest(repo, tmp_path / "claude", bin_dir))
    r = next(x for x in results if x["category"] == "pinned bins" and Path(x["repo_path"]).name == target_name)
    assert r["status"] == "drift"


def test_check_manifest_symlink_without_pin_marker_stays_clean(tmp_path):
    # A symlink is the ORDINARY default state for a never-pinned bin — only a
    # symlink where the marker says pinned is a violation.
    target_name = doctor.PINNED_BIN_NAMES[0]

    def mutate(repo, bin_dir):
        (bin_dir / ".sable-pinned").unlink()
        target = bin_dir / target_name
        target.unlink()
        target.symlink_to(repo / "bin" / target_name)

    repo, bin_dir = make_pinned_repo(tmp_path, bin_mutate=mutate)
    results = doctor.check_manifest(doctor.build_manifest(repo, tmp_path / "claude", bin_dir))
    r = next(x for x in results if x["category"] == "pinned bins" and Path(x["repo_path"]).name == target_name)
    assert r["status"] == "clean"


def test_check_manifest_pinned_bin_missing_entirely(tmp_path):
    target_name = doctor.PINNED_BIN_NAMES[0]

    def mutate(repo, bin_dir):
        (bin_dir / target_name).unlink()

    repo, bin_dir = make_pinned_repo(tmp_path, bin_mutate=mutate)
    results = doctor.check_manifest(doctor.build_manifest(repo, tmp_path / "claude", bin_dir))
    r = next(x for x in results if x["category"] == "pinned bins" and Path(x["repo_path"]).name == target_name)
    assert r["status"] == "missing"


# --- guarded remedy (SABLE-mkj6k acceptance criterion) --------------------------
# A drifted GUARDED file's report must NOT tell an agent to run `bash
# install.sh`, and must name the safe per-file path instead; a drifted
# UNGUARDED file must still get the normal remedy (over-suppression would
# hide real drift, which is its own regression).

def test_render_text_report_guarded_pinned_bin_does_not_recommend_install_sh(tmp_path, capsys):
    target_name = doctor.PINNED_BIN_NAMES[0]

    def mutate(repo, bin_dir):
        target = bin_dir / target_name
        target.unlink()
        target.symlink_to(repo / "bin" / target_name)

    repo, bin_dir = make_pinned_repo(tmp_path, bin_mutate=mutate)
    results = doctor.check_manifest(doctor.build_manifest(repo, tmp_path / "claude", bin_dir))
    doctor.render_text_report(results)
    out = capsys.readouterr().out
    assert "bash install.sh" not in out
    assert "cp " in out
    assert target_name in out


def test_render_text_report_unguarded_drift_still_recommends_install_sh(tmp_path, capsys):
    def mutate(repo, claude_dir):
        (claude_dir / "hooks" / "tdd-gate.sh").write_text("tampered\n")

    repo, claude_dir = make_repo(tmp_path, mutate=mutate)
    results = doctor.check_manifest(doctor.build_manifest(repo, claude_dir))
    doctor.render_text_report(results)
    out = capsys.readouterr().out
    assert "bash install.sh" in out


def test_quiet_mode_guarded_pinned_bin_does_not_recommend_install_sh(tmp_path, capsys):
    target_name = doctor.PINNED_BIN_NAMES[0]

    def mutate(repo, bin_dir):
        target = bin_dir / target_name
        target.unlink()
        target.symlink_to(repo / "bin" / target_name)

    repo, bin_dir = make_pinned_repo(tmp_path, bin_mutate=mutate)
    rc = doctor.main([
        "--repo", str(repo), "--claude-dir", str(tmp_path / "claude"),
        "--bin-dir", str(bin_dir), "--quiet",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert "bash install.sh" not in captured.err
    assert captured.out == ""


def test_quiet_mode_unguarded_drift_still_recommends_install_sh(tmp_path, capsys):
    def mutate(repo, claude_dir):
        (claude_dir / "hooks" / "tdd-gate.sh").write_text("tampered\n")

    repo, claude_dir = make_repo(tmp_path, mutate=mutate)
    rc = doctor.main([
        "--repo", str(repo), "--claude-dir", str(claude_dir),
        "--bin-dir", str(tmp_path / "empty-bin-dir"), "--quiet",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "bash install.sh" in captured.err


# --- resolve_claude_dir ---------------------------------------------------------

def test_resolve_claude_dir_explicit_arg_wins():
    assert doctor.resolve_claude_dir("/explicit", {"CLAUDE_USER_DIR": "/env"}) == Path("/explicit")


def test_resolve_claude_dir_env_override():
    assert doctor.resolve_claude_dir(None, {"CLAUDE_USER_DIR": "/env"}) == Path("/env")


def test_resolve_claude_dir_default_is_home_claude():
    result = doctor.resolve_claude_dir(None, {})
    assert result == Path.home() / ".claude"


# --- resolve_project_claude_dir (--project sugar, git-common-dir) ------------

def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_doctor_project_flag_resolves_claude_dir_to_repo_root_via_git_common_dir(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _git("init", "-q", cwd=project)
    (project / ".claude").mkdir()

    resolved = doctor.resolve_project_claude_dir(str(project))
    assert resolved == (project / ".claude").resolve()


def test_doctor_project_flag_from_linked_worktree_targets_main_checkout_claude_dir(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _git("init", "-q", cwd=project)
    (project / ".claude").mkdir()
    (project / "README.md").write_text("hi\n")
    _git("add", "README.md", cwd=project)
    _git("-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "init", cwd=project)

    linked = tmp_path / "linked-worktree"
    _git("worktree", "add", "-q", "-b", "feature", str(linked), cwd=project)

    # the linked worktree has NO .claude of its own — resolution must still
    # land on the MAIN checkout's install, not the linked worktree's (missing) one.
    resolved = doctor.resolve_project_claude_dir(str(linked))
    assert resolved == (project / ".claude").resolve()


def test_doctor_project_raises_named_error_when_not_a_git_worktree(tmp_path):
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    with pytest.raises(doctor.NoProjectInstallError):
        doctor.resolve_project_claude_dir(str(plain_dir))


def test_doctor_project_raises_named_error_when_claude_dir_absent(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _git("init", "-q", cwd=project)
    # no .claude directory created — no project install to check
    with pytest.raises(doctor.NoProjectInstallError):
        doctor.resolve_project_claude_dir(str(project))


# --- main(): exit codes + json shape (still pure-python, no subprocess) -------

def test_main_returns_zero_and_prints_clean_when_matching(tmp_path, capsys):
    repo, claude_dir = make_repo(tmp_path)
    rc = doctor.main(["--repo", str(repo), "--claude-dir", str(claude_dir)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "clean" in out


def test_main_returns_one_and_reports_drift(tmp_path, capsys):
    def mutate(repo, claude_dir):
        (claude_dir / "hooks" / "tdd-gate.sh").write_text("tampered\n")

    repo, claude_dir = make_repo(tmp_path, mutate=mutate)
    rc = doctor.main(["--repo", str(repo), "--claude-dir", str(claude_dir)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "DRIFT DETECTED" in out
    assert "tdd-gate.sh" in out
    assert "bash install.sh" in out


def test_main_json_output_is_valid_and_matches_status(tmp_path, capsys):
    def mutate(repo, claude_dir):
        (claude_dir / "agents" / "sherlock.md").unlink()

    repo, claude_dir = make_repo(tmp_path, mutate=mutate)
    rc = doctor.main(["--repo", str(repo), "--claude-dir", str(claude_dir), "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 1
    assert payload["clean"] is False
    missing = [r for r in payload["results"] if r["status"] == "missing"]
    assert len(missing) == 1
    assert missing[0]["category"] == "agent definitions"


def test_main_quiet_suppresses_output_when_clean(tmp_path, capsys):
    repo, claude_dir = make_repo(tmp_path)
    rc = doctor.main(["--repo", str(repo), "--claude-dir", str(claude_dir), "--quiet"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""


def test_main_quiet_prints_one_line_to_stderr_when_drifted(tmp_path, capsys):
    def mutate(repo, claude_dir):
        (claude_dir / "hooks" / "tdd-gate.sh").write_text("tampered\n")

    repo, claude_dir = make_repo(tmp_path, mutate=mutate)
    rc = doctor.main(["--repo", str(repo), "--claude-dir", str(claude_dir), "--quiet"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "drifted" in captured.err
    assert "bash install.sh" in captured.err


# --- worker cap line (SABLE-61dy) ----------------------------------------------
# SABLE_MAX_WORKERS is invisible to operators until sable-spawn-worker refuses
# a spawn at cap. This adds an additive informational line — sourced from the
# same sable_pane_lib.worker_cap()/WORKER_CAP_DEFAULT that gates spawns
# (sable-view, sable-spawn-worker) — so the reported value can never drift
# from the gate. Must NOT change the clean/drift verdict or exit code.

def test_doctor_reports_worker_cap_default(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SABLE_MAX_WORKERS", raising=False)
    repo, claude_dir = make_repo(tmp_path)
    rc = doctor.main(["--repo", str(repo), "--claude-dir", str(claude_dir)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "worker cap: 8 (default)" in out


def test_doctor_reports_worker_cap_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SABLE_MAX_WORKERS", "3")
    repo, claude_dir = make_repo(tmp_path)
    rc = doctor.main(["--repo", str(repo), "--claude-dir", str(claude_dir)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "worker cap: 3 (env SABLE_MAX_WORKERS)" in out


def test_doctor_reports_worker_cap_line_even_when_drifted(tmp_path, monkeypatch, capsys):
    # additive-only: the cap line must appear alongside DRIFT DETECTED without
    # altering the verdict or exit code.
    monkeypatch.delenv("SABLE_MAX_WORKERS", raising=False)

    def mutate(repo, claude_dir):
        (claude_dir / "hooks" / "tdd-gate.sh").write_text("tampered\n")

    repo, claude_dir = make_repo(tmp_path, mutate=mutate)
    rc = doctor.main(["--repo", str(repo), "--claude-dir", str(claude_dir)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "DRIFT DETECTED" in out
    assert "worker cap: 8 (default)" in out


# --- pinned snapshot bins (SABLE-9boz4) -----------------------------------------
# Follow-up to SABLE-mkj6k: a regular-file pin only works for a bin with zero
# repo-local imports. sable-spawn-worker / sable-msg import sibling modules
# (sable_pane_lib.py, ...), so a naive copy severs the import. The pin unit
# here is a whole versioned snapshot directory, not a single file — these
# cover python_sibling_importing_bins() detection and _check_snapshot_pin()'s
# status classification, including the "sha-compare snapshot vs repo AT ITS
# PINNED SHA, not current HEAD" acceptance criterion (a plain sha256-vs-HEAD
# compare would agree-while-wrong for a deliberately older pin).

def make_snapshot_repo(tmp_path):
    """A real git repo with bin/sable-importer (imports sable_helper_lib.py)
    plus an ordinary no-import tool, committed so _check_snapshot_pin can
    `git show <sha>:bin/<name>` against it."""
    repo = tmp_path / "repo"
    bin_dir = repo / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "sable_helper_lib.py").write_text("MARK = 'v1'\n")
    (bin_dir / "sable-importer").write_text(
        "#!/usr/bin/env python3\nfrom sable_helper_lib import MARK\nprint(MARK)\n"
    )
    (bin_dir / "sable-plain").write_text("#!/usr/bin/env bash\necho plain\n")

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"], cwd=repo,
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    return repo, bin_dir, sha


def pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest, *, tamper=False):
    """Simulates what `sable-bin-install --pin-snapshot` does: copy bin/ as
    one unit into a versioned ~/.local/lib/sable-<sha>/ dir, then symlink the
    entry point into it."""
    snapshot_dir = tmp_path / "lib" / f"sable-{sha}"
    snapshot_dir.mkdir(parents=True)
    for f in bin_dir.iterdir():
        if f.is_file():
            (snapshot_dir / f.name).write_bytes(f.read_bytes())
    if tamper:
        (snapshot_dir / "sable-importer").write_text("# hand-tampered\n")

    bin_dir_dest.mkdir(parents=True, exist_ok=True)
    (bin_dir_dest / "sable-importer").symlink_to(snapshot_dir / "sable-importer")
    return snapshot_dir


def test_python_sibling_importing_bins_detects_only_the_importer(tmp_path):
    repo, bin_dir, _sha = make_snapshot_repo(tmp_path)
    assert doctor.python_sibling_importing_bins(repo) == ["sable-importer"]


def test_build_manifest_includes_pinned_snapshot_bins_category(tmp_path):
    repo, bin_dir, _sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    names = {src.name for c, src, _ in entries if c == "pinned snapshot bins"}
    assert names == {"sable-importer"}


def test_check_manifest_snapshot_pin_missing_when_never_installed(tmp_path):
    repo, bin_dir, _sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    bin_dir_dest.mkdir()
    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if x["category"] == "pinned snapshot bins")
    assert r["status"] == "missing"


def test_check_manifest_snapshot_pin_clean_for_ordinary_unpinned_symlink(tmp_path):
    # the default un-pinned state (symlink straight into the live repo bin/)
    # is NOT a defect — pinning is opt-in, same as the mkj6k pinned-bins category.
    repo, bin_dir, _sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    bin_dir_dest.mkdir()
    (bin_dir_dest / "sable-importer").symlink_to(bin_dir / "sable-importer")
    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if x["category"] == "pinned snapshot bins")
    assert r["status"] == "clean"


def test_check_manifest_snapshot_pin_clean_when_content_matches_pinned_sha(tmp_path):
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest)
    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if x["category"] == "pinned snapshot bins")
    assert r["status"] == "clean"


def test_check_manifest_snapshot_pin_broken_copy_pin_when_regular_file(tmp_path):
    # a python-importing bin pinned as a BARE regular file has no sibling lib
    # beside it -- the exact ImportError hazard this bead exists to prevent.
    repo, bin_dir, _sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    bin_dir_dest.mkdir()
    (bin_dir_dest / "sable-importer").write_bytes((bin_dir / "sable-importer").read_bytes())
    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if x["category"] == "pinned snapshot bins")
    assert r["status"] == "broken-copy-pin"


def test_check_manifest_snapshot_pin_drift_when_snapshot_hand_tampered(tmp_path):
    # the failure class this bead exists to prevent: a second, untracked
    # drift surface INSIDE the snapshot dir itself. A plain sha256-vs-current-
    # HEAD compare would be the wrong check (the pin is deliberately frozen at
    # an older sha) -- this must compare against the repo AT THE PINNED SHA.
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest, tamper=True)
    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if x["category"] == "pinned snapshot bins")
    assert r["status"] == "snapshot-drift"
    assert r["detail"] == "sable-importer"


# --- REVISE: doctor must compare the WHOLE snapshot unit, not just the entry
# point (optimus review, 2026-07-21) -----------------------------------------
# _check_snapshot_pin used to read ONLY the resolved entry-point file's bytes
# -- dst.read_bytes() -- and never looked at any other file inside the
# snapshot directory. So hand-tampering, adding, or removing a SIBLING module
# (e.g. sable_helper_lib.py, the very file that justifies a directory-shaped
# pin unit in the first place) reported "clean": a false green in the drift
# detector whose entire job is to not have one. These pair with the
# entry-point tamper case above -- an instrument that catches one and not the
# other is the bug the pair exists to prove is fixed.

def test_check_manifest_snapshot_pin_drift_when_sibling_lib_tampered(tmp_path):
    # sibling_module_tamper_is_detected (unit-level pair to the integration
    # test of the same name in hooks/test/test-spine-pinning.sh): tamper
    # sable_helper_lib.py -- a sibling module, NEVER the entry point
    # sable-importer -- and assert it is still caught, naming the file.
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    snapshot_dir = pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest)
    (snapshot_dir / "sable_helper_lib.py").write_text("MARK = 'tampered'\n")

    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if x["category"] == "pinned snapshot bins")
    assert r["status"] == "snapshot-drift"
    assert r["detail"] == "sable_helper_lib.py"


def test_check_manifest_snapshot_pin_drift_when_snapshot_gains_a_file(tmp_path):
    # a file physically present in the snapshot that the pinned sha never
    # shipped is drift too -- an untracked addition inside the pin unit.
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    snapshot_dir = pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest)
    (snapshot_dir / "sable_extra_lib.py").write_text("# not part of the pinned sha\n")

    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if x["category"] == "pinned snapshot bins")
    assert r["status"] == "snapshot-drift"
    assert r["detail"] == "sable_extra_lib.py"


def test_check_manifest_snapshot_pin_drift_when_snapshot_loses_a_file(tmp_path):
    # a file the pinned sha shipped with that the snapshot has since lost --
    # the shape a future bin/ module split takes (SABLE-jd5fj.3): the pin
    # unit and the implementation unit stop being coextensive, and that must
    # read as drift, not agreement.
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    snapshot_dir = pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest)
    (snapshot_dir / "sable_helper_lib.py").unlink()

    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if x["category"] == "pinned snapshot bins")
    assert r["status"] == "snapshot-drift"
    assert r["detail"] == "sable_helper_lib.py"


def test_render_text_report_snapshot_drift_names_the_drifted_sibling_file(tmp_path, capsys):
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    snapshot_dir = pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest)
    (snapshot_dir / "sable_helper_lib.py").write_text("MARK = 'tampered'\n")

    results = doctor.check_manifest(doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest))
    doctor.render_text_report(results)
    out = capsys.readouterr().out
    assert "SNAPSHOT-DRIFT" in out
    assert "sable_helper_lib.py" in out


def test_check_manifest_snapshot_pin_clean_even_when_pinned_sha_predates_head(tmp_path):
    # the pin is deliberately frozen at an OLD sha while the repo moves on --
    # that must NOT be reported as drift (drift means the SNAPSHOT no longer
    # matches what it claims to be pinned to, not "repo has since changed").
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest)

    # repo moves on after the pin was taken
    (bin_dir / "sable-importer").write_text(
        "#!/usr/bin/env python3\nfrom sable_helper_lib import MARK\nprint(MARK, 'v2')\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "v2"], cwd=repo, check=True)

    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if x["category"] == "pinned snapshot bins")
    assert r["status"] == "clean"


def test_render_text_report_snapshot_drift_never_recommends_install_sh(tmp_path, capsys):
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest, tamper=True)
    results = doctor.check_manifest(doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest))
    doctor.render_text_report(results)
    out = capsys.readouterr().out
    assert "bash install.sh" not in out
    assert "--pin-snapshot" in out
    assert "sable-importer" in out


def test_quiet_mode_snapshot_drift_never_recommends_install_sh(tmp_path, capsys):
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest, tamper=True)
    rc = doctor.main([
        "--repo", str(repo), "--claude-dir", str(tmp_path / "claude"),
        "--bin-dir", str(bin_dir_dest), "--quiet",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert "bash install.sh" not in captured.err
    assert captured.out == ""
