#!/usr/bin/env bash
# stash-worktree-guard.sh — PreToolUse:Bash guard: deny unscoped `git stash`.
# Trigger: PreToolUse:Bash | Timeout: 3000ms
#
# SABLE-5dmh / SABLE-nhrb: `git worktree add` gives each worktree its own
# working directory, HEAD, and index, but refs/stash lives in the shared
# COMMON .git dir — every worktree of a repo, including the PRIMARY checkout,
# pushes and pops from ONE shared stack. A bare `git stash` in one tree and a
# bare `git stash pop` in another lands the wrong diff in the wrong tree, with
# no absolute path, cd-out, or symlink involved (reproduced live in
# hooks/test/test-worktree-isolation.sh Test 3 — a p84b-branch stash entry was
# visible from, and poppable by, the unrelated wk-worktree-isolation tree).
# templates/worker-dispatch.md already documents this ban ("Git Stash
# Policy") — that documentation-only ban did not prevent nhrb, or the same-day
# e4mvy near-miss ("stash my work to check whether this predates my change" —
# the single most natural instinct a careful worker has). This hook makes the
# ban load-bearing instead of a doc appendix nobody reads.
#
# SCOPE — binding ruling (lincoln via tarzan, SABLE-8hvqt, superseding the
# narrower "linked worktree only" gate in this bead's original PROPOSED FIX):
# this hook denies in EVERY checkout, PRIMARY included. Worktree location
# (git-dir vs git-common-dir) is NOT part of the decision. Rationale: the
# stack is ONE shared stack, so a stash taken in the primary checkout is
# exactly as poppable from a linked worktree as the reverse — a linked-only
# guard would close only half of a bidirectional hazard while reading as
# complete (the instrument-scope failure shape: a check that cannot fail in
# the direction it does not look). This is why the tokeniser below never
# inspects CWD or git-dir/common-dir at all.
#
# BEHAVIOR (break-glass forms are ALLOWED but WARNED, never silently allowed —
# break-glass should feel like break-glass; all other forms DENY outright):
#
#   git stash clear                          -> DENY, always. No break-glass
#                                                form makes nuking the entire
#                                                shared stack safe.
#   git stash | git stash push | git stash save
#     with -m/--message "<scope>: <what>"    -> ALLOW + warning
#     without a scope-prefixed message        -> DENY
#   git stash pop | apply | drop
#     with explicit stash@{N}                 -> ALLOW + warning
#     without an explicit index               -> DENY
#   git stash list | show | branch | create | store
#                                              -> ALLOW, silently (read-only /
#                                                 not part of the banned set;
#                                                 `git stash list` is itself
#                                                 the recommended pre-pop check)
#   anything else (not a `git stash` invocation) -> ALLOW, silently
#
# `git rebase --autostash` was considered for the same treatment (bead text:
# "uses an internal entry on the same shared stack during the rebase
# window"). EMPIRICALLY FALSE, and NOT flagged here: a conflicted
# `git rebase --autostash` leaves `git stash list` EMPTY throughout the pause
# (verified live: created a real add/add conflict under autostash, `git stash
# list` printed nothing while the rebase sat paused, and `git rebase --abort`
# printed "Applied autostash." and restored the dirty work with no stash-list
# entry ever appearing). The autostash reference lives in the per-worktree
# private rebase-state area (alongside HEAD/index), not the shared
# refs/stash reflog stack `git stash list`/`pop` read — so it is not a
# cross-worktree hazard and is out of scope for this guard.
#
# The deny message hands over the two worker-facing alternatives instead of
# just refusing (a bare "no" teaches nothing and the instinct resurfaces next
# time): the SABLE.md 6.4a diff-to-file route, and — for the specific "check
# whether this predates my change" instinct that caused the e4mvy near-miss —
# reading a file at the base branch without touching the working tree at all.

set -uo pipefail

HOOK_INPUT=$(cat 2>/dev/null) || HOOK_INPUT=""

COMMAND=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print((d.get('tool_input') or {}).get('command', '') or '')
" 2>/dev/null) || COMMAND=""

[ -z "$COMMAND" ] && exit 0

