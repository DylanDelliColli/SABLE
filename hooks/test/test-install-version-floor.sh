#!/usr/bin/env bash
# test-install-version-floor.sh — CC >= 2.1.172 version-floor warning in install.sh
# (SABLE-ppy). Below floor warns; at/above is silent; an unparseable/absent version
# does not crash. Runs install.sh --dry-run against a scratch HOME with a fake
# `claude` shim prepended to PATH (real bd/dolt/python stay reachable).
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
fails=0
ok(){ printf '  ok  %s\n' "$1"; }
no(){ printf '  FAIL %s — %s\n' "$1" "${2:-}"; fails=$((fails+1)); }

run_with_claude() { # $1 = version string (empty => unparseable shim)
  local d h; d="$(mktemp -d)"; h="$(mktemp -d)"
  if [ -n "$1" ]; then
    printf '#!/bin/sh\necho "claude %s (build)"\n' "$1" > "$d/claude"
  else
    printf '#!/bin/sh\necho "no parseable version"\n' > "$d/claude"
  fi
  chmod +x "$d/claude"
  HOME="$h" PATH="$d:$PATH" bash "$REPO/install.sh" --dry-run 2>&1
  rm -rf "$d" "$h"
}
warns(){ printf '%s' "$1" | grep -q 'below 2.1.172'; }

if warns "$(run_with_claude 2.1.150)"; then ok "below floor (2.1.150) warns"; else no "below floor warns" "no warning"; fi
if warns "$(run_with_claude 2.1.172)"; then no "at floor (2.1.172) silent" "warned"; else ok "at floor silent"; fi
if warns "$(run_with_claude 2.5.0)"; then no "above floor (2.5.0) silent" "warned"; else ok "above floor silent"; fi
if warns "$(run_with_claude '')"; then no "unparseable version: no warn" "warned"; else ok "unparseable version: no warn, no crash"; fi

if [ "$fails" -eq 0 ]; then printf 'PASS test-install-version-floor\n'; else printf 'FAIL test-install-version-floor (%d)\n' "$fails"; exit 1; fi
