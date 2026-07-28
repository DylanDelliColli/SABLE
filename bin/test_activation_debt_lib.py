#!/usr/bin/env python3
"""Unit tests for the install/activation obligation ledger (SABLE-3zjc1).

These drive the pure classification, acceptance-validation and lifecycle logic
against a REAL filesystem layout in tmp_path — real files, real symlinks, real
`os.path.realpath` — because every defect this module exists for lives in the
difference between what a path is NAMED and what it RESOLVES TO. A mocked
filesystem here would agree with whatever the code believed.

THE SCRATCH HOME IS NOT OPTIONAL. `Roots` is always constructed from tmp_path;
nothing in this suite may read or write the developer's live ~/.claude or
~/.local (SABLE-2avau, SABLE-k0nvp).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import sable_activation_debt_lib as debt  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures — a real repo-shaped tree and a real installed layout
# --------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    """A repo-shaped tree with one file of each ownership family."""
    root = tmp_path / "repo"
    (root / "hooks" / "multi-manager").mkdir(parents=True)
    (root / "bin").mkdir(parents=True)
    (root / "templates" / "multi-manager" / "roles").mkdir(parents=True)
    (root / "hooks" / "tdd-evidence.sh").write_text(
        "#!/usr/bin/env bash\ntimeout 600 bd close \"$BEAD\"\n")
    (root / "hooks" / "multi-manager" / "post-push-merge-notify.sh").write_text(
        "#!/usr/bin/env bash\npython3 \"$HOOK_DIR/../../bin/sable_demo_lib.py\"\n")
    (root / "bin" / "sable_demo_lib.py").write_text("DEMO = 1\n")
    (root / "bin" / "sable-hook-matrix").write_text("#!/usr/bin/env bash\n:\n")
    (root / "templates" / "multi-manager" / "roles" / "chuck.md").write_text(
        "# chuck\nrun_in_background: false\n")
    (root / "bin" / "test_promote_decision.py").write_text("def test_x():\n    pass\n")
    return root


@pytest.fixture
def roots(tmp_path):
    """A scratch HOME's worth of install roots. Never the live ones."""
    claude = tmp_path / "home" / ".claude"
    local_bin = tmp_path / "home" / ".local" / "bin"
    claude.mkdir(parents=True)
    local_bin.mkdir(parents=True)
    return debt.Roots(claude=str(claude), local_bin=str(local_bin))


def install_copy(roots, consumed_rel, text):
    """Install as a PLAIN COPY — the shape that owes an install."""
    dest = Path(roots.claude if not consumed_rel.startswith("<bin>")
                else roots.local_bin) / consumed_rel.replace("<bin>/", "")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    return dest


def install_symlink(roots, consumed_rel, target):
    """Install as a LIVE SYMLINK into the checkout — the hot-swap shape."""
    dest = Path(roots.local_bin) / consumed_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(target)
    return dest


# --------------------------------------------------------------------------
# Ownership: which tool discharges, and what authorization it needs (P3)
# --------------------------------------------------------------------------

def test_ownership_separates_the_two_hook_installers(repo, roots):
    """THE DISCRIMINATING CASE from the live instance. hooks/tdd-evidence.sh and
    hooks/multi-manager/post-push-merge-notify.sh look like the same kind of
    file and are owned by DIFFERENT installers with different blast radii."""
    base = debt.consumed_sites(str(repo), "hooks/tdd-evidence.sh", roots)
    orch = debt.consumed_sites(
        str(repo), "hooks/multi-manager/post-push-merge-notify.sh", roots)

    assert [s.installer for s in base] == [debt.BASE_INSTALL]
    assert [s.installer for s in orch] == [debt.ORCHESTRATION_INSTALL]
    # The authorization column, not just the tool name: artifact installation
    # and settings activation have different consent boundaries.
    assert "manual paste" in base[0].authorization
    assert "--merge-settings" in orch[0].authorization


def test_multi_manager_rule_wins_over_the_generic_hook_rule(repo, roots):
    """First-match-wins ordering is load-bearing: hooks/*.sh would otherwise
    swallow hooks/multi-manager/*.sh and attribute the orchestration layer to
    install.sh — an ownership error in the direction that makes a seat watch
    the wrong tool succeed."""
    sites = debt.consumed_sites(
        str(repo), "hooks/multi-manager/post-push-merge-notify.sh", roots)
    assert sites[0].consumed_path == os.path.join(
        roots.claude, "hooks", "multi-manager", "post-push-merge-notify.sh")


