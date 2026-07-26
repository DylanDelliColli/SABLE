#!/usr/bin/env bash
# worktree-placement-guard.sh — PreToolUse:Bash guard: deny a `bd worktree
# create <name>` invocation whose resolved path lands INSIDE this repo's
# checkout.
# Trigger: PreToolUse:Bash | Timeout: 3000ms
#
# SABLE-djgy.2 / SABLE-z56d (VICTOR-CORRECTED 2026-07-17): `bd worktree
# create <name>` documents its own behavior plainly ("Creates a git worktree
# at ./<name>" + "Adds the worktree path to .gitignore (if inside repo
# root)") — a bare, repo-root-relative name therefore (1) NESTS the new
# worktree inside the shared checkout and (2) writes to the checkout's
# TRACKED .gitignore. Both are load-bearing hazards: a nested worktree
# blocks rebase/merge on the parent checkout, and `git clean -fdx` from the
# parent eats the nested worktree's state outright.
#
# The convention that produces this hazard ("run bare `bd worktree create
# <name>` from repo root") lives in the OUT-OF-REPO global
# ~/.claude/CLAUDE.md — not in this repo, and not in SABLE code. The
# worker-spawn helper (bin/sable-spawn-worker resolve_worktree_path,
# :123-128) already places every worktree it creates as an ABSOLUTE SIBLING
# of the repo root (repo_root's parent / wk-<name>) — outside the checkout,
# so it never trips this guard. This hook makes that placement rule
# mechanically enforced for every OTHER caller instead of leaving it as a
# convention an agent has to remember (hook-over-lint).
#
# BEHAVIOR:
#   `bd worktree create <name>` (or `... create <name> --branch=...`),
#   possibly preceded by a `cd <dir> &&` shift or a `bd -C <dir>` /
#   `bd --directory <dir>` flag, is resolved to an absolute target path
#   against the effective base directory. That target is then checked
#   against the base directory's repo root (`git rev-parse --show-toplevel`):
#
#     target is INSIDE the repo root   -> DENY, point at the safe
#                                          absolute-sibling form.
#     target is OUTSIDE the repo root  -> ALLOW (exit 0, silent) — this is
#                                          the sable-spawn-worker shape and
#                                          any other already-safe form (e.g.
#                                          `bd worktree create ../foo`).
#     not a `bd worktree create` call,
#     no resolvable target, or the base
#     directory is not inside a git
#     repo at all                      -> NONE (exit 0, silent) — fail open,
#                                          matching every other guard in this
#                                          hooks/multi-manager/ layer
#                                          (stash-worktree-guard.sh,
#                                          tree-claim.sh): an unparseable
#                                          command is not this guard's call
#                                          to block.
#
# `bd worktree remove` and every other bd subcommand are untouched.

set -uo pipefail

HOOK_INPUT=$(cat 2>/dev/null) || HOOK_INPUT=""
[ -z "$HOOK_INPUT" ] && exit 0

printf '%s' "$HOOK_INPUT" | PWD_FALLBACK="$PWD" python3 -c '
import json, os, shlex, sys

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)

command = (d.get("tool_input") or {}).get("command", "") or ""
hook_cwd = d.get("cwd") or os.environ.get("PWD_FALLBACK", "") or os.getcwd()

if not command.strip():
    sys.exit(0)

try:
    tokens = shlex.split(command)
except ValueError:
    sys.exit(0)

SHELL_SEPS = {";", "&&", "||", "|", "&"}
ENV_ASSIGN_PREFIX = None  # set below, avoid importing re for one pattern
import re
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

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
        if t == "env":
            i += 1
            while i < n:
                tt = seg[i]
                if ENV_ASSIGN_RE.match(tt):
                    i += 1
                    continue
                if tt == "-u" and i + 1 < n:
                    i += 2
                    continue
                break
            continue
        break
    return seg[i:]

