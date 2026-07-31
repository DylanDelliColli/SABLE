#!/usr/bin/env bash
# lib-push-attestation.sh — bind a completed agent gate to git's pre-push hook.

sable_push_attestation_path() {
  git -C "$1" rev-parse --git-path sable/push-gate.attestation 2>/dev/null
}

sable_write_push_attestation() {
  local repo="$1" verdict="$2" path dir tmp sha branch now
  path="$(sable_push_attestation_path "$repo")" || return 1
  case "$path" in /*) ;; *) path="$repo/$path" ;; esac
  dir="$(dirname "$path")"
  mkdir -p "$dir" || return 1
  sha="$(git -C "$repo" rev-parse HEAD 2>/dev/null)" || return 1
  branch="$(git -C "$repo" symbolic-ref --quiet --short HEAD 2>/dev/null)" || return 1
  now="$(date +%s)" || return 1
  verdict="$(printf '%s' "$verdict" | tr '\r\n' '  ')"
  tmp="$path.tmp.$$"
  umask 077
  if ! printf '%s\n%s\n%s\n%s\n' "$sha" "$branch" "$now" "$verdict" > "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  mv -f "$tmp" "$path"
}

sable_consume_push_attestation() {
  local repo="$1" path sha branch written verdict now max_age age
  path="$(sable_push_attestation_path "$repo")" || return 1
  case "$path" in /*) ;; *) path="$repo/$path" ;; esac
  [ -f "$path" ] || return 1
  {
    IFS= read -r sha
    IFS= read -r branch
    IFS= read -r written
    IFS= read -r verdict
  } < "$path" || { rm -f "$path"; return 1; }
  rm -f "$path"
  [ "$sha" = "$(git -C "$repo" rev-parse HEAD 2>/dev/null)" ] || return 1
  [ "$branch" = "$(git -C "$repo" symbolic-ref --quiet --short HEAD 2>/dev/null)" ] || return 1
  case "$written" in *[!0-9]*|'') return 1 ;; esac
  now="$(date +%s)" || return 1
  max_age="${SABLE_PUSH_ATTESTATION_MAX_AGE:-60}"
  case "$max_age" in *[!0-9]*|'') max_age=60 ;; esac
  age=$((now - written))
  [ "$age" -ge 0 ] && [ "$age" -le "$max_age" ] || return 1
  [ -n "$verdict" ] || return 1
  printf '%s\n' "$verdict"
}
