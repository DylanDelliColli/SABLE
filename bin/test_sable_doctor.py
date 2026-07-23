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
REAL_BIN_INSTALL = Path(__file__).resolve().parent / "sable-bin-install"


def _install_real_classifier(repo: Path):
    """Copy the REAL sable-bin-install into a synthetic repo/bin fixture so
    doctor.classify_bin_shape's subprocess call (SABLE-rucuh: doctor now
    defers to bin-install's own --classify instead of re-implementing the
    detection) has something authoritative to consult, exactly as it would
    in the real repo. Without this, every fixture's shape is "undetermined"
    (no script present), which is a real, distinct code path but not the one
    most of these tests are about."""
    dest = repo / "bin" / "sable-bin-install"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(REAL_BIN_INSTALL.read_bytes())
    dest.chmod(0o755)


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


# --- installed hook syntax check (SABLE-9rj7m) ----------------------------------
# hooks/test/test-tree-claim.sh's `bash -n` tripwire only ever checks the REPO
# copy of a hook, never the copy-installed one at ~/.claude/hooks/ (install.sh
# / sable-orchestration-install), so a truncated, half-written, or hand-edited
# installed copy is invisible to every test in that suite. This is the doctor-
# side proactive check: a second predicate over the same manifest entries the
# drift walk already builds, not a new traversal.

def test_installed_hook_syntax_error_is_reported(tmp_path, capsys):
    def mutate(repo, claude_dir):
        # unterminated double quote — a real parse error, not mere drift
        (claude_dir / "hooks" / "tdd-gate.sh").write_text('#!/bin/sh\necho "unterminated\n')

    repo, claude_dir = make_repo(tmp_path, mutate=mutate)
    rc = doctor.main(["--repo", str(repo), "--claude-dir", str(claude_dir)])
    out = capsys.readouterr().out

    assert rc == 1
    assert "SYNTAX-ERROR" in out
    assert "tdd-gate.sh" in out
    # the intact multi-manager hook must NOT be reported — no blanket-fail
    assert "post-push-merge-notify.sh" not in out


def test_check_manifest_syntax_error_names_the_parse_error_in_detail(tmp_path):
    def mutate(repo, claude_dir):
        (claude_dir / "hooks" / "tdd-gate.sh").write_text('#!/bin/sh\necho "unterminated\n')

    repo, claude_dir = make_repo(tmp_path, mutate=mutate)
    results = doctor.check_manifest(doctor.build_manifest(repo, claude_dir))
    hook_result = next(r for r in results if r["category"] == "hooks")
    mm_result = next(r for r in results if r["category"] == "multi-manager hooks")

    assert hook_result["status"] == "syntax-error"
    assert hook_result["detail"]  # names the parse error, non-empty
    assert mm_result["status"] == "clean"


def test_check_manifest_syntax_error_wins_over_ordinary_drift_label(tmp_path):
    # a syntax-broken installed hook is ALSO byte-different from the repo
    # copy — the syntax-error verdict must win, since it's the louder,
    # actionable fact (the file is broken right now, not merely stale).
    def mutate(repo, claude_dir):
        (claude_dir / "hooks" / "tdd-gate.sh").write_text('#!/bin/sh\necho "unterminated\n')
        (repo / "hooks" / "tdd-gate.sh").write_text("#!/bin/sh\necho gate\necho post-fix-line\n")

    repo, claude_dir = make_repo(tmp_path, mutate=mutate)
    results = doctor.check_manifest(doctor.build_manifest(repo, claude_dir))
    hook_result = next(r for r in results if r["category"] == "hooks")
    assert hook_result["status"] == "syntax-error"


def test_check_manifest_valid_hooks_stay_clean_no_blanket_fail(tmp_path):
    repo, claude_dir = make_repo(tmp_path)
    results = doctor.check_manifest(doctor.build_manifest(repo, claude_dir))
    for r in results:
        if r["category"] in doctor.HOOK_SYNTAX_CATEGORIES:
            assert r["status"] == "clean"


def test_bash_syntax_error_returns_none_for_valid_script(tmp_path):
    good = tmp_path / "good.sh"
    good.write_text("#!/bin/sh\necho hi\n")
    assert doctor._bash_syntax_error(good) is None


def test_bash_syntax_error_returns_message_for_broken_script(tmp_path):
    bad = tmp_path / "bad.sh"
    bad.write_text('#!/bin/sh\necho "unterminated\n')
    error = doctor._bash_syntax_error(bad)
    assert error is not None
    assert "bad.sh" in error or "unexpected EOF" in error.lower() or error


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
    _install_real_classifier(repo)
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
    _install_real_classifier(repo)

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


