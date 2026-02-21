#!/bin/bash
# Run the full token management system test suite
#
# Usage: bash tests/run_tests.sh  (from repo root)
#
# Tests:
#   - test_token_guard.py           (51 tests — all 7 rules, config, state, audit, anti-evasion)
#   - test_read_efficiency_guard.py (27 tests — duplicate blocking, escalation, post-Explore)
#   - test_integration.py           ( 6 tests — cross-hook coordination, concurrent access)
#   - test_self_heal.py             (12 tests — all 5 repair phases, audit rotation)

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TESTS_DIR="$REPO_ROOT/tests"

echo "╔═══════════════════════════════════════════════════════════╗"
echo "║       Token Management System — Full Test Suite          ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""

# Python syntax checks first
echo "=== Syntax Checks ==="
python3 -c "import py_compile; py_compile.compile('$REPO_ROOT/token-guard.py', doraise=True)" && echo "  PASS  token-guard.py"
python3 -c "import py_compile; py_compile.compile('$REPO_ROOT/read-efficiency-guard.py', doraise=True)" && echo "  PASS  read-efficiency-guard.py"
python3 -c "import py_compile; py_compile.compile('$REPO_ROOT/self-heal.py', doraise=True)" && echo "  PASS  self-heal.py"
python3 -c "import py_compile; py_compile.compile('$REPO_ROOT/hook_utils.py', doraise=True)" && echo "  PASS  hook_utils.py"
bash -n "$REPO_ROOT/health-check.sh" && echo "  PASS  health-check.sh"
python3 -c "import json; json.load(open('$REPO_ROOT/token-guard-config.json')); print('  PASS  token-guard-config.json')"
echo ""

echo "=== All Python Tests ==="
python3 -m pytest "$TESTS_DIR/" -v --tb=short 2>&1
echo ""

echo "╔═══════════════════════════════════════════════════════════╗"
echo "║                  ALL TESTS PASSED                        ║"
echo "╚═══════════════════════════════════════════════════════════╝"
