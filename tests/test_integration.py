"""
Integration tests — verify cross-hook state coordination.

Tests that token-guard.py and read-efficiency-guard.py work together correctly:
- token-guard records Explore target directories in session state
- read-efficiency-guard reads that state to detect post-Explore duplicate reads
- Shared hook_utils.py functions work correctly across both hooks
"""

import json
import os
import subprocess
import time

import pytest

TOKEN_GUARD = os.path.expanduser("~/.claude/hooks/token-guard.py")
READ_GUARD = os.path.expanduser("~/.claude/hooks/read-efficiency-guard.py")


@pytest.fixture
def integrated_env(tmp_path):
    """Create an isolated environment shared by both hooks."""
    state_dir = tmp_path / "session-state"
    state_dir.mkdir()
    config_path = tmp_path / "token-guard-config.json"
    config_path.write_text(json.dumps({
        "max_agents": 5,
        "parallel_window_seconds": 30,
        "global_cooldown_seconds": 0,
        "max_per_subagent_type": 1,
        "state_ttl_hours": 24,
        "audit_log": True,
        "one_per_session": ["Explore", "Plan"],
        "always_allowed": ["claude-code-guide"],
    }))
    env = os.environ.copy()
    env["TOKEN_GUARD_STATE_DIR"] = str(state_dir)
    env["TOKEN_GUARD_CONFIG_PATH"] = str(config_path)
    return env, state_dir


