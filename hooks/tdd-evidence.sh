#!/usr/bin/env bash
# tdd-evidence.sh — Silent logger for test command evidence
# Fires on every PreToolUse:Bash, writes only when it detects test commands.
# Partner to tdd-gate.sh which checks the evidence file on bd close.

set -euo pipefail

HOOK_INPUT=$(cat 2>/dev/null) || exit 0

# market-brief-package-sqcr: detect test-runner commands even when wrapped in
# a compound command (cd/git -C prefix, &&/;/| chains) or invoked directly
# without a 'bash '/'sh ' prefix (./test-x.sh, /abs/path/test-x.sh), and TAG
# each recorded run with the repo it actually ran against. A plain substring
# grep can already find "pytest" anywhere in a line regardless of a leading
# cd-compound — the real gaps were (a) direct/absolute script execution with
# no interpreter token to anchor on, and (b) no record of WHICH repo a
# cross-repo run (git -C <repo> / cd <repo> && ...) targeted, which is what
# tdd-gate needs to recognize companion-repo evidence for a cross-repo bead
# (73t4-style: a SABLE-hooks fix tracked as a market-brief-package bead).
# Mirrors sable_is_git_push's shlex-tokenize-then-walk approach
# (hooks/multi-manager/lib-identity.sh) — token-level parsing, not a blind
# substring grep, is what lets us track "which repo did THIS segment run in."
RESULT=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, re, shlex, sys

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)

cmd = d.get('tool_input', {}).get('command', '') or ''
cwd = d.get('cwd', '') or ''
sid = d.get('session_id', '') or ''
aid = d.get('agent_id', '') or ''
if not cmd or not sid:
    sys.exit(0)

SEPS = {';', '&&', '||', '|'}
SCRIPT_RE = re.compile(r'test-[A-Za-z0-9_-]+\.sh\$')
PYTEST_FILE_RE = re.compile(r'test_[A-Za-z0-9_-]+\.py')
# A redirect operator as its own shlex token (2>&1, >, >>, 2>, <, 1>&2, ...).
# SEPS only splits on command separators, not redirects, so a trailing
# 'script.sh 2>&1' leaves '2>&1' — not the script path — as seg[-1].
REDIRECT_RE = re.compile(r'^\d*(>>?|<)&?\d*\$')

try:
    tokens = shlex.split(cmd)
except ValueError:
    sys.exit(0)

segments = [[]]
for t in tokens:
    if t in SEPS:
        segments.append([])
    else:
        segments[-1].append(t)

def join_path(base, p):
    if p.startswith('/'):
        return p
    return (base.rstrip('/') + '/' + p) if base else p

effective_repo = cwd
hits = []  # list of (repo, joined-segment-text)
for seg in segments:
    if not seg:
        continue
    head = seg[0]

    # 'cd <path>' persists as the effective repo for later segments in this
    # same compound command (real shell semantics within one bash -c).
    if head == 'cd' and len(seg) > 1:
        effective_repo = join_path(effective_repo, seg[1])
        continue

    # 'git -C <path> ...' declares an effective repo too (precedent:
    # sable_resolve_push_repo_dir treats -C the same way for push commands).
    if head == 'git' and '-C' in seg:
        ci = seg.index('-C')
        if ci + 1 < len(seg):
            effective_repo = join_path(effective_repo, seg[ci + 1])
        continue

    joined = ' '.join(seg)
    matched = False

    # SABLE-dhfj: the runner keyword must be a real invocation token — the
    # segment's own command, or the subcommand of a known wrapper like
    # 'npx' — not merely a substring anywhere in the segment's text. A
    # blind substring match over the joined segment text fires on
    # 'grep vitest f', 'echo npm test', or a --description value that
    # happens to mention 'pytest', none of which run a test.
    eff_head, rest = head, seg
    if head == 'npx' and len(seg) > 1:
        eff_head, rest = seg[1], seg[1:]

    if eff_head == 'vitest':
        matched = True
    elif eff_head == 'pytest':
        matched = True
    elif eff_head == 'npm' and len(rest) > 1 and rest[1] == 'test':
        matched = True
    elif eff_head in ('python', 'python3'):
        for i in range(len(rest) - 1):
            if rest[i] == '-m' and rest[i + 1] == 'pytest':
                matched = True
                break
        if not matched and PYTEST_FILE_RE.search(joined):
            matched = True

    if not matched:
        # Drop a trailing redirect operator and everything after it (its
        # target file, fd, etc.) so the script path is still found as the
        # segment's true last token — 'test.sh 2>&1' must match like 'test.sh'.
        core = seg
        for i, t in enumerate(seg):
            if REDIRECT_RE.match(t):
                core = seg[:i]
                break
        last = core[-1] if core else seg[-1]
        # 'bash x' / 'sh x' / 'source x' style, OR direct execution
        # (./test-x.sh, /abs/path/test-x.sh — no interpreter token at all).
        interpreted = head in ('bash', 'sh', 'source', '.') and SCRIPT_RE.search(last)
        direct_exec = (last == head) and (last.startswith('./') or last.startswith('/')) and SCRIPT_RE.search(last)
        if interpreted or direct_exec:
            matched = True
            # An absolute path naming its own hooks/ tree is a stronger repo
            # signal than any earlier cd/-C tracking — use it directly.
            if last.startswith('/') and '/hooks/' in last:
                effective_repo = last.split('/hooks/')[0]

    if matched:
        hits.append((effective_repo or cwd or '', joined))

if not hits:
    sys.exit(0)

evidence_file = ('/tmp/tdd-evidence-%s-%s' % (sid, aid)) if aid else ('/tmp/tdd-evidence-%s' % sid)
print(evidence_file)
for repo, seg_text in hits:
    print('REPO=%s CMD=%s' % (repo, seg_text))
" 2>/dev/null) || exit 0

[ -z "$RESULT" ] && exit 0

EVIDENCE_FILE=$(printf '%s\n' "$RESULT" | head -1)
TS="$(date -Iseconds)"
printf '%s\n' "$RESULT" | tail -n +2 | while IFS= read -r line; do
  printf '%s %s\n' "$TS" "$line" >> "$EVIDENCE_FILE"
done

exit 0
