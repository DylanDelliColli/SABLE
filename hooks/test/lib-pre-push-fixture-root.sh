#!/usr/bin/env bash
# Shared allocation seam for the pre-push hook suite and its concurrency plant.

sable_pre_push_fixture_root() {
  mktemp -d "${TMPDIR:-/tmp}/sable-test-pre-push.XXXXXX"
}
