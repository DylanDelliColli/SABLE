#!/usr/bin/env bash
# tree-claim-impl.sh — the tree-claim gate's logic: a PreToolUse:Bash
# lockfile giving one main session per checkout.
#
# NOT REGISTERED DIRECTLY. tree-claim.sh is the registered entrypoint; it runs
# this file and decides what a non-zero exit MEANS (SABLE-k2h0m). The split
# exists because a single-file hook cannot fail closed on its OWN syntax
# error — bash parses the whole file before executing a line, so no in-file
# check ever runs. All the volatile machinery lives here (the embedded python
# programs below, whose double-quoted-string bodies are the demonstrated
# hazard); the entrypoint stays small and embedded-language-free.
#
# CONTRACT WITH THE ENTRYPOINT: every normal path of this file exits 0 —
# allow, deny and no-op alike. A non-zero exit therefore means "this file
# could not run", and the entrypoint reads it that way. Do not introduce a
# non-zero exit as a decision.
#
# Prevents two main sessions sharing one checkout from racing on the git index.
# Implements the SABLE-7kq operator decision (ba5424d incident: a side session
# swept staged changes into its gitignore commit because both sessions shared
# the index without a claim protocol).
#
# BEHAVIOR:
#   1. Only fires for index-mutating git commands:
#        git add, git commit, git rm, git mv,
#        git restore --staged, git reset
#      Tolerates global git flags (-C, -c k=v, --no-pager, etc.).
#      All other commands exit 0 immediately.
#
#   2. Resolves the claim file per checkout:
#        $(git -C "$CWD" rev-parse --git-dir)/sable-tree-claim
#      For a normal checkout this is <repo>/.git/sable-tree-claim.
#      For a `git worktree add` worktree, .git is a file pointing to the
#      per-worktree gitdir; rev-parse resolves it correctly, giving each
#      worktree its own independent claim file.
#      A leading `cd <dir> &&` in the same command line shifts the shell's
#      working directory before the git invocation runs; the effective-dir
#      walk tracks that (SABLE-5pci) so `cd <worktree> && git add` and
#      `git -C <worktree> add` resolve to the SAME claim file instead of the
#      cd-form silently falling back to the hook's original cwd.
#      The claim gates the command's ACTUAL TARGET repo, never the ambient
#      session cwd (SABLE-vx4aj). Command boundaries are recognised at ';',
#      '&&', '||', '|', '&', newline, subshell parens and brace groups, so a
#      multi-line or grouped command cannot hide either the `cd` prefix (which
#      would gate an unrelated repo) or the git write itself (which would
#      evade the claim outright). Subshell parens additionally SCOPE the
#      directory: bash unwinds a `cd` at the closing paren, so
#      `( cd /tmp ) ; git commit` writes HERE, while a brace group runs in the
#      current shell and its `cd` DOES persist — the two must not be treated
#      alike. Where the target cannot be resolved from the command text —
#      `cd "$VAR"`, `cd -`, `popd`, unparseable quoting — the command is gated
#      against the best directory the hook can infer rather than allowed
#      through.
#
#   3. Claim lifecycle (TTL default: 3600s, override SABLE_TREE_CLAIM_TTL):
#        No claim      → write this session's claim, allow.
#        Own claim     → refresh timestamp, allow.
#        Foreign fresh → deny, name holder + age + escape hatches.
#        Foreign stale → take over (overwrite), allow + additionalContext.
#
#   4. Escape hatches:
#        SABLE_TREE_CLAIM_OVERRIDE=1  → allow + take over + additionalContext.
#        Delete the claim file manually and retry.
#
#   5. Fail open on infrastructure errors (rev-parse failure, unreadable claim
#      file, missing session identity): allow + additionalContext, never deny.
#      These are cases where the gate DID run and reached a considered
#      conclusion — "not a repo", "no claim to conflict with". They are
#      categorically different from the gate not running at all, which fails
#      CLOSED in tree-claim.sh (SABLE-k2h0m).
#
# TODO: When SABLE-0u1/SABLE-jpr (shared git-subcommand tokenizer in
#       lib-identity.sh) lands, replace the local sable__parse_git_subcommand
#       below with the shared helper.