def test_hook_sibling_lib_site_is_derived_from_the_reference_not_hardcoded(repo, roots):
    """The hook reaches for ../../bin/<lib>.py from the INSTALLED hook dir, so
    the lib is consumed from {claude}/bin/ — not from beside the hook. Derived
    by resolving the reference, which is what the installer itself does."""
    sites = debt.consumed_sites(str(repo), "bin/sable_demo_lib.py", roots)
    assert len(sites) == 1
    assert sites[0].consumed_path == os.path.join(roots.claude, "bin",
                                                  "sable_demo_lib.py")
    assert sites[0].installer == debt.ORCHESTRATION_INSTALL


def test_unowned_paths_have_no_consumed_site(repo, roots):
    assert debt.consumed_sites(str(repo), "bin/test_promote_decision.py", roots) == []


# --------------------------------------------------------------------------
# Required by the bead's test spec
# --------------------------------------------------------------------------

def test_copy_installed_path_in_diff_creates_an_obligation(repo, roots):
    """A diff touching a path whose installed copy is a REAL FILE yields an
    obligation naming the target and an acceptance command."""
    install_copy(roots, "hooks/tdd-evidence.sh", "#!/usr/bin/env bash\n:\n")

    owed = debt.obligations_for_landing(
        str(repo), ["hooks/tdd-evidence.sh"], "SABLE-z95e2",
        roots=roots, roots_live=(str(repo),),
        acceptances={"hooks/tdd-evidence.sh": debt.Acceptance(
            debt.AcceptanceKind.PROPERTY_GREP,
            os.path.join(roots.claude, "hooks", "tdd-evidence.sh"),
            prop="timeout 600 bd close")})

    assert len(owed) == 1
    obligation = owed[0]
    assert obligation.consumed_path == os.path.join(roots.claude, "hooks",
                                                    "tdd-evidence.sh")
    assert obligation.installer == debt.BASE_INSTALL
    assert debt.acceptance_command(obligation.acceptance) == [
        "grep", "-c", "-F", "-e", "timeout 600 bd close", "--",
        obligation.consumed_path]


def test_symlinked_path_in_diff_creates_NO_obligation(repo, roots):
    """NEGATIVE CONTROL, load-bearing. A path installed as a LIVE SYMLINK
    activates on pull and must NOT generate debt.

    Without this the ledger fires on every landing, becomes noise, and gets
    ignored — the SABLE-r5pfw erosion, which would leave us worse off than no
    ledger at all. Paired with the test above so a ledger that simply never
    fires cannot pass both.
    """
    install_symlink(roots, "sable-hook-matrix", repo / "bin" / "sable-hook-matrix")

    owed = debt.obligations_for_landing(
        str(repo), ["bin/sable-hook-matrix"], "SABLE-31b5l",
        roots=roots, roots_live=(str(repo),))

    assert owed == []
    verdict = debt.classify_path(str(repo), "bin/sable-hook-matrix", roots,
                                 (str(repo),))
    assert verdict.activation is debt.ActivationClass.HOT_SWAP


def test_symlink_into_ANOTHER_worktree_is_still_hot_swap(repo, roots, tmp_path):
    """The live shape, measured 2026-07-26: ~/.local/bin/sable-hook-matrix
    resolves into the SHARED checkout while the worker asking the question sits
    in a wk-* worktree. Comparing against the caller's own tree alone would
    call that a plain copy and report an install owed for a file that activates
    on merge — wrong class, wrong tool, wrong landability answer."""
    other = tmp_path / "shared-checkout"
    (other / "bin").mkdir(parents=True)
    (other / "bin" / "sable-hook-matrix").write_text("#!/usr/bin/env bash\n:\n")
    install_symlink(roots, "sable-hook-matrix", other / "bin" / "sable-hook-matrix")

    # `repo` is the caller's worktree; `other` is the checkout that was installed
    # from. Only the multi-root comparison gets this right.
    assert debt.classify_path(str(repo), "bin/sable-hook-matrix", roots,
                              (str(repo),)).activation is debt.ActivationClass.INSTALL_OWED
    assert debt.classify_path(str(repo), "bin/sable-hook-matrix", roots,
                              (str(repo), str(other))).activation is debt.ActivationClass.HOT_SWAP


