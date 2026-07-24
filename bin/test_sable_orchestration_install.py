#!/usr/bin/env python3
"""Unit tests for bin/sable-orchestration-install's hook sibling-library
closure (SABLE-nn54x).

WHAT WENT WRONG. Hooks are COPY-installed. inline-body-guard.sh resolves its
pattern logic through "$HOOK_DIR/../../bin/sable_inline_body_guard_lib.py",
which from the repo lands on repo/bin/ and from the installed path lands on
BASE/bin/ -- a directory the installer never created. The copy severed the
import, so the guard fired on every Bash call fleet-wide and checked nothing.
Four independent activation probes reported it healthy (installed-path
presence, `test -L`, a settings.json wiring grep, a cmp against the landed
blob) because NONE inspected the dependency closure.

WHY THESE TESTS RUN THE INSTALLER AGAINST A SYNTHETIC REPO. The closure logic
has to be exercised on hooks whose libraries can be made deliberately
unreadable, and on a hook that references a lib at all -- neither is possible
against the real repo without mutating it. Composition against the real repo
is covered by test_sable_orchestration_install_integration.py, which runs the
REAL installed hook end to end.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "bin" / "sable-orchestration-install"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="installer is bash+python3; not available in this clean room",
)

PLAIN_HOOK = "#!/usr/bin/env bash\nexit 0\n"

LIB_HOOK = """#!/usr/bin/env bash
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
LIB_PATH="${HOOK_DIR}/../../bin/foo_lib.py"
[ -f "$LIB_PATH" ] || exit 1
exit 0
"""


def make_repo(root, hooks, libs=None):
    """Build the minimum tree sable-orchestration-install installs FROM.

    hooks: {basename: content}; every one is registered in the settings
    snippet, so "was this hook registered?" is a real question for each.
    libs:  {basename: content} written into bin/.
    """
    (root / "hooks" / "multi-manager").mkdir(parents=True)
    for name, content in hooks.items():
        (root / "hooks" / "multi-manager" / name).write_text(content)

    (root / "bin").mkdir(parents=True)
    for name, content in (libs or {}).items():
        (root / "bin" / name).write_text(content)

    tmpl = root / "templates" / "multi-manager"
    (tmpl / "roles").mkdir(parents=True)
    (tmpl / "agents.yaml").write_text("agents: []\n")
    for role in ("lincoln", "optimus", "tarzan", "chuck"):
        (tmpl / "roles" / f"{role}.md").write_text(f"# {role}\n")
    (tmpl / "settings-snippet.json").write_text(json.dumps({
        "hooks": {
            "PreToolUse": [{
                "matcher": "Bash",
                "hooks": [
                    {"type": "command",
                     "command": f"bash ~/.claude/hooks/multi-manager/{name}",
                     "timeout": 3000}
                    for name in sorted(hooks)
                ],
            }],
        },
    }, indent=2))

    skill = root / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\n---\n")
    return root


def run_install(repo, project, expect_ok=True):
    result = subprocess.run(
        ["bash", str(INSTALLER), "--project"],
        env={**os.environ,
             "SABLE_REPO_DIR": str(repo),
             "SABLE_PROJECT_DIR": str(project)},
        capture_output=True, text=True, timeout=120,
    )
    if expect_ok:
        assert result.returncode == 0, f"installer failed:\n{result.stdout}\n{result.stderr}"
    return result


def resolved_lib_path(project, lib_name):
    """The path the INSTALLED hook itself resolves -- computed the same way the
    hook does (installed dir + the ../../bin/ prefix), never hardcoded, so this
    cannot drift away from the thing under test."""
    installed_hook_dir = project / ".claude" / "hooks" / "multi-manager"
    return Path(os.path.normpath(installed_hook_dir / ".." / ".." / "bin" / lib_name))


def registered_commands(project):
    settings = project / ".claude" / "settings.json"
    if not settings.exists():
        return []
    data = json.loads(settings.read_text())
    return [
        h.get("command", "")
        for blocks in data.get("hooks", {}).values()
        for b in blocks
        for h in b.get("hooks", [])
    ]


def test_hook_with_sibling_lib_installs_the_lib_to_the_resolved_path(tmp_path):
    repo = make_repo(tmp_path / "repo", {"needs-lib.sh": LIB_HOOK},
                     libs={"foo_lib.py": "print('hi')\n"})
    project = tmp_path / "proj"
    project.mkdir()

    run_install(repo, project)

    target = resolved_lib_path(project, "foo_lib.py")
    assert target.is_file(), f"lib absent from the path the installed hook resolves: {target}"
    assert target.read_text() == "print('hi')\n"
    # And the installed hook can actually satisfy its own import -- the hook
    # exits 1 when its lib is missing, which is the assertion that failed
    # before this closure existed.
    installed = project / ".claude" / "hooks" / "multi-manager" / "needs-lib.sh"
    assert subprocess.run(["bash", str(installed)], timeout=30).returncode == 0


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores file mode, so an unreadable lib cannot be simulated")
def test_hook_whose_lib_cannot_be_installed_is_NOT_registered(tmp_path):
    repo = make_repo(tmp_path / "repo", {"needs-lib.sh": LIB_HOOK},
                     libs={"foo_lib.py": "print('hi')\n"})
    (repo / "bin" / "foo_lib.py").chmod(0o000)
    project = tmp_path / "proj"
    project.mkdir()

    result = run_install(repo, project)

    installed = project / ".claude" / "hooks" / "multi-manager" / "needs-lib.sh"
    assert not installed.exists(), "wired-inert hook was installed anyway"
    # THE LOAD-BEARING HALF. A hook file that is merely absent is recoverable;
    # a settings row pointing at a hook whose logic cannot load is the failure
    # this bead is about, and it is the half nothing checked.
    assert not any("needs-lib.sh" in c for c in registered_commands(project)), \
        "refused hook was still registered in settings.json"
    assert "REFUSED" in result.stderr


def test_hook_without_a_sibling_lib_installs_unchanged(tmp_path):
    # NEGATIVE CONTROL: without this, an implementation that refuses every hook
    # passes the refusal test above and looks correct.
    repo = make_repo(tmp_path / "repo", {"plain.sh": PLAIN_HOOK})
    project = tmp_path / "proj"
    project.mkdir()

    run_install(repo, project)

    installed = project / ".claude" / "hooks" / "multi-manager" / "plain.sh"
    assert installed.is_file()
    assert installed.read_text() == PLAIN_HOOK
    assert any("plain.sh" in c for c in registered_commands(project))


def test_install_log_names_each_resolved_lib_path(tmp_path):
    repo = make_repo(tmp_path / "repo", {"needs-lib.sh": LIB_HOOK},
                     libs={"foo_lib.py": "print('hi')\n"})
    project = tmp_path / "proj"
    project.mkdir()

    result = run_install(repo, project)

    target = resolved_lib_path(project, "foo_lib.py")
    assert "needs-lib.sh" in result.stdout
    assert str(target) in result.stdout, \
        f"install log does not name the resolved lib path:\n{result.stdout}"


def test_refusal_is_per_hook_not_fleet_wide(tmp_path):
    # The second control the refusal path needs: refusing one hook must not
    # take the rest of the layer down with it.
    repo = make_repo(tmp_path / "repo",
                     {"needs-lib.sh": LIB_HOOK, "plain.sh": PLAIN_HOOK},
                     libs={"foo_lib.py": "print('hi')\n"})
    (repo / "bin" / "foo_lib.py").unlink()
    project = tmp_path / "proj"
    project.mkdir()

    run_install(repo, project)

    commands = registered_commands(project)
    assert not any("needs-lib.sh" in c for c in commands)
    assert any("plain.sh" in c for c in commands)
    assert (project / ".claude" / "hooks" / "multi-manager" / "plain.sh").is_file()


def test_refused_hook_has_its_stale_registration_and_copy_removed(tmp_path):
    # An earlier install that DID satisfy the closure leaves a live file and a
    # live settings row. Refusing on a later run has to clear both, or the
    # inert copy simply stays wired -- the exact state this bead names.
    repo = make_repo(tmp_path / "repo", {"needs-lib.sh": LIB_HOOK},
                     libs={"foo_lib.py": "print('hi')\n"})
    project = tmp_path / "proj"
    project.mkdir()

    run_install(repo, project)
    assert any("needs-lib.sh" in c for c in registered_commands(project))

    (repo / "bin" / "foo_lib.py").unlink()
    run_install(repo, project)

    assert not (project / ".claude" / "hooks" / "multi-manager" / "needs-lib.sh").exists()
    assert not any("needs-lib.sh" in c for c in registered_commands(project))


# --------------------------------------------------------------------------
# MENTION vs EXECUTION. The scanner reads text, not shell. A hook's own
# comments routinely NAME the library they document -- inline-body-guard.sh's
# header does exactly that -- so a scanner that read a comment as a dependency
# would refuse to install a hook over a stale sentence, on the tool that
# installs everything, fleet-wide. These two tests pin the failure direction:
# a comment can install a harmless extra copy, and can never cost a hook.
# --------------------------------------------------------------------------

COMMENT_ONLY_REAL = """#!/usr/bin/env bash
# Pattern logic is documented in ../../bin/foo_lib.py (this line is a COMMENT).
exit 0
"""

COMMENT_ONLY_ABSENT = """#!/usr/bin/env bash
# This once lived in ../../bin/deleted_lib.py and no longer exists anywhere.
exit 0
"""


def test_comment_only_reference_to_a_missing_lib_does_NOT_refuse_the_hook(tmp_path):
    # THE BLOCKER CASE. A stale sentence naming a file that no longer exists
    # must not take a hook -- and its settings row -- off the fleet.
    repo = make_repo(tmp_path / "repo", {"documented.sh": COMMENT_ONLY_ABSENT})
    project = tmp_path / "proj"
    project.mkdir()

    result = run_install(repo, project)

    assert (project / ".claude" / "hooks" / "multi-manager" / "documented.sh").is_file()
    assert any("documented.sh" in c for c in registered_commands(project))
    assert "REFUSED" not in result.stderr


def test_comment_only_reference_to_a_real_lib_installs_it_harmlessly(tmp_path):
    # The other polarity: when the comment is ACCURATE, installing the named
    # lib costs one file copy and is correct. Without this control, "never
    # refuse on a comment" could be implemented as "ignore comments entirely",
    # which silently narrows the closure.
    repo = make_repo(tmp_path / "repo", {"documented.sh": COMMENT_ONLY_REAL},
                     libs={"foo_lib.py": "print('hi')\n"})
    project = tmp_path / "proj"
    project.mkdir()

    result = run_install(repo, project)

    assert resolved_lib_path(project, "foo_lib.py").is_file()
    assert "comment-only reference" in result.stdout, \
        "install log must distinguish a documented reference from a real dependency"


def test_a_real_reference_still_refuses_even_when_a_comment_also_names_it(tmp_path):
    # The precision check: the same lib named in BOTH a comment and live code
    # is a HARD dependency. A rule that keyed on "appears in a comment" would
    # soften a genuine dependency into documentation and reopen the bug.
    hook = "#!/usr/bin/env bash\n# see ../../bin/foo_lib.py for details\n" + LIB_HOOK.split("\n", 1)[1]
    repo = make_repo(tmp_path / "repo", {"needs-lib.sh": hook},
                     libs={"foo_lib.py": "print('hi')\n"})
    (repo / "bin" / "foo_lib.py").unlink()
    project = tmp_path / "proj"
    project.mkdir()

    result = run_install(repo, project)

    assert not (project / ".claude" / "hooks" / "multi-manager" / "needs-lib.sh").exists()
    assert not any("needs-lib.sh" in c for c in registered_commands(project))
    assert "REFUSED" in result.stderr


def test_every_shipped_multi_manager_hook_resolves_its_closure(tmp_path):
    # THE LANDING GATE. The refusal path is only a safety property if no hook
    # currently shipped can trip it. Installing the REAL repo into a throwaway
    # scope must produce ZERO refusals; if a future hook grows a sibling
    # reference this resolver cannot satisfy, this test goes red at authoring
    # time rather than during a fleet-wide install refresh.
    project = tmp_path / "proj"
    project.mkdir()

    result = subprocess.run(
        ["bash", str(INSTALLER), "--project"],
        env={**os.environ, "SABLE_PROJECT_DIR": str(project)},
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr
    assert "REFUSED" not in result.stderr, \
        f"a shipped hook cannot resolve its sibling-library closure:\n{result.stderr}"

    # Positive control: the run must have resolved at least one real closure,
    # or "no refusals" would be vacuously true for a scanner that finds nothing.
    assert "Hook sibling libraries installed" in result.stdout


def test_transitive_sibling_import_is_also_installed(tmp_path):
    # The closure is a CLOSURE. A one-level scan rebuilds this same defect one
    # level down: python resolves a lib's own sable_* imports from that lib's
    # directory, which is the installed dir, not the repo's.
    repo = make_repo(
        tmp_path / "repo", {"needs-lib.sh": LIB_HOOK},
        libs={
            "foo_lib.py": "import sable_dep_lib\nprint(sable_dep_lib.VALUE)\n",
            "sable_dep_lib.py": "VALUE = 1\n",
        },
    )
    project = tmp_path / "proj"
    project.mkdir()

    run_install(repo, project)

    assert resolved_lib_path(project, "foo_lib.py").is_file()
    assert resolved_lib_path(project, "sable_dep_lib.py").is_file(), \
        "transitive sibling import was not installed"


def test_second_run_reports_the_lib_identical_not_changed(tmp_path):
    # The libs go through install_file like every other artifact, so a re-run
    # must be a no-op in the change manifest. A lib that reports CHANGED on
    # every install would make the manifest useless for brokered-window
    # accounting.
    repo = make_repo(tmp_path / "repo", {"needs-lib.sh": LIB_HOOK},
                     libs={"foo_lib.py": "print('hi')\n"})
    project = tmp_path / "proj"
    project.mkdir()

    run_install(repo, project)
    second = run_install(repo, project)

    assert "IDENTICAL bin/foo_lib.py" in second.stdout
    assert "CHANGED" not in second.stdout


def test_uninstall_removes_the_installed_sibling_libs(tmp_path):
    repo = make_repo(tmp_path / "repo", {"needs-lib.sh": LIB_HOOK},
                     libs={"foo_lib.py": "print('hi')\n"})
    project = tmp_path / "proj"
    project.mkdir()
    run_install(repo, project)
    target = resolved_lib_path(project, "foo_lib.py")
    assert target.is_file()  # positive control: the probe can see it present

    result = subprocess.run(
        ["bash", str(INSTALLER), "--project", "--uninstall"],
        env={**os.environ, "SABLE_REPO_DIR": str(repo), "SABLE_PROJECT_DIR": str(project)},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr

    assert not target.exists(), "uninstall left the hook's sibling library behind"