# --- SABLE-8kpk8: un-pinned symlink into a DIFFERENT (sibling) checkout ---------
# _check_snapshot_pin's "clean, ordinary un-pinned symlink" fast path only ever
# matched an EXACT path (target == src.resolve()). On the real machine this
# bead was filed from, real pinned-snapshot-shaped bins (sable-agents,
# sable-msg, sable-spawn-worker, ...) are ordinary un-pinned symlinks into the
# canonical SABLE checkout -- but running sable-doctor from a WORKER worktree
# (its own bin/sable-doctor, rather than the installed CLI entry point that
# resolves --repo via the symlink chain) makes --repo resolve to that
# worktree, a different absolute path than the symlink target even though both
# are the same commit's content. The path-only check then fell through to the
# snapshot-sha regex, found no sable-<sha> match, and misreported plain DRIFT
# for byte-identical, genuinely un-pinned bins. Fixed by falling back to a
# content compare before concluding drift.

def test_check_manifest_snapshot_pin_clean_when_symlink_points_at_a_different_checkout_with_identical_content(tmp_path):
    repo, bin_dir, _sha = make_snapshot_repo(tmp_path)
    # a second, unrelated-by-path directory holding byte-identical content --
    # models a sibling git worktree of the same repo at the same commit.
    sibling_bin = tmp_path / "sibling-worktree-bin"
    sibling_bin.mkdir()
    (sibling_bin / "sable-importer").write_bytes((bin_dir / "sable-importer").read_bytes())

    bin_dir_dest = tmp_path / "local-bin"
    bin_dir_dest.mkdir()
    (bin_dir_dest / "sable-importer").symlink_to(sibling_bin / "sable-importer")

    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if x["category"] == "pinned snapshot bins")
    assert r["status"] == "clean"


def test_check_manifest_snapshot_pin_stays_drift_when_different_checkout_content_actually_differs(tmp_path):
    # the counterpart: a symlink into an unrecognized (non sable-<sha>)
    # directory whose content genuinely differs must still report drift --
    # the content-compare fallback must not paper over a real divergence.
    repo, bin_dir, _sha = make_snapshot_repo(tmp_path)
    sibling_bin = tmp_path / "sibling-worktree-bin"
    sibling_bin.mkdir()
    (sibling_bin / "sable-importer").write_text("#!/usr/bin/env python3\nprint('stale')\n")

    bin_dir_dest = tmp_path / "local-bin"
    bin_dir_dest.mkdir()
    (bin_dir_dest / "sable-importer").symlink_to(sibling_bin / "sable-importer")

    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if x["category"] == "pinned snapshot bins")
    assert r["status"] == "drift"


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


def test_check_manifest_snapshot_pin_clean_when_pinned_sha_predates_head_but_closure_is_unchanged(tmp_path):
    # The pin is frozen at an OLD sha while the repo moves on -- but the commit
    # since touched NOTHING inside the pin unit, so the pinned content is still
    # byte-identical to HEAD. That must stay clean: the staleness check added
    # by SABLE-0jplo is a check on the CLOSURE, not on the sha, precisely so it
    # does not cry wolf every time any commit lands anywhere in the repo. This
    # is the half of SABLE-9boz4's original intent that survives that bead --
    # "repo has since changed" is still not, by itself, a defect.
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest)

    # repo moves on after the pin was taken, OUTSIDE bin/
    (repo / "README.md").write_text("unrelated to the pin unit\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "docs"], cwd=repo, check=True)

    assert doctor.current_repo_head(repo)[:12] != sha  # HEAD really did move
    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if x["category"] == "pinned snapshot bins")
    assert r["status"] == "clean"


# --- SABLE-0jplo: an honestly-named STALE pin is not "clean" -----------------
# The live incident: ~/.local/bin/sable-merge-gate resolved into
# ~/.local/lib/sable-5e47cc4/, that directory held exactly 5e47cc4's content,
# and the repo was three files further on -- SABLE-jd5fj.4's module was absent
# from the pin entirely, so optimistic promotion was merged-but-dark. Doctor
# said "clean -- 63 installed files match repo HEAD."
#
# Doctor was only ever asking "is this snapshot still what it CLAIMS to be?"
# Nobody asked "is what it claims to be still CURRENT?" -- so the one state
# where the answer matters most (an honest pin, quietly behind) was the one
# state that reported agreement. These pin down the invariant: doctor reporting
# clean for a pinned bin IMPLIES the pinned entry-point content is
# byte-identical to repo HEAD's copy of that same file.
#
# REVISED by SABLE-5gxj3: the original fix here compared the STALENESS
# question against the whole shared bin/ closure too (three ways at once --
# modified / added / removed, including files with no relationship at all to
# the tool under test). That over-corrected -- --pin-snapshot copies the
# ENTIRE bin/ directory as one unit, so a commit touching any OTHER tool's
# file in that same directory aged every pinned tool's staleness verdict even
# though its own bytes never moved (measured live: sable-reconcile-handoffs
# byte-identical to HEAD, reported "merged but NOT live" because 37 unrelated
# bin/ files had changed). Staleness is now scoped to the one file the pinned
# tool actually runs; tamper detection (SNAPSHOT-DRIFT, above) stays
# whole-directory, since hand-tampering anywhere in a frozen pin is still a
# real integrity concern.

