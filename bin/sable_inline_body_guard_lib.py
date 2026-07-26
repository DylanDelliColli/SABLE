#!/usr/bin/env python3
"""sable_inline_body_guard_lib -- residue detection for SABLE-qwthx.

An agent-composed Bash command carrying PROSE (a bd description, a bd notes
append, a sable-msg body, ...) is parsed by the CALLING shell before the
intended command ever sees it. An unescaped backtick or `$(...)` inside that
prose is command-substituted -- and the substituted command actually runs --
before argv reaches `bd` or `sable-msg`. SABLE-qwthx's own incident was `bd
hooks install` running mid-promote this way, with `bd create --body-file`
sitting unused on the very command being invoked.

PATTERN-KEYED, not command-name-keyed. The bead's second instance (a
backtick in a `sable-msg` body) defeated a mitigation that had been scoped to
"bd write commands" only -- a name-list is exactly the shape that already
failed once. So every entry in SURFACES below still names one concrete
prose carrier -- detection has to know WHERE prose can appear, or it cannot
tell a hazardous field from an inert one such as `--body-file <path>` -- but
the table spans multiple tools on purpose, and adding a new tool means adding
a row, not widening a name filter.

Most rows name a (command, argument) pair. One names a SHAPE instead: an
interpreter wrapper (`python3 -c`, `bash -c`, ...) whose script string
performs the bd/sable-msg write itself, so the wrapper's OWN literal is
shell-parsed before the interpreter exists (SABLE-jjn0d). Still a row, still
scoped to where substitution is never intended -- see that row's comment.

This module is intentionally import-only-safe (no top-level side effects) so
bin/test_sable_inline_body_guard.py can unit-test `classify()` directly, and
it also runs as a CLI for hooks/multi-manager/inline-body-guard.sh: reads the
raw Bash command line on stdin, writes "ALLOW" or "REFUSE\n<reason>" to
stdout.
"""
from __future__ import annotations

import os
import re
import sys

