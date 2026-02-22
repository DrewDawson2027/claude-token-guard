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

from guard_contracts import build_audit_entry
from guard_events import append_jsonl
from guard_normalize import normalize_file_path, normalize_session_key, normalize_text, short_hash

# Shared infrastructure — locking, state, atomic writes
from hook_utils import lock, unlock, load_json_state, save_json_state

STATE_DIR = os.environ.get("TOKEN_GUARD_STATE_DIR", os.path.expanduser("~/.claude/hooks/session-state"))
AUDIT_LOG = os.path.join(STATE_DIR, "audit.jsonl")

SEQUENTIAL_THRESHOLD = 4    # Warn after this many sequential reads
ESCALATION_THRESHOLD = 15   # Block after this many sequential reads (raised: 10 was too aggressive)
DUPLICATE_FILE_LIMIT = 3    # Block same file after this many reads
SEQUENTIAL_WINDOW = 120     # Seconds window for sequential detection (raised: 90s too tight for analysis)
READ_TTL = 300              # Prune read records older than 5 minutes


def default_read_state() -> Dict:
    """Return the default empty state for read tracking."""
    return {"schema_version": 2, "session_key": "", "reads": [], "last_sequential_warn": 0}


def main():
    # Load config for failure_mode check
    _config_path = os.environ.get("TOKEN_GUARD_CONFIG_PATH", os.path.expanduser("~/.claude/hooks/token-guard-config.json"))
    _failure_mode = "fail_open"
    try:
        with open(_config_path, "r") as _cf:
            _cfg = json.load(_cf)
            if isinstance(_cfg, dict):
                _failure_mode = _cfg.get("failure_mode", "fail_open")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except OSError:
        if _failure_mode == "fail_closed":
            print("BLOCKED: Cannot create state directory (strict mode)", file=sys.stderr)
            sys.exit(2)
        sys.exit(0)  # Can't create state dir — fail-open

    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        _emit_fault("stdin_parse_error", "json_decode")
        sys.exit(0)

    if not isinstance(input_data, dict):
        sys.exit(0)  # Non-dict JSON (null, array, scalar) — fail-open

    tool_name = normalize_text(input_data.get("tool_name", ""), max_len=80)
    tool_input = input_data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {}
    session_id = input_data.get("session_id", "unknown")

    if tool_name != "Read":
        sys.exit(0)

    file_path = tool_input.get("file_path", "")
    if not file_path:
        sys.exit(0)
    normalized_file_path = normalize_file_path(file_path)
    session_key = normalize_session_key(session_id)

    state_file = os.path.join(STATE_DIR, f"{session_key}-reads.json")
    lock_file = state_file + ".lock"

    try:
        lf = open(lock_file, "w")
    except OSError:
        _emit_fault("lock_open_error", "state_lock")
        sys.exit(0)  # Can't create lock file — fail-open
    try:
        lock(lf)
        try:
            state = load_json_state(state_file, default_read_state)
            now = time.time()
            state.setdefault("schema_version", 2)
            state["session_key"] = session_key

            # Prune old reads (older than TTL)
            state["reads"] = [r for r in state["reads"] if now - r["timestamp"] < READ_TTL]

            # CHECK 1: Duplicate file — BLOCK at 3+ total reads of same path
            path_count = sum(
                1 for r in state["reads"]
                if r.get("normalized_path") == normalized_file_path or r.get("path") == file_path
            ) + 1  # +1 for this attempt
            if path_count >= DUPLICATE_FILE_LIMIT:
                state["reads"].append({
                    "path": file_path,
                    "normalized_path": normalized_file_path,
                    "path_hash": short_hash(normalized_file_path, 12),
                    "timestamp": now,
                    "blocked": True,
                })
                save_json_state(state_file, state)
                _audit_block("duplicate_file", file_path, session_id, path_count)
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
                state["reads"].append({
                    "path": file_path,
                    "normalized_path": normalized_file_path,
                    "path_hash": short_hash(normalized_file_path, 12),
                    "timestamp": now,
                    "blocked": True,
                })
                save_json_state(state_file, state)
                _audit_block("sequential_reads", file_path, session_id, recent_count)
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
            explore_dirs = get_explore_dirs(session_key)
            if explore_dirs:
                for explore_dir in explore_dirs:
                    explore_norm = normalize_file_path(explore_dir)
                    if (
                        file_path.startswith(explore_dir + "/")
                        or file_path == explore_dir
                        or (normalized_file_path and explore_norm and (
                            normalized_file_path.startswith(explore_norm + os.sep)
                            or normalized_file_path == explore_norm
                        ))
                    ):
                        warn(
                            f"TOKEN EFFICIENCY: Reading '{os.path.basename(file_path)}' which is inside "
                            f"'{explore_dir}' — a directory already mapped by your Explore agent. "
                            f"Trust the Explore output instead of re-reading. "
                            f"(No Duplicate Reads After Explore rule)"
                        )
                        break

            # ALLOWED — record and proceed
            state["reads"].append({
                "path": file_path,
                "normalized_path": normalized_file_path,
                "path_hash": short_hash(normalized_file_path, 12),
                "timestamp": now,
            })
            save_json_state(state_file, state)

        finally:
            unlock(lf)
    finally:
        lf.close()

    sys.exit(0)


def warn(message: str) -> None:
    """Output warning via stderr (advisory, not blocking)."""
    print(message, file=sys.stderr)


def _audit_block(reason: str, file_path: str, session_id: str, count: int = 0) -> None:
    """Log a read-guard block decision to the shared audit log."""
    try:
        entry = build_audit_entry(
            event_type="block",
            subagent_type="read-guard",
            description=file_path,
            session_id=session_id,
            reason=reason,
            message=f"count={count}",
        )
        append_jsonl(AUDIT_LOG, entry)
    except Exception:
        pass  # Audit logging must never block the hook


def _emit_fault(reason: str, fault_class: str) -> None:
    """Emit a structured fault event to the shared audit log. Non-fatal."""
    try:
        from guard_contracts import build_audit_entry
        entry = build_audit_entry(
            event_type="fault",
            subagent_type="read-guard",
            description="",
            session_id="unknown",
            reason=reason,
            fault_class=fault_class,
        )
        append_jsonl(AUDIT_LOG, entry)
    except Exception:
        pass  # Fault logging must never block the hook


def get_explore_dirs(session_key: str) -> List[str]:
    """Read token-guard state to find directories mapped by Explore agents.

    Acquires the token-guard lock to prevent reading a partially-written state file
    during concurrent token-guard writes. Uses sanitized session_key for file lookup.
    """
    guard_state_file = os.path.join(STATE_DIR, f"{session_key}.json")
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
                    dirs.append(normalize_file_path(known_dir) or known_dir)
    return dirs


if __name__ == "__main__":
    main()