def advance_repo_past_the_pin(repo, bin_dir):
    """Move HEAD so the pinned tool's OWN entry-point file differs from what
    the snapshot pin captured -- the one file SABLE-5gxj3 scoped staleness
    down to. Returns its basename, the value `detail` will now carry."""
    (bin_dir / "sable-importer").write_text(
        "#!/usr/bin/env python3\nfrom sable_helper_lib import MARK\nprint('v2: ' + MARK)\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "advance the entrypoint past the pin"], cwd=repo, check=True)
    return "sable-importer"


def test_check_manifest_snapshot_pin_stale_when_honestly_named_but_behind_head(tmp_path):
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest)
    expected = advance_repo_past_the_pin(repo, bin_dir)

    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if x["category"] == "pinned snapshot bins")
    assert r["status"] == "snapshot-stale"
    assert r["detail"] == expected


def test_check_manifest_snapshot_pin_stays_clean_when_only_an_unrelated_sibling_bin_changes(tmp_path):
    # THE FALSE ALARM THIS BEAD (SABLE-5gxj3) WAS FILED FROM, reproduced: a
    # commit that only touches a DIFFERENT tool sharing the same snapshot
    # directory must not age this tool's own staleness verdict.
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest)

    (bin_dir / "sable-plain").write_text("#!/usr/bin/env bash\necho changed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "unrelated tool changes"], cwd=repo, check=True)

    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if x["category"] == "pinned snapshot bins")
    assert r["status"] == "clean"


# --- SABLE-5gxj3 test spec: identity vs content, and the fail-closed edge ----
# A pin honestly named for an OLD sha whose entry-point content is
# byte-identical to repo HEAD must read clean and must never say "NOT live" --
# that claim is about executability, which an identity (snapshot-name) check
# cannot support. Paired with the opposite polarity (content genuinely
# differs) so the check is provably not vacuous in either direction, plus the
# fail-closed edge: an unreadable pinned file must never read as clean.

def test_pin_drift_reports_clean_when_content_matches_despite_older_snapshot_name(tmp_path, capsys):
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest)

    # repo moves on, but never touches sable-importer's own bytes -- only an
    # unrelated tool sharing the SAME physical snapshot directory changes.
    # The snapshot dir's NAME is now stale (baked-in sha predates HEAD) even
    # though the pinned tool's content is not.
    (bin_dir / "sable-plain").write_text("#!/usr/bin/env bash\necho unrelated change\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "unrelated tool changes"], cwd=repo, check=True)
    assert doctor.current_repo_head(repo)[:12] != sha  # the snapshot's name is now stale

    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if x["category"] == "pinned snapshot bins")
    assert r["status"] == "clean"

    doctor.render_text_report(results)
    out = capsys.readouterr().out
    assert "NOT live" not in out


def test_pin_drift_reports_drift_when_content_differs(tmp_path):
    # Opposite polarity of the test above: same old snapshot name, but this
    # time the pinned tool's OWN content genuinely changed at HEAD. A drift
    # check that never fires and one that always fires are indistinguishable
    # without both directions covered.
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest)
    expected = advance_repo_past_the_pin(repo, bin_dir)

    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if x["category"] == "pinned snapshot bins")
    assert r["status"] == "snapshot-stale"
    assert r["detail"] == expected


def test_unreadable_pinned_file_reports_could_not_assess(tmp_path, capsys):
    # The fail-closed edge this bead's fix must preserve: an unreadable
    # pinned file must hold as drift (COULD-NOT-ASSESS), never silently read
    # as clean just because the content compare had nothing to compare.
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    snapshot_dir = pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest)
    installed = snapshot_dir / "sable-importer"
    installed.chmod(0o000)
    try:
        entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
        results = doctor.check_manifest(entries)
        r = next(x for x in results if x["category"] == "pinned snapshot bins")
        assert r["status"] == "could-not-assess"
        assert r["status"] != "clean"

        doctor.render_text_report(results)
        out = capsys.readouterr().out
        assert "COULD-NOT-ASSESS" in out
        assert "sable-doctor: clean" not in out
    finally:
        installed.chmod(0o755)