set -uo pipefail

HOOK_INPUT=$(cat 2>/dev/null) || HOOK_INPUT=""

# ---------------------------------------------------------------------------
# Parse hook input
# ---------------------------------------------------------------------------
_python3_extract() {
  python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get('$1', '') or '')
" 2>/dev/null
}

COMMAND=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print((d.get('tool_input') or {}).get('command', '') or '')
" 2>/dev/null) || COMMAND=""

CWD=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get('cwd', '') or '')
" 2>/dev/null) || CWD=""
[ -z "$CWD" ] && CWD="${PWD:-}"

SESSION_ID=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get('session_id', '') or '')
" 2>/dev/null) || SESSION_ID=""

# Fall back to env, then to unknown-PPID (fail open — don't deny on missing identity)
# SABLE-hccq: CLAUDE_CODE_SESSION_ID is the actual env var Claude Code
# exports into a shell's environment (CLAUDE_SESSION_ID is checked too in
# case some environment sets it, but is unset in practice) — checked here so
# the rare case of a hook invocation with no session_id in its JSON still
# resolves the SAME identity that a later 'sable-claim release' call (which
# only has env, never the JSON) would see.
[ -z "$SESSION_ID" ] && SESSION_ID="${CLAUDE_SESSION_ID:-${CLAUDE_CODE_SESSION_ID:-}}"
IDENTITY_KNOWN=1
if [ -z "$SESSION_ID" ]; then
  SESSION_ID="unknown-${PPID:-0}"
  IDENTITY_KNOWN=0
fi

# market-brief-package-q6yu: a human-attributable name for the claim record's
# third field (status output shows "lincoln", not just a raw session UUID —
# the misidentification that led to a wrong release against an ACTIVE
# holder). Same signal order as SESSION_ID: hook-input agent_type (subagent
# context) first, then the legacy env terminal name. "-" when neither is set
# (the common case for an unnamed session) — sable-claim status/release still
# work off the session_id field in that case.
AGENT_NAME=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get('agent_type', '') or '')
" 2>/dev/null) || AGENT_NAME=""
[ -z "$AGENT_NAME" ] && AGENT_NAME="${CLAUDE_AGENT_NAME:-}"
[ -z "$AGENT_NAME" ] && AGENT_NAME="-"

# ---------------------------------------------------------------------------
# Helper: emit an additionalContext response and exit 0 (allow)
# ---------------------------------------------------------------------------
allow_with_context() {
  # $1 = message
  MSG="$1" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'allow',
        'additionalContext': os.environ.get('MSG', '')
    }
}))
"
  exit 0
}

# ---------------------------------------------------------------------------
# Helper: emit a deny response and exit 0
# ---------------------------------------------------------------------------
deny_with_reason() {
  # $1 = reason
  REASON="$1" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': os.environ.get('REASON', '')
    }
}))
"
  exit 0
}

