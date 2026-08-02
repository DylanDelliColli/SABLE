#!/usr/bin/env python3
"""Integration tests for the activation-debt ledger (SABLE-3zjc1).

REAL COMPOSITION, NO MOCKS. A REAL git repository (git init, real commits, real
`git diff`), the REAL bin/sable-orchestration-install and the REAL
bin/sable-bin-install run against it, a REAL installed layout on disk, and the
acceptance checks executed as REAL `grep` subprocesses against the REAL
installed files. Nothing synthetic stands between the ledger and the thing it
measures, because the entire defect class lives in the gap between what a
landing did to the repo and what is true at the consumed path — a fixture in
that gap would test nothing, which is precisely how three landed-but-inert
fixes accumulated while every instrument read green.

THE SCRATCH HOME IS THE SAFETY PROPERTY OF THIS SUITE. Every install runs with
CLAUDE_USER_DIR / --dir pointed inside tmp_path. SABLE-2avau records that
`sable-orchestration-install --user` targets the LIVE ~/.claude for artifact
writes unless that variable redirects it, and SABLE-k0nvp records a pinning
suite polluting the real ~/.local twice. Landing a hook or a settings row on
the developer's live scope from a test run is an unbrokered activation.

ONE PIECE OF FIXTURE SETUP IS NOT AN INSTALLER RUN, and it is called out so it
is not mistaken for a mock: the install.sh-owned hook is placed at its consumed
path as a stale copy by hand. That is not standing in for the system under
test — it reconstructs PRIOR HISTORY (an earlier install.sh run), which is
exactly the state the live incident started from. install.sh installs from its
own checkout by location, so running it here would install the developer's real
repo and could not be pointed at the scratch fleet at all.
"""
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import sable_activation_debt_lib as debt  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ORCH_INSTALLER = REPO / "bin" / "sable-orchestration-install"
BIN_INSTALLER = REPO / "bin" / "sable-bin-install"
SNIPPET = REPO / "templates" / "multi-manager" / "settings-snippet.json"
AGENTS_YAML = REPO / "templates" / "multi-manager" / "agents.yaml"
INBOX_COMMAND = REPO / "templates" / "multi-manager" / "commands" / "inbox.md"

pytestmark = pytest.mark.skipif(
    not all(
        Path(p).exists()
        for p in (ORCH_INSTALLER, BIN_INSTALLER, SNIPPET, INBOX_COMMAND)
    ),
    reason="real installers/templates absent from this checkout",
)

# The content each landing adds. Long and specific enough to clear
# MIN_PROPERTY_LEN and to be genuinely absent from the pre-fix file — an
# acceptance built on a common fragment is a green that could not have failed.
ROLE_FIX = 'run_in_background: false  # chuck must not background the merge'
HOOK_FIX = 'timeout 600 bd close "$BEAD" --reason "$REASON"'
TOOL_FIX = 'echo "sable-demo-tool: the landed behaviour"'


@dataclass
class Fleet:
    repo: Path
    claude: Path
    local_bin: Path
    roots: debt.Roots
    base_sha: str


def git(repo, *args, check=True):
    cp = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test",
         "-c", "user.email=test@example.invalid", *args],
        capture_output=True, text=True, timeout=60)
    if check:
        assert cp.returncode == 0, f"git {args} failed:\n{cp.stdout}\n{cp.stderr}"
    return cp


