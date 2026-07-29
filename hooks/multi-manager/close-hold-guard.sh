#!/usr/bin/env bash
# close-hold-guard.sh — PreToolUse:Bash guard: refuse a `bd close` that would
# leave the bead's branch INVISIBLE — unlanded, unheld, and unhanded-off
# (SABLE-hl9fu).
# Trigger: PreToolUse:Bash | Timeout: 25000ms
#
# THE DEFECT. CLOSED IS NOT LANDED here. A worker closes its bead; its branch
# sits pushed-and-unmerged; and with no hold and no for-chuck handoff that work
# is invisible from the ready pool, from the lane's own record, and from the
# merge seat AT THE SAME TIME (SABLE-zvx4i). The four-field hold protocol
# (hold/hold_by/hold_since/hold_until, SABLE-jejx3) exists for exactly this and
# everyone believes in it — but CLOSING A BEAD AND APPLYING ITS HOLD ARE TWO
# SEPARATE ACTS AND NOTHING COUPLED THEM. Measured 2026-07-23: an optimus sweep
# over 309 closed beads found six unheld closed beads with live unmerged
# branches; WITHIN THE HOUR tarzan produced a seventh, and the optimus lane four
# more. Measured again 2026-07-26, while both lanes were actively watching for
# it: four more branches (wk-footprint-parser-consol, wk-recover-diverged-frozen,
# wk-identifier-decay-scope, wk-reconcile-containment) reached the merge seat
# ONLY because a manager hand-carried them in a message. Chuck: "a closed work
# bead with no for-chuck handoff is invisible to me by construction."
#
# THE MANAGERS WHO DEFINED THE PROTOCOL AND ENFORCED IT ON EACH OTHER STILL
# PRODUCED THE DEFECT ROUTINELY. That is the signature of a missing mechanism,
# not of insufficient discipline.
#
# WHY A HOOK AND NOT THE SWEEP (bin/sable-reconcile-handoffs, SABLE-23upx). A
# sweep is a post-hoc query: it can only find the state AFTER it exists, and only
# when someone remembers to run it. The window between the close and the sweep is
# exactly when a manager is choosing what to dispatch and a seat is choosing what
# to promote. The property is checkable at the moment it is CREATED, so it is
# checked there. The sweep remains the backstop for state created before this
# hook was wired, or by a path that never reaches a PreToolUse gate.
#
# ---------------------------------------------------------------------------
# WHAT IT DOES — and, load-bearingly, WHAT IT DOES NOT
# ---------------------------------------------------------------------------
#   bd close <id>  where the bead has NO `branch` metadata        -> ALLOW, SILENT
#                  where the branch IS contained in integration   -> ALLOW, SILENT
#                  where the branch is unlanded but HELD (4/4)    -> ALLOW, SILENT
#                  where the branch is unlanded but a for-chuck
#                    handoff already NAMES it                     -> ALLOW, SILENT
#                  where the branch's ref is ABSENT               -> ALLOW, LOUD
#                  anything unreadable / unresolvable             -> ALLOW, LOUD
#                  where the branch is unlanded, unheld AND
#                    unhanded-off                                 -> DENY
#   anything that is not a `bd close`                             -> ALLOW, SILENT
#
# THE NEGATIVE CONTROLS ARE THE DELIVERABLE, not a nicety. This is a gate on the
# single highest-traffic write in the fleet. IF IT IS NOISY IT WILL BE BYPASSED
# WITHIN A DAY, and a bypassed guard is worse than none because it also teaches
# the reflex (SABLE-r5pfw erosion). Every ordinary close — no branch, or a landed
# one — must emit NOTHING AT ALL, and hooks/test/test-close-hold-guard.sh asserts
# that by exact decision value, not by substring.
#
# REFUSE, NOT AUTO-HOLD, AND THE REASON IS LOAD-BEARING (SABLE-hl9fu). The bead
# offered (a) refuse the close until the four hold fields are supplied, or (b)
# auto-apply a hold. It prefers (a): A hold_until THAT NOBODY WROTE IS WORSE THAN
# NO HOLD, because it LOOKS like a considered release condition. This fleet has
# been bitten by precisely that — a hold whose stated conditions had all been
# discharged nearly got lifted by someone being conscientious. So this guard
# generates NO release prose at any point; it hands back a command template with
# an unmistakable `<...>` placeholder the human must fill in.
#
# SCOPE: BOTH WORKERS AND MANAGERS — and here is why that does not wedge a
# worker. A refusal that a worker cannot satisfy would block the normal path, so
# the guard deliberately accepts TWO forms of visibility, not one:
#   * a HOLD  — the manager's route: this work is deliberately not merging.
#   * a for-chuck HANDOFF naming the branch — the worker's route, and the worker
#     ALREADY HAS IT: the worker's contract is push-then-close, and the push
#     fires hooks/multi-manager/post-push-merge-notify.sh, which files that
#     handoff before the close is ever typed.
# So on the normal worker path this guard is silent by construction, and it fires
# exactly when the handoff did NOT happen — which is the defect. A worker that
# trips it is not wedged: it is being told its push never reached the seat, and
# re-pushing (or `sable-reconcile-handoffs`) clears it. Scoping the guard to
# managers only would have missed every one of the 2026-07-26 instances, all four
# of which were worker closes.
#
# ---------------------------------------------------------------------------
# THE CONTAINMENT PREDICATE — REUSED, NOT REINVENTED
# ---------------------------------------------------------------------------
# Containment is delegated ENTIRELY to `sable-contained` (bin/sable-contained,
# SABLE-gdp05/4snb4), which hardcodes the `merge-base --is-ancestor` argument
# order and cross-checks it against a second independent method. A third
# hand-written containment check is the SABLE-ev5i5 three-mirror shape and cost
# this fleet a full investigation.
#
# What this hook DOES own is the step BEFORE containment, and it is the subtle
# one:
#     tip = git rev-parse --verify --quiet <ref>     # --verify --quiet REQUIRED
# A BARE `git rev-parse` ECHOES AN UNRESOLVABLE REF BACK TO STDOUT, so every
# reaped branch resolves to a garbage "tip" that is trivially not-an-ancestor —
# that defect produced 300 false findings in one hand-written sweep. And
# `--verify --quiet` only makes the RESOLUTION safe: THE CALLER MUST BRANCH ON
# THE EMPTY TIP BEFORE TESTING CONTAINMENT, or a naive `|| not-contained` prints
# a confident wrong verdict. Note the direction — the empty-tip bug resolves
# RELEASING for holds and BLOCKING for materialization, wrong both ways from one
# cause. Here it would BLOCK: every close of a bead whose branch had been reaped
# would be refused, with no way to satisfy the refusal.
#
# So an empty tip is a THIRD STATE, never "not contained":
#   * an `archive/<branch>` tag exists -> DELIBERATELY RETIRED, reported, allowed
#   * no such tag                      -> UNKNOWN, reported, allowed
# Neither ever denies. An absence probe must never decay toward blocking, and it
# must never decay toward silence either — both are reported out loud.
#
# FAIL-OPEN, BUT NEVER SILENTLY (Standing Discipline 7). Every could-not-assess
# path ALLOWS and says so, naming what it could not read. A guard that allows
# silently on error is indistinguishable from one that allowed on purpose — the
# SABLE-2az2x/6sdpx defect class.
#
# `bd` absent from PATH exits silently: the guarded `bd close` cannot write
# either, so there is nothing to protect (same reasoning as
# notes-clobber-guard.sh).

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

