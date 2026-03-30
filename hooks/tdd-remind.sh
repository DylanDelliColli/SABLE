#!/usr/bin/env bash
# tdd-remind.sh — Inject reminder when editing source files without tests
# Fires on PreToolUse:Edit|Write. Does a fuzzy search for test files
# matching the source file basename. Silent when tests exist.

set -euo pipefail

# Read stdin and parse with python3 (jq not available)
PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
fp = d.get('tool_input', {}).get('file_path', '')
cwd = d.get('cwd', '.')
print(f'{fp}\n{cwd}')
" 2>/dev/null) || exit 0

FILE_PATH=$(echo "$PARSED" | sed -n '1p')
CWD=$(echo "$PARSED" | sed -n '2p')

[ -z "$FILE_PATH" ] && exit 0

# Only act on source files
echo "$FILE_PATH" | grep -qE '\.(ts|tsx|py)$' || exit 0

# Skip test files themselves
echo "$FILE_PATH" | grep -qiE '(test|spec|__tests__|conftest)' && exit 0

# Extract basename without extension (e.g., "VarianceAnalysisTable")
BASENAME=$(basename "$FILE_PATH" | sed 's/\.[^.]*$//')

# Fuzzy search: any file with "test" and the basename in its name
SEARCH_ROOT="${CWD:-.}"
MATCH=$(find "$SEARCH_ROOT" \
  -type f \
  \( -name "*test*${BASENAME}*" -o -name "*${BASENAME}*test*" -o -name "*${BASENAME}*.spec.*" \) \
  -not -path "*/node_modules/*" \
  -not -path "*/.beads/*" \
  -not -path "*/.git/*" \
  -print -quit 2>/dev/null || true)

if [ -z "$MATCH" ]; then
  echo "TDD: No test file found for ${BASENAME}. Write a failing test before implementing changes."
fi

exit 0
