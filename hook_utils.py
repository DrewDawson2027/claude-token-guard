"""
Shared infrastructure for Claude Code hooks.

Provides portable file locking, atomic state management, and audit logging
used by both token-guard.py and read-efficiency-guard.py.

This module exists to eliminate DRY violations — bug fixes here propagate
automatically to all hooks that import it.
"""

import json
import os
import sys
import tempfile
from typing import Any, Callable, Dict, IO, List, Optional

# Portable file locking — fcntl on Unix, msvcrt on Windows
if sys.platform == "win32":
    import msvcrt

    def lock(f: IO) -> None:
        """Acquire an exclusive lock on the file."""
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)

    def unlock(f: IO) -> None:
        """Release the lock on the file."""
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def lock(f: IO) -> None:
        """Acquire an exclusive lock on the file."""
        fcntl.flock(f, fcntl.LOCK_EX)

    def unlock(f: IO) -> None:
        """Release the lock on the file."""
        fcntl.flock(f, fcntl.LOCK_UN)


def load_json_state(path: str, default_factory: Optional[Callable[[], Dict]] = None) -> Dict:
    """Load JSON state from file, returning default on any error.

    Args:
        path: Path to the JSON state file.
        default_factory: Callable returning the default state dict.
                         If None, returns an empty dict.
    """
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default_factory() if default_factory else {}


def save_json_state(path: str, state: Dict) -> bool:
    """Atomically persist state — write to temp file, then rename.

    Uses os.replace() which is atomic on both POSIX and Windows.
    If the process crashes mid-write, the original file is untouched.

    Returns True on success, False on failure (non-fatal).
    """
    dir_name = os.path.dirname(path)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, path)
            return True
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return False
    except OSError:
        return False


def locked_append(path: str, line: str) -> bool:
    """Append a line to a file with exclusive file locking.

    Prevents interleaved writes from concurrent hook processes.
    Non-fatal — returns False on any error.
    """
    lock_path = path + ".lock"
    try:
        with open(lock_path, "w") as lf:
            lock(lf)
            try:
                with open(path, "a") as f:
                    f.write(line)
                return True
            finally:
                unlock(lf)
    except OSError:
        return False


def read_jsonl_fault_tolerant(path: str) -> List[Dict]:
    """Read a JSONL file, skipping corrupt lines instead of failing.

    Returns a list of successfully parsed entries.
    One bad line does NOT discard all valid entries.
    """
    entries = []
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except (FileNotFoundError, OSError):
        pass
    return entries


# Single source of truth for default config — used by token-guard.py and self-heal.py.
# Both import from here to prevent config drift.
DEFAULT_CONFIG = {
    "max_agents": 5,
    "parallel_window_seconds": 30,
    "global_cooldown_seconds": 5,
    "max_per_subagent_type": 1,
    "state_ttl_hours": 24,
    "audit_log": True,
    "one_per_session": [
        "Explore",
        "master-coder",
        "master-researcher",
        "master-architect",
        "master-workflow",
        "Plan",
    ],
    "always_allowed": [
        "claude-code-guide",
        "statusline-setup",
        "haiku",
    ],
}
