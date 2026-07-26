#!/usr/bin/env bash
# tdd-evidence.sh — Silent logger for test command evidence
# Fires on every PreToolUse:Bash, writes only when it detects test commands.
# Partner to tdd-gate.sh which checks the evidence file on bd close.

set -euo pipefail

# SABLE-jfg6.1 (contract D1): durable entry trace at TRUE line 1, before any
# stdin read, so absence-of-line == hook-never-fired (separable from
# fired-with-empty-stdin, which additionally logs STDIN_BYTES=0). Additive
# instrumentation only — the test-command detection below is unchanged.
# shellcheck source=multi-manager/lib-hook-trace.sh
. "$(dirname "${BASH_SOURCE[0]}")/multi-manager/lib-hook-trace.sh"
sable_trace_entry tdd-evidence

# SABLE-jfg6.4 (contract D4): derive the evidence-file path via the shared
# lib-evidence-key.sh so this WRITER can never drift from the tdd-gate.sh READER
# (the tfkv mismatch class). The Python below emits the parsed session_id and
# agent_id; the single derivation lives in the lib, called once from bash.
# shellcheck source=multi-manager/lib-evidence-key.sh
. "$(dirname "${BASH_SOURCE[0]}")/multi-manager/lib-evidence-key.sh"

HOOK_INPUT=$(sable_trace_read_stdin) || exit 0

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

# SABLE-5lli.1 (S2 prerequisite): a CMD= line today proves a test command
# RAN, not that it passed -- the S2 close-gate needs a target test to
# 'appear GREEN in evidence', which has no mechanical backing without a
# pass/fail signal. This hook still fires PreToolUse (before the command
# executes) with no result to report, so on that path STATUS stays
# unknown/omitted -- existing evidence-registration behavior is unchanged.
# When invoked with a completed tool result (a PostToolUse-shaped payload,
# or a synthetic test fixture standing in for one), read the exit status
# independently of any worker self-report. Field names are defensive
# because the platform's tool-result key has been observed under more than
# one name (tool_response vs tool_result) -- checking both, plus a couple
# of plausible shapes inside, means whichever the running platform emits
# is picked up without a schema guess breaking the other.
def _bool(v):
    return v if isinstance(v, bool) else None

tool_result = d.get('tool_response')
if not isinstance(tool_result, dict) or not tool_result:
    tool_result = d.get('tool_result')
if not isinstance(tool_result, dict):
    tool_result = {}

STATUS = None
_exit_code = None
for _key in ('exit_code', 'exitCode', 'code'):
    _v = tool_result.get(_key)
    if isinstance(_v, int) and not isinstance(_v, bool):
        _exit_code = _v
        break
if _exit_code is not None:
    STATUS = 'PASS' if _exit_code == 0 else 'FAIL'
else:
    _success = _bool(tool_result.get('success'))
    if _success is not None:
        STATUS = 'PASS' if _success else 'FAIL'
    else:
        _is_error = _bool(d.get('is_error'))
        if _is_error is None:
            _is_error = _bool(tool_result.get('is_error'))
        if _is_error is not None:
            STATUS = 'FAIL' if _is_error else 'PASS'
# SABLE-jfg6.4: an ABSENT session id no longer short-circuits — the shared lib
# derives a deterministic ppid fallback key downstream, so hccq-trap runs
# (Agent-subagent / gc-managed, no CLAUDE_SESSION_ID) still record evidence at
# the same key tdd-gate derives. A missing command still has nothing to detect.
if not cmd:
    sys.exit(0)

SEPS = {';', '&&', '||', '|'}
SCRIPT_RE = re.compile(r'test-[A-Za-z0-9_-]+\.sh\$')
# SABLE-rd9n0: SCRIPT_RE requires the basename to START with the literal
# 'test-', so it never matches this repo's DOCUMENTED canonical suite
# ('.github/ci/shell-run-set.sh' -- CLAUDE.md, Build & Test) even though a
# close citing it is legitimate TDD evidence. An allowlist of the specific
# canonical basenames (not a loosened regex) closes that gap without
# crediting an arbitrary '.sh' -- 'test-tiers.sh' is included too even
# though it already matches SCRIPT_RE, so the allowlist names every
# documented canonical script in one place.
CANONICAL_TEST_SCRIPTS = {'shell-run-set.sh', 'test-tiers.sh'}
PYTEST_FILE_RE = re.compile(r'test_[A-Za-z0-9_-]+\.py')
# A redirect operator as its own shlex token (2>&1, >, >>, 2>, <, 1>&2, ...).
# SEPS only splits on command separators, not redirects, so a trailing
# 'script.sh 2>&1' leaves '2>&1' — not the script path — as seg[-1].
REDIRECT_RE = re.compile(r'^\d*(>>?|<)&?\d*\$')

def is_test_script(token):
    if SCRIPT_RE.search(token):
        return True
    basename = token.rsplit('/', 1)[-1]
    return basename in CANONICAL_TEST_SCRIPTS

