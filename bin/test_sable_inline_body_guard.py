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


# --------------------------------------------------------------------------
# SABLE-jjn0d — the INTERPRETER-WRAPPER shape
#
# The guard was live and functional when a real corruption slipped past it.
# The corrupting command was not a bd inline-body argument at all: it was a
# `python3 -c "..."` wrapper that itself performed the bd write. Bash
# substitutes the backtick inside that double-quoted string before python
# exists, so the wrapper hands bd a clean argv element while the wrapper's OWN
# literal is already corrupted. SABLE-s5103's description lost a whole
# backticked span this way and stayed a grammatical sentence.
#
# The bitter part: three agents adopted that wrapper across a shift
# SPECIFICALLY to avoid this hazard, reasoning that an argv list is not
# shell-parsed. True of the bd call. False of the string carrying it. The
# mitigation is only sound when the wrapper contains no backticks -- exactly
# the condition it was supposed to make irrelevant.
# --------------------------------------------------------------------------

def test_python_dash_c_wrapper_performing_a_bd_write_with_a_backtick_is_REFUSED():
    # The exact reproduction from the bead. This is the assertion that failed
    # before the wrapper surface existed.
    cmd = (
        'python3 -c "import subprocess; subprocess.run([\'bd\',\'update\','
        '\'SABLE-x\',\'--append-notes\',\'Hooks run as `parented` directly\'])"'
    )
    result = guard.classify(cmd)
    assert result['verdict'] == 'refuse'
    assert result['surface_id'] == 'interpreter-wrapper-bd-write'


def test_bash_dash_c_wrapper_performing_a_sable_msg_send_with_dollar_paren_is_REFUSED():
    # Same defect through a different interpreter, so the fix cannot be
    # python3-keyed -- and through sable-msg, so it cannot be bd-keyed either.
    cmd = 'bash -c "sable-msg tarzan \\"status: $(bd hooks install) landed\\""'
    result = guard.classify(cmd)
    assert result['verdict'] == 'refuse'
    assert result['surface_id'] == 'interpreter-wrapper-bd-write'


def test_wrapper_with_a_hazard_but_NO_bd_or_sable_msg_is_ALLOWED():
    # THE LOAD-BEARING NEGATIVE CONTROL. Without it the fix degenerates into
    # "any backtick anywhere", which SABLE-qwthx considered and rejected on
    # principle: in an ordinary argument $(...) is frequently INTENDED.
    cmd = 'python3 -c "print($(date +%s))"'
    assert guard.classify(cmd)['verdict'] == 'allow'


def test_derive_never_hardcode_idiom_still_ALLOWED():
    # This command contains BOTH `$(` and a sable tool, so a rule keyed on
    # "any sable binary" refuses the fleet's own mandated practice and trips
    # its own rollback condition. The rule must key on bd WRITE subcommands
    # and sable-msg specifically. This test is what forces that precision.
    cmd = ('timeout $(sable-merge-gate promote-budget --seconds) '
           'sable-merge-gate promote --branch wk-example')
    assert guard.classify(cmd)['verdict'] == 'allow'


def test_bd_read_inside_a_wrapper_is_ALLOWED():
    # The companion precision check: `bd show`/`bd list` are reads. Substitution
    # around a read is the derive-never-hardcode idiom, not residue in prose.
    cmd = 'python3 -c "import subprocess; subprocess.run([\'bd\',\'show\',\'SABLE-x\',\'--json\'])" # $(date)'
    assert guard.classify(cmd)['verdict'] == 'allow'


def test_single_quoted_wrapper_body_is_ALLOWED():
    # Single quotes disable substitution entirely; the existing tokenizer
    # already knows this and the new surface must not regress it.
    cmd = ("python3 -c 'import subprocess; subprocess.run([\"bd\",\"update\","
           "\"SABLE-x\",\"--append-notes\",\"literal ` stays literal\"])'")
    assert guard.classify(cmd)['verdict'] == 'allow'


def test_safe_hint_names_a_file_based_path():
    # The whole failure mode is people reaching for a wrapper they BELIEVE is
    # safe, so the refusal has to name the paths that actually are -- and say
    # outright that an argv list does not help.
    cmd = (
        'python3 -c "import subprocess; subprocess.run([\'bd\',\'note\','
        '\'SABLE-x\',\'see `whoami`\'])"'
    )
    reason = guard.classify(cmd)['reason']
    assert '--file' in reason
    assert '--body-file' in reason
    assert '--stdin' in reason
    assert 'argv' in reason.lower()


def test_wrapper_shape_is_caught_behind_a_prefix_command():
    # `timeout 30 python3 -c ...` and `env FOO=1 bash -c ...` are the same
    # hazard; keying on argv[0] alone would miss both.
    cmd = ('timeout 30 python3 -c "import subprocess; subprocess.run('
           '[\'bd\',\'q\',\'see `whoami`\'])"')
    assert guard.classify(cmd)['verdict'] == 'refuse'
