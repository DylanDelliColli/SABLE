#!/usr/bin/env bash
# Shared persistent JSON encoder for high-volume synthetic hook suites.
#
# Bash variables cannot carry NUL, so NUL-delimited FIFOs preserve every byte
# they can carry while one long-lived Python process retains json.dumps's exact
# escaping.  The suites are serial; a single request/response pair therefore
# needs no request id.  Callers own cleanup and must call
# sable_json_encoder_stop from their EXIT trap.

SABLE_JSON_ENCODER_PID=""
SABLE_JSON_ENCODER_REQUEST_FIFO=""
SABLE_JSON_ENCODER_RESPONSE_FIFO=""
SABLE_JSON_ENCODER_TRACE=""

sable_json_encoder_start() {
  local root="$1"
  if [ -n "$SABLE_JSON_ENCODER_PID" ] \
     && kill -0 "$SABLE_JSON_ENCODER_PID" 2>/dev/null; then
    echo "sable-json-input-encoder: encoder already running (pid=$SABLE_JSON_ENCODER_PID)" >&2
    return 2
  fi
  mkdir -p "$root"
  SABLE_JSON_ENCODER_REQUEST_FIFO="$root/json-input.request"
  SABLE_JSON_ENCODER_RESPONSE_FIFO="$root/json-input.response"
  SABLE_JSON_ENCODER_TRACE="$root/json-input.trace"
  rm -f "$SABLE_JSON_ENCODER_REQUEST_FIFO" "$SABLE_JSON_ENCODER_RESPONSE_FIFO"
  mkfifo "$SABLE_JSON_ENCODER_REQUEST_FIFO" "$SABLE_JSON_ENCODER_RESPONSE_FIFO"
  : > "$SABLE_JSON_ENCODER_TRACE"

  python3 -u - \
    "$SABLE_JSON_ENCODER_REQUEST_FIFO" \
    "$SABLE_JSON_ENCODER_RESPONSE_FIFO" \
    "$SABLE_JSON_ENCODER_TRACE" <<'PYEOF' &
import json
import os
import sys

request_path, response_path, trace_path = sys.argv[1:]


def read_field(stream):
    value = bytearray()
    while True:
        byte = stream.read(1)
        if not byte:
            return None
        if byte == b"\0":
            return os.fsdecode(bytes(value))
        value.extend(byte)


with open(trace_path, "a", encoding="utf-8") as trace:
    trace.write("start\n")
    trace.flush()
    while True:
        with open(request_path, "rb", buffering=0) as request:
            fields = [read_field(request) for _ in range(8)]
        if any(field is None for field in fields):
            continue
        shape, command, cwd, stdout, stderr, agent_id, agent_type, typed = fields
        if shape == "pre":
            out = {"tool_input": {"command": command}, "cwd": cwd}
            if agent_id:
                out["agent_id"] = agent_id
            if typed == "1" and agent_type:
                out["agent_type"] = agent_type
        elif shape == "post":
            if typed == "1":
                out = {"agent_id": agent_id, "agent_type": agent_type}
            else:
                out = {}
            out.update({
                "tool_input": {"command": command},
                "cwd": cwd,
                "tool_response": {"stdout": stdout, "stderr": stderr},
            })
        else:
            raise ValueError(f"unsupported hook payload shape: {shape!r}")
        encoded = json.dumps(out)
        trace.write("request\n")
        trace.flush()
        with open(response_path, "w", encoding="utf-8") as response:
            response.write(encoded + "\n")
PYEOF
  SABLE_JSON_ENCODER_PID=$!
}

sable_json_encoder_encode() {
  # shape command cwd stdout stderr agent_id agent_type typed
  local shape="$1"
  case "$shape" in
    pre|post) ;;
    *) echo "sable-json-input-encoder: unsupported shape: $shape" >&2; return 2 ;;
  esac
  if [ -z "$SABLE_JSON_ENCODER_PID" ] \
     || ! kill -0 "$SABLE_JSON_ENCODER_PID" 2>/dev/null; then
    echo "sable-json-input-encoder: encoder is not running" >&2
    return 2
  fi
  printf '%s\0%s\0%s\0%s\0%s\0%s\0%s\0%s\0' \
    "$shape" "$2" "$3" "$4" "$5" "$6" "$7" "$8" \
    > "$SABLE_JSON_ENCODER_REQUEST_FIFO"
  local encoded
  IFS= read -r encoded < "$SABLE_JSON_ENCODER_RESPONSE_FIFO"
  printf '%s\n' "$encoded"
}

sable_json_encoder_stop() {
  local pid="$SABLE_JSON_ENCODER_PID"
  if [ -n "$pid" ]; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  SABLE_JSON_ENCODER_PID=""
}