def test_pinned_snapshot_symlink_is_NOT_hot_swap(repo, roots, tmp_path):
    """The shape check's most expensive lie. sable-bin-install can PIN a tool as
    a symlink into a ~/.local/lib/sable-<sha>/ snapshot: it IS a symlink, and it
    does NOT hot-swap — a landing leaves it serving the pinned sha. "Is it a
    symlink?" says hot-swap here and is wrong; resolving the link says
    install-owed and is right."""
    snapshot = tmp_path / "lib" / "sable-0c52680"
    snapshot.mkdir(parents=True)
    (snapshot / "sable-hook-matrix").write_text("#!/usr/bin/env bash\n: stale\n")
    dest = install_symlink(roots, "sable-hook-matrix", snapshot / "sable-hook-matrix")

    assert dest.is_symlink()          # the shape check's answer
    assert debt.classify_path(str(repo), "bin/sable-hook-matrix", roots,
                              (str(repo),)).activation is debt.ActivationClass.INSTALL_OWED


def test_a_claimed_but_absent_path_reads_from_whether_the_LAYER_is_installed(repo, roots):
    """BOTH POLARITIES of the absent-consumed-copy rule, which need OPPOSITE
    answers for the same observation ("nothing is there").

    Layer installed, this ONE file missing -> a brand-new artifact whose first
    install is genuinely owed, and the case the ledger most needs to catch.
    Layer never installed at this scope (a consumer project, a fresh machine)
    -> nothing consumes these paths, and stamping debt for every one of them is
    the noise that gets a ledger ignored.
    """
    def klass(path):
        return debt.classify_path(str(repo), path, roots, (str(repo),)).activation

    # Nothing installed anywhere yet: ~/.claude/hooks/ does not exist.
    assert not Path(roots.claude, "hooks").exists()
    assert klass("hooks/tdd-evidence.sh") is debt.ActivationClass.INERT

    # The layer is installed (a sibling is present) and this file is not.
    install_copy(roots, "hooks/tdd-gate.sh", ": some other hook\n")
    assert klass("hooks/tdd-evidence.sh") is debt.ActivationClass.INSTALL_OWED


def test_acceptance_is_a_behaviour_check_not_a_shape_check(roots):
    """Pin the distinction, because the shape check is the tempting cheap one
    and it is the one that reports success while the fix is inert."""
    consumed = os.path.join(roots.claude, "hooks", "tdd-evidence.sh")

    shape = debt.Acceptance(debt.AcceptanceKind.SHAPE, consumed)
    assert not shape.is_behaviour_check
    with pytest.raises(debt.AcceptanceTooWeak, match="activation MODEL"):
        debt.validate_acceptance(shape)

    behaviour = debt.Acceptance(debt.AcceptanceKind.PROPERTY_GREP, consumed,
                                prop="timeout 600 bd close")
    assert behaviour.is_behaviour_check and behaviour.is_ref_independent
    assert debt.validate_acceptance(behaviour) is behaviour
    # It executes AGAINST THE CONSUMED PATH, not against the repo copy.
    assert debt.acceptance_command(behaviour)[-1] == consumed


def test_hash_equality_acceptance_is_refused_alone_and_must_name_the_ref(roots):
    """THE STRENGTHENING tarzan added within an hour of filing, from a live
    instance: chuck's acceptance was "live matches HEAD" — a check AT THE
    CONSUMED PATH, it hashes the installed file — and it PASSED against
    PRE-fix content, because the installer had copied from that same stale tree
    and the verifier shared the operation's corrupted reference."""
    consumed = os.path.join(roots.claude, "hooks", "tdd-evidence.sh")
    prop = debt.Acceptance(debt.AcceptanceKind.PROPERTY_GREP, consumed,
                           prop="timeout 600 bd close")

    alone = debt.Acceptance(debt.AcceptanceKind.HASH_EQUALITY, consumed,
                            ref="origin/tmux-only")
    assert not alone.is_ref_independent
    with pytest.raises(debt.AcceptanceTooWeak, match="SHARES"):
        debt.validate_acceptance(alone)

    # Paired, but against an unqualified HEAD — HEAD is whatever the local
    # checkout happens to be, which is the variable that went wrong.
    unnamed = debt.Acceptance(debt.AcceptanceKind.HASH_EQUALITY, consumed,
                              ref="HEAD", paired=prop)
    with pytest.raises(debt.AcceptanceTooWeak, match="NAME THE REF"):
        debt.validate_acceptance(unnamed)

    paired = debt.Acceptance(debt.AcceptanceKind.HASH_EQUALITY, consumed,
                             ref="origin/tmux-only", paired=prop)
    assert debt.validate_acceptance(paired) is paired


