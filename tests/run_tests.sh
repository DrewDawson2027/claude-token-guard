#!/bin/bash
# Run the full token management system test suite
#
# Usage: bash ~/.claude/hooks/tests/run_tests.sh
#
# Tests:
#   - test_token_guard.py        (19 tests — all enforcement rules, config, state, audit)
#   - test_read_efficiency_guard.py (10 tests — sequential, duplicate, suppression, state)
#   - test_health_check.sh       (4 tests — default, --stats, --cleanup, edge cases)

set -e

TESTS_DIR="$HOME/.claude/hooks/tests"

echo "╔═══════════════════════════════════════════════════════════╗"
echo "║       Token Management System — Full Test Suite          ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""

# Python syntax checks first
echo "=== Syntax Checks ==="
python3 -c "import py_compile; py_compile.compile('$HOME/.claude/hooks/token-guard.py', doraise=True)" && echo "  PASS  token-guard.py"
python3 -c "import py_compile; py_compile.compile('$HOME/.claude/hooks/read-efficiency-guard.py', doraise=True)" && echo "  PASS  read-efficiency-guard.py"
bash -n "$HOME/.claude/hooks/health-check.sh" && echo "  PASS  health-check.sh"
python3 -c "import json; json.load(open('$HOME/.claude/hooks/token-guard-config.json')); print('  PASS  token-guard-config.json')"
echo ""

echo "=== Token Guard Tests ==="
python3 -m pytest "$TESTS_DIR/test_token_guard.py" -v --tb=short 2>&1
echo ""

echo "=== Read Efficiency Guard Tests ==="
python3 -m pytest "$TESTS_DIR/test_read_efficiency_guard.py" -v --tb=short 2>&1
echo ""

echo "=== Health Check Tests ==="
bash "$TESTS_DIR/test_health_check.sh"
echo ""

echo "╔═══════════════════════════════════════════════════════════╗"
echo "║                  ALL TESTS PASSED                        ║"
echo "╚═══════════════════════════════════════════════════════════╝"