# SABLE-2nak (same class as SABLE-sxhx in lib-identity.sh): plain shlex.split
# only treats ; && || | as separators when they are whitespace-delimited from
# adjacent tokens (shlex.split('a.sh&&b.sh') -> ['a.sh&&b.sh'], not split), so
# an UNSPACED separator fused two commands into one token and the segmenter
# below never split them. shlex.shlex with punctuation_chars=';&|' +
# whitespace_split=True returns each run of separator chars as its own token
# even when unspaced, while leaving separators inside quotes untouched and
# producing output identical to shlex.split for every non-separator case.
try:
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=';&|')
    lexer.whitespace_split = True
    tokens = list(lexer)
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

    # SABLE-x8mx7: a bare inline 'NAME=value ... <cmd>' assignment prefix is the
    # shell's own env-for-one-command form (the fleet's sandbox-pinning contract
    # is exactly this shape: 'SABLE_LIB_DIR=/scratch python3 -m pytest ...').
    # Previously ONLY the explicit 'env VAR=val cmd' wrapper (below) was
    # unwrapped, so a bare prefix left the head as the VAR=value token, matched
    # no runner, and silently produced NO evidence -- a scoped test run was
    # invisible and tdd-gate denied the close. Strip leading NAME=value tokens
    # here, mirroring the env-branch's assignment regex. Leading assignments are
    # always at the segment head in real shell grammar, so this runs FIRST --
    # before the sable-test / env / cd / git unwraps -- and a segment that is
    # ONLY assignments (no command) correctly falls through to match nothing.
    while re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', head) and len(seg) > 1:
        seg = seg[1:]
        head = seg[0]
    # An assignment-only segment ('FOO=1' with no command): the shell would set
    # the var and run nothing, so there is nothing to classify.
    if re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', head):
        continue

    # SABLE-0w0ou: 'sable-test <cmd...>' runs <cmd...> and propagates its
    # exit code -- the REAL command is what must be classified, exactly like
    # npx's subcommand below. Unwrap it FIRST (before cd/git -C/env-strip/
    # interpreter detection) so a sable-test-wrapped run is never invisible
    # to this hook. bin/sable-test still writes its OWN evidence too (the
    # only writer for session types that fire no PreToolUse hooks at all);
    # this unwrap makes the common hooks-fire case self-recording as well,
    # so the two writers agree instead of one silently doing nothing.
    if head == 'sable-test' and len(seg) > 1:
        seg = seg[1:]
        head = seg[0]

    # SABLE-rzsb.5 / SABLE-j10xa: the fleet's hermetic-run contract prefixes
    # test commands with 'env -u VAR ... <cmd>' to scrub identity env vars
    # before the suite runs. Strip env's own option grammar so the REAL
    # interpreter is what gets classified, not the opaque 'env' token. Must
    # run AFTER the sable-test unwrap so 'sable-test env -u A -u B bash t.sh'
    # -- the combined shape both wrappers can appear in together -- still
    # resolves to 'bash'. Arity varies per env option: '-u NAME' and
    # '--unset NAME' consume the NEXT token too; '-uNAME', '--unset=NAME',
    # 'VAR=val', and '-i' consume only themselves; '--' ends option parsing.
    if head == 'env':
        i = 1
        n = len(seg)
        while i < n:
            t = seg[i]
            if t == '--':
                i += 1
                break
            if t in ('-i', '--ignore-environment'):
                i += 1
                continue
            if t in ('-u', '--unset'):
                i += 2
                continue
            if t.startswith('--unset='):
                i += 1
                continue
            if t.startswith('-u') and t != '-u':
                i += 1
                continue
            if re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', t):
                i += 1
                continue
            break
        seg = seg[i:]
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
        # target file, fd, etc.) — 'test.sh 2>&1' must match like 'test.sh'.
        core = seg
        for i, t in enumerate(seg):
            if REDIRECT_RE.match(t):
                core = seg[:i]
                break
        if not core:
            core = seg
        # SABLE-u2cig: the script path is not necessarily the segment's LAST
        # token — real invocations pass flags AFTER the script name (e.g.
        # 'bash .github/ci/test-tiers.sh --run pre_push'). Requiring it be
        # positionally last silently dropped evidence for any such run even
        # though the command genuinely executed (and passed) a test script.
        # Search the interpreter's argument tokens for the one naming a test
        # script, rather than pinning to the last position.
        script_token = None
        if head in ('bash', 'sh', 'source', '.'):
            for t in core[1:]:
                if is_test_script(t):
                    script_token = t
                    break
        # Direct execution (./test-x.sh --run, /abs/path/test-x.sh --run):
        # the script IS the segment's own command token (head), which can
        # likewise carry its own trailing args/flags — not necessarily last.
        if script_token is None and (head.startswith('./') or head.startswith('/')) and is_test_script(head):
            script_token = head
        if script_token is not None:
            matched = True
            # An absolute path naming its own hooks/ tree is a stronger repo
            # signal than any earlier cd/-C tracking — use it directly.
            if script_token.startswith('/') and '/hooks/' in script_token:
                effective_repo = script_token.split('/hooks/')[0]

    if matched:
        hits.append((effective_repo or cwd or '', joined))

if not hits:
    sys.exit(0)

# Emit the parsed identity (session id, agent id) for bash to feed the shared
# key lib — the path itself is derived in exactly ONE place (SABLE-jfg6.4).
print(sid)
print(aid)
for repo, seg_text in hits:
    if STATUS is not None:
        print('REPO=%s CMD=%s STATUS=%s' % (repo, seg_text, STATUS))
    else:
        print('REPO=%s CMD=%s' % (repo, seg_text))
" 2>/dev/null) || exit 0

[ -z "$RESULT" ] && exit 0

SID=$(printf '%s\n' "$RESULT" | sed -n '1p')
AID=$(printf '%s\n' "$RESULT" | sed -n '2p')
EVIDENCE_FILE=$(sable_evidence_key "$SID" "$AID")
TS="$(date -Iseconds)"
printf '%s\n' "$RESULT" | tail -n +3 | while IFS= read -r line; do
  printf '%s %s\n' "$TS" "$line" >> "$EVIDENCE_FILE"
done

exit 0
