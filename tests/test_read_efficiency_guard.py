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

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "read-efficiency-guard.py")


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


class TestDuplicateFileBlocking:
    """Test that reading the same file 3+ times is blocked."""

    def test_read_duplicate_blocks(self, isolated_env):
        """Same file 3x -> 3rd attempt blocked (exit 2)."""
        env, _ = isolated_env
        sid = "dup-test"
        fp = "/some/file.py"

        # 1st read — allowed
        code, _, _ = run_guard(make_read_input(fp, sid), env=env)
        assert code == 0

        # 2nd read — allowed
        code, _, _ = run_guard(make_read_input(fp, sid), env=env)
        assert code == 0

        # 3rd read — BLOCKED
        code, _, stderr = run_guard(make_read_input(fp, sid), env=env)
        assert code == 2
        assert "BLOCKED" in stderr
        assert "file.py" in stderr

    def test_different_files_not_blocked(self, isolated_env):
        """Different files should not trigger duplicate blocking."""
        env, _ = isolated_env
        sid = "diff-files"

        for i in range(5):
            code, _, _ = run_guard(make_read_input(f"/file{i}.py", sid), env=env)
            assert code == 0


class TestSequentialReads:
    """Test sequential read detection (warn at 4, block at 10)."""

    def test_no_warn_under_threshold(self, isolated_env):
        """3 reads in 90s should NOT trigger a warning (threshold is 4)."""
        env, _ = isolated_env
        sid = "seq-under"
        for i in range(3):
            code, _, stderr = run_guard(make_read_input(f"/file{i}.py", sid), env=env)
            assert code == 0
        assert "TOKEN EFFICIENCY" not in stderr

    def test_warn_at_threshold(self, isolated_env):
        """4th read in 90s should trigger the sequential warning."""
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


class TestSequentialReadEscalation:
    """Test sequential read escalation (warn at 4, block at 10)."""

    def test_read_sequential_warns(self, isolated_env):
        """4 reads in 90s -> exit 0 + stderr warning."""
        env, _ = isolated_env
        sid = "seq-warn"

        # First 3 reads — no warning
        for i in range(3):
            code, _, stderr = run_guard(make_read_input(f"/file{i}.py", sid), env=env)
            assert code == 0

        # 4th read — allowed but warned
        code, _, stderr = run_guard(make_read_input("/file3.py", sid), env=env)
        assert code == 0
        assert "TOKEN EFFICIENCY" in stderr
        assert "sequential" in stderr.lower()

    def test_read_sequential_escalation(self, isolated_env):
        """10 reads in 90s -> 10th blocked (exit 2)."""
        env, _ = isolated_env
        sid = "seq-esc"

        # First 9 reads — all allowed
        for i in range(9):
            code, _, _ = run_guard(make_read_input(f"/file{i}.py", sid), env=env)
            assert code == 0

        # 10th read — BLOCKED
        code, _, stderr = run_guard(make_read_input("/file9.py", sid), env=env)
        assert code == 2
        assert "BLOCKED" in stderr
        assert "sequential" in stderr.lower()

    def test_sequential_resets_after_window(self, isolated_env):
        """Sequential count should reset after the 90s window."""
        env, state_dir = isolated_env
        sid = "seq-reset"

        # Manually create state with old reads (>90s ago)
        state_file = state_dir / f"{sid}-reads.json"
        old_time = time.time() - 120  # 2 minutes ago
        state = {
            "reads": [
                {"path": f"/file{i}.py", "timestamp": old_time}
                for i in range(15)
            ],
            "last_sequential_warn": 0,
        }
        state_file.write_text(json.dumps(state))

        # New read should be allowed (old reads are outside window)
        code, _, _ = run_guard(make_read_input("/new-file.py", sid), env=env)
        assert code == 0


class TestEscalationNoFreePass:
    """Regression test: escalation blocks must be unconditional.

    Previously, after blocking at the escalation threshold, a 60s 'free pass'
    was granted due to time-based suppression. Blocks must NEVER be suppressed.
    """

    def test_consecutive_blocks_at_threshold(self, isolated_env):
        """After a block, the next read at threshold should ALSO be blocked."""
        env, state_dir = isolated_env
        sid = "no-free-pass"

        # Create state with 9 recent reads (just under the threshold of 10)
        state_file = state_dir / f"{sid}-reads.json"
        now = time.time()
        state = {
            "reads": [
                {"path": f"/file{i}.py", "timestamp": now - 5}
                for i in range(9)
            ],
            "last_sequential_warn": now - 5,
        }
        state_file.write_text(json.dumps(state))

        # 10th read — should be blocked
        code, _, stderr = run_guard(make_read_input("/file9.py", sid), env=env)
        assert code == 2
        assert "BLOCKED" in stderr

        # 11th read — should ALSO be blocked (no free pass)
        code, _, stderr = run_guard(make_read_input("/file10.py", sid), env=env)
        assert code == 2
        assert "BLOCKED" in stderr


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

    def test_task_tool_passes(self, isolated_env):
        """Task tool calls should be allowed (exit 0, not gated by this hook)."""
        env, _ = isolated_env
        code, _, _ = run_guard({
            "tool_name": "Task",
            "tool_input": {"subagent_type": "Explore"},
            "session_id": "non-read-test",
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


class TestBlockBehavior:
    """Verify the hook blocks at escalation threshold (10+ reads in 90s)."""

    def test_allows_under_escalation(self, isolated_env):
        """The read guard should allow reads below the escalation threshold."""
        env, _ = isolated_env
        sid = "under-esc"
        for i in range(9):
            code, _, _ = run_guard(make_read_input(f"/file{i}.py", sid), env=env)
            assert code == 0, f"Read guard should allow under threshold (iteration {i})"

    def test_blocks_at_escalation(self, isolated_env):
        """The read guard should block at escalation threshold (10+ reads in 90s)."""
        env, _ = isolated_env
        sid = "at-esc"
        for i in range(9):
            code, _, _ = run_guard(make_read_input(f"/file{i}.py", sid), env=env)
            assert code == 0
        # 10th read should be blocked
        code, _, stderr = run_guard(make_read_input("/file9.py", sid), env=env)
        assert code == 2, "10th sequential read should be blocked"
        assert "BLOCKED" in stderr


class TestErrorResilience:
    """Test fail-open behavior when state dir is degraded."""

    def test_readonly_state_dir_fails_open(self, tmp_path):
        """Read-only state dir should cause exit 0 (fail-open), never exit 1."""
        state_dir = tmp_path / "readonly-state"
        state_dir.mkdir()
        env = os.environ.copy()
        env["TOKEN_GUARD_STATE_DIR"] = str(state_dir)

        # Make state dir read-only AFTER creating it
        os.chmod(str(state_dir), 0o555)
        try:
            code, _, _ = run_guard(make_read_input("/test/file.py", "readonly-test"), env=env)
            # Must be 0 (fail-open), never 1 (crash)
            assert code == 0, f"Expected fail-open (exit 0), got exit {code}"
        finally:
            os.chmod(str(state_dir), 0o755)
