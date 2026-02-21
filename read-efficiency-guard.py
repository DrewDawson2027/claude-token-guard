#!/usr/bin/env python3
"""
Read Efficiency Guard — PreToolUse hook that prevents token-wasting read patterns.

Part of the Token Management System (three-layer architecture):
  Layer 1: Prompt hook in settings.json (decision-time prevention for Task)
  Layer 2: Token guard — mechanical enforcement for agent spawning
  Layer 3: This hook — prevents wasteful read patterns with REAL blocking

How it works:
  Claude Code calls this script BEFORE every Read tool invocation.
  Exit 0 = allow the read. Exit 2 = block it with feedback (read NEVER happens).

Checks:
  1. Duplicate file: BLOCK at 3+ reads of the same file path
  2. Sequential reads: WARN at 4, BLOCK at 15 reads within 120s window
  3. Post-Explore duplicates: Advisory warning (non-blocking)

State:  ~/.claude/hooks/session-state/{session_id}-reads.json
Cross-reads: ~/.claude/hooks/session-state/{session_id}.json (from token-guard.py)

Cross-platform: Works on macOS, Linux, and Windows (portable file locking).
"""

import json
import os
import sys
import time
from typing import Dict, List

# Shared infrastructure — locking, state, atomic writes
from hook_utils import lock, unlock, load_json_state, save_json_state

STATE_DIR = os.environ.get("TOKEN_GUARD_STATE_DIR", os.path.expanduser("~/.claude/hooks/session-state"))

SEQUENTIAL_THRESHOLD = 4    # Warn after this many sequential reads
ESCALATION_THRESHOLD = 15   # Block after this many sequential reads (raised: 10 was too aggressive)
DUPLICATE_FILE_LIMIT = 3    # Block same file after this many reads
SEQUENTIAL_WINDOW = 120     # Seconds window for sequential detection (raised: 90s too tight for analysis)
READ_TTL = 300              # Prune read records older than 5 minutes


def default_read_state() -> Dict:
    """Return the default empty state for read tracking."""
    return {"reads": [], "last_sequential_warn": 0}


def main():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except OSError:
        sys.exit(0)  # Can't create state dir — fail-open

    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    session_id = input_data.get("session_id", "unknown")

    if tool_name != "Read":
        sys.exit(0)

    file_path = tool_input.get("file_path", "")
    if not file_path:
        sys.exit(0)

    state_file = os.path.join(STATE_DIR, f"{session_id}-reads.json")
    lock_file = state_file + ".lock"

    try:
        lf = open(lock_file, "w")
    except OSError:
        sys.exit(0)  # Can't create lock file — fail-open
    try:
        lock(lf)
        try:
            state = load_json_state(state_file, default_read_state)
            now = time.time()

            # Prune old reads (older than TTL)
            state["reads"] = [r for r in state["reads"] if now - r["timestamp"] < READ_TTL]

            # CHECK 1: Duplicate file — BLOCK at 3+ total reads of same path
            path_count = sum(1 for r in state["reads"] if r["path"] == file_path) + 1  # +1 for this attempt
            if path_count >= DUPLICATE_FILE_LIMIT:
                state["reads"].append({"path": file_path, "timestamp": now, "blocked": True})
                save_json_state(state_file, state)
                print(
                    f"BLOCKED: '{os.path.basename(file_path)}' read {path_count} times already. "
                    f"Trust your first read. Use Grep for specific lines.",
                    file=sys.stderr
                )
                sys.exit(2)  # REAL block — read never happens

            # CHECK 2: Sequential reads — warn at 4 total, BLOCK at 15 total
            recent = [r for r in state["reads"] if now - r["timestamp"] < SEQUENTIAL_WINDOW]
            recent_count = len(recent) + 1  # +1 for this attempt

            if recent_count >= ESCALATION_THRESHOLD:
                # UNCONDITIONAL block — no time-based suppression for blocks
                # (Time suppression is only for warnings, never for enforcement)
                state["reads"].append({"path": file_path, "timestamp": now, "blocked": True})
                save_json_state(state_file, state)
                print(
                    f"BLOCKED: {recent_count} sequential reads in {SEQUENTIAL_WINDOW}s. "
                    f"Batch into parallel groups of 3-4 per turn.",
                    file=sys.stderr
                )
                sys.exit(2)  # REAL block
            elif recent_count >= SEQUENTIAL_THRESHOLD:
                # Warning uses time suppression to avoid spam (one warning per window)
                last_warn = state.get("last_sequential_warn", 0)
                if now - last_warn > SEQUENTIAL_WINDOW:
                    warn(
                        f"TOKEN EFFICIENCY: {recent_count} sequential reads in {SEQUENTIAL_WINDOW}s. "
                        f"Batch independent reads into parallel groups of 3-4 per turn. "
                        f"Escalation to BLOCK at {ESCALATION_THRESHOLD}. "
                        f"(Parallelism Checkpoint rule)"
                    )
                    state["last_sequential_warn"] = now

            # CHECK 3: Post-Explore duplicate (advisory only — Explore context is useful)
            explore_dirs = get_explore_dirs(session_id)
            if explore_dirs:
                for explore_dir in explore_dirs:
                    if file_path.startswith(explore_dir + "/") or file_path == explore_dir:
                        warn(
                            f"TOKEN EFFICIENCY: Reading '{os.path.basename(file_path)}' which is inside "
                            f"'{explore_dir}' — a directory already mapped by your Explore agent. "
                            f"Trust the Explore output instead of re-reading. "
                            f"(No Duplicate Reads After Explore rule)"
                        )
                        break

            # ALLOWED — record and proceed
            state["reads"].append({"path": file_path, "timestamp": now})
            save_json_state(state_file, state)

        finally:
            unlock(lf)
    finally:
        lf.close()

    sys.exit(0)


def warn(message: str) -> None:
    """Output warning via stderr (advisory, not blocking)."""
    print(message, file=sys.stderr)


def get_explore_dirs(session_id: str) -> List[str]:
    """Read token-guard state to find directories mapped by Explore agents.

    Acquires the token-guard lock to prevent reading a partially-written state file
    during concurrent token-guard writes.
    """
    guard_state_file = os.path.join(STATE_DIR, f"{session_id}.json")
    guard_lock_file = guard_state_file + ".lock"
    try:
        with open(guard_lock_file, "w") as lf:
            lock(lf)
            try:
                with open(guard_state_file, "r") as f:
                    guard_state = json.load(f)
            finally:
                unlock(lf)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

    dirs = []
    for agent in guard_state.get("agents", []):
        if agent.get("type") == "Explore":
            for known_dir in agent.get("target_dirs", []):
                if known_dir not in dirs:
                    dirs.append(known_dir)
    return dirs


if __name__ == "__main__":
    main()
