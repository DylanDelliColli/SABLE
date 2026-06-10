#!/usr/bin/env bash
# test-sable-status.sh — shell entry point for the Python dashboard tests, so
# they run under the same `bash hooks/test/test-*.sh` convention as the rest of
# the suite (and register tdd-evidence, which currently misses bare
# `python3 .../test_*.py` invocations — see SABLE-lb9).
set -uo pipefail
DIR="$(cd "$(dirname "$0")/../.." && pwd)"
exec python3 "$DIR/bin/test_sable_status.py"