@pytest.fixture
def fleet(tmp_path):
    """A real git repo shaped like SABLE plus a scratch HOME's install roots."""
    repo = tmp_path / "fleet"
    (repo / "hooks" / "multi-manager").mkdir(parents=True)
    (repo / "bin").mkdir(parents=True)
    (repo / "templates" / "multi-manager" / "roles").mkdir(parents=True)
    (repo / "templates" / "multi-manager" / "commands").mkdir()

    # Source validation in the real installer rejects a settings snippet whose
    # registered hooks are absent: such a tree would wire silent instruments,
    # not model a usable checkout. Copy the full registered hook surface while
    # preserving this fixture's deliberately stale post-push hook below.
    for source in sorted((REPO / "hooks" / "multi-manager").glob("*.sh")):
        if source.name == "post-push-merge-notify.sh":
            continue
        shutil.copy2(source, repo / "hooks" / "multi-manager" / source.name)

    skill = repo / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\n---\n")

    # Pre-fix content everywhere: each of these is what the CONSUMED copy will
    # hold after the first install, and what the landing then supersedes.
    (repo / "templates" / "multi-manager" / "roles" / "chuck.md").write_text(
        "# chuck — the merge seat\n")
    for role in ("lincoln", "optimus", "tarzan"):
        (repo / "templates" / "multi-manager" / "roles" / f"{role}.md").write_text(
            f"# {role}\n")
    (repo / "hooks" / "multi-manager" / "post-push-merge-notify.sh").write_text(
        "#!/usr/bin/env bash\n: pre-fix notify hook\n")
    (repo / "hooks" / "tdd-evidence.sh").write_text(
        "#!/usr/bin/env bash\n: pre-fix evidence hook\n")
    (repo / "bin" / "sable-demo-tool").write_text(
        '#!/usr/bin/env bash\necho "sable-demo-tool: pre-fix"\n')
    (repo / "bin" / "sable-demo-tool").chmod(0o755)
    (repo / "bin" / "test_demo_decision.py").write_text("def test_x():\n    pass\n")
    # The real bin installer, run from the scratch tree so BIN_DIR resolves here.
    (repo / "bin" / "sable-bin-install").write_bytes(BIN_INSTALLER.read_bytes())
    (repo / "bin" / "sable-bin-install").chmod(0o755)
    # Real templates the orchestration installer requires.
    (repo / "templates" / "multi-manager" / "settings-snippet.json").write_bytes(
        SNIPPET.read_bytes())
    (repo / "templates" / "multi-manager" / "agents.yaml").write_bytes(
        AGENTS_YAML.read_bytes() if AGENTS_YAML.exists() else b"agents: {}\n")
    (repo / "templates" / "multi-manager" / "commands" / "inbox.md").write_bytes(
        INBOX_COMMAND.read_bytes()
    )

    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "base")
    base_sha = git(repo, "rev-parse", "HEAD").stdout.strip()

    home = tmp_path / "home"
    claude = home / ".claude"
    local_bin = home / ".local" / "bin"
    claude.mkdir(parents=True)
    local_bin.mkdir(parents=True)

    return Fleet(repo=repo, claude=claude, local_bin=local_bin, base_sha=base_sha,
                 roots=debt.Roots(claude=str(claude), local_bin=str(local_bin)))


def run_orchestration_install(fleet):
    """The REAL installer, into the scratch scope, from the scratch fleet."""
    cp = subprocess.run(
        ["bash", str(ORCH_INSTALLER), "--user"],
        env={**os.environ, "CLAUDE_USER_DIR": str(fleet.claude),
             "SABLE_REPO_DIR": str(fleet.repo),
             "SABLE_RECONCILE_TARGET_REPO": str(fleet.repo)},
        capture_output=True, text=True, timeout=180)
    assert cp.returncode == 0, f"installer failed:\n{cp.stdout}\n{cp.stderr}"
    return cp


def run_bin_install(fleet):
    """The REAL bin installer — it makes LIVE SYMLINKS, which is the control."""
    cp = subprocess.run(
        ["bash", str(fleet.repo / "bin" / "sable-bin-install"),
         "--dir", str(fleet.local_bin)],
        capture_output=True, text=True, timeout=180)
    assert cp.returncode == 0, f"bin install failed:\n{cp.stdout}\n{cp.stderr}"
    return cp


def land(fleet, edits, message):
    """Append real content to real files and make a real commit."""
    for rel, text in edits.items():
        path = fleet.repo / rel
        path.write_text(path.read_text() + text + "\n")
    git(fleet.repo, "add", "-A")
    git(fleet.repo, "commit", "-q", "--no-verify", "-m", message)
    return git(fleet.repo, "rev-parse", "HEAD").stdout.strip()