def test_a_too_common_property_is_refused_as_an_acceptance(roots):
    """A green that could not have failed. `fi` greps non-zero against almost
    any shell file, so an acceptance built on one reports the fix activated no
    matter what the installer did."""
    consumed = os.path.join(roots.claude, "hooks", "tdd-evidence.sh")
    with pytest.raises(debt.AcceptanceTooWeak, match="too common"):
        debt.validate_acceptance(
            debt.Acceptance(debt.AcceptanceKind.PROPERTY_GREP, consumed, prop="fi"))


def test_obligation_survives_bead_close(repo, roots):
    """THE WHOLE DEFECT: all three founding instances have CLOSED beads.

    Both polarities in one test — the bead's status moves and the debt does
    not; the ACCEPTANCE's answer moves and the debt does.
    """
    consumed = install_copy(roots, "hooks/tdd-evidence.sh",
                            "#!/usr/bin/env bash\n: stale, pre-fix\n")
    obligation = debt.Obligation(
        bead="SABLE-z95e2", repo_path="hooks/tdd-evidence.sh",
        consumed_path=str(consumed), installer=debt.BASE_INSTALL,
        authorization=debt.AUTH_BASE,
        acceptance=debt.Acceptance(debt.AcceptanceKind.PROPERTY_GREP,
                                   str(consumed), prop="timeout 600 bd close"))

    assert debt.open_obligations([obligation], bead_status={}) == [obligation]
    assert debt.open_obligations(
        [obligation], bead_status={"SABLE-z95e2": "closed"}) == [obligation]

    # ...and the ONE thing that does clear it: the acceptance going true.
    consumed.write_text("#!/usr/bin/env bash\ntimeout 600 bd close \"$BEAD\"\n")
    assert debt.open_obligations(
        [obligation], bead_status={"SABLE-z95e2": "closed"}) == []


# --------------------------------------------------------------------------
# P3: a successful run of the WRONG installer discharges nothing
# --------------------------------------------------------------------------

def test_a_successful_run_of_installer_A_does_not_discharge_installer_Bs_file(repo, roots):
    """THE CASE THAT FAILED LIVE. Both lincoln and chuck expected an authorized
    sable-orchestration-install run to clear hooks/tdd-evidence.sh; that
    installer never mentions the file (install.sh owns it) and the run exited 0
    reporting "all files identical to source" — true of its own scope, silent
    about the file they cared about."""
    orch_consumed = install_copy(
        roots, "hooks/multi-manager/post-push-merge-notify.sh",
        "#!/usr/bin/env bash\nOVERLAPS_BRIEF=1 # the landed fix\n")
    base_consumed = install_copy(roots, "hooks/tdd-evidence.sh",
                                 "#!/usr/bin/env bash\n: stale, pre-fix\n")

    owned_by_orch = debt.Obligation(
        bead="SABLE-o7ipa", repo_path="hooks/multi-manager/post-push-merge-notify.sh",
        consumed_path=str(orch_consumed), installer=debt.ORCHESTRATION_INSTALL,
        authorization=debt.AUTH_ORCHESTRATION,
        acceptance=debt.Acceptance(debt.AcceptanceKind.PROPERTY_GREP,
                                   str(orch_consumed), prop="OVERLAPS_BRIEF=1"))
    owned_by_base = debt.Obligation(
        bead="SABLE-z95e2", repo_path="hooks/tdd-evidence.sh",
        consumed_path=str(base_consumed), installer=debt.BASE_INSTALL,
        authorization=debt.AUTH_BASE,
        acceptance=debt.Acceptance(debt.AcceptanceKind.PROPERTY_GREP,
                                   str(base_consumed), prop="timeout 600 bd close"))

    statuses = debt.discharge_scan([owned_by_orch, owned_by_base],
                                   ran_installer=debt.ORCHESTRATION_INSTALL)
    by_bead = {s.obligation.bead: s for s in statuses}

    assert by_bead["SABLE-o7ipa"].state is debt.ObligationState.DISCHARGED
    # The row the successful run could not possibly have touched stays OPEN,
    # and says so in words rather than leaving a reader to infer it.
    assert by_bead["SABLE-z95e2"].state is debt.ObligationState.OPEN
    assert by_bead["SABLE-z95e2"].in_scope_of_run is False
    assert debt.NOT_IN_SCOPE in by_bead["SABLE-z95e2"].detail
    assert by_bead["SABLE-o7ipa"].in_scope_of_run is True


