#!/usr/bin/env bash
# lib-golden-manifest.sh — the install-golden-manifest <-> repo-source relation,
# declared ONCE for the two consumers that must agree about it (SABLE-slip0.7).
#
# THE TWO CONSUMERS:
#   hooks/test/test-install-golden-manifest.sh   asserts each pinned hash still
#                                                equals sha256(its repo source)
#   .github/ci/impact-manifest.sh                selects that suite whenever a
#                                                changed path IS one of those
#                                                repo sources
#
# WHY ONE PLACE. Those two need the same answer to "which repo file does this
# installed artifact come from". Hand-maintaining the list twice — or once here
# and once as a 50-path COVERS string in shell-run-set.sh — recreates exactly
# the drift class the impact manifest exists to prevent: add a new installed
# artifact, regenerate the golden, forget the other copy, and changes to that
# artifact silently stop selecting the suite that would have caught a stale pin.
# Deriving the selection rule FROM the golden means a regenerated golden updates
# the selection rule in the same edit, with nothing to remember.
#
# WHAT "VERBATIM" MEANS HERE. install.sh copies most artifacts byte-for-byte
# into ~/.claude, so the golden's recorded sha256 is just sha256 of the repo
# file. (test-install.sh hashes through a sed that rewrites the scratch HOME
# path to __HOME__; for a copied source file that substitution is a no-op, which
# is why the two hashes agree.) A handful of artifacts are GENERATED or
# TRANSFORMED at install time instead — those are listed in GOLDEN_DERIVED with
# their reason and are deliberately out of scope for the hash comparison; only
# a real install can assert them, which is test-install.sh's job.
#
# FAIL-CLOSED. An installed path that resolves to neither a verbatim source nor
# a GOLDEN_DERIVED entry is an ERROR. A resolver that silently skipped what it
# did not recognize would let a whole new artifact class go unchecked while
# still printing green.
#
# Sourced, not executed, by both consumers; also runnable directly for humans:
#   bash hooks/test/lib-golden-manifest.sh --check

set -uo pipefail

GOLDEN_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GOLDEN_REPO="$(cd "$GOLDEN_LIB_DIR/../.." && pwd)"

# The fixture, and the suite that executes it. Both consumers refer to these
# rather than spelling the paths inline.
GOLDEN_MANIFEST_REL="hooks/test/fixtures/install-golden-manifest.txt"
GOLDEN_MANIFEST_SUITE="test-install-golden-manifest.sh"

# GOLDEN_DERIVED: installed paths install.sh (or its sable-orchestration-install
# delegate) GENERATES or TRANSFORMS rather than copies, so their content is not
# any single repo file's bytes. Each is excluded from the hash comparison WITH
# its reason. This list is itself checked for rot: golden_check errors if an
# entry no longer appears in the golden at all (see check 3 below).
#
#   ./.sable-install-provenance   written per-install from the CURRENT repo HEAD
#                                 + wall clock; test-install.sh normalizes the
#                                 commit=/branch=/timestamp= fields (SABLE-z31s1)
#   ./CLAUDE.md                   the Prime Directive block PREPENDED to whatever
#                                 CLAUDE.md the scope already had (install.sh
#                                 step 7) — a merge result, not a copy
#   ./sable/reconcile-timer/*     three heredocs emitted by
#                                 bin/sable-orchestration-install, embedding the
#                                 source checkout path and the chosen cadence
#                                 (SABLE-jfg6.5 / D3 TIMER LEG)
GOLDEN_DERIVED=(
  "./.sable-install-provenance"
  "./CLAUDE.md"
  "./sable/reconcile-timer/sable-reconcile-timer.service"
  "./sable/reconcile-timer/sable-reconcile-timer.timer"
  "./sable/reconcile-timer/sable-reconcile-timer.cron"
)

golden_is_derived() {
  local p="$1" d
  for d in "${GOLDEN_DERIVED[@]}"; do [ "$d" = "$p" ] && return 0; done
  return 1
}

