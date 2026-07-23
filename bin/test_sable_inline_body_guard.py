#!/usr/bin/env python3
"""Unit tests for sable_inline_body_guard_lib (SABLE-qwthx).

SABLE-qwthx's own text is explicit about why both polarities are required: a
guard that refuses everything is indistinguishable from a working one without
an allow-case proving it isn't just "always deny". The false-positive case
(backticks inside a --body-file path, which is not a monitored prose flag)
exists for the same reason SABLE-rhsuj is cited on the bead: a guard that
fires on unrelated commands becomes noise and gets suppressed. The
sable-msg case exists to prove the guard is PATTERN-KEYED across tools, not a
bd-only name list -- that name-list shape is the one that already failed once
(the bead's "second instance").
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sable_inline_body_guard_lib as guard  # noqa: E402


def test_refuses_bd_create_with_backtick_in_inline_description():
    cmd = 'bd create "add a helper" --description "see `bd hooks install` for context"'
    result = guard.classify(cmd)
    assert result['verdict'] == 'refuse'
    assert '--body-file' in result['reason']


def test_allows_bd_create_with_body_file():
    cmd = 'bd create "add a helper" --body-file /tmp/desc.md'
    result = guard.classify(cmd)
    assert result['verdict'] == 'allow'


def test_allows_inline_description_with_no_metacharacters():
    cmd = 'bd create "add a helper" --description "a plain sentence, no danger here"'
    result = guard.classify(cmd)
    assert result['verdict'] == 'allow'


def test_allows_backticks_inside_a_file_path_argument():
    # --body-file is the SAFE escape hatch itself -- its value is a path, not
    # monitored prose, so a stray backtick in the path must not fire.
    cmd = "bd create \"add a helper\" --body-file '/tmp/weird`name`.md'"
    result = guard.classify(cmd)
    assert result['verdict'] == 'allow'


def test_refuses_sable_msg_inline_body_with_dollar_paren():
    cmd = 'sable-msg optimus "status update: $(bd hooks install) landed"'
    result = guard.classify(cmd)
    assert result['verdict'] == 'refuse'
    assert '--body-file' in result['reason']
    assert result['surface_id'] == 'sable-msg-body'


def test_refuses_bd_update_append_notes_with_backticks():
    cmd = 'bd update SABLE-abc123 --append-notes "ran `bd hooks install` by accident"'
    result = guard.classify(cmd)
    assert result['verdict'] == 'refuse'
    assert '--file' in result['reason']
    assert result['surface_id'] == 'bd-notes'


# --------------------------------------------------------------------------
# Additional surfaces named in the bead's finalized spec
# --------------------------------------------------------------------------

def test_refuses_bd_remember_with_backtick():
    cmd = 'bd remember "the guard checks `bd create` invocations"'
    result = guard.classify(cmd)
    assert result['verdict'] == 'refuse'
    assert 'sable-bd-remember' in result['reason']


def test_refuses_bd_q_with_dollar_paren():
    cmd = 'bd q "capture: $(whoami) filed this"'
    result = guard.classify(cmd)
    assert result['verdict'] == 'refuse'
    assert result['surface_id'] == 'bd-q-title'


def test_refuses_bd_close_reason_with_backtick():
    cmd = 'bd close SABLE-abc123 --reason "fixed via `bd hooks install`"'
    result = guard.classify(cmd)
    assert result['verdict'] == 'refuse'
    assert '--reason-file' in result['reason']


def test_allows_bd_close_with_reason_file():
    cmd = 'bd close SABLE-abc123 --reason-file /tmp/reason.txt'
    result = guard.classify(cmd)
    assert result['verdict'] == 'allow'


def test_allows_backtick_genuinely_protected_by_single_quotes():
    # Single quotes are the one bash construct that actually prevents command
    # substitution -- a backtick inside them is inert, not residue. The guard
    # tracks quote context (tokenize_with_hazard) so it does not refuse
    # already-safe usage; the incidents on this bead all involved double-
    # quoted or unquoted prose, never single-quoted.
    cmd = "bd create title --description 'contains a literal ` character'"
    result = guard.classify(cmd)
    assert result['verdict'] == 'allow'


def test_refuses_backtick_in_double_quoted_description():
    cmd = 'bd create title --description "contains a literal ` character"'
    result = guard.classify(cmd)
    assert result['verdict'] == 'refuse'


def test_allows_unrelated_command_with_backticks():
    cmd = 'echo "this is `not` a guarded command"'
    result = guard.classify(cmd)
    assert result['verdict'] == 'allow'


def test_allows_bd_note_via_file_flag():
    cmd = 'bd note SABLE-abc123 --file /tmp/note.txt'
    result = guard.classify(cmd)
    assert result['verdict'] == 'allow'


def test_refuses_bd_note_inline_text_with_backtick():
    cmd = 'bd note SABLE-abc123 ran `bd hooks install` by mistake'
    result = guard.classify(cmd)
    assert result['verdict'] == 'refuse'
    assert result['surface_id'] == 'bd-note-text'


def test_empty_command_allows():
    assert guard.classify('')['verdict'] == 'allow'