def obligations(fleet, paths, bead, head_sha):
    return debt.obligations_for_landing(
        str(fleet.repo), paths, bead, fleet.base_sha, head_sha,
        roots=fleet.roots, roots_live=(str(fleet.repo),))


# --------------------------------------------------------------------------
# The main sequence: open at landing, closed only by the install
# --------------------------------------------------------------------------

def test_obligation_opens_at_landing_and_closes_only_when_the_install_runs(fleet):
    """The bead's integration spec, end to end against real tools.

    Land a change to a copy-installed file; assert an open obligation exists
    and its acceptance command FAILS; run the install; assert the acceptance
    command now PASSES and the obligation closes.
    """
    run_orchestration_install(fleet)
    consumed = fleet.claude / "sable" / "roles" / "chuck.md"
    assert consumed.is_file() and not consumed.is_symlink(), \
        "the premise of this test is that the consumed copy is a COPY"
    assert ROLE_FIX not in consumed.read_text()

    head = land(fleet, {"templates/multi-manager/roles/chuck.md": ROLE_FIX},
                "fix(chuck): stop backgrounding the merge")

    owed = obligations(fleet, ["templates/multi-manager/roles/chuck.md"],
                       "SABLE-jb5l8", head)
    assert len(owed) == 1
    obligation = owed[0]

    # P1: created mechanically. The acceptance came out of the diff — nothing
    # in this call passed a property, a command, or any seat prose.
    assert obligation.acceptance.kind is debt.AcceptanceKind.PROPERTY_GREP
    assert obligation.acceptance.prop == ROLE_FIX
    # P2/P3: a behaviour check at the consumed path, and the tool that clears it.
    assert obligation.acceptance.consumed_path == str(consumed)
    assert obligation.installer == debt.ORCHESTRATION_INSTALL
    debt.validate_acceptance(obligation.acceptance)

    # The acceptance FAILS — and the real grep, run as its own process from the
    # recorded argv, agrees. The ledger's verdict and the pasteable command are
    # the same thing, so the report cannot drift from what was judged.
    assert not debt.run_acceptance(obligation.acceptance).passed
    argv = debt.acceptance_command(obligation.acceptance)
    assert subprocess.run(argv, capture_output=True, text=True).returncode != 0

    debt.record_obligations(str(fleet.repo), owed)

    # P4: the work bead closes and the debt does not move.
    still_owed = debt.open_obligations(debt.load_obligations(str(fleet.repo)),
                                       bead_status={"SABLE-jb5l8": "closed"})
    assert [o.repo_path for o in still_owed] == \
        ["templates/multi-manager/roles/chuck.md"]

    # THE DISCHARGE: the real installer runs, and only now does it clear.
    run_orchestration_install(fleet)
    assert ROLE_FIX in consumed.read_text()
    assert debt.run_acceptance(obligation.acceptance).passed
    assert subprocess.run(argv, capture_output=True, text=True).returncode == 0
    assert debt.open_obligations(debt.load_obligations(str(fleet.repo)),
                                 bead_status={"SABLE-jb5l8": "closed"}) == []


def test_a_live_symlink_path_never_creates_an_obligation(fleet):
    """THE PAIRED CONTROL, load-bearing. sable-bin-install makes a LIVE SYMLINK
    into the checkout, so the pull IS the activation and no debt is owed —
    before the landing, after it, and after an install run.

    Without this leg the ledger would fire on every ordinary landing, become
    noise, and get ignored (the SABLE-r5pfw erosion), leaving the fleet worse
    off than with no ledger at all.
    """
    run_bin_install(fleet)
    linked = fleet.local_bin / "sable-demo-tool"
    assert linked.is_symlink(), "the premise: the real bin installer links"
    assert os.path.realpath(linked) == str(fleet.repo / "bin" / "sable-demo-tool")

    head = land(fleet, {"bin/sable-demo-tool": TOOL_FIX}, "feat(demo): behaviour")

    assert obligations(fleet, ["bin/sable-demo-tool"], "SABLE-31b5l", head) == []
    # The landing already activated it: the symlink reads the new content with
    # no install at all. That is what makes the obligation genuinely absent
    # rather than merely unrecorded.
    assert TOOL_FIX in linked.read_text()

    run_bin_install(fleet)
    assert obligations(fleet, ["bin/sable-demo-tool"], "SABLE-31b5l", head) == []


