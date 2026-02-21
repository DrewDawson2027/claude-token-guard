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
  2. Sequential reads: WARN at 4, BLOCK at 7 reads within 60s window
  3. Post-Explore duplicates: Advisory warning (non-blocking)

State:  ~/.claude/hooks/session-state/{session_id}-reads.json
Cross-reads: ~/.claude/hooks/session-state/{session_id}.json (from token-guard.py)

Cross-platform: Works on macOS, Linux, and Windows (portable file locking).
"""

import json
import os
import sys
import tempfile
import time

STATE_DIR = os.environ.get("TOKEN_GUARD_STATE_DIR", os.path.expanduser("~/.claude/hooks/session-state"))

# Portable file locking — fcntl on Unix, msvcrt on Windows
if sys.platform == "win32":
    import msvcrt

    def _lock(f):
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(f):
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(f):
        fcntl.flock(f, fcntl.LOCK_EX)

    def _unlock(f):
        fcntl.flock(f, fcntl.LOCK_UN)


SEQUENTIAL_THRESHOLD = 4   # Warn after this many sequential reads
ESCALATION_THRESHOLD = 7   # Block after this many sequential reads
DUPLICATE_FILE_LIMIT = 3   # Block same file after this many reads
SEQUENTIAL_WINDOW = 60     # Seconds window for sequential detection
READ_TTL = 300             # Prune read records older than 5 minutes


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

    with open(lock_file, "w") as lf:
        _lock(lf)
        try:
            state = load_state(state_file)
            now = time.time()

            # Prune old reads (older than TTL)
            state["reads"] = [r for r in state["reads"] if now - r["timestamp"] < READ_TTL]

            # CHECK 1: Duplicate file — BLOCK at 3+ total reads of same path
            path_count = sum(1 for r in state["reads"] if r["path"] == file_path) + 1  # +1 for this attempt
            if path_count >= DUPLICATE_FILE_LIMIT:
                state["reads"].append({"path": file_path, "timestamp": now, "blocked": True})
                save_state(state_file, state)
                print(
                    f"BLOCKED: '{os.path.basename(file_path)}' read {path_count} times already. "
                    f"Trust your first read. Use Grep for specific lines.",
                    file=sys.stderr
                )
                sys.exit(2)  # REAL block — read never happens

            # CHECK 2: Sequential reads — warn at 4 total, BLOCK at 7 total
            recent = [r for r in state["reads"] if now - r["timestamp"] < SEQUENTIAL_WINDOW]
            recent_count = len(recent) + 1  # +1 for this attempt
            if recent_count >= ESCALATION_THRESHOLD:
                last_esc = state.get("last_escalation", 0)
                if now - last_esc > SEQUENTIAL_WINDOW:
                    state["last_escalation"] = now
                    state["reads"].append({"path": file_path, "timestamp": now, "blocked": True})
                    save_state(state_file, state)
                    print(
                        f"BLOCKED: {recent_count} sequential reads in {SEQUENTIAL_WINDOW}s. "
                        f"Batch into parallel groups of 3-4 per turn.",
                        file=sys.stderr
                    )
                    sys.exit(2)  # REAL block
            elif recent_count >= SEQUENTIAL_THRESHOLD:
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
            save_state(state_file, state)

        finally:
            _unlock(lf)

    sys.exit(0)


def warn(message):
    """Output warning via stderr (advisory, not blocking)."""
    print(message, file=sys.stderr)


def get_explore_dirs(session_id):
    """Read token-guard state to find directories mapped by Explore agents.

    Acquires the token-guard lock to prevent reading a partially-written state file
    during concurrent token-guard writes.
    """
    guard_state_file = os.path.join(STATE_DIR, f"{session_id}.json")
    guard_lock_file = guard_state_file + ".lock"
    try:
        with open(guard_lock_file, "w") as lf:
            _lock(lf)
            try:
                with open(guard_state_file, "r") as f:
                    guard_state = json.load(f)
            finally:
                _unlock(lf)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

    dirs = []
    for agent in guard_state.get("agents", []):
        if agent.get("type") == "Explore":
            for known_dir in agent.get("target_dirs", []):
                if known_dir not in dirs:
                    dirs.append(known_dir)
    return dirs


def load_state(path):
    """Load per-session read state, returning empty state on any error."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"reads": [], "last_sequential_warn": 0, "last_escalation": 0}


def save_state(path, state):
    """Atomically persist state — write to temp, then rename.

    Uses os.replace() which is atomic on both POSIX and Windows.
    If the process crashes mid-write, the original file is untouched.
    """
    dir_name = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        # Don't re-raise — state write failed but enforcement still works for this invocation


if __name__ == "__main__":
    main()
