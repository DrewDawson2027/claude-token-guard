"""
Tests for read-efficiency-guard.py — the PreToolUse hook that prevents wasteful reads.

Uses subprocess to pipe JSON into the script and check exit codes + stderr.
All tests use isolated temp directories so they never touch real session state.
"""

import json
import os
import subprocess
import time

import pytest

SCRIPT = os.path.expanduser("~/.claude/hooks/read-efficiency-guard.py")


@pytest.fixture
def isolated_env(tmp_path):
    """Create an isolated environment with custom STATE_DIR."""
    state_dir = tmp_path / "session-state"
    state_dir.mkdir()
    env = os.environ.copy()
    env["TOKEN_GUARD_STATE_DIR"] = str(state_dir)
    return env, state_dir


def run_read_guard(input_data, env=None):
    """Run read-efficiency-guard.py with the given input and return (exit_code, stdout, stderr)."""
    result = subprocess.run(
        ["python3", SCRIPT],
        input=json.dumps(input_data),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    return result.returncode, result.stdout, result.stderr


def make_read_input(file_path="/some/file.py", session_id="test-session"):
    """Create a standard Read tool input payload."""
    return {
        "tool_name": "Read",
        "tool_input": {"file_path": file_path},
        "session_id": session_id,
    }


class TestDuplicateFileBlocking:
    """Test that reading the same file 3+ times is blocked."""

    def test_read_duplicate_blocks(self, isolated_env):
        """Same file 3x → 3rd attempt blocked (exit 2)."""
        env, _ = isolated_env
        sid = "dup-test"
        fp = "/some/file.py"

        # 1st read — allowed
        code, _, _ = run_read_guard(make_read_input(fp, sid), env=env)
        assert code == 0

        # 2nd read — allowed
        code, _, _ = run_read_guard(make_read_input(fp, sid), env=env)
        assert code == 0

        # 3rd read — BLOCKED
        code, _, stderr = run_read_guard(make_read_input(fp, sid), env=env)
        assert code == 2
        assert "BLOCKED" in stderr
        assert "file.py" in stderr

    def test_different_files_not_blocked(self, isolated_env):
        """Different files should not trigger duplicate blocking."""
        env, _ = isolated_env
        sid = "diff-files"

        for i in range(5):
            code, _, _ = run_read_guard(make_read_input(f"/file{i}.py", sid), env=env)
            assert code == 0


class TestSequentialReadEscalation:
    """Test sequential read warning and blocking."""

    def test_read_sequential_warns(self, isolated_env):
        """4 reads in 60s → exit 0 + stderr warning."""
        env, _ = isolated_env
        sid = "seq-warn"

        # First 3 reads — no warning
        for i in range(3):
            code, _, stderr = run_read_guard(make_read_input(f"/file{i}.py", sid), env=env)
            assert code == 0

        # 4th read — allowed but warned
        code, _, stderr = run_read_guard(make_read_input("/file3.py", sid), env=env)
        assert code == 0
        assert "TOKEN EFFICIENCY" in stderr
        assert "sequential" in stderr.lower()

    def test_read_sequential_escalation(self, isolated_env):
        """7 reads in 60s → 7th blocked (exit 2)."""
        env, _ = isolated_env
        sid = "seq-esc"

        # First 6 reads — all allowed
        for i in range(6):
            code, _, _ = run_read_guard(make_read_input(f"/file{i}.py", sid), env=env)
            assert code == 0

        # 7th read — BLOCKED
        code, _, stderr = run_read_guard(make_read_input("/file6.py", sid), env=env)
        assert code == 2
        assert "BLOCKED" in stderr
        assert "sequential" in stderr.lower()

    def test_sequential_resets_after_window(self, isolated_env):
        """Sequential count should reset after the 60s window."""
        env, state_dir = isolated_env
        sid = "seq-reset"

        # Manually create state with old reads (>60s ago)
        state_file = state_dir / f"{sid}-reads.json"
        old_time = time.time() - 120  # 2 minutes ago
        state = {
            "reads": [
                {"path": f"/file{i}.py", "timestamp": old_time}
                for i in range(10)
            ],
            "last_sequential_warn": 0,
            "last_escalation": 0,
        }
        state_file.write_text(json.dumps(state))

        # New read should be allowed (old reads are outside window)
        code, _, _ = run_read_guard(make_read_input("/new-file.py", sid), env=env)
        assert code == 0


class TestNonReadCalls:
    """Test that non-Read tool calls pass through."""

    def test_task_tool_passes(self, isolated_env):
        """Task tool calls should be allowed (exit 0, not gated by this hook)."""
        env, _ = isolated_env
        code, _, _ = run_read_guard({
            "tool_name": "Task",
            "tool_input": {"subagent_type": "Explore"},
            "session_id": "non-read-test",
        }, env=env)
        assert code == 0

    def test_empty_file_path_passes(self, isolated_env):
        """Read with empty file_path should pass through."""
        env, _ = isolated_env
        code, _, _ = run_read_guard({
            "tool_name": "Read",
            "tool_input": {"file_path": ""},
            "session_id": "empty-path-test",
        }, env=env)
        assert code == 0


class TestStdinProtection:
    """Test graceful handling of malformed input."""

    def test_empty_stdin(self, isolated_env):
        """Empty stdin should exit 0."""
        env, _ = isolated_env
        result = subprocess.run(
            ["python3", SCRIPT],
            input="",
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0

    def test_malformed_json(self, isolated_env):
        """Invalid JSON should exit 0."""
        env, _ = isolated_env
        result = subprocess.run(
            ["python3", SCRIPT],
            input="not json {{{",
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0