def test_check_manifest_snapshot_pin_verdict_unchanged_across_the_install_path(tmp_path):
    # THE REGRESSION GUARD (red before SABLE-0jplo: both verdicts were "clean").
    # Chuck observed doctor go quiet about a stale pin around a `bash install.sh`
    # run. Reproducing it showed the install path suppresses nothing -- the
    # verdict was ALREADY a false green on both sides. So this asserts the
    # transition rather than either state: whatever the install path does (run
    # the real sable-bin-install in ordinary mode, which regenerates the
    # .sable-pinned marker; write the provenance stamp, which is the other
    # plausible short-circuit) the verdict must be identical before and after.
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest)
    advance_repo_past_the_pin(repo, bin_dir)

    def verdict():
        entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
        results = doctor.check_manifest(entries)
        r = next(x for x in results if x["category"] == "pinned snapshot bins")
        return r["status"], r["detail"]

    before = verdict()
    assert before[0] == "snapshot-stale"

    # what install.sh actually does to this scope
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / doctor.PROVENANCE_FILENAME).write_text(
        f"commit={doctor.current_repo_head(repo)}\nbranch=main\ndirty=false\n"
    )
    subprocess.run(
        ["bash", str(repo / "bin" / "sable-bin-install"), "--dir", str(bin_dir_dest)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    assert (bin_dir_dest / "sable-importer").is_symlink()
    assert verdict() == before


def test_check_manifest_snapshot_pin_tamper_wins_over_staleness(tmp_path):
    # Both conditions true at once. Tamper is the louder fact (something wrote
    # into a frozen pin) and its remedy is a rollback, not an advance -- so it
    # must not be masked by the staleness verdict that now shares the code path.
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    snapshot_dir = pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest)
    advance_repo_past_the_pin(repo, bin_dir)
    (snapshot_dir / "sable-importer").write_text("# hand-tampered\n")

    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if x["category"] == "pinned snapshot bins")
    assert r["status"] == "snapshot-drift"
    assert r["detail"] == "sable-importer"


def test_render_text_report_snapshot_stale_names_files_and_never_recommends_install_sh(tmp_path, capsys):
    # install.sh CORRECTLY refuses to touch a pin (SABLE-mkj6k), so pointing a
    # reader at it for a stale pin sends them to a command guaranteed to leave
    # the staleness in place -- and then report nothing.
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest)
    expected = advance_repo_past_the_pin(repo, bin_dir)

    results = doctor.check_manifest(doctor.build_manifest(repo, tmp_path / "claude", bin_dir_dest))
    doctor.render_text_report(results)
    out = capsys.readouterr().out
    assert "SNAPSHOT-STALE" in out
    assert "bash install.sh" not in out
    assert "--pin-snapshot sable-importer" in out
    assert expected in out


