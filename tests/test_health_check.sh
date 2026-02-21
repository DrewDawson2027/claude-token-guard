#!/bin/bash
# Tests for health-check.sh
# Run: bash ~/.claude/hooks/tests/test_health_check.sh

SCRIPT="$HOME/.claude/hooks/health-check.sh"
PASS=0
FAIL=0

assert_exit() {
  local name="$1" expected="$2" actual="$3"
  if [ "$actual" -eq "$expected" ]; then
    echo "  PASS  $name"
    PASS=$((PASS + 1))
  else
    echo "  FAIL  $name (expected exit $expected, got $actual)"
    FAIL=$((FAIL + 1))
  fi
}

assert_contains() {
  local name="$1" output="$2" pattern="$3"
  if echo "$output" | grep -q "$pattern"; then
    echo "  PASS  $name"
    PASS=$((PASS + 1))
  else
    echo "  FAIL  $name (output does not contain '$pattern')"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== Health Check Tests ==="
echo ""

# Test 1: Default mode exits 0 and shows HEALTHY
echo "Test: default mode"
OUTPUT=$(bash "$SCRIPT" 2>&1)
EXIT=$?
assert_exit "default exits 0" 0 "$EXIT"
assert_contains "shows HEALTHY" "$OUTPUT" "HEALTHY"
assert_contains "shows results line" "$OUTPUT" "Results:"

# Test 2: --stats flag works
echo ""
echo "Test: --stats flag"
OUTPUT=$(bash "$SCRIPT" --stats 2>&1)
EXIT=$?
assert_exit "--stats exits 0" 0 "$EXIT"
assert_contains "--stats shows header" "$OUTPUT" "Token Guard Audit Stats"

# Test 3: --cleanup flag works
echo ""
echo "Test: --cleanup flag"
# Create a fake stale file
STALE="$HOME/.claude/hooks/session-state/test-health-stale.json"
echo '{}' > "$STALE"
# Set mtime to 2 days ago (macOS syntax)
touch -t "$(date -v-2d +%Y%m%d%H%M)" "$STALE" 2>/dev/null || touch -d "2 days ago" "$STALE" 2>/dev/null
OUTPUT=$(bash "$SCRIPT" --cleanup 2>&1)
EXIT=$?
assert_exit "--cleanup exits 0" 0 "$EXIT"
assert_contains "--cleanup shows cleaned" "$OUTPUT" "Cleaned"
if [ -f "$STALE" ]; then
  echo "  FAIL  stale file should have been deleted"
  FAIL=$((FAIL + 1))
else
  echo "  PASS  stale file was deleted"
  PASS=$((PASS + 1))
fi

# Test 4: --stats handles missing audit log
echo ""
echo "Test: --stats with missing audit"
AUDIT="$HOME/.claude/hooks/session-state/audit.jsonl"
if [ -f "$AUDIT" ]; then
  mv "$AUDIT" "${AUDIT}.bak"
  OUTPUT=$(bash "$SCRIPT" --stats 2>&1)
  EXIT=$?
  mv "${AUDIT}.bak" "$AUDIT"
else
  OUTPUT=$(bash "$SCRIPT" --stats 2>&1)
  EXIT=$?
fi
assert_exit "--stats with no audit exits 0" 0 "$EXIT"

echo ""
echo "─────────────────────────────"
echo "  Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  echo "  STATUS: SOME TESTS FAILED"
  exit 1
else
  echo "  STATUS: ALL TESTS PASSED"
  exit 0
fi
