# Hook Upgrade Notes for Multi-Manager Mode

When the multi-manager pattern is active, two existing SABLE hooks should be upgraded from "warn" to "block" because rolling execution depends on the discipline they enforce.

## bead-description-gate.sh

**Current behavior**: warns (injects `additionalContext`) when a `bd create` lacks file paths or test specs.

**Multi-manager upgrade**: change the final `additionalContext` injection to a `permissionDecision: deny` block. Vague bead descriptions break:
- Pre-dispatch claim writing (no files to claim)
- Overlap detection (no claims to compare against)
- Worker dispatch quality (workers re-explore instead of acting)

The fix: replace the final python3 invocation in `bead-description-gate.sh` with:

```python
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': f'SABLE multi-manager: bead description missing {MISSING}. Rolling execution depends on file paths in descriptions for overlap detection. Add them and retry.'
    }
}))
```

## bead-quality.sh

**Current behavior**: warns when required sections (Steps to Reproduce, Acceptance Criteria) are missing after `bd create`.

**Multi-manager upgrade**: this is a PostToolUse hook so it cannot deny — but it can be paired with a PreToolUse companion that runs the same check on the command before creation. Or, more simply: trust the `bead-description-gate` upgrade above to catch quality issues at creation time.

## When to revert

If you decide to roll back from multi-manager mode to standard SABLE, restore the original warn-only behavior. The standard pattern is more forgiving of imperfect bead descriptions because work is more sequential.

## Verification

After applying upgrades, test with:

```bash
bd create --title="Test bead" --description="fix the thing" --type=task --priority=4
# Expected: blocked with "missing file paths" message
```

```bash
bd create --title="Test bead" --description="modify src/foo.ts to handle null input. Test in tests/test_foo.py" --type=task --priority=4
# Expected: succeeds
```