# --------------------------------------------------------------------------
# P3: the discriminating case that failed live
# --------------------------------------------------------------------------

def test_a_green_run_of_installer_A_does_not_discharge_installer_Bs_file(fleet):
    """chuck's suggested acceptance for the ledger's own tests, and the one
    that failed live: given a file owned by installer B, a SUCCESSFUL run of
    installer A must not mark it discharged.

    This also measures THE DANGEROUS PROPERTY directly — the orchestration
    installer's output contains zero mentions of the file, so its exit 0 and
    "all files identical to source" are true of its own scope and silent about
    the file the operator cared about.
    """
    run_orchestration_install(fleet)

    # PRIOR HISTORY, not a mock: an earlier install.sh run left a stale copy.
    base_consumed = fleet.claude / "hooks" / "tdd-evidence.sh"
    base_consumed.parent.mkdir(parents=True, exist_ok=True)
    base_consumed.write_text((fleet.repo / "hooks" / "tdd-evidence.sh").read_text())

    head = land(fleet, {
        "hooks/tdd-evidence.sh": HOOK_FIX,                              # install.sh
        "hooks/multi-manager/post-push-merge-notify.sh": ROLE_FIX,      # orchestration
    }, "fix: two hooks, two installers")

    owed = obligations(fleet, ["hooks/tdd-evidence.sh",
                               "hooks/multi-manager/post-push-merge-notify.sh"],
                       "SABLE-z95e2", head)
    by_path = {o.repo_path: o for o in owed}
    assert by_path["hooks/tdd-evidence.sh"].installer == debt.BASE_INSTALL
    assert by_path["hooks/multi-manager/post-push-merge-notify.sh"].installer == \
        debt.ORCHESTRATION_INSTALL

    cp = run_orchestration_install(fleet)          # exits 0, on its own terms
    assert "tdd-evidence" not in cp.stdout + cp.stderr, \
        "the silence is the hazard: a correct omission produces no signal"

    statuses = debt.discharge_scan(owed, ran_installer=debt.ORCHESTRATION_INSTALL)
    by_bead_path = {s.obligation.repo_path: s for s in statuses}

    orch = by_bead_path["hooks/multi-manager/post-push-merge-notify.sh"]
    base = by_bead_path["hooks/tdd-evidence.sh"]
    assert orch.state is debt.ObligationState.DISCHARGED
    assert orch.in_scope_of_run is True
    # The row a green run could not possibly have touched. Believing it
    # discharged by watching the wrong tool succeed is the live failure.
    assert base.state is debt.ObligationState.OPEN
    assert base.in_scope_of_run is False
    assert debt.NOT_IN_SCOPE in base.detail
    assert HOOK_FIX not in base_consumed.read_text()

    # And the seat-facing report says both things without a reader inferring.
    report = debt.format_ledger_report(statuses)
    assert "manual paste" in report
    assert "hooks/tdd-evidence.sh" in report


