#!/usr/bin/env bash
# spike-agent-context-logger.sh — THROWAWAY instrumentation for SABLE-uz9.1.
# Dumps the full hook input JSON (one compact line per event) to
# /tmp/sable-spike-hooklog.jsonl so the spike can answer the agent_id /
# subagent-discrimination behavior matrix. Silent, fail-open, never blocks.
#
# REMOVE (and deregister from settings.json) when SABLE-uz9.1 closes.
set -uo pipefail

python3 -c "
import json, sys, time
try:
    d = json.load(sys.stdin)
    d['_spike_ts'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    with open('/tmp/sable-spike-hooklog.jsonl', 'a') as f:
        f.write(json.dumps(d) + '\n')
except Exception:
    pass
" 2>/dev/null

exit 0
