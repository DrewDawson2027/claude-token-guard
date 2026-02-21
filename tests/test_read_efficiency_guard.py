"""
Tests for read-efficiency-guard.py — the PostToolUse hook that warns about wasteful reads.

Uses subprocess to pipe JSON into the script and check stderr for warnings.
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


def run_guard(input_data, env=None):
    """Run read-efficiency-guard.py and return (exit_code, stdout, stderr)."""
    result = subprocess.run(
        ["python3", SCRIPT],
        input=json.dumps(input_data),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    return result.returncode, result.stdout, result.stderr


def make_read_input(file_path="/some/file.py", session_id="test-read"):
    """Create a standard Read tool input payload."""
    return {
        "tool_name": "Read",
        "tool_input": {"file_path": file_path},
        "session_id": session_id,
    }


def create_explore_state(state_dir, session_id, target_dirs):
    """Create a token-guard state file with Explore agent target dirs."""
    state_file = os.path.join(str(state_dir), f"{session_id}.json")
    state = {
        "agent_count": 1,
        "agents": [{
            "type": "Explore",
            "description": "test explore",
            "timestamp": time.time(),
            "target_dirs": target_dirs,
        }],
    }
    with open(state_file, "w") as f:
        json.dump(state, f)


class TestSequentialReads:
    """Test sequential read detection."""

    def test_no_warn_under_threshold(self, isolated_env):
        """3 reads in 60s should NOT trigger a warning (threshold is 4)."""
        env, _ = isolated_env
        sid = "seq-under"
        for i in range(3):
            code, _, stderr = run_guard(make_read_input(f"/file{i}.py", sid), env=env)
            assert code == 0
        assert "TOKEN EFFICIENCY" not in stderr

    def test_warn_at_threshold(self, isolated_env):
        """4th read in 60s should trigger the sequential warning."""
        env, _ = isolated_env
        sid = "seq-at"
        for i in range(3):
            run_guard(make_read_input(f"/file{i}.py", sid), env=env)
        _, _, stderr = run_guard(make_read_input("/file3.py", sid), env=env)
        assert "TOKEN EFFICIENCY" in stderr
        assert "sequential reads" in stderr
        assert "Parallelism Checkpoint" in stderr

    def test_warn_suppression(self, isolated_env):
        """5th read within same window should NOT repeat the warning."""
        env, _ = isolated_env
        sid = "seq-suppress"
        for i in range(4):
            run_guard(make_read_input(f"/file{i}.py", sid), env=env)
        # 5th read — warning should be suppressed
        _, _, stderr = run_guard(make_read_input("/file4.py", sid), env=env)
        assert "TOKEN EFFICIENCY" not in stderr


class TestPostExploreDuplicates:
    """Test post-Explore duplicate detection."""

    def test_warn_reading_explored_dir(self, isolated_env):
        """Reading a file in an Explore'd directory should trigger a warning."""
        env, state_dir = isolated_env
        sid = "explore-dup"
        home = os.path.expanduser("~")
        explore_dir = f"{home}/Projects/my-app"
        create_explore_state(state_dir, sid, [explore_dir])

        _, _, stderr = run_guard(make_read_input(f"{explore_dir}/src/main.py", sid), env=env)
        assert "TOKEN EFFICIENCY" in stderr
        assert "already mapped by your Explore agent" in stderr

    def test_path_boundary_no_false_positive(self, isolated_env):
        """Similar-prefix paths should NOT trigger false positive warnings."""
        env, state_dir = isolated_env
        sid = "explore-boundary"
        home = os.path.expanduser("~")
        explore_dir = f"{home}/Projects"
        create_explore_state(state_dir, sid, [explore_dir])

        # /Projects-backup should NOT match /Projects
        _, _, stderr = run_guard(make_read_input(f"{home}/Projects-backup/file.py", sid), env=env)
        assert "already mapped by your Explore agent" not in stderr

    def test_no_warn_different_dir(self, isolated_env):
        """Reading a file outside the Explore'd directory should be fine."""
        env, state_dir = isolated_env
        sid = "explore-diff"
        home = os.path.expanduser("~")
        create_explore_state(state_dir, sid, [f"{home}/Projects/app-a"])

        _, _, stderr = run_guard(make_read_input(f"{home}/Documents/notes.txt", sid), env=env)
        assert "already mapped by your Explore agent" not in stderr