def test_a_stale_checkout_install_leaves_the_obligation_open(fleet):
    """THE STRENGTHENED CRITERION, reproduced against real tools: an acceptance
    must be independent of the reference the install used.

    chuck's first run installed from a checkout at 0c52680 while the spine was
    d5eae21, exited 0, and was silent; his "live matches HEAD" acceptance
    PASSED against pre-fix content because the installer had copied from that
    same stale tree. Here the install genuinely runs and genuinely succeeds
    while the working tree sits at the pre-fix commit — and the ref-independent
    property check, which asks whether the file HAS the property rather than
    whether it matches some reference, still says OPEN.
    """
    run_orchestration_install(fleet)
    head = land(fleet, {"templates/multi-manager/roles/chuck.md": ROLE_FIX},
                "fix(chuck): the landing the stale install will miss")
    owed = obligations(fleet, ["templates/multi-manager/roles/chuck.md"],
                       "SABLE-jb5l8", head)[0]

    # The spine has the fix; the checkout the installer will read does not.
    git(fleet.repo, "checkout", "-q", fleet.base_sha)
    cp = run_orchestration_install(fleet)
    assert cp.returncode == 0                     # green, on its own terms
    git(fleet.repo, "checkout", "-q", "-")

    consumed = Path(owed.acceptance.consumed_path)
    stale = git(fleet.repo, "show",
                f"{fleet.base_sha}:templates/multi-manager/roles/chuck.md").stdout
    # The installed file is BYTE-IDENTICAL to the stale tree the install read,
    # so a hash-equality acceptance against that reference would PASS — the
    # verifier and the operation agreeing because they share one reference,
    # which is agreement that carries no information.
    assert consumed.read_text() == stale
    assert ROLE_FIX not in consumed.read_text()
    # The ref-independent check disagrees, which is the whole point.
    assert not debt.run_acceptance(owed.acceptance).passed
    assert debt.open_obligations([owed]) == [owed]


# --------------------------------------------------------------------------
# The dispatch-time leg, measured against the real installed layout
# --------------------------------------------------------------------------

def test_landability_reads_the_real_installed_layout(fleet):
    """Four paths, four different activation answers, none of them derivable
    from the footprint alone — measured with real readlink over real installs."""
    run_orchestration_install(fleet)
    run_bin_install(fleet)

    def klass(path):
        return debt.classify_path(str(fleet.repo), path, fleet.roots,
                                  (str(fleet.repo),)).activation

    assert klass("bin/sable-demo-tool") is debt.ActivationClass.HOT_SWAP
    assert klass("templates/multi-manager/roles/chuck.md") is \
        debt.ActivationClass.INSTALL_OWED
    assert klass("bin/test_demo_decision.py") is debt.ActivationClass.INERT

    # NEGATIVE CONTROL for this leg: a footprint of test-only and repo-only
    # paths must come back freely landable. A predicate that answered hot-swap
    # for everything would stop all dispatch under a constrained regime.
    free = debt.landability(str(fleet.repo), ["bin/test_demo_decision.py"],
                            fleet.roots, (str(fleet.repo),))
    assert free.verdict is debt.Landability.FREE

    held = debt.landability(str(fleet.repo),
                            ["bin/test_demo_decision.py", "bin/sable-demo-tool"],
                            fleet.roots, (str(fleet.repo),))
    assert held.verdict is debt.Landability.HOT_SWAP


def test_the_cli_report_runs_against_a_real_ledger(fleet):
    """The cadence report a seat reads, exercised through the module's own
    process boundary rather than by calling the renderer directly."""
    run_orchestration_install(fleet)
    head = land(fleet, {"templates/multi-manager/roles/chuck.md": ROLE_FIX},
                "fix(chuck): for the report")

    cp = subprocess.run(
        [sys.executable, str(REPO / "bin" / "sable_activation_debt_lib.py"),
         "--repo", str(fleet.repo), "stamp", "--bead", "SABLE-jb5l8",
         "--from-sha", fleet.base_sha, "--to-sha", head,
         "templates/multi-manager/roles/chuck.md"],
        env={**os.environ, "HOME": str(fleet.claude.parent),
             "CLAUDE_USER_DIR": str(fleet.claude)},
        capture_output=True, text=True, timeout=120)
    assert cp.returncode == 0, cp.stderr
    assert "OPEN" in cp.stdout
    assert debt.ORCHESTRATION_INSTALL in cp.stdout

    report = subprocess.run(
        [sys.executable, str(REPO / "bin" / "sable_activation_debt_lib.py"),
         "--repo", str(fleet.repo), "report"],
        env={**os.environ, "HOME": str(fleet.claude.parent),
             "CLAUDE_USER_DIR": str(fleet.claude)},
        capture_output=True, text=True, timeout=120)
    assert report.returncode == 0, report.stderr
    assert "ACTIVATION DEBT" in report.stdout
    assert "templates/multi-manager/roles/chuck.md" in report.stdout