def test_discharge_never_consults_an_installers_exit_code(repo, roots):
    """State comes from the acceptance and only from the acceptance. An
    in-scope obligation whose consumed copy is still stale stays OPEN however
    green the run that claimed to install it."""
    consumed = install_copy(roots, "sable/roles/chuck.md", "# chuck\n: stale\n")
    obligation = debt.Obligation(
        bead="SABLE-jb5l8", repo_path="templates/multi-manager/roles/chuck.md",
        consumed_path=str(consumed), installer=debt.ORCHESTRATION_INSTALL,
        authorization=debt.AUTH_ORCHESTRATION,
        acceptance=debt.Acceptance(debt.AcceptanceKind.PROPERTY_GREP,
                                   str(consumed), prop="run_in_background: false"))
    status = debt.discharge_scan([obligation],
                                 ran_installer=debt.ORCHESTRATION_INSTALL)[0]
    assert status.in_scope_of_run is True
    assert status.state is debt.ObligationState.OPEN


def test_an_unrunnable_acceptance_is_open_not_discharged(roots):
    """Fail-closed. "I could not tell" must read as still-owed; the opposite
    would put the ledger's own error in the optimistic direction it exists to
    remove."""
    missing = debt.Acceptance(debt.AcceptanceKind.MISSING,
                              os.path.join(roots.claude, "nope.sh"))
    obligation = debt.Obligation("SABLE-x", "hooks/nope.sh",
                                 os.path.join(roots.claude, "nope.sh"),
                                 debt.BASE_INSTALL, debt.AUTH_BASE, missing)
    assert debt.discharge_scan([obligation])[0].state is debt.ObligationState.OPEN
    # And a property check against a file that does not exist at all.
    absent = debt.Acceptance(debt.AcceptanceKind.PROPERTY_GREP,
                             os.path.join(roots.claude, "nope.sh"),
                             prop="the property we installed it for")
    assert not debt.run_acceptance(absent).passed


# --------------------------------------------------------------------------
# P1: derived mechanically from the diff, with no seat prose
# --------------------------------------------------------------------------

def test_distinctive_property_prefers_a_line_that_can_actually_fail():
    lines = ["fi", "}", "    timeout 600 bd close \"$BEAD\" --reason \"$R\"", "  esac"]
    assert debt.distinctive_property(lines) == \
        "timeout 600 bd close \"$BEAD\" --reason \"$R\""


def test_no_distinctive_line_yields_MISSING_rather_than_a_weak_check():
    """Refusing to invent an acceptance is the safe direction: MISSING can
    never pass, so the obligation stays visible instead of self-clearing."""
    assert debt.distinctive_property(["fi", "}", "  ;;"]) == ""


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def test_ledger_round_trips_including_the_installer_and_authorization(repo, roots,
                                                                      tmp_path,
                                                                      monkeypatch):
    monkeypatch.setenv("SABLE_MERGE_GATE_STATE", str(tmp_path / "state"))
    consumed = install_copy(roots, "hooks/tdd-evidence.sh", ": stale\n")
    obligation = debt.Obligation(
        "SABLE-z95e2", "hooks/tdd-evidence.sh", str(consumed),
        debt.BASE_INSTALL, debt.AUTH_BASE,
        debt.Acceptance(debt.AcceptanceKind.PROPERTY_GREP, str(consumed),
                        prop="timeout 600 bd close"))

    debt.record_obligations(str(repo), [obligation])
    loaded = debt.load_obligations(str(repo))

    assert loaded == [obligation]
    # Re-recording the same landing must not double the debt.
    debt.record_obligations(str(repo), [obligation])
    assert len(debt.load_obligations(str(repo))) == 1


def test_a_corrupt_ledger_row_is_counted_not_silently_swallowed(repo, tmp_path,
                                                                monkeypatch):
    """A ledger that fails to parse and a ledger with no debt render
    identically, and only one of them is good news."""
    monkeypatch.setenv("SABLE_MERGE_GATE_STATE", str(tmp_path / "state"))
    path = debt.ledger_path(str(repo))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all\n")

    obligations, bad = debt.load_report(str(repo))
    assert obligations == [] and bad == 1
    assert "LOWER BOUND" in debt.format_ledger_report([], unreadable=bad)


