#!/usr/bin/env python3
"""test_clean_room_binary_parity.py -- mechanical guard for SABLE-apt5a.

THE DEFECT THIS EXISTS FOR
---------------------------
ci-verify.yml's Clean-room contract check step fail-loud-checks tmux/jq/git
with `command -v X` before the suite runs, but never checked gh -- even
though two real call sites depend on it at runtime: the SABLE-r3i6 dedup
guard (.github/ci/preview-already-verified.sh's `gh api ...`) and
sable-merge-gate's wait_for_ci poll (bin/sable_gate_preview_lib.py's
`gh run list ...`). Both call sites are deliberately fail-open on a missing
gh, so the gap never crashed anything -- it just silently and permanently
defeated the SABLE-r3i6 dedup optimization, with zero signal that it had
happened. This test pins the fix in place: gh must be part of the checked
set, the same way tmux/jq/git already are.

DERIVATION, NOT A SECOND HARDCODED LIST
----------------------------------------
REQUIRED_SYSTEM_BINARIES below is not "every binary that could plausibly be
useful" -- a general scanner over every script's subprocess calls is
SABLE-eqpjn's scope, not this bead's. It is exactly the binaries this
repo's own CI scripts invoke on ci-verify.yml's job path: tmux/jq/git
(already checked before this bead) and gh (the two call sites named above).
The negative control asserts a binary NOT actually invoked by those scripts
is not silently demanded either -- otherwise the positive check would pass
by requiring everything, proving nothing about real coverage.

Clean-room safe (SABLE-59zu): reads only tracked workflow text, no
subprocess, no network.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_VERIFY_YML = REPO_ROOT / ".github" / "workflows" / "ci-verify.yml"

# The two real call sites, verified by hand (SABLE-apt5a):
#   .github/ci/preview-already-verified.sh  -- `gh api repos/.../actions/runs`
#   bin/sable_gate_preview_lib.py:_gh_runs  -- `gh run list --branch ...`
# tmux/jq/git predate this bead and were already checked; gh is what it adds.
REQUIRED_SYSTEM_BINARIES = {"tmux", "jq", "git", "gh"}

_COMMAND_V_RE = re.compile(r"command -v (\S+)")
_STEP_NAME_RE = re.compile(r"^ +- name:", re.MULTILINE)


def checked_binaries(contract_check_step_text: str) -> set[str]:
    """Binary names named in a `command -v X` presence check anywhere in the
    given text (expected to be the Clean-room contract check step's `run:`
    block, sliced out by contract_check_step_text() below)."""
    return set(_COMMAND_V_RE.findall(contract_check_step_text))


def contract_check_step_text(workflow_text: str, marker: str = "Clean-room contract check") -> str:
    """Slice a ci-verify.yml-shaped workflow down to just the step whose name
    contains `marker`, ending at the next `- name:` step. Slicing to one step
    (rather than scanning the whole file) means a `command -v X` anywhere
    else in the workflow -- there is none today -- cannot inflate this
    test's result and mask the contract check step itself missing gh."""
    start = workflow_text.index(marker)
    next_step = _STEP_NAME_RE.search(workflow_text, start + len(marker))
    end = next_step.start() if next_step else len(workflow_text)
    return workflow_text[start:end]


# --- static parity checks -----------------------------------------------------

def test_contract_check_declares_every_required_binary():
    """THE CHECK (SABLE-apt5a). Before this bead's fix, ci-verify.yml checked
    tmux/jq/git but not gh, even though the dedup guard and merge-gate poll
    both depend on it at runtime."""
    text = contract_check_step_text(CI_VERIFY_YML.read_text(encoding="utf-8"))
    checked = checked_binaries(text)
    missing = REQUIRED_SYSTEM_BINARIES - checked
    assert not missing, (
        f"ci-verify.yml's Clean-room contract check step does not check for "
        f"{sorted(missing)}, but the SABLE-r3i6 dedup guard and/or "
        f"sable-merge-gate's wait_for_ci depend on them at runtime "
        f"(SABLE-apt5a)."
    )


def test_required_set_does_not_demand_an_unrelated_binary():
    """Negative control: a binary this repo's CI scripts never invoke must
    not be on the required list, or the check above would be satisfiable by
    demanding everything and would prove nothing about real coverage."""
    assert "curl" not in REQUIRED_SYSTEM_BINARIES
    assert "docker" not in REQUIRED_SYSTEM_BINARIES


def test_contract_check_step_text_does_not_swallow_the_whole_file():
    """Sanity on the slicing helper itself: it must stop at the NEXT step,
    not silently fall back to end-of-file -- otherwise a `command -v` call in
    some unrelated later step could inflate the two tests above, defeating
    the point of slicing to just the contract check step at all."""
    workflow_text = CI_VERIFY_YML.read_text(encoding="utf-8")
    sliced = contract_check_step_text(workflow_text)
    assert len(sliced) < len(workflow_text)
    assert "pytest — full bin/ suite" not in sliced
