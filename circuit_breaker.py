"""
Circuit breaker for Claude Code hooks.

Usage in any Python hook:
    from circuit_breaker import check_circuit
    if not check_circuit("budget-guard"):
        sys.exit(0)  # skip silently

After 3 consecutive failures within 5 minutes, the hook is bypassed.
Auto-resets after 5 minutes of cool-down.
"""

import json
import os
import sys
import time

STATE_FILE = os.path.expanduser("~/.claude/hooks/session-state/circuit-breaker.json")
MAX_FAILURES = 3
COOLDOWN_SECONDS = 300  # 5 minutes


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except IOError:
        pass


def check_circuit(hook_name):
    """Returns True if the hook should run, False if tripped."""
    state = _load_state()
    entry = state.get(hook_name)
    if not entry:
        return True

    failures = entry.get("failures", 0)
    last_failure = entry.get("last_failure", 0)
    now = time.time()

    # Auto-reset after cooldown
    if now - last_failure > COOLDOWN_SECONDS:
        entry["failures"] = 0
        state[hook_name] = entry
        _save_state(state)
        return True

    # Trip if too many failures
    if failures >= MAX_FAILURES:
        return False

    return True


def record_success(hook_name):
    """Reset failure count on success."""
    state = _load_state()
    if hook_name in state:
        state[hook_name] = {"failures": 0, "last_failure": 0}
        _save_state(state)


def record_failure(hook_name):
    """Increment failure count."""
    state = _load_state()
    entry = state.get(hook_name, {"failures": 0, "last_failure": 0})
    entry["failures"] = entry.get("failures", 0) + 1
    entry["last_failure"] = time.time()
    state[hook_name] = entry
    _save_state(state)
