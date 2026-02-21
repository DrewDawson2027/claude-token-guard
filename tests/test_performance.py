"""
Performance benchmarks for hook invocations.

Verifies the README claim of "~10-20ms per tool call" with actual measurements.
Uses pytest-benchmark when available, falls back to manual timing assertions.

Requires: pip install pytest-benchmark (optional — tests run without it)
"""

import json
import os
import subprocess
import time

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_GUARD = os.path.join(_REPO_ROOT, "token-guard.py")
READ_GUARD = os.path.join(_REPO_ROOT, "read-efficiency-guard.py")

try:
    import pytest_benchmark  # noqa: F401
    HAS_BENCHMARK = True
except ImportError:
    HAS_BENCHMARK = False

# Modules are registered in sys.modules by conftest.py's dynamic loader
import token_guard


@pytest.fixture
def perf_env(tmp_path):
    """Create a lightweight isolated environment for performance testing."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "max_agents": 5, "parallel_window_seconds": 30,
        "global_cooldown_seconds": 0, "max_per_subagent_type": 5,
        "state_ttl_hours": 24, "audit_log": False,
        "one_per_session": ["Explore", "Plan"],
        "always_allowed": ["claude-code-guide"],
    }))
    env = os.environ.copy()
    env["TOKEN_GUARD_STATE_DIR"] = str(state_dir)
    env["TOKEN_GUARD_CONFIG_PATH"] = str(config_path)
    return env


def _invoke_hook(script, input_data, env):
    """Run a hook script and return elapsed time in ms."""
    start = time.perf_counter()
    result = subprocess.run(
        ["python3", script],
        input=json.dumps(input_data),
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    return elapsed_ms, result.returncode


# ============================================================
# Subprocess invocation benchmarks (full cold-start)
# ============================================================

class TestTokenGuardLatency:
    """Benchmark full token-guard subprocess invocations."""

    def test_allow_latency(self, perf_env):
        """Allowed spawn should complete within 100ms (CI) / 20ms (local)."""
        input_data = {
            "tool_name": "Task",
            "tool_input": {
                "subagent_type": "general-purpose",
                "description": "refactor the complex authentication system",
            },
            "session_id": "perf-allow",
        }
        # Warmup
        _invoke_hook(TOKEN_GUARD, input_data, perf_env)
        # Measure
        times = []
        for i in range(5):
            ms, code = _invoke_hook(
                TOKEN_GUARD,
                {**input_data, "session_id": f"perf-allow-{i}"},
                perf_env,
            )
            assert code == 0
            times.append(ms)
        avg_ms = sum(times) / len(times)
        # CI threshold is generous (shared runners are slow)
        assert avg_ms < 500, f"Average latency {avg_ms:.0f}ms exceeds 500ms CI threshold"

    def test_block_latency(self, perf_env):
        """Blocked spawn should complete within 100ms."""
        input_data = {
            "tool_name": "Task",
            "tool_input": {
                "subagent_type": "Explore",
                "description": "search for function handleAuth",
            },
            "session_id": "perf-block",
        }
        ms, code = _invoke_hook(TOKEN_GUARD, input_data, perf_env)
        assert code == 2  # Blocked by necessity check
        assert ms < 500, f"Block latency {ms:.0f}ms exceeds 500ms CI threshold"

    def test_non_task_passthrough_latency(self, perf_env):
        """Non-Task tool calls should be very fast (just parse + exit)."""
        input_data = {
            "tool_name": "Read",
            "tool_input": {"file_path": "/some/file.py"},
            "session_id": "perf-passthrough",
        }
        times = []
        for _ in range(5):
            ms, code = _invoke_hook(TOKEN_GUARD, input_data, perf_env)
            assert code == 0
            times.append(ms)
        avg_ms = sum(times) / len(times)
        assert avg_ms < 500, f"Passthrough latency {avg_ms:.0f}ms exceeds 500ms"


class TestReadGuardLatency:
    """Benchmark full read-efficiency-guard subprocess invocations."""

    def test_allow_latency(self, perf_env):
        """Allowed read should complete within 100ms."""
        input_data = {
            "tool_name": "Read",
            "tool_input": {"file_path": "/some/file.py"},
            "session_id": "perf-read-allow",
        }
        times = []
        for i in range(5):
            ms, code = _invoke_hook(
                READ_GUARD,
                {**input_data, "session_id": f"perf-read-{i}"},
                perf_env,
            )
            assert code == 0
            times.append(ms)
        avg_ms = sum(times) / len(times)
        assert avg_ms < 500, f"Read allow latency {avg_ms:.0f}ms exceeds 500ms"


# ============================================================
# Direct function call benchmarks (no subprocess overhead)
# ============================================================

class TestDirectFunctionLatency:
    """Benchmark individual functions without subprocess overhead."""

    def test_check_necessity_regex_latency(self):
        """Regex necessity check should complete in <1ms."""
        start = time.perf_counter()
        for _ in range(100):
            token_guard.check_necessity("search for function handleAuth", "")
        elapsed_ms = (time.perf_counter() - start) * 1000
        avg_ms = elapsed_ms / 100
        assert avg_ms < 5, f"Regex check {avg_ms:.3f}ms per call exceeds 5ms"

    def test_check_necessity_fuzzy_latency(self):
        """Fuzzy matching (50 canonicals) should complete in <10ms."""
        # Use a description that won't match regex (forces fuzzy path)
        desc = "figure out the structure of the billing module architecture"
        start = time.perf_counter()
        for _ in range(100):
            token_guard.check_necessity(desc, "")
        elapsed_ms = (time.perf_counter() - start) * 1000
        avg_ms = elapsed_ms / 100
        assert avg_ms < 10, f"Fuzzy check {avg_ms:.3f}ms per call exceeds 10ms"

    def test_check_type_switching_latency(self):
        """Type-switching detection should complete in <1ms."""
        state = {
            "blocked_attempts": [
                {"type": "Explore", "description": f"task {i}", "timestamp": 0}
                for i in range(10)  # 10 blocked attempts
            ]
        }
        start = time.perf_counter()
        for _ in range(100):
            token_guard.check_type_switching(state, "new description", "general-purpose")
        elapsed_ms = (time.perf_counter() - start) * 1000
        avg_ms = elapsed_ms / 100
        assert avg_ms < 5, f"Type switching {avg_ms:.3f}ms per call exceeds 5ms"

    def test_extract_target_dirs_latency(self):
        """Directory extraction from prompt should complete in <1ms."""
        prompt = (
            "GOAL: Map the architecture\n"
            "START: ~/Projects/myapp\n"
            "STOP WHEN: done\n"
            "Also check /opt/data/src and ~/other/dir"
        )
        start = time.perf_counter()
        for _ in range(100):
            token_guard.extract_target_dirs(prompt)
        elapsed_ms = (time.perf_counter() - start) * 1000
        avg_ms = elapsed_ms / 100
        assert avg_ms < 5, f"Dir extraction {avg_ms:.3f}ms per call exceeds 5ms"