# ---------------------------------------------------------------------------
# Emit helpers (same hookSpecificOutput shape as tree-claim.sh)
# ---------------------------------------------------------------------------
allow_with_context() {
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

deny_with_reason() {
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

ALTERNATIVES="Alternatives instead of stash: (1) diff-to-file (SABLE.md 6.4a) -- 'git diff -- <path> > /path/to/scratchpad/patch.diff && git checkout -- <path>' to test against committed state, then 'git apply /path/to/scratchpad/patch.diff' to restore your change. (2) To check whether a failure predates your change WITHOUT touching your working tree -- 'git show origin/<base-branch>:<path/to/file>', or check out a disposable base worktree. Break-glass, only if truly unavoidable: 'git stash push -m \"<scope>: <what>\"' (scope-prefixed message required), and 'git stash pop/apply/drop stash@{N}' by EXPLICIT index only -- run 'git stash list' first, never assume stash@{0} is yours."

# ---------------------------------------------------------------------------
# Classify the command. Prints one of:
#   NONE                    no git-stash invocation found
#   DENY <kind>              deny; <kind> in CLEAR|PUSH|POP:<subcmd>
#   WARN <kind>              allow-with-warning; <kind> in PUSH|POP:<subcmd>
# ---------------------------------------------------------------------------
CLASSIFY=$(CMD_STR="$COMMAND" python3 -c "
import os, re, shlex, sys

cmd = os.environ.get('CMD_STR', '')
try:
    tokens = shlex.split(cmd)
except ValueError:
    print('NONE')
    sys.exit(0)

SHELL_SEPS = {';', '&&', '||', '|'}
CONSUME_NEXT = {'-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path'}
STANDALONE = {
    '--no-pager', '-p', '--paginate', '-P', '--no-replace-objects', '--bare',
    '--literal-pathspecs', '--glob-pathspecs', '--noglob-pathspecs',
    '--icase-pathspecs', '--no-optional-locks', '--html-path', '--man-path',
    '--info-path', '--version', '--help',
}
STANDALONE_PREFIXES = ('--exec-path=', '--git-dir=', '--work-tree=', '--namespace=')
ENV_ASSIGN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
KNOWN_SUBCMDS = {'push', 'pop', 'apply', 'drop', 'clear', 'list', 'show', 'branch', 'create', 'store', 'save'}
STASH_REF_RE = re.compile(r'^stash@\{\d+\}$')
SCOPED_MSG_RE = re.compile(r'^\S+:\s*\S')

def segments(toks):
    seg = []
    for t in toks:
        if t in SHELL_SEPS:
            yield seg
            seg = []
        else:
            seg.append(t)
    yield seg

def strip_env_prefix(seg):
    i, n = 0, len(seg)
    while i < n:
        t = seg[i]
        if ENV_ASSIGN_RE.match(t):
            i += 1
            continue
        if t == 'env':
            i += 1
            while i < n:
                tt = seg[i]
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

def find_message(args):
    i, n = 0, len(args)
    while i < n:
        t = args[i]
        if t in ('-m', '--message') and i + 1 < n:
            return args[i + 1]
        if t.startswith('--message='):
            return t[len('--message='):]
        i += 1
    return None

def find_explicit_index(args):
    for t in args:
        if STASH_REF_RE.match(t):
            return t
    return None

verdict = 'NONE'

for raw_seg in segments(tokens):
    seg = strip_env_prefix(raw_seg)
    if not seg or seg[0] != 'git':
        continue
    i, n = 1, len(seg)
    while i < n:
        t = seg[i]
        if t == '-C' and i + 1 < n:
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
        break
    if i >= n:
        continue
    if seg[i] != 'stash':
        continue
    rest = seg[i + 1:]

    if rest and rest[0] in KNOWN_SUBCMDS:
        subcmd = rest[0]
        args = rest[1:]
    else:
        # bare 'git stash', 'git stash -m ...', 'git stash <pathspec>' — all
        # implicit push forms.
        subcmd = 'push'
        args = rest

    if subcmd == 'clear':
        verdict = 'DENY CLEAR'
        break
    elif subcmd in ('push', 'save'):
        msg = find_message(args)
        if msg is not None and SCOPED_MSG_RE.match(msg):
            verdict = 'WARN PUSH'
        else:
            verdict = 'DENY PUSH'
        break
    elif subcmd in ('pop', 'apply', 'drop'):
        if find_explicit_index(args) is not None:
            verdict = 'WARN ' + subcmd.upper()
        else:
            verdict = 'DENY ' + subcmd.upper()
        break
    else:
        # list, show, branch, create, store — not gated.
        verdict = 'NONE'
        break

print(verdict)
" 2>/dev/null) || CLASSIFY="NONE"

[ -z "$CLASSIFY" ] && CLASSIFY="NONE"

DECISION="${CLASSIFY%% *}"
KIND="${CLASSIFY#* }"

case "$DECISION" in
  NONE)
    exit 0
    ;;
  DENY)
    case "$KIND" in
      CLEAR)
        deny_with_reason "stash-worktree-guard: 'git stash clear' denied -- it destroys the ENTIRE refs/stash stack shared by every worktree of this repo (primary checkout included), not just yours. There is no break-glass form for this; it is destructive by construction. $ALTERNATIVES"
        ;;
      PUSH)
        deny_with_reason "stash-worktree-guard: bare 'git stash'/'git stash push' denied -- refs/stash is a SINGLE stack shared across every worktree of this repo, primary checkout included (SABLE-nhrb/SABLE-8hvqt); an unscoped push is indistinguishable from anyone else's entry. $ALTERNATIVES"
        ;;
      POP|APPLY|DROP)
        deny_with_reason "stash-worktree-guard: 'git stash ${KIND,,}' without an explicit stash@{N} index denied -- the shared stack means stash@{0} may belong to a different worktree entirely; this is the exact SABLE-nhrb contamination path (a stash pushed in one tree popped in another with no absolute path, cd-out, or symlink involved). $ALTERNATIVES"
        ;;
      *)
        deny_with_reason "stash-worktree-guard: 'git stash' denied. $ALTERNATIVES"
        ;;
    esac
    ;;
  WARN)
    allow_with_context "stash-worktree-guard: break-glass stash op allowed (${KIND,,}) -- refs/stash is a SINGLE stack shared across every worktree of this repo, primary checkout included; this op is exactly as visible/poppable from a sibling worktree as the reverse. Run 'git stash list' before any pop/apply/drop and act ONLY by the explicit index you just verified -- never assume stash@{0} is yours."
    ;;
  *)
    exit 0
    ;;
esac