SHELL_SEPARATORS = {";", "&&", "||", "|", "&"}
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Escaping bash actually honors for the two hazard constructs (backtick,
# `$(`): inside double quotes only \, $, ", `, and newline are escapable;
# unquoted, backslash escapes the very next character outright. Single
# quotes disable all of it -- nothing inside them is ever substituted, so
# tokens built from a single-quoted span are never marked hazardous.
_DOUBLE_QUOTE_ESCAPABLE = set('\\$"`\n')


def tokenize_with_hazard(command):
    """Split *command* into (token_text, hazardous) pairs.

    `hazardous` is True iff the token contains an unescaped backtick or an
    unescaped `$(` OUTSIDE of single quotes -- i.e. a construct bash would
    command-substitute. Not a full bash parser (no here-docs, no $'...'
    quoting, no brace expansion) -- exact for the plain single/double/
    unquoted+backslash cases this guard exists to catch, which is the same
    scope every sibling hook in this catalog (e.g. notes-clobber-guard.sh)
    accepts for its own shlex-based tokenizer.
    """
    tokens = []
    buf = []
    hazardous = False
    started = False
    i = 0
    n = len(command)

    def flush():
        nonlocal buf, hazardous, started
        if started:
            tokens.append((''.join(buf), hazardous))
        buf = []
        hazardous = False
        started = False

    while i < n:
        c = command[i]

        if c in ' \t\n':
            flush()
            i += 1
            continue

        started = True

        if c == "'":
            j = command.find("'", i + 1)
            if j == -1:
                buf.append(command[i + 1:])
                i = n
            else:
                buf.append(command[i + 1:j])
                i = j + 1
            continue

        if c == '"':
            i += 1
            while i < n and command[i] != '"':
                ch = command[i]
                if ch == '\\' and i + 1 < n and command[i + 1] in _DOUBLE_QUOTE_ESCAPABLE:
                    buf.append(command[i + 1])
                    i += 2
                    continue
                if ch == '`':
                    hazardous = True
                elif ch == '$' and i + 1 < n and command[i + 1] == '(':
                    hazardous = True
                buf.append(ch)
                i += 1
            i += 1  # skip the closing quote (or run off the end, same as bash's own unterminated-quote behavior)
            continue

        if c == '\\' and i + 1 < n:
            buf.append(command[i + 1])
            i += 2
            continue

        if c == '`':
            hazardous = True
            buf.append(c)
            i += 1
            continue

        if c == '$' and i + 1 < n and command[i + 1] == '(':
            hazardous = True
            buf.append(c)
            i += 1
            continue

        buf.append(c)
        i += 1

    flush()
    return tokens


def _segments(tokens):
    seg = []
    for tok, hz in tokens:
        if tok in SHELL_SEPARATORS and not hz:
            yield seg
            seg = []
        else:
            seg.append((tok, hz))
    yield seg


def _strip_env_prefix(seg):
    i, n = 0, len(seg)
    while i < n:
        tok = seg[i][0]
        if ENV_ASSIGN_RE.match(tok):
            i += 1
            continue
        if tok == 'env':
            i += 1
            while i < n:
                tt = seg[i][0]
                if ENV_ASSIGN_RE.match(tt):
                    i += 1
                    continue
                if tt == '-u' and i + 1 < n:
                    i += 2
                    continue
                break
            continue
        break
    return seg[i:]


# Each row names one PROSE carrier: a (command basename, subcommand) pair
# plus either the flags whose VALUE is prose, or the positional slot(s) that
# are prose (bd note/remember/q and sable-msg take their body as a bare
# argument, not a flag). safe_hint is spelled out verbatim in the refusal --
# that is the point of the guard: the fix falls out of the message.
SURFACES = [
    {
        'id': 'bd-create-description',
        'command': 'bd', 'subcommand': 'create',
        'flags': ('-d', '--description'),
        'arg_desc': '-d/--description',
        'safe_hint': "bd create --body-file <path> (or --stdin) instead of inline -d/--description",
    },
    {
        'id': 'bd-create-design',
        'command': 'bd', 'subcommand': 'create',
        'flags': ('--design',),
        'arg_desc': '--design',
        'safe_hint': "bd create --design-file <path> instead of inline --design",
    },
    {
        'id': 'bd-update-description',
        'command': 'bd', 'subcommand': 'update',
        'flags': ('-d', '--description'),
        'arg_desc': '-d/--description',
        'safe_hint': "bd update --body-file <path> (or --stdin) instead of inline -d/--description",
    },
    {
        'id': 'bd-update-design',
        'command': 'bd', 'subcommand': 'update',
        'flags': ('--design',),
        'arg_desc': '--design',
        'safe_hint': "bd update --design-file <path> instead of inline --design",
    },
    {
        'id': 'bd-notes',
        'command': 'bd', 'subcommand': ('create', 'update'),
        'flags': ('--notes', '--append-notes'),
        'arg_desc': '--notes/--append-notes',
        'safe_hint': "bd note <id> --file <path> (or --stdin) instead of inline --notes/--append-notes -- create/update have no --notes-file",
    },
    {
        'id': 'bd-close-reason',
        'command': 'bd', 'subcommand': 'close',
        'flags': ('--reason',),
        'arg_desc': '--reason',
        'safe_hint': "bd close --reason-file <path> (or --stdin) instead of inline --reason",
    },
    {
        'id': 'bd-note-text',
        'command': 'bd', 'subcommand': 'note',
        'positional_from': 1,  # skip the id (the first positional after "note")
        'arg_desc': 'its inline note text',
        'safe_hint': "bd note <id> --file <path> (or --stdin) instead of inline text",
    },
    {
        'id': 'bd-remember-insight',
        'command': 'bd', 'subcommand': 'remember',
        'positional_from': 0,
        'arg_desc': 'its inline insight text',
        'safe_hint': "sable-bd-remember --file <path> (or --stdin) instead of raw 'bd remember' with inline text -- bd remember itself has no --file/--stdin",
    },
    {
        'id': 'bd-q-title',
        'command': 'bd', 'subcommand': 'q',
        'positional_from': 0,
        'arg_desc': 'its inline title',
        'safe_hint': "bd q has no file-based form -- keep the title free of backticks/$(...); use bd create --body-file for anything longer",
    },
    {
        'id': 'sable-msg-body',
        'command': 'sable-msg', 'subcommand': None,
        'positional_from': 0,
        'arg_desc': 'its inline body argument',
        'safe_hint': "sable-msg --body-file <path> (or --stdin) instead of the inline body argument",
    },
    # SABLE-jjn0d. An ADDED ROW, not a widened filter -- the distinction the
    # module docstring draws, and the one qwthx's recorded scope rationale
    # turns on. The rows above inspect bd's OWN inline-body arguments; this one
    # inspects an INTERPRETER WRAPPER whose -c/-e script string itself performs
    # the bd/sable-msg write:
    #
    #   python3 -c "import subprocess; subprocess.run(['bd','update','SABLE-x',
    #               '--append-notes','Hooks run as `parented` directly'])"
    #
    # Bash substitutes the backtick inside that double-quoted string BEFORE
    # python exists. The wrapper hands bd a clean argv element and the
    # wrapper's own literal is already corrupted. Three agents adopted this
    # wrapper across a shift SPECIFICALLY to avoid the hazard, reasoning that
    # an argv list is not shell-parsed -- true of the bd call, false of the
    # string carrying it. SABLE-s5103's description lost a whole backticked
    # span this way while staying a grammatical sentence.
    #
    # WHY NOT "any backtick anywhere": rejected on principle when qwthx landed.
    # In an ordinary argument $(...) is frequently INTENDED --
    # `timeout $(sable-merge-gate promote-budget --seconds) ...` is this
    # fleet's own mandated derive-never-hardcode practice -- so a blanket guard
    # would refuse doctrine and trip its own rollback condition. The principled
    # boundary is WHERE SUBSTITUTION IS NEVER INTENDED, and a command whose
    # purpose is a bd/sable-msg WRITE is such a place regardless of which
    # argument carries the prose. Note the rule keys on bd WRITE subcommands
    # and sable-msg, NOT on "any sable binary": the derive-never-hardcode idiom
    # above contains both $( and a sable tool, and must still pass.
    {
        'id': 'interpreter-wrapper-bd-write',
        'command': None, 'subcommand': None,
        'interpreter_wrapper': True,
        'arg_desc': "an interpreter wrapper's own -c/-e script string, which itself performs a bd/sable-msg write",
        'safe_hint': (
            "a file-based path -- bd note <id> --file <path>, bd create/update "
            "--body-file <path>, --stdin, sable-msg --body-file <path>, or a "
            "QUOTED heredoc; a file's content is never shell-parsed. Passing an "
            "argv LIST from python/bash -c does NOT help: the calling shell "
            "substitutes the wrapper's own literal before the interpreter exists"
        ),
    },
]

# Interpreters whose -c/-e/--command argument is a SCRIPT the calling shell
# parses first. Matched anywhere in the segment, not just at argv[0], so a
# `timeout 30 python3 -c ...` or `env FOO=1 bash -c ...` prefix is still seen.
INTERPRETERS = {'python', 'python3', 'perl', 'ruby', 'node', 'bash', 'sh', 'zsh', 'ksh'}
SCRIPT_FLAGS = ('-c', '-e', '--command')

# bd's WRITE subcommands. `bd show`/`bd list` are reads: a $(...) in one of
# those is the derive-never-hardcode idiom, not residue in prose.
_BD_WRITE_RE = re.compile(
    r"(?<![\w-])bd(?![\w-]).{0,160}?"
    r"(?<![\w-])(?:create|update|note|remember|q|close)(?![\w-])",
    re.S,
)
_SABLE_MSG_RE = re.compile(r"(?<![\w-])sable-msg(?![\w-])")


def _performs_bd_write(text):
    """True when *text* invokes a bd WRITE subcommand or sable-msg."""
    return bool(_BD_WRITE_RE.search(text) or _SABLE_MSG_RE.search(text))


def _scan_interpreter_wrapper(seg):
    """Return the wrapper SURFACE when *seg* runs an interpreter whose script
    string BOTH carries a substitution hazard AND performs a bd/sable-msg
    write. Both clauses are required on the SAME span: a hazardous wrapper
    doing no bd write is the ordinary command-substitution the fleet relies on.
    """
    surface = next((s for s in SURFACES if s.get('interpreter_wrapper')), None)
    if surface is None:
        return None

    for i, (tok, _hz) in enumerate(seg):
        if os.path.basename(tok) not in INTERPRETERS:
            continue
        for j in range(i + 1, len(seg)):
            flag_tok, flag_hz = seg[j]
            flag_name = flag_tok.split('=', 1)[0]
            if flag_name not in SCRIPT_FLAGS:
                continue
            if '=' in flag_tok and flag_tok.startswith('--'):
                script, hazardous = flag_tok.split('=', 1)[1], flag_hz
            elif j + 1 < len(seg):
                script, hazardous = seg[j + 1]
            else:
                continue
            if hazardous and _performs_bd_write(script):
                return surface
    return None


def _flag_value_hazardous(rest, k, flag_tok):
    """rest[k] is a token matching --flag or --flag=value. Return whether the
    VALUE carries a hazard: the part after '=' when inline, else the next
    token."""
    tok, hz = rest[k]
    if '=' in tok and tok.startswith('--'):
        return hz
    if k + 1 < len(rest):
        return rest[k + 1][1]
    return False


def _scan_segment(seg):
    """seg: list of (token, hazardous) for one shell-separated command.
    Returns a matching SURFACE dict on the first hazardous prose hit found,
    else None."""
    seg = _strip_env_prefix(seg)
    if not seg:
        return None

    base = os.path.basename(seg[0][0])
    rest = seg[1:]
    subcommand = rest[0][0] if rest else None

    applicable = [
        s for s in SURFACES
        if s['command'] == base and (
            s['subcommand'] is None
            or subcommand == s['subcommand']
            or (isinstance(s['subcommand'], tuple) and subcommand in s['subcommand'])
        )
    ]
    if not applicable:
        # The command is not itself a prose carrier -- but it may be an
        # interpreter wrapper around one (SABLE-jjn0d).
        return _scan_interpreter_wrapper(seg)

    # Args considered for a flag surface are everything after the subcommand
    # token when this tool has a subcommand concept (bd), else everything
    # after argv[0] itself (sable-msg has none).
    args = rest[1:] if any(s['subcommand'] is not None for s in applicable) else rest

    for surface in applicable:
        if 'flags' in surface:
            for k, (tok, _hz) in enumerate(args):
                flag_name = tok.split('=', 1)[0]
                if flag_name in surface['flags'] and _flag_value_hazardous(args, k, tok):
                    return surface
            continue

        # Positional surface: walk the non-flag tokens in argument order.
        positionals = [t for t in (args if surface['subcommand'] is not None else rest) if not t[0].startswith('-')]
        want_from = surface['positional_from']
        for tok, hz in positionals[want_from:]:
            if hz:
                return surface

    return _scan_interpreter_wrapper(seg)


def classify(command):
    """Classify a raw Bash command line.

    Returns {"verdict": "allow"} or
            {"verdict": "refuse", "surface_id": ..., "reason": "..."}.
    """
    if not command or not command.strip():
        return {'verdict': 'allow'}

    tokens = tokenize_with_hazard(command)
    for seg in _segments(tokens):
        surface = _scan_segment(seg)
        if surface is not None:
            reason = (
                "inline-body-guard: DENIED -- this command carries an unescaped "
                "backtick or dollar-paren in {arg_desc}. The CALLING SHELL "
                "command-substitutes (and executes) that content before the "
                "intended command ever sees it (SABLE-qwthx: this exact class "
                "ran 'bd hooks install' against a live repo mid-promote). Use "
                "{safe_hint}."
            ).format(arg_desc=surface['arg_desc'], safe_hint=surface['safe_hint'])
            return {'verdict': 'refuse', 'surface_id': surface['id'], 'reason': reason}

    return {'verdict': 'allow'}


def main(argv):
    command = sys.stdin.read()
    result = classify(command)
    if result['verdict'] == 'refuse':
        print('REFUSE')
        print(result['reason'])
    else:
        print('ALLOW')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