# ---------------------------------------------------------------------------
# sable__is_index_mutating_git_command <command> <cwd>
#
# Tokenises the command with Python shlex (handles quoting).  Walks ALL
# command positions (start of string, and after shell separators ; && || |
# & newline ( ) { }) transparently through NAME=VALUE environment-assignment
# prefixes and env(1) invocations — aligning with the sable_is_git_push walk
# in lib-identity.sh.
#
# If ANY command-position segment resolves to an index-mutating git invocation,
# the function prints TWO lines to stdout and returns 0:
#     line 1: the effective repo directory
#     line 2: 'ok' if that directory was resolved confidently, 'ambiguous'
#             if it is a fail-safe fallback to the session cwd
# When multiple segments match, the FIRST mutating segment's -C path wins
# (multi-segment chained mutating git commands are rare; using the first keeps
# the scope conservative).
#
# Returns 0 (match) or 1 (no match).
#
# Index-mutating subcommands: add, commit, rm, mv, restore (only when
# --staged/-S is also present), reset (any form).
#
# Effective repo directory:
#   - No -C flags on the matching segment → the shell cwd in effect at that
#     point in the command line (session cwd, shifted by any preceding cd).
#   - One or more -C flags → accumulated left-to-right, resolved against that
#     directory (relative -C args are joined to the accumulated base; absolute
#     args replace it), matching git's own -C behaviour.
#
# SABLE-vx4aj — attribution must follow the command's ACTUAL TARGET, never the
# ambient session cwd. Two rules make that hold:
#
#   (a) EVERY shell construct that starts a new command must reset the walk to
#       command position. Previously only ';', '&&', '||' and '|' did. A
#       newline, a background '&', a subshell '( ... )' or a brace group
#       '{ ...; }' left the walk stuck mid-segment, which broke BOTH ways: the
#       preceding 'cd <elsewhere>' went unseen (so the write was gated by the
#       session cwd's claim — the observed false positive, an unrelated
#       throwaway repo refused because the SABLE checkout was claimed), and a
#       later 'git commit' went unseen entirely (so a write TARGETING the
#       claimed repo evaded the claim — the false-negative wrong-tree class of
#       SABLE-041/936y/nsmc). A two-line Bash command was enough to trigger it.
#
#   (b) When the target CANNOT be resolved confidently — an unexpandable
#       'cd \"\$VAR\"', a 'cd -', a command shlex cannot parse — gate against the
#       session cwd rather than allow. A false positive is recoverable (the
#       deny names the override and release hatches); a missed claim is the
#       incident class.
# ---------------------------------------------------------------------------
sable__is_index_mutating_git_command() {
  local cmd="$1"
  local cwd="$2"
  CWD_VAL="$cwd" CMD_STR="$cmd" python3 -c "
import os, re, shlex, sys

cmd = os.environ.get('CMD_STR', '')
cwd = os.environ.get('CWD_VAL', '')

# --- Quote-aware operator normalisation (SABLE-vx4aj) ---------------------
# shlex.split() discards newlines as plain whitespace and never separates
# '&', '(', ')', '{', '}' from their neighbours, so those command boundaries
# were invisible to the walk below. Rewrite them — OUTSIDE quotes only — into
# spaced separators before tokenising. Quoted text is left byte-for-byte
# intact so 'echo \"cd /x && git commit\"' is still one argument, not a
# command. A backslash-newline line continuation is preserved as whitespace
# (the backslash branch consumes the newline verbatim), which is correct:
# it does not start a new command.
#
# SUBSHELL PARENS ARE EMITTED AS THEIR OWN TOKENS, NOT AS ';'. Collapsing
# '(' and ')' into a plain separator resets command position but DISCARDS
# SCOPE, and scope is load-bearing for a gate that attributes writes to
# directories. bash unwinds a subshell's 'cd' at the closing paren, so
# '( cd /tmp && true ) ; git commit' writes to the ORIGINAL directory; a walk
# that keeps the shifted cwd attributes the commit to /tmp and lets a write
# into the claimed repo through. The walk therefore pushes shell_cwd on '('
# and pops it on ')'.
#
# BRACE GROUPS ARE DELIBERATELY NOT SYMMETRIC with parens: '{ ...; }' runs in
# the CURRENT shell, so its 'cd' DOES persist past the closing brace. They
# stay plain separators. Treating the two alike is wrong by construction in
# one direction or the other.
def normalize_operators(s):
    out = []
    i, n = 0, len(s)
    quote = None
    while i < n:
        ch = s[i]
        if quote == chr(39):            # inside single quotes: literal
            out.append(ch)
            if ch == chr(39):
                quote = None
            i += 1
            continue
        if quote == chr(34):            # inside double quotes: honour \\x
            if ch == chr(92) and i + 1 < n:
                out.append(ch); out.append(s[i + 1]); i += 2; continue
            out.append(ch)
            if ch == chr(34):
                quote = None
            i += 1
            continue
        if ch == chr(92) and i + 1 < n:  # unquoted escape (incl. \\<newline>)
            out.append(ch); out.append(s[i + 1]); i += 2; continue
        if ch in (chr(34), chr(39)):
            quote = ch; out.append(ch); i += 1; continue
        if ch in '&|':
            if i + 1 < n and s[i + 1] == ch:      # '&&' / '||'
                out.append(' ' + ch + ch + ' '); i += 2; continue
            if ch == '|':
                out.append(' | '); i += 1; continue
            out.append(' ; '); i += 1; continue   # background '&'
        if ch in '()':                            # subshell: scoped, see above
            out.append(' ' + ch + ' '); i += 1; continue
        if ch in ';\n{}':
            out.append(' ; '); i += 1; continue
        out.append(ch)
        i += 1
    return ''.join(out)

# Coarse fallback detector for commands shlex cannot tokenise (unbalanced
# quoting). Rather than fail open on a command that plainly contains an
# index-mutating git invocation, gate it against the session cwd.
#
# The reserved-word alternation mirrors RESERVED_WORDS below for the same
# reason (SABLE-hfkdd): without it a keyword-wrapped write that also carries
# an unbalanced quote (if true; then git commit -am ...) matched nothing here
# and fell through to fail OPEN, which is the one direction this fallback
# exists to prevent. Both paths to a decision must see a keyword-prefixed git.
# (No literal double quote in this comment: the whole program is embedded in a
# double-quoted bash string, so one would terminate it — same reason the code
# below spells quote characters as chr(34)/chr(39).)
UNPARSEABLE_RE = re.compile(
    r'(^|[;&|(){}\n])\s*'
    r'(?:(?:if|then|elif|else|while|until|do|for|select|case|in|function|time|!)\s+)*'
    r'(\w+=\S*\s+)*'
    r'git\b[^;&|(){}\n]*\b(add|commit|rm|mv|reset)\b')

try:
    tokens = shlex.split(normalize_operators(cmd))
except ValueError:
    if UNPARSEABLE_RE.search(cmd):
        print(cwd)
        print('ambiguous')
        sys.exit(0)
    sys.exit(1)

# '(' and ')' are members so that every existing 'skip to end of this segment'
# and 'break out of the git-flag walk' loop treats them as boundaries; the
# main loop then gives them their scoping behaviour (push/pop) on top.
SHELL_SEPS = {';', '&&', '||', '|', '(', ')'}
# SABLE-hfkdd: bash reserved words are TRANSPARENT at command position — skip
# the token and STAY at command position, exactly as this walk already does
# for NAME=VALUE prefixes and env(1).
#
# Operators alone are not enough to track command position. Every compound
# construct puts a reserved word precisely where a command name is expected,
# so without this the walk fell OFF command position on 'then'/'do' and the
# write behind it was never evaluated as a command at all:
#   'if true; then git commit -am x; fi'      -> the ';' reset correctly, but
#   'for f in 1; do git commit -am x; done'      'then'/'do' knocked the walk
#   'while false; do git commit -am x; done'     off and the git was invisible
# Those are ordinary shapes in agent-composed bash, not adversarial ones, and
# they evaded the claim entirely. The same blindness also mis-ATTRIBUTED
# writes: a 'cd' behind 'then' went unseen, so 'if true; then cd <other>;
# git commit; fi' was gated against the wrong directory.
#
# Stated as a property rather than an enumeration of constructs, because the
# failure mode is always the same one thing: a keyword sat where a command
# name was expected. None of these words can name a real command, so skipping
# them at command position cannot hide one.
#
# This is orthogonal to the paren scoping above and must stay so: keywords
# change only WHERE the walk looks for a command, never the cwd stack. Loop
# and if bodies run in the CURRENT shell, so a 'cd' inside one persists — the
# same reason brace groups are not popped and subshells are.
RESERVED_WORDS = {
    'if', 'then', 'elif', 'else', 'fi',
    'for', 'select', 'while', 'until', 'do', 'done',
    'case', 'esac', 'in',
    'function', 'time', '!', 'coproc',
}
# git global flags that consume the next token as an argument
CONSUME_NEXT = {'-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path'}
STANDALONE = {
    '--no-pager', '-p', '--paginate', '-P', '--no-replace-objects', '--bare',
    '--literal-pathspecs', '--glob-pathspecs', '--noglob-pathspecs',
    '--icase-pathspecs', '--no-optional-locks', '--html-path', '--man-path',
    '--info-path', '--version', '--help',
}
STANDALONE_PREFIXES = ('--exec-path=', '--git-dir=', '--work-tree=', '--namespace=')
MUTATING = {'add', 'commit', 'rm', 'mv', 'reset'}
ENV_ASSIGN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

def resolve_effective_dir(c_paths, base_cwd):
    \"\"\"Accumulate -C paths left-to-right, resolved against base_cwd.\"\"\"
    cur = base_cwd
    for p in c_paths:
        if os.path.isabs(p):
            cur = p
        else:
            cur = os.path.join(cur, p)
    return cur

# Tracks the shell's working directory as command-position 'cd' segments are
# walked, so a preceding 'cd <dir> &&' shifts the base a later 'git ...'
# segment resolves -C paths (or lack thereof) against — same namespace a
# 'git -C <dir> ...' invocation of the same physical op would land in
# (SABLE-5pci: without this, 'cd <wt> && git add' fell back to the hook's
# original cwd instead of <wt>, landing in the wrong repo's claim file).
shell_cwd = cwd
# Saved shell_cwd per open subshell. bash restores the caller's directory at
# the closing paren, so the walk must too (see normalize_operators).
cwd_stack = []
# SABLE-vx4aj: set when a cd target could not be resolved to a real path
# (variable/command substitution, glob, 'cd -', popd). shell_cwd then stays
# at the session cwd and the decision is reported as a fail-safe fallback.
target_ambiguous = False

# A cd target we cannot expand here: anything carrying shell expansion or a
# glob. Left unexpanded these would be joined literally onto shell_cwd and
# resolve to nothing, which fails OPEN — the wrong direction.
UNRESOLVABLE_RE = re.compile(r'[\$\`\*\?\[]|^~')

i = 0
n = len(tokens)
at_cmd_pos = True
while i < n:
    tok = tokens[i]
    # Shell separator -> next token is a new command position.
    # Parens additionally save/restore the shell cwd (subshell scope).
    if tok in SHELL_SEPS:
        if tok == '(':
            cwd_stack.append(shell_cwd)
        elif tok == ')':
            if cwd_stack:
                shell_cwd = cwd_stack.pop()
            else:
                # Unbalanced ')' — in practice a 'case' pattern terminator
                # ('case x in pat) ...'), since '(' is always emitted too.
                # A case body runs in the CURRENT shell, so the running
                # shell_cwd is the right answer and must be KEPT: resetting to
                # the session cwd here re-opened the false negative outright —
                # 'cd <claimed>; case x in x) git commit;; esac' from another
                # repo's cwd really lands in <claimed> (verified against real
                # bash), and a reset attributed it to the session cwd and
                # allowed it. Flag it ambiguous so the deny explains the guess,
                # but do not move the directory.
                target_ambiguous = True
        at_cmd_pos = True
        i += 1
        continue
    # At command position: a reserved word is transparent — stay at command
    # position and keep looking (SABLE-hfkdd, see RESERVED_WORDS).
    if at_cmd_pos and tok in RESERVED_WORDS:
        i += 1
        continue
    # At command position: transparent NAME=VALUE env-assignment prefix
    if at_cmd_pos and ENV_ASSIGN_RE.match(tok):
        i += 1
        continue
    # At command position: env(1) prefix — consume it and its own options
    if at_cmd_pos and tok == 'env':
        i += 1
        while i < n:
            t = tokens[i]
            if ENV_ASSIGN_RE.match(t):
                i += 1
                continue
            if t == '-u' and i + 1 < n:
                i += 2
                continue
            break
        continue  # re-evaluate tokens[i] still at command position
    # At command position: 'cd' — shifts shell_cwd for subsequent segments
    # in this same command line.
    # At command position: 'popd' — destination is unknowable from the text
    # alone; fall back to the session cwd (fail safe, SABLE-vx4aj).
    if at_cmd_pos and tok == 'popd':
        shell_cwd = cwd
        target_ambiguous = True
        i += 1
        while i < n and tokens[i] not in SHELL_SEPS:
            i += 1
        at_cmd_pos = False
        continue
    if at_cmd_pos and tok in ('cd', 'pushd'):
        i += 1
        # Skip cd's own flags (-L, -P, ...) but not a lone '-' (previous dir).
        while i < n and tokens[i] not in SHELL_SEPS and tokens[i].startswith('-') and tokens[i] != '-':
            i += 1
        target = None
        if i < n and tokens[i] not in SHELL_SEPS:
            target = tokens[i]
            i += 1
        if target is None:
            shell_cwd = os.environ.get('HOME', shell_cwd)
        elif target == '-' or UNRESOLVABLE_RE.search(target):
            # Previous dir / an unexpanded variable, glob or substitution.
            # SABLE-vx4aj: gate against the session cwd rather than let the
            # write resolve to a path that does not exist and fail open.
            shell_cwd = cwd
            target_ambiguous = True
        elif os.path.isabs(target):
            shell_cwd = target
        else:
            shell_cwd = os.path.join(shell_cwd, target)
        # Skip any further args on this cd invocation until the next separator
        while i < n and tokens[i] not in SHELL_SEPS:
            i += 1
        at_cmd_pos = False
        continue
    # At command position: found 'git' — walk flags and identify subcommand
    if at_cmd_pos and tok == 'git':
        i += 1
        c_paths = []
        while i < n:
            t = tokens[i]
            if t in SHELL_SEPS:
                break
            if t == '-C' and i + 1 < n:
                c_paths.append(tokens[i + 1])
                i += 2
                continue
            if t in CONSUME_NEXT:
                i += 2
                continue
            if t in STANDALONE or any(t.startswith(p) for p in STANDALONE_PREFIXES):
                i += 1
                continue
            if t == '--':
                i += 1
                break
            if t.startswith('-'):
                i += 1
                continue
            # This is the subcommand
            subcommand = t
            remaining = tokens[i + 1:]
            is_mutating = False
            if subcommand in MUTATING:
                is_mutating = True
            elif subcommand == 'restore':
                for rt in remaining:
                    if rt in ('--staged', '-S'):
                        is_mutating = True
                        break
            if is_mutating:
                eff = resolve_effective_dir(c_paths, shell_cwd)
                ambiguous = target_ambiguous and not any(
                    os.path.isabs(p) for p in c_paths)
                if any(UNRESOLVABLE_RE.search(p) for p in c_paths):
                    # An unexpandable -C target: same fail-safe rule as cd.
                    eff = cwd
                    ambiguous = True
                print(eff)
                print('ambiguous' if ambiguous else 'ok')
                sys.exit(0)
            # Not mutating — this segment is done, continue outer loop
            i += 1
            # Skip rest of this segment's args until a separator
            while i < n and tokens[i] not in SHELL_SEPS:
                i += 1
            at_cmd_pos = False
            break
        # If we exhausted tokens inside the git-flag walk (no subcommand found)
        # or hit a separator — just continue outer loop
        continue
    # Not at command position, or not a recognised command-position token
    at_cmd_pos = False
    i += 1

sys.exit(1)
" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Step 1: Check if this command is index-mutating; capture effective repo dir
# ---------------------------------------------------------------------------
[ -z "$COMMAND" ] && exit 0
DETECTION=$(sable__is_index_mutating_git_command "$COMMAND" "$CWD") || exit 0
EFFECTIVE_DIR=$(printf '%s\n' "$DETECTION" | sed -n '1p')
TARGET_RESOLUTION=$(printf '%s\n' "$DETECTION" | sed -n '2p')
# Normalise: if EFFECTIVE_DIR came back empty for any reason, fall back to CWD
[ -z "$EFFECTIVE_DIR" ] && EFFECTIVE_DIR="$CWD"

# SABLE-vx4aj: an unresolvable target was gated against the session cwd
# instead of being allowed through. Say so in the deny reason — otherwise the
# blocked party sees a claim on a repo their command never names and has no
# way to tell why.
AMBIGUITY_NOTE=""
if [ "$TARGET_RESOLUTION" = "ambiguous" ]; then
  AMBIGUITY_NOTE=" NOTE: the command's target repo could not be resolved from its text with confidence (an unexpanded variable, glob, 'cd -', 'popd', or an unmatched ')'), so it was gated against the best directory the hook could infer ($EFFECTIVE_DIR) rather than allowed through unchecked. Re-run with a literal path to be gated against the repo you actually mean."
fi

# ---------------------------------------------------------------------------
# Step 2: Resolve the claim file (per-checkout via git-dir)
# ---------------------------------------------------------------------------
GIT_DIR=$(git -C "$EFFECTIVE_DIR" rev-parse --git-dir 2>/dev/null) || {
  # Not inside a git repo — fail open
  exit 0
}

# git rev-parse --git-dir returns a relative path when called without -C on
# older git versions; resolve it relative to EFFECTIVE_DIR (the -C target).
case "$GIT_DIR" in
  /*) ;;                             # already absolute
  *)  GIT_DIR="$EFFECTIVE_DIR/$GIT_DIR" ;;
esac

CLAIM_FILE="$GIT_DIR/sable-tree-claim"
TTL="${SABLE_TREE_CLAIM_TTL:-3600}"
NOW=$(date +%s 2>/dev/null) || NOW=0

# market-brief-package-q6yu: helper for every "this session now HOLDS the
# claim" message. Names the claim file + a release reminder — the gap the
# bead was filed for: the claim-TAKER previously learned nothing about where
# the record lives or how to release it (only the deny message, shown to the
# BLOCKED party, named the path). Use `sable-claim status/release
# <repo>` — see bin/sable-claim.
claim_taken_suffix() {
  printf ' Claim file: %s. Release when done: sable-claim release "%s" (from this session/agent), or sable-claim status "%s" to inspect.' \
    "$CLAIM_FILE" "$EFFECTIVE_DIR" "$EFFECTIVE_DIR"
}

# ---------------------------------------------------------------------------
# Step 3: Missing identity — fail open, but never clobber an existing claim
# ---------------------------------------------------------------------------
if [ "$IDENTITY_KNOWN" -eq 0 ]; then
  # Write a claim ONLY when no claim file exists yet.  An existing claim
  # (regardless of holder or age) must never be overwritten by an
  # identity-unknown invocation — doing so would evict the legitimate holder.
  if [ ! -f "$CLAIM_FILE" ]; then
    printf '%s %s %s\n' "$SESSION_ID" "$NOW" "$AGENT_NAME" > "$CLAIM_FILE" 2>/dev/null || true
    allow_with_context "tree-claim: session identity unknowable (PPID=${PPID:-?}); claim written as $SESSION_ID.$(claim_taken_suffix) If two sessions share this checkout, set CLAUDE_SESSION_ID or use 'bd worktree create <name>' for isolation."
  else
    allow_with_context "tree-claim: session identity unknowable (PPID=${PPID:-?}); existing claim preserved (holder: $(awk '{print $1}' "$CLAIM_FILE" 2>/dev/null)). If two sessions share this checkout, set CLAUDE_SESSION_ID or use 'bd worktree create <name>' for isolation."
  fi
fi

# ---------------------------------------------------------------------------
# Step 4: SABLE_TREE_CLAIM_OVERRIDE — allow unconditionally, take over
# ---------------------------------------------------------------------------
if [ "${SABLE_TREE_CLAIM_OVERRIDE:-}" = "1" ]; then
  printf '%s %s %s\n' "$SESSION_ID" "$NOW" "$AGENT_NAME" > "$CLAIM_FILE" 2>/dev/null || true
  allow_with_context "tree-claim: override active (SABLE_TREE_CLAIM_OVERRIDE=1). Claim taken over by session $SESSION_ID.$(claim_taken_suffix)"
fi

# ---------------------------------------------------------------------------
# Step 5: Read the existing claim (if any)
# ---------------------------------------------------------------------------
if [ ! -f "$CLAIM_FILE" ]; then
  # No claim — write and allow, naming the path so the new holder can find
  # and release it later without reading hook source (market-brief-package-q6yu).
  printf '%s %s %s\n' "$SESSION_ID" "$NOW" "$AGENT_NAME" > "$CLAIM_FILE" 2>/dev/null || true
  allow_with_context "tree-claim: claim taken by session $SESSION_ID.$(claim_taken_suffix)"
fi

# Read claim: "session_id timestamp agent_name"
CLAIM_SESSION=$(awk '{print $1}' "$CLAIM_FILE" 2>/dev/null) || CLAIM_SESSION=""
CLAIM_TS=$(awk '{print $2}' "$CLAIM_FILE" 2>/dev/null) || CLAIM_TS=0

# Unreadable / corrupt claim — fail open
if [ -z "$CLAIM_SESSION" ] || [ -z "$CLAIM_TS" ]; then
  printf '%s %s %s\n' "$SESSION_ID" "$NOW" "$AGENT_NAME" > "$CLAIM_FILE" 2>/dev/null || true
  allow_with_context "tree-claim: could not parse existing claim file; took over for session $SESSION_ID.$(claim_taken_suffix)"
fi

# ---------------------------------------------------------------------------
# Step 6: Evaluate claim ownership
# ---------------------------------------------------------------------------
if [ "$CLAIM_SESSION" = "$SESSION_ID" ]; then
  # Own claim — refresh timestamp and allow. Stays silent (no context): this
  # is the common per-command path for a session that already knows it holds
  # the claim, not a "you just took a new claim" event.
  printf '%s %s %s\n' "$SESSION_ID" "$NOW" "$AGENT_NAME" > "$CLAIM_FILE" 2>/dev/null || true
  exit 0
fi

# Foreign claim
CLAIM_AGE=$(( NOW - CLAIM_TS ))
if [ "$CLAIM_AGE" -lt 0 ]; then CLAIM_AGE=0; fi
# market-brief-package-q6yu: attributable name, not just a raw session UUID
# (LIVE EVIDENCE ROUND 2 — an anonymous uuid+epoch record invited a wrong
# release against an ACTIVE holder via timing-correlation misattribution).
CLAIM_AGENT=$(awk '{print $3}' "$CLAIM_FILE" 2>/dev/null) || CLAIM_AGENT=""
[ -z "$CLAIM_AGENT" ] && CLAIM_AGENT="-"

if [ "$CLAIM_AGE" -lt "$TTL" ]; then
  # Fresh foreign claim — deny
  deny_with_reason "tree-claim: index locked by session '$CLAIM_SESSION' (agent '$CLAIM_AGENT', ${CLAIM_AGE}s ago, TTL ${TTL}s). Your session: $SESSION_ID. Escape hatches: (1) set SABLE_TREE_CLAIM_OVERRIDE=1 to take over, or (2) delete $CLAIM_FILE manually and retry, or (3) if you ARE '$CLAIM_AGENT', run: sable-claim release \"$EFFECTIVE_DIR\". If the other session is no longer active, the claim will expire automatically after $((TTL - CLAIM_AGE))s.$AMBIGUITY_NOTE"
else
  # Stale claim — take over and allow
  printf '%s %s %s\n' "$SESSION_ID" "$NOW" "$AGENT_NAME" > "$CLAIM_FILE" 2>/dev/null || true
  allow_with_context "tree-claim: stale claim by '$CLAIM_SESSION' (agent '$CLAIM_AGENT', ${CLAIM_AGE}s old, TTL ${TTL}s) taken over by session $SESSION_ID.$(claim_taken_suffix)"
fi