# golden_source_for <installed-path>: the repo-relative file whose bytes the
# installed artifact at ./<path> is a verbatim copy of — empty when there is no
# such mapping (caller decides whether that is a GOLDEN_DERIVED entry or an
# error). The install destinations are RENAMES in three cases, which is why this
# is a case table and not a bare identity:
#   templates/agents/X.md            -> ./agents/X.md
#   templates/multi-manager/agents.yaml -> ./sable/agents.yaml
#   templates/multi-manager/roles/X.md  -> ./sable/roles/X.md
# everything under hooks/, bin/ and skills/ installs at its own repo path.
golden_source_for() {
  local p="${1#./}"
  case "$p" in
    sable/agents.yaml)  printf 'templates/multi-manager/agents.yaml\n' ;;
    sable/roles/*)      printf 'templates/multi-manager/roles/%s\n' "${p#sable/roles/}" ;;
    agents/*)           printf 'templates/%s\n' "$p" ;;
    hooks/*|bin/*|skills/*) printf '%s\n' "$p" ;;
  esac
}

# golden_pinned_sources [manifest]: every repo-relative source path the golden
# pins a hash for, one per line. This is the SELECTION side of the contract —
# .github/ci/impact-manifest.sh treats each of these as a path that must select
# GOLDEN_MANIFEST_SUITE, so that changing an installed artifact without
# refreshing its pin cannot pass a proportional run. Unresolvable entries are
# skipped here and reported by golden_check; this function is a lookup, not the
# gate.
golden_pinned_sources() {
  local man="${1:-$GOLDEN_REPO/$GOLDEN_MANIFEST_REL}"
  local line path src
  [ -f "$man" ] || return 0
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    path="${line#*  }"
    golden_is_derived "$path" && continue
    src="$(golden_source_for "$path")"
    [ -n "$src" ] && printf '%s\n' "$src"
  done < "$man"
}

# golden_check [manifest] [repo]: the assertion. Populates GOLDEN_ERRORS (one
# human-readable line per bad entry), GOLDEN_CHECKED (verbatim entries actually
# hashed and compared) and GOLDEN_DERIVED_SEEN (entries excused by
# GOLDEN_DERIVED). Returns non-zero iff GOLDEN_ERRORS is non-empty.
#
# The manifest path is a PARAMETER precisely so a caller can point it at a
# corrupted copy and prove the check bites — an assertion that can only ever be
# run against the known-good file cannot demonstrate it would catch anything.
golden_check() {
  local man="${1:-$GOLDEN_REPO/$GOLDEN_MANIFEST_REL}"
  local repo="${2:-$GOLDEN_REPO}"
  local line hash path src actual d seen
  GOLDEN_ERRORS=(); GOLDEN_CHECKED=0; GOLDEN_DERIVED_SEEN=0

  if [ ! -f "$man" ]; then
    GOLDEN_ERRORS+=("golden manifest is missing: $man (regenerate with GOLDEN_REGEN=1 bash hooks/test/test-install.sh)")
    return 1
  fi

  while IFS= read -r line; do
    [ -n "$line" ] || continue
    hash="${line%%  *}"
    path="${line#*  }"
    if [ "$hash" = "$line" ] || ! printf '%s' "$hash" | grep -qE '^[0-9a-f]{64}$'; then
      GOLDEN_ERRORS+=("unparseable golden line (expected '<sha256>  ./<path>'): $line")
      continue
    fi
    if golden_is_derived "$path"; then
      GOLDEN_DERIVED_SEEN=$((GOLDEN_DERIVED_SEEN+1))
      continue
    fi
    src="$(golden_source_for "$path")"
    if [ -z "$src" ]; then
      GOLDEN_ERRORS+=("$path has no repo source mapping — extend golden_source_for() in hooks/test/lib-golden-manifest.sh, or add it to GOLDEN_DERIVED with the reason install.sh generates rather than copies it")
      continue
    fi
    if [ ! -f "$repo/$src" ]; then
      GOLDEN_ERRORS+=("$path is pinned to $src, which does not exist in the repo — the artifact was moved or deleted without refreshing the golden")
      continue
    fi
    actual="$(sha256sum "$repo/$src" | cut -d' ' -f1)"
    if [ "$actual" != "$hash" ]; then
      GOLDEN_ERRORS+=("$path is STALE: golden pins $hash but sha256($src) is $actual — refresh with GOLDEN_REGEN=1 bash hooks/test/test-install.sh (and land the refresh in the SAME change as the source edit)")
      continue
    fi
    GOLDEN_CHECKED=$((GOLDEN_CHECKED+1))
  done < "$man"

  # 3. The exemption list cannot rot: every GOLDEN_DERIVED entry must still be
  # an entry in the golden. A leftover exemption would keep excusing a path
  # nobody installs any more — and would silently excuse it again if the path
  # ever came back meaning something else.
  for d in "${GOLDEN_DERIVED[@]}"; do
    seen=0
    while IFS= read -r line; do
      [ "${line#*  }" = "$d" ] && { seen=1; break; }
    done < "$man"
    [ "$seen" -eq 1 ] || GOLDEN_ERRORS+=("GOLDEN_DERIVED lists $d but the golden has no such entry — a stale exemption; drop it from GOLDEN_DERIVED in hooks/test/lib-golden-manifest.sh")
  done

  [ "${#GOLDEN_ERRORS[@]}" -eq 0 ]
}

# Direct execution: human-facing report. Sourcing consumers skip this entirely
# (same sourced-vs-executed guard shell-run-set.sh and impact-manifest.sh use).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  case "${1:---check}" in
    --check)
      if golden_check "${2:-}"; then
        echo "lib-golden-manifest --check: golden agrees with $GOLDEN_CHECKED pinned source(s); $GOLDEN_DERIVED_SEEN generated/transformed entr(y/ies) excused"
        exit 0
      fi
      printf '::error::lib-golden-manifest: %s\n' "${GOLDEN_ERRORS[@]}"
      echo "lib-golden-manifest --check: ${#GOLDEN_ERRORS[@]} bad golden entr(y/ies)"
      exit 1
      ;;
    --sources) golden_pinned_sources "${2:-}" ;;
    *) echo "usage: $0 [--check|--sources] [manifest]" >&2; exit 2 ;;
  esac
fi