BD_CONSUME_NEXT = {"-C", "--directory", "--db", "--actor", "--dolt-auto-commit"}
BD_CONSUME_NEXT_PREFIXES = ("--directory=", "--db=", "--actor=", "--dolt-auto-commit=")
BD_STANDALONE = {
    "--global", "--ignore-schema-skew", "--json", "-q", "--quiet",
    "--profile", "-v", "--verbose", "--readonly", "--sandbox", "-h", "--help",
}

def resolve_against(base, path):
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(base, path))

def parse_bd_worktree_create(seg, cur_cwd):
    """seg[0] == 'bd'. Returns (bd_dir, target) or None if this segment is
    not a `bd worktree create <name>` invocation, or target is None if the
    invocation has no positional name (bd will error on that itself)."""
    i, n = 1, len(seg)
    bd_dir = cur_cwd
    while i < n:
        t = seg[i]
        if t in ("-C", "--directory"):
            if i + 1 < n:
                bd_dir = resolve_against(bd_dir, seg[i + 1])
                i += 2
                continue
            i += 1
            continue
        if t.startswith("--directory="):
            bd_dir = resolve_against(bd_dir, t[len("--directory="):])
            i += 1
            continue
        if t in BD_CONSUME_NEXT:
            i += 2
            continue
        if any(t.startswith(p) for p in BD_CONSUME_NEXT_PREFIXES):
            i += 1
            continue
        if t in BD_STANDALONE:
            i += 1
            continue
        if t.startswith("-"):
            i += 1
            continue
        break
    if i >= n or seg[i] != "worktree":
        return None
    i += 1
    if i >= n or seg[i] != "create":
        return None
    i += 1

    target = None
    while i < n:
        t = seg[i]
        if t == "--branch":
            i += 2
            continue
        if t.startswith("--branch="):
            i += 1
            continue
        if t.startswith("-"):
            i += 1
            continue
        if target is None:
            target = t
        i += 1
    return (bd_dir, target)

cur_cwd = os.path.realpath(hook_cwd) if os.path.isdir(hook_cwd) else hook_cwd
match = None

for raw_seg in segments(tokens):
    seg = strip_env_prefix(raw_seg)
    if not seg:
        continue
    if seg[0] == "cd" and len(seg) >= 2 and seg[1] not in ("-",) and not seg[1].startswith("$"):
        cur_cwd = resolve_against(cur_cwd, seg[1])
        continue
    if seg[0] != "bd":
        continue
    parsed = parse_bd_worktree_create(seg, cur_cwd)
    if parsed is not None:
        match = parsed
        break

if match is None:
    sys.exit(0)

bd_dir, target = match
if not target:
    sys.exit(0)

if not os.path.isdir(bd_dir):
    sys.exit(0)

import subprocess
try:
    proc = subprocess.run(
        ["git", "-C", bd_dir, "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=5,
    )
except Exception:
    sys.exit(0)

if proc.returncode != 0:
    sys.exit(0)

repo_root = os.path.normpath(proc.stdout.strip())
if not repo_root:
    sys.exit(0)

resolved_target = resolve_against(bd_dir, target)

is_inside = resolved_target == repo_root or resolved_target.startswith(repo_root + os.sep)

if not is_inside:
    sys.exit(0)

sibling_example = os.path.join(os.path.dirname(repo_root), "wk-<name>")
reason = (
    "worktree-placement-guard: \x27bd worktree create " + target + "\x27 denied -- "
    "resolves to " + resolved_target + ", INSIDE this repo\x27s checkout (" + repo_root + "). "
    "bd worktree create places a bare/relative name at ./<name> and, per its own "
    "docs, ADDS THAT PATH TO THE TRACKED .gitignore when it lands inside the repo "
    "root -- this is the SABLE-z56d hazard: a nested worktree blocks rebase/merge "
    "on the parent checkout, dirties the shared .gitignore, and gets eaten by a "
    "`git clean -fdx` run from the parent tree. Use the safe absolute-sibling form "
    "instead, exactly as bin/sable-spawn-worker resolve_worktree_path does: "
    "`bd worktree create " + sibling_example + "` -- a path OUTSIDE the checkout, "
    "as a sibling of the repo root."
)
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
}))
' 2>/dev/null

exit 0