class TestStateManagement:
    """Test state persistence and pruning."""

    def test_state_prune(self, isolated_env):
        """Reads older than 5 minutes should be pruned from state."""
        env, state_dir = isolated_env
        sid = "state-prune"
        state_file = state_dir / f"{sid}-reads.json"

        # Create state with an old read record
        old_state = {
            "reads": [{"path": "/old/file.py", "timestamp": time.time() - 400}],
            "last_sequential_warn": 0,
        }
        state_file.write_text(json.dumps(old_state))

        # Trigger a new read to cause pruning
        run_guard(make_read_input("/new/file.py", sid), env=env)

        state = json.loads(state_file.read_text())
        paths = [r["path"] for r in state["reads"]]
        assert "/old/file.py" not in paths
        assert "/new/file.py" in paths

    def test_state_file_valid_json(self, isolated_env):
        """State file should always be valid JSON after writes."""
        env, state_dir = isolated_env
        sid = "state-valid"
        for i in range(5):
            run_guard(make_read_input(f"/file{i}.py", sid), env=env)
        state_file = state_dir / f"{sid}-reads.json"
        state = json.loads(state_file.read_text())
        assert "reads" in state


class TestNonReadCalls:
    """Test that non-Read tool calls pass through."""

    def test_bash_tool_ignored(self, isolated_env):
        """Bash tool calls should be silently ignored."""
        env, _ = isolated_env
        code, _, stderr = run_guard({
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
            "session_id": "non-read-test",
        }, env=env)
        assert code == 0
        assert stderr == ""

    def test_empty_file_path_ignored(self, isolated_env):
        """Read with empty file_path should be silently ignored."""
        env, _ = isolated_env
        code, _, stderr = run_guard({
            "tool_name": "Read",
            "tool_input": {"file_path": ""},
            "session_id": "empty-path-test",
        }, env=env)
        assert code == 0
        assert stderr == ""


class TestBlockBehavior:
    """Verify the hook blocks at escalation threshold (7+ reads in 60s)."""

    def test_allows_under_escalation(self, isolated_env):
        """The read guard should allow reads below the escalation threshold."""
        env, _ = isolated_env
        sid = "under-esc"
        for i in range(6):
            code, _, _ = run_guard(make_read_input(f"/file{i}.py", sid), env=env)
            assert code == 0, f"Read guard should allow under threshold (iteration {i})"

    def test_blocks_at_escalation(self, isolated_env):
        """The read guard should block at escalation threshold (7+ reads in 60s)."""
        env, _ = isolated_env
        sid = "at-esc"
        for i in range(6):
            code, _, _ = run_guard(make_read_input(f"/file{i}.py", sid), env=env)
            assert code == 0
        # 7th read should be blocked
        code, _, stderr = run_guard(make_read_input("/file6.py", sid), env=env)
        assert code == 2, "7th sequential read should be blocked"
        assert "BLOCKED" in stderr


class TestDuplicateFileBlocking:
    """Test duplicate file read blocking (BLOCK at 3+ reads of same path)."""

    def test_allows_two_reads_of_same_file(self, isolated_env):
        """2 reads of the same file should be allowed."""
        env, _ = isolated_env
        sid = "dup-allow"
        for _ in range(2):
            code, _, _ = run_guard(make_read_input("/foo.py", sid), env=env)
            assert code == 0

    def test_blocks_third_read_of_same_file(self, isolated_env):
        """3rd read of the same file should be blocked (exit 2)."""
        env, _ = isolated_env
        sid = "dup-block"
        for _ in range(2):
            code, _, _ = run_guard(make_read_input("/foo.py", sid), env=env)
            assert code == 0
        # 3rd read — should be blocked
        code, _, stderr = run_guard(make_read_input("/foo.py", sid), env=env)
        assert code == 2, "3rd read of same file should be blocked"
        assert "BLOCKED" in stderr
        assert "foo.py" in stderr

    def test_duplicate_different_files_ok(self, isolated_env):
        """3 reads of 3 different files should all be allowed."""
        env, _ = isolated_env
        sid = "dup-diff"
        for i in range(3):
            code, _, _ = run_guard(make_read_input(f"/different{i}.py", sid), env=env)
            assert code == 0, f"Different files should not trigger duplicate blocking (file {i})"


class TestEscalationState:
    """Test escalation state field tracking."""

    def test_escalation_state_recorded(self, isolated_env):
        """After 7+ reads, state should have last_escalation > 0."""
        env, state_dir = isolated_env
        sid = "esc-state"
        for i in range(7):
            run_guard(make_read_input(f"/file{i}.py", sid), env=env)
        state_file = state_dir / f"{sid}-reads.json"
        state = json.loads(state_file.read_text())
        assert state.get("last_escalation", 0) > 0, "last_escalation should be set after blocking"

    def test_blocked_reads_flagged_in_state(self, isolated_env):
        """Blocked read should have 'blocked': true in state."""
        env, state_dir = isolated_env
        sid = "esc-flagged"
        # Trigger duplicate file block (3 reads of same file)
        for _ in range(3):
            run_guard(make_read_input("/same.py", sid), env=env)
        state_file = state_dir / f"{sid}-reads.json"
        state = json.loads(state_file.read_text())
        blocked_reads = [r for r in state["reads"] if r.get("blocked")]
        assert len(blocked_reads) >= 1, "Blocked reads should be flagged in state"