def run_token_guard(input_data, env):
    """Run token-guard.py."""
    result = subprocess.run(
        ["python3", TOKEN_GUARD],
        input=json.dumps(input_data),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    return result.returncode, result.stdout, result.stderr


def run_read_guard(input_data, env):
    """Run read-efficiency-guard.py."""
    result = subprocess.run(
        ["python3", READ_GUARD],
        input=json.dumps(input_data),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    return result.returncode, result.stdout, result.stderr


class TestCrossHookCoordination:
    """Test that token-guard state is correctly read by read-efficiency-guard."""

    def test_explore_dirs_flow_to_read_guard(self, integrated_env):
        """Full flow: Explore spawn -> read in Explore'd dir -> warning."""
        env, state_dir = integrated_env
        sid = "integration-explore"
        home = os.path.expanduser("~")

        # Step 1: Spawn an Explore agent via token-guard
        code, _, _ = run_token_guard({
            "tool_name": "Task",
            "tool_input": {
                "subagent_type": "Explore",
                "description": "explore codebase",
                "prompt": f"GOAL: Map architecture\nSTART: {home}/Projects/myapp\nSTOP WHEN: done",
            },
            "session_id": sid,
        }, env=env)
        assert code == 0

        # Verify token-guard saved target_dirs in state
        state_file = state_dir / f"{sid}.json"
        with open(state_file, "r") as f:
            state = json.load(f)
        assert len(state["agents"]) == 1
        assert "target_dirs" in state["agents"][0]
        assert f"{home}/Projects/myapp" in state["agents"][0]["target_dirs"]

        # Step 2: Read a file in the Explore'd directory via read-guard
        code, _, stderr = run_read_guard({
            "tool_name": "Read",
            "tool_input": {"file_path": f"{home}/Projects/myapp/src/main.py"},
            "session_id": sid,
        }, env=env)
        assert code == 0  # Advisory, not blocking
        assert "already mapped by your Explore agent" in stderr

    def test_no_cross_contamination_between_sessions(self, integrated_env):
        """Explore in session A should NOT affect reads in session B."""
        env, state_dir = integrated_env
        home = os.path.expanduser("~")

        # Session A: Explore
        run_token_guard({
            "tool_name": "Task",
            "tool_input": {
                "subagent_type": "Explore",
                "description": "explore",
                "prompt": f"START: {home}/Projects/app-a",
            },
            "session_id": "session-a",
        }, env=env)

        # Session B: Read in same directory — should NOT get Explore warning
        code, _, stderr = run_read_guard({
            "tool_name": "Read",
            "tool_input": {"file_path": f"{home}/Projects/app-a/file.py"},
            "session_id": "session-b",
        }, env=env)
        assert code == 0
        assert "already mapped by your Explore agent" not in stderr


class TestSharedStateIntegrity:
    """Test that both hooks can safely access shared state without corruption."""

    def test_sequential_hook_calls(self, integrated_env):
        """Multiple hook calls in sequence should produce valid state."""
        env, state_dir = integrated_env
        sid = "integrity-test"

        # Token guard: allow an Explore
        run_token_guard({
            "tool_name": "Task",
            "tool_input": {"subagent_type": "Explore", "description": "explore"},
            "session_id": sid,
        }, env=env)

        # Read guard: 3 reads
        for i in range(3):
            run_read_guard({
                "tool_name": "Read",
                "tool_input": {"file_path": f"/file{i}.py"},
                "session_id": sid,
            }, env=env)

        # Verify both state files are valid JSON
        token_state = state_dir / f"{sid}.json"
        read_state = state_dir / f"{sid}-reads.json"

        with open(token_state, "r") as f:
            ts = json.load(f)
        assert ts["agent_count"] == 1

        with open(read_state, "r") as f:
            rs = json.load(f)
        assert len(rs["reads"]) == 3


class TestHookUtilsIntegration:
    """Test that hook_utils.py functions work correctly in subprocess context."""

    def test_locked_audit_append(self, integrated_env):
        """Audit log entries from token-guard should be valid JSON lines."""
        env, state_dir = integrated_env

        # Multiple allowed spawns
        for i in range(3):
            run_token_guard({
                "tool_name": "Task",
                "tool_input": {"subagent_type": f"type-{i}", "description": f"task {i}"},
                "session_id": "audit-integrity",
            }, env=env)

        # Read and validate all audit entries
        audit_file = state_dir / "audit.jsonl"
        lines = audit_file.read_text().strip().split("\n")
        assert len(lines) == 3
        for line in lines:
            entry = json.loads(line)  # Should not raise
            assert "event" in entry
            assert "type" in entry


class TestConcurrentExecution:
    """Test that concurrent hook calls don't corrupt state."""

    def test_concurrent_read_guard_calls(self, integrated_env):
        """10 parallel read-guard calls should not corrupt state."""
        import concurrent.futures
        env, state_dir = integrated_env
        sid = "concurrent-reads"

        def do_read(i):
            return run_read_guard({
                "tool_name": "Read",
                "tool_input": {"file_path": f"/file{i}.py"},
                "session_id": sid,
            }, env=env)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(do_read, i) for i in range(10)]
            results = [f.result() for f in futures]

        # All should complete without crash (exit 0 or 2, never 1)
        for code, _, _ in results:
            assert code in (0, 2), f"Unexpected exit code {code} — hook crashed"

        # State file should be valid JSON
        state_file = state_dir / f"{sid}-reads.json"
        state = json.loads(state_file.read_text())
        assert "reads" in state

    def test_concurrent_token_guard_calls(self, integrated_env):
        """5 parallel token-guard calls should not corrupt state."""
        import concurrent.futures
        env, state_dir = integrated_env
        sid = "concurrent-tokens"

        def do_spawn(i):
            return run_token_guard({
                "tool_name": "Task",
                "tool_input": {
                    "subagent_type": f"type-{i}",
                    "description": f"concurrent task {i} with complex multi-service refactoring",
                },
                "session_id": sid,
            }, env=env)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(do_spawn, i) for i in range(5)]
            results = [f.result() for f in futures]

        # All should complete without crash (exit 0 or 2, never 1)
        for code, _, _ in results:
            assert code in (0, 2), f"Unexpected exit code {code} — hook crashed"

        # State file should be valid JSON
        state_file = state_dir / f"{sid}.json"
        state = json.loads(state_file.read_text())
        assert "agents" in state