# Cheap pre-filter so the evaluator never spawns on the overwhelming majority of
# Bash calls. Deliberately loose (the evaluator re-derives the real answer with a
# proper tokenizer): `bd` and `close` both present, in that order, anywhere.
case "$COMMAND" in
  *bd*close*) ;;
  *) exit 0 ;;
esac

# ---------------------------------------------------------------------------
# Emit helpers — the same hookSpecificOutput shape as notes-clobber-guard.sh.
# ---------------------------------------------------------------------------
json_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\t'/\\t}"
  printf '%s' "$value"
}

allow_with_context() {
  local message
  message="$(json_escape "$1")"
  printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "additionalContext": "%s"}}\n' "$message"
  exit 0
}

deny_with_reason() {
  local reason
  reason="$(json_escape "$1")"
  printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "%s"}}\n' "$reason"
  exit 0
}

# --- PLANT-AND-FAIL SEAM (SABLE-5lli.7) ------------------------------------
# hooks/test/test-close-hold-guard.sh builds a MUTANT of this file by inserting
# a `deny_with_reason() { exit 0; }` override immediately after the marker line
# below — the coupling, and nothing else, removed — and asserts the
# uncontained-close case flips from DENY to silent. That is what makes the deny
# assertion load-bearing rather than green for some unrelated reason. The same
# mutant is asserted to leave every negative control unchanged, so the plant is
# shown to bite in one direction only. DO NOT REMOVE THIS MARKER.
# PLANT-MARKER: deny-override-insertion-point

