#!/usr/bin/env python3
"""Unit tests for the sable-contained containment mandate on role cards (SABLE-2c2wb).

SABLE-4snb4 added a block to optimus.md and tarzan.md instructing the seat to
verify containment with `sable-contained` rather than a hand-rolled git probe
(the exit-3 DISAGREE / exit-4 COULD-NOT-ASSESS semantics a hand-rolled probe
cannot express). chuck.md — the merge seat, which makes more containment calls
than both manager lanes combined — was left out of that pass entirely, because
the block was hand-pasted onto the two cards its author was thinking about
rather than derived from which roles actually make the call.

Keyed on FILE PATH, not role name (in-flight scope correction on this bead,
tarzan 2026-07-22). victor is dual-carded: templates/multi-manager/roles/
victor.md (installed to ~/.claude/sable/roles/ — NOT actually used; victor is
a producer, not a pane) and templates/agents/victor.md (generated from the
former by bin/sable-build-agents, installed by install.sh to
~/.claude/agents/victor.md — the file an Agent-tool `victor` spawn actually
loads as its system prompt, per bin/sable-build-agents's own docstring: role
files are edited, agents/*.md is generated, never hand-edited). A role-keyed
assertion checking only the roles/ copy is exactly the exit-3 blindness this
bead is about, one level up: it would go green while the file victor actually
executes from never got checked. hooks/test/test-agent-definitions.sh already
gates roles/victor.md <-> agents/victor.md sync (regenerate-and-diff, already
in the CI allowlist) — the file-keyed check here is deliberate defense in
depth, not a duplicate of that drift gate: it directly asserts the mandate
text exists in the file being loaded, rather than only inferring it via a
generator-sync argument.

test_containment_mandate_present_on_every_containment_making_file is
parametrised over the files verified (by reading each card, not by
assumption) to belong to a role that makes live containment/merged-vs-closed
determinations:
  - chuck.md — the merge seat itself (this bead's own premise)
  - optimus.md, tarzan.md — dispatch-gating on a sequenced blocker
  - lincoln.md — cross-lane status synthesis; SABLE-7yked is a live incident
    of a manager's false "merged" claim that only a Lincoln probe caught
  - roles/victor.md AND agents/victor.md — victor's fingerprint-grep-against-
    HEAD freshness check is contaminated by checkout lag during an active
    merge drain (SABLE-z9ux, open/unfixed): a not-found fingerprint can mean
    "genuinely fixed" or "my checkout hasn't pulled the merge yet", and only
    a real containment probe (exit 3 DISAGREE / exit 4 COULD-NOT-ASSESS) can
    tell the two apart. SABLE-nsmc note 13/14 additionally shows a victor
    presence determination was load-bearing AND correct against a false
    terminal claim — this is an observed containment call, not a reasoned one.

Its opposite-polarity companion, test_mandate_absent_from_non_containment_files,
is required so the first test cannot be satisfied by a blanket paste: columbo,
rudy, and sherlock (both their roles/ source and their generated agents/
copy) are session-scoped, read-only planning producers that never reason
about branch/ref containment — static analysis, a test-coverage interview, and
live browser QA against a deploy, respectively. Pasting the mandate onto their
cards would be noise that trains skimming (the SABLE-rhsuj failure mode), not
a fix.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROLES_DIR = REPO / "templates" / "multi-manager" / "roles"
AGENTS_DIR = REPO / "templates" / "agents"

CONTAINMENT_FILES = [
    ROLES_DIR / "chuck.md",
    ROLES_DIR / "optimus.md",
    ROLES_DIR / "tarzan.md",
    ROLES_DIR / "lincoln.md",
    ROLES_DIR / "victor.md",
    AGENTS_DIR / "victor.md",
]

NON_CONTAINMENT_FILES = [
    ROLES_DIR / "columbo.md",
    ROLES_DIR / "rudy.md",
    ROLES_DIR / "sherlock.md",
    AGENTS_DIR / "columbo.md",
    AGENTS_DIR / "rudy.md",
    AGENTS_DIR / "sherlock.md",
]

# \s+ (not a single-char class) because the cards word-wrap mid-phrase —
# e.g. optimus.md line-breaks "COULD\nNOT ASSESS" across a markdown line.
_DISAGREE_RE = re.compile(r"DISAGREE", re.IGNORECASE)
_COULD_NOT_ASSESS_RE = re.compile(r"COULD[\s-]+NOT[\s-]+ASSESS", re.IGNORECASE)


def _rel(path):
    return str(path.relative_to(REPO))


def _read(path):
    assert path.exists(), f"role card not found: {path}"
    return path.read_text()


@pytest.mark.parametrize("path", CONTAINMENT_FILES, ids=_rel)
def test_containment_mandate_present_on_every_containment_making_file(path):
    text = _read(path)
    rel = _rel(path)
    assert "sable-contained" in text, (
        f"{rel} belongs to a role that makes containment determinations but "
        "never mentions the sable-contained mandate"
    )
    assert _DISAGREE_RE.search(text), (
        f"{rel} mentions sable-contained but doesn't name the exit-3 "
        "DISAGREE semantics — a bare tool-name mention decays back into a "
        "hand-rolled probe that can't express disagreement"
    )
    assert _COULD_NOT_ASSESS_RE.search(text), (
        f"{rel} mentions sable-contained but doesn't name the exit-4 "
        "COULD NOT ASSESS semantics"
    )


@pytest.mark.parametrize("path", NON_CONTAINMENT_FILES, ids=_rel)
def test_mandate_absent_from_non_containment_files(path):
    text = _read(path)
    rel = _rel(path)
    assert "sable-contained" not in text, (
        f"{rel} carries the sable-contained mandate but this role never "
        "makes containment determinations — this is the indiscriminate-paste "
        "failure the bead explicitly warns against (SABLE-rhsuj-class noise)"
    )