# --------------------------------------------------------------------------
# The dispatch-time leg
# --------------------------------------------------------------------------

def test_landability_of_a_test_only_footprint_is_FREE(repo, roots):
    """NEGATIVE CONTROL for the dispatch leg, load-bearing. A predicate that
    classified everything as hot-swap would stop all dispatch under a
    constrained regime — worse than the gap it replaces, and the same
    always-serialize erosion recorded on SABLE-47try."""
    report = debt.landability(str(repo), ["bin/test_promote_decision.py"],
                              roots, (str(repo),))
    assert report.verdict is debt.Landability.FREE


def test_landability_answers_the_four_measured_shapes_differently(repo, roots):
    """The four paths optimus measured, none of whose activation answers is
    derivable from the footprint alone."""
    install_symlink(roots, "sable-hook-matrix", repo / "bin" / "sable-hook-matrix")
    install_copy(roots, "hooks/tdd-evidence.sh", ": stale\n")

    classes = {
        p: debt.classify_path(str(repo), p, roots, (str(repo),)).activation
        for p in ("bin/sable-hook-matrix", "hooks/tdd-evidence.sh",
                  "bin/test_promote_decision.py")
    }
    assert classes["bin/sable-hook-matrix"] is debt.ActivationClass.HOT_SWAP
    assert classes["hooks/tdd-evidence.sh"] is debt.ActivationClass.INSTALL_OWED
    assert classes["bin/test_promote_decision.py"] is debt.ActivationClass.INERT

    # A mixed footprint folds to the WORST class: one hot-swap path makes the
    # whole branch un-landable under a regime that holds hot-swaps.
    mixed = debt.landability(str(repo), list(classes), roots, (str(repo),))
    assert mixed.verdict is debt.Landability.HOT_SWAP


def test_unresolved_outranks_hot_swap_when_folding(repo, roots):
    """Fail-closed severity: an unreadable consumed path is not evidence of
    freedom to land."""
    assert debt._CLASS_SEVERITY[debt.ActivationClass.UNRESOLVED] > \
        debt._CLASS_SEVERITY[debt.ActivationClass.HOT_SWAP]
    assert debt._CLASS_SEVERITY[debt.ActivationClass.HOT_SWAP] > \
        debt._CLASS_SEVERITY[debt.ActivationClass.INSTALL_OWED]


# --------------------------------------------------------------------------
# The three founding instances must be representable (acceptance criterion 6)
# --------------------------------------------------------------------------

def test_the_three_founding_instances_are_representable(repo, roots):
    """z95e2, jb5l8 and o7ipa — with the SCOPE CORRECTION that they are NOT all
    owned by the same installer, which is what changes who discharges them."""
    owners = {
        "hooks/tdd-evidence.sh": debt.BASE_INSTALL,                        # z95e2
        "templates/multi-manager/roles/chuck.md": debt.ORCHESTRATION_INSTALL,  # jb5l8
        "hooks/multi-manager/post-push-merge-notify.sh":
            debt.ORCHESTRATION_INSTALL,                                    # o7ipa
    }
    for path, installer in owners.items():
        sites = debt.consumed_sites(str(repo), path, roots)
        assert sites, f"{path} has no consumed site — it would report INERT"
        assert sites[0].installer == installer, path


def test_report_names_the_discharging_tool_and_its_authorization(roots):
    """"An install is owed" is not actionable and is actively misleading when
    several installers with different blast radii exist."""
    consumed = install_copy(roots, "hooks/tdd-evidence.sh", ": stale\n")
    obligation = debt.Obligation(
        "SABLE-z95e2", "hooks/tdd-evidence.sh", str(consumed),
        debt.BASE_INSTALL, debt.AUTH_BASE,
        debt.Acceptance(debt.AcceptanceKind.PROPERTY_GREP, str(consumed),
                        prop="timeout 600 bd close"))
    text = debt.format_ledger_report(debt.discharge_scan([obligation]))

    assert "install.sh" in text
    assert "manual paste" in text
    assert "grep -c -F -e" in text
    assert "SABLE-z95e2" in text
    # The report states its own bound, so an empty ledger cannot be read as a
    # clean one.
    assert "reports INERT" in text