# `bd` absent means the guarded close cannot write anything either.
command -v bd >/dev/null 2>&1 || exit 0

# ---------------------------------------------------------------------------
# The evaluator. Prints exactly one of:
#   SILENT
#   NOTE\n<context>
#   DENY\n<reason>
# Kept in one python block (rather than a bin/ library) so the hook stays
# self-contained under the COPY install into ~/.claude/hooks — there is no
# PATH-installed form of a *.py lib (sable-bin-install only symlinks *.py-less
# sable-* entrypoints; SABLE-nn54x is what a hook that loses its library does).
# ---------------------------------------------------------------------------
RESULT=$(CHG_COMMAND="$COMMAND" python3 - <<'PYEOF' 2>&1
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

CMD = os.environ.get("CHG_COMMAND", "")

SHELL_SEPS = {";", "&&", "||", "|", "&"}
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Bead ids as bd mints them: <prefix>-<suffix>, suffix may carry .N for children.
BEAD_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-[A-Za-z0-9]+(\.[0-9]+)*$")

HOLD_FIELDS = ("hold", "hold_by", "hold_since", "hold_until")


# --- command-line classification (same tokenizer contract as
# --- notes-clobber-guard.sh / close-decay-sweep.sh) -------------------------

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


def classify(cmd):
    """-> ('none'|'unresolved'|'targets', [bead ids])

    Positional-until-the-first-flag, so a flag VALUE (`--reason SABLE-x`) can
    never be mistaken for a close target. Every `bd close` segment on the line
    contributes, because `cd x && bd close A && bd close B` is one command."""
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return ("unresolved", []) if re.search(r"\bbd\b.*\bclose\b", cmd) else ("none", [])
    found = False
    ids = []
    for raw_seg in segments(tokens):
        seg = strip_env_prefix(raw_seg)
        if len(seg) < 2:
            continue
        if os.path.basename(seg[0]) != "bd" or seg[1] != "close":
            continue
        found = True
        for t in seg[2:]:
            if t.startswith("-"):
                break
            if BEAD_ID_RE.match(t):
                ids.append(t)
            else:
                break
    if not found:
        return ("none", [])
    if not ids:
        return ("unresolved", [])
    return ("targets", ids)


# --- process boundary -------------------------------------------------------

def run(argv):
    """Never raises: a missing/hung tool is a could-not-assess here, not a
    traceback that would take the whole hook down with it."""
    try:
        return subprocess.run(argv, text=True, capture_output=True, timeout=20)
    except Exception as exc:  # noqa: BLE001 — see docstring
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def bead_record(bead_id):
    cp = run(["bd", "show", bead_id, "--json"])
    if cp.returncode != 0:
        return None
    try:
        d = json.loads(cp.stdout)
    except Exception:
        return None
    if isinstance(d, list):
        d = d[0] if d else None
    return d if isinstance(d, dict) else None


def metadata_of(bead):
    meta = bead.get("metadata")
    return meta if isinstance(meta, dict) else {}


def field(meta, key):
    v = meta.get(key)
    return v.strip() if isinstance(v, str) and v.strip() else None


def in_git_repo():
    return run(["git", "rev-parse", "--git-dir"]).returncode == 0


def resolve_tip(ref):
    """`git rev-parse --verify --quiet <ref>` -> sha, or None.

    BOTH conditions are required: a clean exit AND non-empty output. This is the
    whole SABLE-hl9fu trap — a bare `git rev-parse` echoes an unresolvable ref
    back to stdout, and every caller downstream then treats that garbage string
    as a real tip. Returning None here is what lets the CALLER branch on the
    empty tip BEFORE asking the containment question."""
    cp = run(["git", "rev-parse", "--verify", "--quiet", ref])
    sha = (cp.stdout or "").strip()
    if cp.returncode != 0 or not sha:
        return None
    return sha


def branch_tips(branch):
    """Every ref this branch name currently resolves to, as (ref, sha).

    Both the local head and the published origin ref are probed, and BOTH must
    be contained for the branch to count as landed. A local head ahead of origin
    is unpushed work about to be closed — exactly the invisible state — so
    judging on origin alone would let it through."""
    tips = []
    for ref in ("refs/heads/" + branch, "refs/remotes/origin/" + branch):
        sha = resolve_tip(ref)
        if sha:
            tips.append((ref, sha))
    return tips


def is_archived(branch):
    """A deliberately retired branch carries an `archive/<branch>` tag. This is
    what separates DELIBERATELY RETIRED from merely UNKNOWN once the ref itself
    is gone — two different reports, neither of them a denial."""
    return resolve_tip("refs/tags/archive/" + branch) is not None


_CONTAINED_EXE = None
_CONTAINED_LOOKED = False


def contained_exe():
    global _CONTAINED_EXE, _CONTAINED_LOOKED
    if not _CONTAINED_LOOKED:
        _CONTAINED_EXE = shutil.which("sable-contained")
        _CONTAINED_LOOKED = True
    return _CONTAINED_EXE


def containment(sha):
    """-> ('contained'|'not-contained'|'unresolved', detail)

    Delegated wholesale to bin/sable-contained (SABLE-gdp05/4snb4) — its exit
    vocabulary is 0 contained / 1 not-contained / 2 usage / 3 the two internal
    methods disagreed / 4 could not assess. Only a clean 1 is ever treated as
    not-contained; 2/3/4 are unresolved and must never be guessed at."""
    exe = contained_exe()
    if not exe:
        return ("unresolved",
                "sable-contained is not on PATH (run bin/sable-bin-install) — "
                "no containment check ran")
    cp = run([exe, sha])
    out = (cp.stdout or "").strip() or (cp.stderr or "").strip()
    if cp.returncode == 0:
        return ("contained", out)
    if cp.returncode == 1:
        return ("not-contained", out)
    return ("unresolved", "sable-contained exited %d: %s" % (cp.returncode, out))


_FOR_CHUCK = "unfetched"


def for_chuck_titles():
    """Titles of open/in-progress `for-chuck` beads, or None when the query
    itself failed. None is NOT an empty corpus: an unreadable corpus means the
    handoff can be neither confirmed nor ruled out, and this guard reports that
    rather than denying on it — refusing a close because bd hiccuped is exactly
    the noise that gets a gate bypassed."""
    global _FOR_CHUCK
    if _FOR_CHUCK == "unfetched":
        cp = run(["bd", "list", "--status", "open,in_progress",
                  "--label", "for-chuck", "--json"])
        if cp.returncode != 0:
            _FOR_CHUCK = None
        else:
            try:
                d = json.loads(cp.stdout)
            except Exception:
                d = None
            if isinstance(d, dict):
                d = [d]
            if isinstance(d, list):
                _FOR_CHUCK = [b.get("title") or "" for b in d if isinstance(b, dict)]
            else:
                _FOR_CHUCK = None
    return _FOR_CHUCK


def title_names_branch(title, branch):
    """Does a for-chuck bead TITLE name this exact branch? Matched on a
    delimited-token boundary so `wk-foo` never matches a handoff about
    `wk-foobar`. Mirrors find-the-handoff in bin/sable-reconcile-handoffs
    (title_names_branch) deliberately rather than importing it: that module
    loads sable-merge-gate and sable-worker-status at import time, which is far
    outside a PreToolUse budget, and the installed hook has no adjacent bin/ to
    import from at all. If that regex ever changes, change it here too."""
    if not title:
        return False
    pat = r"(?:^|[^\w./-])" + re.escape(branch) + r"(?:$|[^\w./-])"
    return re.search(pat, title) is not None


def hold_state(meta):
    """-> ('none'|'complete'|'partial', [missing field names])

    All four fields are required. A PARTIAL hold is refused rather than accepted
    with a review flag (which is what the after-the-fact sweep does): at
    creation time the missing field can still be supplied by the person who
    knows the answer, and an unowned or never-expiring hold decays into a
    permanent quiet veto that outlives everyone who knew why it was placed
    (SABLE-jejx3)."""
    present = [k for k in HOLD_FIELDS if field(meta, k)]
    if not present:
        return ("none", list(HOLD_FIELDS))
    missing = [k for k in HOLD_FIELDS if not field(meta, k)]
    return ("complete" if not missing else "partial", missing)


def hold_hint(bead_id, branch):
    """The exact command that places a complete hold. NOTE THE PLACEHOLDERS: this
    guard NEVER writes the reason or the release condition. A hold_until nobody
    wrote is worse than no hold, because it reads as a considered decision."""
    return ("bd update %s --sandbox "
            "--set-metadata hold=\"<why this must not merge yet>\" "
            "--set-metadata hold_by=\"<your agent name>\" "
            "--set-metadata hold_since=\"<ISO8601 now>\" "
            "--set-metadata hold_until=\"<the condition that releases it>\"  "
            "# branch %s" % (bead_id, branch))


# --- per-bead decision ------------------------------------------------------

def assess(bead_id):
    """-> ('silent'|'note'|'deny', message)"""
    bead = bead_record(bead_id)
    if bead is None:
        return ("note", "%s: COULD NOT ASSESS — `bd show %s --json` returned "
                        "nothing readable, so this close was NOT checked for an "
                        "unlanded branch." % (bead_id, bead_id))

    meta = metadata_of(bead)
    branch = field(meta, "branch")
    if not branch:
        # THE COMMON CASE. Must stay completely silent.
        return ("silent", "")

    tips = branch_tips(branch)
    if not tips:
        # THE EMPTY-TIP TRAP. Absent is a THIRD STATE, never "not contained".
        if is_archived(branch):
            return ("note", "%s: branch %s has no ref in refs/heads or "
                            "refs/remotes/origin, and an archive/%s tag exists — "
                            "DELIBERATELY RETIRED, not stranded. Allowing the "
                            "close; no hold is needed for retired work."
                            % (bead_id, branch, branch))
        return ("note", "%s: branch %s does not resolve in refs/heads or "
                        "refs/remotes/origin and carries no archive/%s tag, so "
                        "its containment is UNKNOWN — NOT 'unlanded'. Allowing "
                        "the close (an absence probe must never block), but "
                        "nothing verified where that work went. If it was "
                        "reaped on purpose, tag it archive/%s."
                        % (bead_id, branch, branch, branch))

    unresolved = []
    uncontained = []
    for ref, sha in tips:
        verdict, detail = containment(sha)
        if verdict == "contained":
            continue
        if verdict == "not-contained":
            uncontained.append((ref, sha))
        else:
            unresolved.append((ref, detail))

    if unresolved:
        return ("note", "%s: COULD NOT ASSESS containment of branch %s (%s). "
                        "Allowing the close, but nothing verified that this "
                        "work is landed, held, or handed off."
                        % (bead_id, branch,
                           "; ".join("%s: %s" % (r, d) for r, d in unresolved)))

    if not uncontained:
        # LANDED. The other load-bearing negative control: silent.
        return ("silent", "")

    # --- unlanded. Is it VISIBLE to anyone? --------------------------------
    state, missing = hold_state(meta)
    if state == "complete":
        return ("silent", "")

    where = ", ".join("%s (%s)" % (r, s[:12]) for r, s in uncontained)

    if state == "partial":
        return ("deny",
                "close-hold-guard: DENIED closing %s — branch %s is NOT merged "
                "into the integration branch (%s) and its hold is INCOMPLETE: "
                "missing %s. A hold missing hold_by is unreleasable after a "
                "recycle and one missing hold_until cannot expire, so it decays "
                "into a permanent quiet veto (SABLE-jejx3). Supply all four "
                "fields, then close:\n  %s"
                % (bead_id, branch, where, ", ".join(missing),
                   hold_hint(bead_id, branch)))

    titles = for_chuck_titles()
    if titles is None:
        return ("note", "%s: branch %s is NOT merged and carries no hold, but "
                        "the for-chuck corpus could not be read (`bd list` "
                        "failed), so whether a handoff already names it is "
                        "UNKNOWN. Allowing the close rather than refusing on an "
                        "unreadable query — but if no handoff exists, this "
                        "branch is now invisible to the merge seat (SABLE-zvx4i). "
                        "Verify with: sable-reconcile-handoffs --dry-run"
                        % (bead_id, branch))
    if any(title_names_branch(t, branch) for t in titles):
        # HANDED OFF. The worker's normal path — silent.
        return ("silent", "")

    return ("deny",
            "close-hold-guard: DENIED closing %s — branch %s is NOT merged into "
            "the integration branch (%s), carries NO hold, and NO open for-chuck "
            "bead names it. Closing now creates the SABLE-zvx4i invisible state: "
            "the work drops out of the ready pool, out of the lane's record, and "
            "out of the merge seat's view at the same time — \"a closed work bead "
            "with no for-chuck handoff is invisible to me by construction\" "
            "(chuck). Pick the one that is true:\n"
            "  (1) IT SHOULD MERGE — the push-time handoff was missed. Re-push "
            "from the worktree root (post-push-merge-notify files it), or run "
            "`sable-reconcile-handoffs` to file it now. Then close.\n"
            "  (2) IT MUST NOT MERGE YET — place the four-field hold. Write the "
            "reason and the release condition YOURSELF; this guard deliberately "
            "does not generate them, because a hold_until nobody wrote reads as a "
            "considered decision and gets honoured as one:\n"
            "  %s\n"
            "  (3) THE BRANCH IS DEAD — retire it deliberately: "
            "`git tag archive/%s %s` (an archive tag is reported, not blocked)."
            % (bead_id, branch, where, hold_hint(bead_id, branch),
               branch, branch))


def main():
    verdict, ids = classify(CMD)
    if verdict == "none":
        print("SILENT")
        return
    if verdict == "unresolved":
        print("NOTE")
        print("close-hold-guard: COULD NOT ASSESS this `bd close` — no target "
              "bead id was parseable from the command line, so nothing was "
              "checked for an unlanded branch. ALLOWING (fail-open).")
        return

    if not in_git_repo():
        print("NOTE")
        print("close-hold-guard: COULD NOT ASSESS %s — the working directory is "
              "not inside a git repository, so no branch containment check could "
              "run. ALLOWING (fail-open); this is NOT a clean result."
              % " ".join(ids))
        return

    denies, notes = [], []
    for bead_id in ids:
        kind, msg = assess(bead_id)
        if kind == "deny":
            denies.append(msg)
        elif kind == "note":
            notes.append(msg)

    if denies:
        print("DENY")
        print("\n\n".join(denies))
        if notes:
            print("\nAlso could not assess: " + " | ".join(notes))
        return
    if notes:
        print("NOTE")
        print("close-hold-guard: " + " | ".join(notes))
        return
    print("SILENT")


main()
PYEOF
) || RESULT=""

# The evaluator itself failing is a could-not-assess, never a block and never a
# silent pass.
if [ -z "$RESULT" ]; then
  allow_with_context "close-hold-guard: COULD NOT ASSESS — the guard's own evaluator produced no verdict, so this 'bd close' was NOT checked for an unlanded branch. ALLOWING (fail-open). SABLE-hl9fu: a closed bead whose branch is unmerged, unheld and unhanded-off is invisible to the ready pool, the lane record and the merge seat at the same time."
fi

VERDICT="$(printf '%s\n' "$RESULT" | head -1)"
BODY="$(printf '%s\n' "$RESULT" | tail -n +2)"

case "$VERDICT" in
  SILENT)
    exit 0
    ;;
  DENY)
    deny_with_reason "$BODY"
    ;;
  NOTE)
    allow_with_context "$BODY"
    ;;
  *)
    allow_with_context "close-hold-guard: COULD NOT ASSESS — the guard's evaluator returned an unrecognized verdict ($(printf '%s' "$VERDICT" | head -c 200)), so this 'bd close' was NOT checked. ALLOWING (fail-open)."
    ;;
esac

exit 0