def test_quiet_mode_reports_a_stale_snapshot_pin(tmp_path, capsys):
    # the SessionStart-hook path -- the highest-traffic message in the system,
    # and the one that said nothing at all while the gate ran pre-jd5fj.4 code.
    repo, bin_dir, sha = make_snapshot_repo(tmp_path)
    bin_dir_dest = tmp_path / "local-bin"
    pin_snapshot(tmp_path, repo, bin_dir, sha, bin_dir_dest)
    advance_repo_past_the_pin(repo, bin_dir)

    rc = doctor.main([
        "--repo", str(repo), "--claude-dir", str(tmp_path / "claude"),
        "--bin-dir", str(bin_dir_dest), "--quiet",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert "drifted from repo HEAD" in captured.err
    assert "bash install.sh" not in captured.err


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


# --- shared classifier: a PINNED_BIN_NAMES entry that becomes snapshot-shaped
# (SABLE-rucuh) ---------------------------------------------------------------
# sable-merge-gate started life as a plain PINNED_BIN_NAMES entry, then grew
# repo-local python imports (SABLE-jd5fj.3's module split) and became
# snapshot-shaped. bin-install's --classify correctly says "snapshot" for it;
# doctor's OLD "pinned bins" check had no concept of that and called a
# properly snapshot-pinned instance "unpinned", then recommended a bare `cp`
# that severs the sibling imports it now needs. These prove: doctor's own
# classification agrees with bin-install's for both shapes (structurally --
# it is the same subprocess call, so they cannot re-diverge); a name in
# PINNED_BIN_NAMES that is snapshot-shaped is routed to -- and ONLY to -- the
# "pinned snapshot bins" category; a genuinely clean snapshot pin of such a
# name is never reported "unpinned"; and the remedy for it never reduces to
# a bare cp, including the render-time defense-in-depth check.

def make_snapshot_shaped_pinned_bin_repo(tmp_path):
    """repo/bin/<PINNED_BIN_NAMES[0]> reshaped to import a sibling module --
    modeling sable-merge-gate after SABLE-jd5fj.3 -- plus the real
    sable-bin-install so classify_bin_shape has something authoritative to
    ask. The other two PINNED_BIN_NAMES stay plain shell scripts. Returns
    (repo, bin_dir, target_name)."""
    target_name = doctor.PINNED_BIN_NAMES[0]
    repo = tmp_path / "repo"
    bin_dir = tmp_path / "local-bin"
    (repo / "bin").mkdir(parents=True)
    bin_dir.mkdir()
    _install_real_classifier(repo)
    (repo / "bin" / "sable_gate_helper_lib.py").write_text("MARK = 'v1'\n")
    (repo / "bin" / target_name).write_text(
        "#!/usr/bin/env python3\nfrom sable_gate_helper_lib import MARK\nprint(MARK)\n"
    )
    for name in doctor.PINNED_BIN_NAMES:
        if name != target_name:
            (repo / "bin" / name).write_text(f"#!/bin/sh\necho {name}\n")
    return repo, bin_dir, target_name


def test_classify_bin_shape_agrees_with_sable_bin_install_for_plain(tmp_path):
    repo, _bin_dir, target_name = make_snapshot_shaped_pinned_bin_repo(tmp_path)
    other_name = next(n for n in doctor.PINNED_BIN_NAMES if n != target_name)
    expected = subprocess.run(
        ["bash", str(repo / "bin" / "sable-bin-install"), "--classify", other_name],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert expected == "plain"
    assert doctor.classify_bin_shape(repo, other_name) == expected


def test_classify_bin_shape_agrees_with_sable_bin_install_for_snapshot(tmp_path):
    repo, _bin_dir, target_name = make_snapshot_shaped_pinned_bin_repo(tmp_path)
    expected = subprocess.run(
        ["bash", str(repo / "bin" / "sable-bin-install"), "--classify", target_name],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert expected == "snapshot"
    assert doctor.classify_bin_shape(repo, target_name) == expected


def test_classify_bin_shape_returns_none_when_bin_install_is_missing(tmp_path):
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    (repo / "bin" / "sable-plain").write_text("#!/bin/sh\necho hi\n")
    assert doctor.classify_bin_shape(repo, "sable-plain") is None


def test_build_manifest_routes_snapshot_shaped_pinned_bin_name_to_snapshot_category_only(tmp_path):
    repo, bin_dir, target_name = make_snapshot_shaped_pinned_bin_repo(tmp_path)
    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir)
    pinned_bins_names = {src.name for c, src, _ in entries if c == "pinned bins"}
    snapshot_bins_names = {src.name for c, src, _ in entries if c == "pinned snapshot bins"}
    assert target_name not in pinned_bins_names
    assert target_name in snapshot_bins_names
    assert pinned_bins_names == set(doctor.PINNED_BIN_NAMES) - {target_name}


def test_check_manifest_snapshot_shaped_pinned_bin_never_reported_unpinned_when_genuinely_pinned(tmp_path):
    # THE regression: reproduces the live incident this bead was filed from
    # -- a PINNED_BIN_NAMES entry (sable-merge-gate) that became
    # snapshot-shaped and IS correctly snapshot-pinned must never be reported
    # "unpinned".
    repo, bin_dir, target_name = make_snapshot_shaped_pinned_bin_repo(tmp_path)

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"], cwd=repo,
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    snapshot_dir = tmp_path / "lib" / f"sable-{sha}"
    snapshot_dir.mkdir(parents=True)
    for f in (repo / "bin").iterdir():
        if f.is_file():
            (snapshot_dir / f.name).write_bytes(f.read_bytes())
    (bin_dir / target_name).symlink_to(snapshot_dir / target_name)

    entries = doctor.build_manifest(repo, tmp_path / "claude", bin_dir)
    results = doctor.check_manifest(entries)
    r = next(x for x in results if Path(x["repo_path"]).name == target_name)
    assert r["status"] == "clean"
    assert r["category"] == "pinned snapshot bins"


def test_render_text_report_never_prints_cp_for_a_snapshot_shaped_pinned_bin_name(tmp_path, capsys):
    # even in a BROKEN state (bare regular-file pin, severing the import),
    # the report for a snapshot-shaped PINNED_BIN_NAMES name must recommend
    # --pin-snapshot, never the bare cp that plain-shaped pins get.
    repo, bin_dir, target_name = make_snapshot_shaped_pinned_bin_repo(tmp_path)
    (bin_dir / target_name).write_bytes((repo / "bin" / target_name).read_bytes())

    results = doctor.check_manifest(doctor.build_manifest(repo, tmp_path / "claude", bin_dir))
    doctor.render_text_report(results)
    out = capsys.readouterr().out
    assert f"cp {repo / 'bin' / target_name}" not in out
    assert "--pin-snapshot" in out
    assert target_name in out


# --- _guarded_remedy_lines: shape-aware, defense in depth (SABLE-rucuh) ------
# These call the remedy renderer directly with a hand-built "pinned bins"
# result, bypassing build_manifest's routing -- proving the remedy generator
# ITSELF refuses to guess "plain" for a snapshot-shaped or undetermined name,
# independent of whether the routing fix above also holds. optimus (dispatch
# note, 2026-07-21): "for every pin shape the classifier can return, assert
# doctor's emitted remedy is non-destructive for that shape, and assert an
# undetermined shape emits no remedy."

def _fake_guarded_result(repo_path, installed_path):
    return {"category": "pinned bins", "repo_path": str(repo_path),
            "installed_path": str(installed_path), "status": "unpinned", "detail": None}


def test_guarded_remedy_lines_plain_shape_gets_the_cp_remedy(tmp_path):
    repo, bin_dir, target_name = make_snapshot_shaped_pinned_bin_repo(tmp_path)
    plain_name = next(n for n in doctor.PINNED_BIN_NAMES if n != target_name)
    r = _fake_guarded_result(repo / "bin" / plain_name, bin_dir / plain_name)
    text = "\n".join(doctor._guarded_remedy_lines([r]))
    assert f"cp {repo / 'bin' / plain_name}" in text
    assert "--pin-snapshot" not in text


def test_guarded_remedy_lines_snapshot_shape_never_prints_a_bare_cp(tmp_path):
    repo, bin_dir, target_name = make_snapshot_shaped_pinned_bin_repo(tmp_path)
    r = _fake_guarded_result(repo / "bin" / target_name, bin_dir / target_name)
    text = "\n".join(doctor._guarded_remedy_lines([r]))
    destructive_cmd = f"cp {r['repo_path']} {r['installed_path']} && chmod +x {r['installed_path']}"
    assert destructive_cmd not in text
    assert f"--pin-snapshot {target_name}" in text


def test_guarded_remedy_lines_undetermined_shape_prints_no_remedy(tmp_path):
    # no sable-bin-install script at all in this repo -> classify_bin_shape
    # returns None for every name -> "no remedy printed", not a guess.
    repo = tmp_path / "repo"
    bin_dir = tmp_path / "local-bin"
    (repo / "bin").mkdir(parents=True)
    bin_dir.mkdir()
    name = doctor.PINNED_BIN_NAMES[0]
    (repo / "bin" / name).write_text(f"#!/bin/sh\necho {name}\n")
    r = _fake_guarded_result(repo / "bin" / name, bin_dir / name)
    text = "\n".join(doctor._guarded_remedy_lines([r]))
    destructive_cmd = f"cp {r['repo_path']} {r['installed_path']} && chmod +x {r['installed_path']}"
    assert destructive_cmd not in text
    assert "--pin-snapshot" not in text
    assert "could not be determined" in text


# --- install provenance (SABLE-78kxu) -------------------------------------
# WHICH COMMIT did the installed set come from? build_manifest's clean/drift
# compare only proves installed files match the repo TREE as it exists
# today; a file the tree doesn't have yet is invisible to it, so "clean" is
# compatible with a not-yet-merged guard being entirely absent (the incident
# this bead was filed from). read_provenance / current_repo_head /
# provenance_ancestor_of_head answer the separate, narrower question of
# which commit the CURRENTLY INSTALLED files were copied from — a fact about
# files, never a claim that any feature in that commit is wired active.

def write_provenance(claude_dir, *, commit="a" * 40, branch="main", dirty="false", untracked="false", timestamp="2026-07-21T00:00:00Z"):
    claude_dir.mkdir(parents=True, exist_ok=True)
    stamp = claude_dir / doctor.PROVENANCE_FILENAME
    stamp.write_text(
        f"commit={commit}\nbranch={branch}\ndirty={dirty}\nuntracked={untracked}\ntimestamp={timestamp}\n"
    )
    return stamp


def test_read_provenance_returns_none_without_a_stamp(tmp_path):
    # the ordinary case for every install that predates this bead — must
    # read as UNKNOWN, never as an error and never as "clean".
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    assert doctor.read_provenance(claude_dir) is None


def test_read_provenance_parses_all_fields(tmp_path):
    claude_dir = tmp_path / "claude"
    write_provenance(claude_dir, commit="b" * 40, branch="wk-feature", dirty="true", timestamp="2026-07-21T12:34:56Z")
    data = doctor.read_provenance(claude_dir)
    assert data["commit"] == "b" * 40
    assert data["branch"] == "wk-feature"
    assert data["dirty"] == "true"
    assert data["timestamp"] == "2026-07-21T12:34:56Z"


def test_read_provenance_dirty_flag_round_trips_false(tmp_path):
    claude_dir = tmp_path / "claude"
    write_provenance(claude_dir, dirty="false")
    assert doctor.read_provenance(claude_dir)["dirty"] == "false"


def test_read_provenance_missing_commit_line_is_treated_as_no_stamp(tmp_path):
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    (claude_dir / doctor.PROVENANCE_FILENAME).write_text("branch=main\ndirty=false\n")
    assert doctor.read_provenance(claude_dir) is None


def test_current_repo_head_returns_none_for_non_git_dir(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    assert doctor.current_repo_head(not_a_repo) is None


def test_current_repo_head_returns_sha_for_real_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "f.txt").write_text("hi\n")
    _git("add", "f.txt", cwd=repo)
    _git("-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "init", cwd=repo)
    head = doctor.current_repo_head(repo)
    expected = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head == expected


def test_provenance_ancestor_of_head_true_when_sha_is_an_ancestor(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "f.txt").write_text("v1\n")
    _git("add", "f.txt", cwd=repo)
    _git("-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "v1", cwd=repo)
    first_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo / "f.txt").write_text("v2\n")
    _git("add", "f.txt", cwd=repo)
    _git("-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "v2", cwd=repo)
    assert doctor.provenance_ancestor_of_head(repo, first_sha) is True


def test_provenance_ancestor_of_head_false_for_unresolvable_sha(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "f.txt").write_text("v1\n")
    _git("add", "f.txt", cwd=repo)
    _git("-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "v1", cwd=repo)
    # a syntactically plausible but unresolvable sha must resolve to None,
    # never True — an unresolvable probe is not evidence of ancestry.
    assert doctor.provenance_ancestor_of_head(repo, "f" * 40) is None


def test_render_text_report_clean_includes_provenance_sha(tmp_path, capsys):
    repo, claude_dir = make_repo(tmp_path)
    write_provenance(claude_dir, commit="c" * 40, branch="main")
    results = doctor.check_manifest(doctor.build_manifest(repo, claude_dir))
    provenance = doctor.read_provenance(claude_dir)
    doctor.render_text_report(results, provenance=provenance)
    out = capsys.readouterr().out
    assert "installed from" in out
    assert "c" * 40 in out
    assert "main" in out


def test_render_text_report_no_stamp_says_unknown_not_clean(tmp_path, capsys):
    # the manifest itself is clean (files match), but provenance is a
    # SEPARATE claim — it must read as UNKNOWN, not be swallowed into the
    # overall "clean" verdict.
    repo, claude_dir = make_repo(tmp_path)
    results = doctor.check_manifest(doctor.build_manifest(repo, claude_dir))
    doctor.render_text_report(results, provenance=None)
    out = capsys.readouterr().out
    assert "UNKNOWN" in out
    assert "sable-doctor: clean" in out  # file-match verdict is unaffected


def test_render_text_report_drift_includes_both_provenance_and_current_head(tmp_path, capsys):
    def mutate(repo, claude_dir):
        (claude_dir / "hooks" / "tdd-gate.sh").write_text("tampered\n")

    repo, claude_dir = make_repo(tmp_path, mutate=mutate)
    write_provenance(claude_dir, commit="d" * 40, branch="main")
    results = doctor.check_manifest(doctor.build_manifest(repo, claude_dir))
    provenance = doctor.read_provenance(claude_dir)
    doctor.render_text_report(results, provenance=provenance, current_head="e" * 40)
    out = capsys.readouterr().out
    assert "installed from" in out
    assert "d" * 40 in out
    assert "repo now at" in out
    assert "e" * 40 in out


def test_render_text_report_dirty_flag_shown_in_report(tmp_path, capsys):
    repo, claude_dir = make_repo(tmp_path)
    write_provenance(claude_dir, commit="1" * 40, dirty="true")
    provenance = doctor.read_provenance(claude_dir)
    doctor.render_text_report(
        doctor.check_manifest(doctor.build_manifest(repo, claude_dir)), provenance=provenance,
    )
    out = capsys.readouterr().out
    assert "DIRTY" in out


def test_render_text_report_untracked_only_does_not_say_not_reproducible(tmp_path, capsys):
    # SABLE-dt92b: an untracked-only tree must NOT be reported as "not
    # reproducible" -- that claim is about TRACKED content, which the
    # recorded SHA reconstructs exactly regardless of untracked files. This
    # is the (b) case from the bead's test spec, asserted directly.
    repo, claude_dir = make_repo(tmp_path)
    write_provenance(claude_dir, commit="6" * 40, dirty="false", untracked="true")
    provenance = doctor.read_provenance(claude_dir)
    assert provenance["dirty"] == "false"
    doctor.render_text_report(
        doctor.check_manifest(doctor.build_manifest(repo, claude_dir)), provenance=provenance,
    )
    out = capsys.readouterr().out
    assert "not reproducible" not in out
    assert "untracked files present" in out


def test_render_text_report_dirty_and_untracked_are_independent_notes(tmp_path, capsys):
    # the mirror direction: a genuinely dirty TRACKED file must still say
    # "not reproducible" -- separating the untracked note must not have
    # swallowed the real dirty signal.
    repo, claude_dir = make_repo(tmp_path)
    write_provenance(claude_dir, commit="7" * 40, dirty="true", untracked="false")
    provenance = doctor.read_provenance(claude_dir)
    doctor.render_text_report(
        doctor.check_manifest(doctor.build_manifest(repo, claude_dir)), provenance=provenance,
    )
    out = capsys.readouterr().out
    assert "not reproducible" in out
    assert "untracked files present" not in out


def test_read_provenance_untracked_flag_defaults_false_for_pre_dt92b_stamps(tmp_path):
    # stamps written before SABLE-dt92b have no "untracked" line at all --
    # must degrade to false, not error or mis-render.
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    (claude_dir / doctor.PROVENANCE_FILENAME).write_text(
        "commit=" + "8" * 40 + "\nbranch=main\ndirty=false\ntimestamp=2026-07-21T00:00:00Z\n"
    )
    data = doctor.read_provenance(claude_dir)
    assert data["untracked"] == "false"


def test_render_text_report_ancestor_false_adds_a_note_not_an_error(tmp_path, capsys):
    repo, claude_dir = make_repo(tmp_path)
    write_provenance(claude_dir, commit="2" * 40)
    provenance = doctor.read_provenance(claude_dir)
    doctor.render_text_report(
        doctor.check_manifest(doctor.build_manifest(repo, claude_dir)),
        provenance=provenance, ancestor_ok=False,
    )
    out = capsys.readouterr().out
    assert "not an ancestor" in out
    assert "sable-doctor: clean" in out  # advisory note, never changes the verdict


def test_main_json_includes_provenance_block(tmp_path, capsys):
    repo, claude_dir = make_repo(tmp_path)
    write_provenance(claude_dir, commit="3" * 40, branch="main", dirty="false")
    rc = doctor.main(["--repo", str(repo), "--claude-dir", str(claude_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["provenance"]["commit"] == "3" * 40
    assert payload["provenance"]["branch"] == "main"


def test_main_json_provenance_is_null_without_a_stamp(tmp_path, capsys):
    repo, claude_dir = make_repo(tmp_path)
    rc = doctor.main(["--repo", str(repo), "--claude-dir", str(claude_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["provenance"] is None


def test_main_installed_from_prints_bare_sha_and_exits_zero(tmp_path, capsys):
    repo, claude_dir = make_repo(tmp_path)
    write_provenance(claude_dir, commit="4" * 40)
    rc = doctor.main(["--repo", str(repo), "--claude-dir", str(claude_dir), "--installed-from"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "4" * 40


def test_main_installed_from_exits_one_without_a_stamp(tmp_path, capsys):
    repo, claude_dir = make_repo(tmp_path)
    rc = doctor.main(["--repo", str(repo), "--claude-dir", str(claude_dir), "--installed-from"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "no provenance stamp" in captured.err


def test_quiet_mode_clean_stays_silent_with_a_provenance_stamp_present(tmp_path, capsys):
    # SABLE-78kxu must not make the SessionStart-hook path (`sable-doctor
    # --quiet`) start speaking on a healthy run just because provenance now
    # exists — that hook fires on every fresh pane and staying silent when
    # clean is the whole point of --quiet.
    repo, claude_dir = make_repo(tmp_path)
    write_provenance(claude_dir, commit="5" * 40)
    rc = doctor.main(["--repo", str(repo), "--claude-dir", str(claude_dir), "--quiet"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""
    assert captured.out == ""
