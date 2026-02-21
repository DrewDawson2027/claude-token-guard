"""
Tests for token-guard.py — the PreToolUse hook that enforces agent spawning limits.

Uses subprocess to pipe JSON into the script and check exit codes + stderr.
All tests use isolated temp directories so they never touch real session state.
"""

import json
import os
import subprocess
import tempfile
import time

import pytest

SCRIPT = os.path.expanduser("~/.claude/hooks/token-guard.py")


@pytest.fixture
def isolated_env(tmp_path):
    """Create an isolated environment with custom STATE_DIR and CONFIG_PATH.

    NOTE: global_cooldown_seconds=0 disables cooldown for existing tests.
    Use isolated_env_with_cooldown for cooldown-specific tests.
    """
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
        "always_allowed": ["claude-code-guide", "statusline-setup"],
    }))
    env = os.environ.copy()
    env["TOKEN_GUARD_STATE_DIR"] = str(state_dir)
    env["TOKEN_GUARD_CONFIG_PATH"] = str(config_path)
    return env, state_dir, config_path


@pytest.fixture
def isolated_env_with_cooldown(tmp_path):
    """Create an isolated environment with global cooldown enabled (1s for fast tests)."""
    state_dir = tmp_path / "session-state"
    state_dir.mkdir()
    config_path = tmp_path / "token-guard-config.json"
    config_path.write_text(json.dumps({
        "max_agents": 5,
        "parallel_window_seconds": 30,
        "global_cooldown_seconds": 1,
        "max_per_subagent_type": 1,
        "state_ttl_hours": 24,
        "audit_log": True,
        "one_per_session": ["Explore", "Plan"],
        "always_allowed": ["claude-code-guide", "statusline-setup"],
    }))
    env = os.environ.copy()
    env["TOKEN_GUARD_STATE_DIR"] = str(state_dir)
    env["TOKEN_GUARD_CONFIG_PATH"] = str(config_path)
    return env, state_dir, config_path


def run_guard(input_data, env=None):
    """Run token-guard.py with the given input and return (exit_code, stdout, stderr)."""
    result = subprocess.run(
        ["python3", SCRIPT],
        input=json.dumps(input_data),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    return result.returncode, result.stdout, result.stderr


def run_guard_raw(raw_input, env=None):
    """Run token-guard.py with raw string input (for malformed stdin tests)."""
    result = subprocess.run(
        ["python3", SCRIPT],
        input=raw_input,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    return result.returncode, result.stdout, result.stderr


def make_task_input(subagent_type="Explore", description="test", session_id="test-session",
                    prompt="", resume=None, team_name=None, model=None):
    """Create a standard Task tool input payload."""
    payload = {
        "tool_name": "Task",
        "tool_input": {
            "subagent_type": subagent_type,
            "description": description,
        },
        "session_id": session_id,
    }
    if prompt:
        payload["tool_input"]["prompt"] = prompt
    if resume:
        payload["tool_input"]["resume"] = resume
    if team_name:
        payload["tool_input"]["team_name"] = team_name
    if model:
        payload["tool_input"]["model"] = model
    return payload


class TestBasicRules:
    """Test the core enforcement rules."""

    def test_allow_first_explore(self, isolated_env):
        """First Explore agent should be allowed (exit 0)."""
        env, _, _ = isolated_env
        code, _, _ = run_guard(make_task_input("Explore", session_id="rule1-first"), env=env)
        assert code == 0

    def test_block_second_explore(self, isolated_env):
        """Second Explore agent in same session should be blocked (exit 2)."""
        env, _, _ = isolated_env
        sid = "rule1-second"
        run_guard(make_task_input("Explore", session_id=sid), env=env)
        code, _, stderr = run_guard(make_task_input("Explore", description="second", session_id=sid), env=env)
        assert code == 2
        assert "BLOCKED" in stderr
        assert "Max 1 per session" in stderr

    def test_allow_first_general_purpose(self, isolated_env):
        """First general-purpose agent should be allowed."""
        env, _, _ = isolated_env
        code, _, _ = run_guard(make_task_input("general-purpose", session_id="rule2-first"), env=env)
        assert code == 0

    def test_block_second_general_purpose(self, isolated_env):
        """Second general-purpose agent should be blocked (max_per_subagent_type=1)."""
        env, _, _ = isolated_env
        sid = "rule2-second"
        run_guard(make_task_input("general-purpose", session_id=sid), env=env)
        code, _, stderr = run_guard(make_task_input("general-purpose", description="second", session_id=sid), env=env)
        assert code == 2
        assert "BLOCKED" in stderr

    def test_session_cap(self, isolated_env):
        """6th agent should be blocked when cap is 5."""
        env, _, _ = isolated_env
        sid = "rule3-cap"
        types = ["general-purpose", "master-coder", "master-researcher", "master-workflow", "master-architect"]
        for t in types:
            code, _, _ = run_guard(make_task_input(t, session_id=sid), env=env)
            assert code == 0, f"Agent {t} should be allowed but was blocked"
        # 6th should fail
        code, _, stderr = run_guard(make_task_input("vibe-coder", session_id=sid), env=env)
        assert code == 2
        assert "Agent cap reached" in stderr

    def test_always_allowed_bypass(self, isolated_env):
        """claude-code-guide should never be blocked and never count toward caps."""
        env, _, _ = isolated_env
        sid = "bypass-test"
        # Spawn 5 normal agents to hit cap
        for t in ["general-purpose", "master-coder", "master-researcher", "master-workflow", "master-architect"]:
            run_guard(make_task_input(t, session_id=sid), env=env)
        # claude-code-guide should still work despite cap
        code, _, _ = run_guard(make_task_input("claude-code-guide", session_id=sid), env=env)
        assert code == 0


class TestConfigLoading:
    """Test configuration handling edge cases."""

    def test_config_missing(self, isolated_env):
        """Missing config file should use defaults gracefully."""
        env, state_dir, config_path = isolated_env
        # Delete the config file to simulate missing config
        os.unlink(str(config_path))
        code, _, _ = run_guard(make_task_input("Explore", session_id="config-missing"), env=env)
        assert code == 0

    def test_config_corrupt(self, isolated_env):
        """Corrupt JSON config should use defaults gracefully."""
        env, state_dir, config_path = isolated_env
        config_path.write_text("not valid json {{{")
        code, _, _ = run_guard(make_task_input("Explore", session_id="config-corrupt"), env=env)
        assert code == 0

    def test_config_bad_type_coercion(self, isolated_env):
        """Config with non-numeric max_agents should not crash (uses default)."""
        env, state_dir, config_path = isolated_env
        config_path.write_text(json.dumps({
            "max_agents": "banana",
            "parallel_window_seconds": "not_a_number",
            "global_cooldown_seconds": 0,
            "max_per_subagent_type": 1,
        }))
        code, _, _ = run_guard(make_task_input("Explore", session_id="config-coerce"), env=env)
        assert code == 0, "Bad type coercion in config should not crash the hook"


class TestStateCleanup:
    """Test that stale state files are cleaned up."""

    def test_state_cleanup(self, isolated_env):
        """Files older than 24h should be deleted on next invocation."""
        env, state_dir, _ = isolated_env
        # Create a fake old state file
        old_file = state_dir / "test-cleanup-old.json"
        old_file.write_text(json.dumps({"agent_count": 0, "agents": []}))
        # Set mtime to 25 hours ago
        old_time = time.time() - (25 * 3600)
        os.utime(str(old_file), (old_time, old_time))

        # Run the guard — cleanup runs at start of main()
        run_guard(make_task_input("Explore", session_id="test-cleanup-trigger"), env=env)

        assert not old_file.exists(), "Stale state file should have been deleted"

    def test_state_cleanup_preserves_audit(self, isolated_env):
        """audit.jsonl should never be deleted even if old."""
        env, state_dir, _ = isolated_env
        audit_file = state_dir / "audit.jsonl"
        audit_file.write_text('{"test": true}\n')
        old_time = time.time() - (25 * 3600)
        os.utime(str(audit_file), (old_time, old_time))

        run_guard(make_task_input("Explore", session_id="test-audit-preserve"), env=env)
        assert audit_file.exists(), "audit.jsonl should never be deleted"


class TestAuditLog:
    """Test audit log entries."""

    def test_audit_log_allow(self, isolated_env):
        """Allowed spawns should create an audit entry."""
        env, state_dir, _ = isolated_env
        audit_file = state_dir / "audit.jsonl"

        run_guard(make_task_input("Explore", session_id="audit-allow-test"), env=env)

        lines = audit_file.read_text().strip().split("\n")
        assert len(lines) >= 1, "Should have audit entry"
        last = json.loads(lines[-1])
        assert last["event"] == "allow"
        assert last["type"] == "Explore"

    def test_audit_log_block(self, isolated_env):
        """Blocked spawns should create an audit entry with reason."""
        env, state_dir, _ = isolated_env
        sid = "audit-block-test"
        run_guard(make_task_input("Explore", session_id=sid), env=env)

        audit_file = state_dir / "audit.jsonl"
        before = len(audit_file.read_text().strip().split("\n"))

        run_guard(make_task_input("Explore", description="second", session_id=sid), env=env)

        lines = audit_file.read_text().strip().split("\n")
        assert len(lines) > before
        last = json.loads(lines[-1])
        assert last["event"] == "block"
        assert "reason" in last


class TestExtractTargetDirs:
    """Test the directory extraction from Explore prompts."""

    def test_extract_start_directive(self, isolated_env):
        """START: ~/Projects/foo should extract correctly."""
        env, state_dir, _ = isolated_env
        code, _, _ = run_guard(make_task_input(
            "Explore", session_id="dirs-start",
            prompt="GOAL: Find things\nSTART: ~/Projects/foo\nSTOP WHEN: done"
        ), env=env)
        assert code == 0
        with open(state_dir / "dirs-start.json", "r") as f:
            state = json.load(f)
        agents = state["agents"]
        assert len(agents) == 1
        assert "target_dirs" in agents[0]
        home = os.path.expanduser("~")
        assert f"{home}/Projects/foo" in agents[0]["target_dirs"]

    def test_extract_absolute_path(self, isolated_env):
        """Absolute /Users/x/src/y paths should extract correctly."""
        env, state_dir, _ = isolated_env
        home = os.path.expanduser("~")
        code, _, _ = run_guard(make_task_input(
            "Explore", session_id="dirs-abs",
            prompt=f"Map the architecture of {home}/src/myapp thoroughly"
        ), env=env)
        assert code == 0
        with open(state_dir / "dirs-abs.json", "r") as f:
            state = json.load(f)
        agents = state["agents"]
        assert len(agents) == 1
        assert "target_dirs" in agents[0]

    def test_extract_no_paths(self, isolated_env):
        """Prompts without paths should produce no target_dirs."""
        env, state_dir, _ = isolated_env
        code, _, _ = run_guard(make_task_input(
            "Explore", session_id="dirs-none",
            prompt="Just look around for interesting stuff"
        ), env=env)
        assert code == 0
        with open(state_dir / "dirs-none.json", "r") as f:
            state = json.load(f)
        agents = state["agents"]
        assert len(agents) == 1
        assert "target_dirs" not in agents[0] or agents[0]["target_dirs"] == []


class TestAtomicWrite:
    """Test that state writes are atomic."""

    def test_state_file_valid_json_after_write(self, isolated_env):
        """State file should always contain valid JSON after writes."""
        env, state_dir, _ = isolated_env
        sid = "atomic-test"
        for i in range(3):
            run_guard(make_task_input(f"type-{i}", session_id=sid), env=env)
        with open(state_dir / f"{sid}.json", "r") as f:
            state = json.load(f)  # Should not raise
        assert state["agent_count"] == 3


class TestNonTaskCalls:
    """Test that non-Task tool calls pass through."""

    def test_read_tool_passes(self, isolated_env):
        """Read tool calls should be allowed (exit 0, not gated)."""
        env, _, _ = isolated_env
        code, _, _ = run_guard({
            "tool_name": "Read",
            "tool_input": {"file_path": "/some/file.py"},
            "session_id": "non-task-test",
        }, env=env)
        assert code == 0

    def test_bash_tool_passes(self, isolated_env):
        """Bash tool calls should be allowed."""
        env, _, _ = isolated_env
        code, _, _ = run_guard({
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
            "session_id": "non-task-test",
        }, env=env)
        assert code == 0


class TestStdinProtection:
    """Test that malformed/empty stdin is handled gracefully."""

    def test_empty_stdin(self, isolated_env):
        """Empty stdin should exit 0 (fail-open, not crash)."""
        env, _, _ = isolated_env
        code, _, _ = run_guard_raw("", env=env)
        assert code == 0

    def test_malformed_json_stdin(self, isolated_env):
        """Invalid JSON stdin should exit 0 (fail-open, not crash)."""
        env, _, _ = isolated_env
        code, _, _ = run_guard_raw("not json at all {{{", env=env)
        assert code == 0

    def test_partial_json_stdin(self, isolated_env):
        """Partial JSON stdin should exit 0 (fail-open, not crash)."""
        env, _, _ = isolated_env
        code, _, _ = run_guard_raw('{"tool_name": "Task", "tool_input":', env=env)
        assert code == 0


# ============================================================
# NEW TESTS: Resume, Team, Necessity, Advisory, Anti-Evasion
# ============================================================


class TestResumeDetection:
    """Test that resuming agents always succeeds."""

    def test_resume_always_allowed(self, isolated_env):
        """Task with resume param should always exit 0, even after type is maxed."""
        env, _, _ = isolated_env
        sid = "resume-test"
        # Spawn an Explore (uses up the one-per-session slot)
        code, _, _ = run_guard(make_task_input("Explore", session_id=sid), env=env)
        assert code == 0
        # Resume should succeed even though Explore is maxed
        code, _, _ = run_guard(make_task_input(
            "Explore", description="resume existing",
            session_id=sid, resume="agent-abc-123"
        ), env=env)
        assert code == 0

    def test_resume_audit_entry(self, isolated_env):
        """Resume should create an audit entry with 'resume' event."""
        env, state_dir, _ = isolated_env
        run_guard(make_task_input(
            "Explore", description="resuming",
            session_id="resume-audit", resume="agent-xyz"
        ), env=env)
        audit_file = state_dir / "audit.jsonl"
        lines = audit_file.read_text().strip().split("\n")
        last = json.loads(lines[-1])
        assert last["event"] == "resume"

    def test_resume_does_not_increment_count(self, isolated_env):
        """After resume, agent_count should stay at 1 (not increment)."""
        env, state_dir, _ = isolated_env
        sid = "resume-count"
        # Spawn an Explore
        run_guard(make_task_input("Explore", session_id=sid), env=env)
        # Resume it
        run_guard(make_task_input(
            "Explore", description="resume",
            session_id=sid, resume="agent-abc"
        ), env=env)
        state_file = state_dir / f"{sid}.json"
        state = json.loads(state_file.read_text())
        assert state["agent_count"] == 1, "Resume should not increment agent_count"


class TestTeamDetection:
    """Test team-aware agent spawning."""

    def test_team_spawn_bypasses_rules(self, isolated_env):
        """Task with team_name should bypass rules 1-7 and exit 0."""
        env, _, _ = isolated_env
        sid = "team-test"
        # Spawn an Explore (uses up one-per-session slot)
        run_guard(make_task_input("Explore", session_id=sid), env=env)
        # Team spawn of another Explore should succeed (bypasses Rule 1)
        code, _, _ = run_guard(make_task_input(
            "Explore", description="team explore",
            session_id=sid, team_name="my-team"
        ), env=env)
        assert code == 0

    def test_team_spawn_hits_cap(self, isolated_env):
        """Team spawn after cap reached should be blocked."""
        env, _, _ = isolated_env
        sid = "team-cap-test"
        # Fill up to cap with team spawns
        for i in range(5):
            code, _, _ = run_guard(make_task_input(
                f"type-{i}", description=f"team agent {i}",
                session_id=sid, team_name="my-team"
            ), env=env)
            assert code == 0
        # 6th team spawn should be blocked
        code, _, stderr = run_guard(make_task_input(
            "type-6", description="over cap",
            session_id=sid, team_name="my-team"
        ), env=env)
        assert code == 2
        assert "Agent cap reached" in stderr

    def test_team_spawn_audit_entry(self, isolated_env):
        """Team spawn should create audit entry with 'allow_team' event."""
        env, state_dir, _ = isolated_env
        run_guard(make_task_input(
            "Explore", description="team task",
            session_id="team-audit", team_name="my-team"
        ), env=env)
        audit_file = state_dir / "audit.jsonl"
        lines = audit_file.read_text().strip().split("\n")
        last = json.loads(lines[-1])
        assert last["event"] == "allow_team"

    def test_team_state_records_team_name(self, isolated_env):
        """Team spawn should record team name in agent record."""
        env, state_dir, _ = isolated_env
        sid = "team-state"
        run_guard(make_task_input(
            "Explore", description="team work",
            session_id=sid, team_name="my-team"
        ), env=env)
        state_file = state_dir / f"{sid}.json"
        state = json.loads(state_file.read_text())
        assert len(state["agents"]) == 1
        assert state["agents"][0].get("team") == "my-team"


class TestNecessityScoring:
    """Test that obviously simple tasks are blocked."""

    def test_necessity_blocks_grep_task(self, isolated_env):
        """'search for function X' should be blocked — use Grep."""
        env, _, _ = isolated_env
        code, _, stderr = run_guard(make_task_input(
            "Explore", description="search for function handleAuth in the codebase",
            session_id="necessity-grep"
        ), env=env)
        assert code == 2
        assert "direct tools" in stderr

    def test_necessity_blocks_read_task(self, isolated_env):
        """'read the config file' should be blocked — use Read."""
        env, _, _ = isolated_env
        code, _, stderr = run_guard(make_task_input(
            "general-purpose", description="read the config file and check settings",
            session_id="necessity-read"
        ), env=env)
        assert code == 2
        assert "direct tools" in stderr

    def test_necessity_blocks_run_task(self, isolated_env):
        """'run the test suite' should be blocked — use Bash."""
        env, _, _ = isolated_env
        code, _, stderr = run_guard(make_task_input(
            "general-purpose", description="run the test suite and report results",
            session_id="necessity-run"
        ), env=env)
        assert code == 2
        assert "direct tools" in stderr

    def test_necessity_allows_complex(self, isolated_env):
        """Complex multi-file tasks should be allowed."""
        env, _, _ = isolated_env
        code, _, _ = run_guard(make_task_input(
            "general-purpose",
            description="refactor authentication across 12 microservice modules",
            session_id="necessity-complex"
        ), env=env)
        assert code == 0

    def test_necessity_via_prompt_field(self, isolated_env):
        """Necessity check should also match patterns in the prompt field."""
        env, _, _ = isolated_env
        code, _, stderr = run_guard(make_task_input(
            "general-purpose",
            description="do something complex",
            session_id="necessity-prompt",
            prompt="search for function parseConfig in the codebase"
        ), env=env)
        assert code == 2
        assert "direct tools" in stderr

    def test_necessity_all_patterns(self, isolated_env):
        """At least 5 of the 10 direct-tool patterns should trigger blocks."""
        env, _, _ = isolated_env
        test_cases = [
            "search for class UserAuth in the codebase",
            "read the config file and check settings",
            "check if the module exists in the project",
            "edit the bug on line 42 in main.py",
            "analyze the file structure of the module",
            "what does the function handleRequest do",
            "list all files in the src directory",
            "count how many tests are in the suite",
            "compare the two config files",
            "run the test script and check output",
        ]
        blocked = 0
        for i, desc in enumerate(test_cases):
            code, _, _ = run_guard(make_task_input(
                f"type-{i}", description=desc, session_id=f"necessity-all-{i}"
            ), env=env)
            if code == 2:
                blocked += 1
        assert blocked >= 5, f"Expected at least 5/10 patterns to trigger, got {blocked}"


class TestAdvisories:
    """Test non-blocking advisory messages."""

    def test_first_spawn_advisory(self, isolated_env):
        """First agent should produce advisory on stderr, still exit 0."""
        env, _, _ = isolated_env
        code, _, stderr = run_guard(make_task_input(
            "general-purpose", description="do complex work",
            session_id="advisory-first"
        ), env=env)
        assert code == 0
        assert "FIRST AGENT THIS SESSION" in stderr

    def test_model_advisory_opus(self, isolated_env):
        """Opus model should produce cost advisory on stderr, still exit 0."""
        env, _, _ = isolated_env
        code, _, stderr = run_guard(make_task_input(
            "general-purpose", description="do complex work",
            session_id="advisory-model", model="opus"
        ), env=env)
        assert code == 0
        assert "MODEL COST" in stderr
        assert "opus" in stderr

    def test_no_model_advisory_sonnet(self, isolated_env):
        """Sonnet model should NOT produce cost advisory."""
        env, _, _ = isolated_env
        code, _, stderr = run_guard(make_task_input(
            "general-purpose", description="do complex work",
            session_id="advisory-sonnet", model="sonnet"
        ), env=env)
        assert code == 0
        assert "MODEL COST" not in stderr


class TestTypeSwitching:
    """Test type-switching detection (anti-evasion)."""

    def test_type_switching_blocks(self, isolated_env):
        """Blocked Explore → general-purpose with similar desc → blocked."""
        env, _, _ = isolated_env
        sid = "type-switch-block"
        desc = "explore the authentication architecture thoroughly"
        # First Explore: allowed
        code, _, _ = run_guard(make_task_input("Explore", description=desc, session_id=sid), env=env)
        assert code == 0
        # Second Explore: blocked (one_per_session) → creates blocked_attempt
        code, _, _ = run_guard(make_task_input("Explore", description=desc, session_id=sid), env=env)
        assert code == 2
        # general-purpose with similar desc → blocked (type-switching)
        code, _, stderr = run_guard(make_task_input(
            "general-purpose",
            description="investigate the authentication architecture thoroughly",
            session_id=sid
        ), env=env)
        assert code == 2
        assert "resembles" in stderr

    def test_type_switching_allows_different_desc(self, isolated_env):
        """Blocked Explore → general-purpose with very different desc → allowed."""
        env, _, _ = isolated_env
        sid = "type-switch-allow"
        # Block an Explore
        run_guard(make_task_input("Explore", description="map the auth system", session_id=sid), env=env)
        run_guard(make_task_input("Explore", description="map the auth system", session_id=sid), env=env)
        # general-purpose with very different desc → allowed
        code, _, _ = run_guard(make_task_input(
            "general-purpose",
            description="build the new payment processing pipeline",
            session_id=sid
        ), env=env)
        assert code == 0

    def test_similarity_below_threshold_allows(self, isolated_env):
        """Description with low similarity to blocked attempt should be allowed."""
        env, _, _ = isolated_env
        sid = "type-switch-low-sim"
        # Block an Explore with specific description
        run_guard(make_task_input("Explore", description="analyze database schema migrations", session_id=sid), env=env)
        run_guard(make_task_input("Explore", description="analyze database schema migrations", session_id=sid), env=env)
        # Different type with completely different topic — low similarity
        code, _, _ = run_guard(make_task_input(
            "general-purpose",
            description="configure CI/CD pipeline for deployment",
            session_id=sid
        ), env=env)
        assert code == 0, "Low-similarity description should not trigger type-switching detection"


class TestGlobalCooldown:
    """Test global cooldown between any-type spawns."""

    def test_global_cooldown_blocks(self, isolated_env_with_cooldown):
        """Two different-type spawns within cooldown → second blocked."""
        env, _, _ = isolated_env_with_cooldown
        sid = "cooldown-block"
        # First spawn
        code, _, _ = run_guard(make_task_input(
            "general-purpose", description="do complex work", session_id=sid
        ), env=env)
        assert code == 0
        # Immediate second spawn (different type) — should be blocked by cooldown
        code, _, stderr = run_guard(make_task_input(
            "master-coder", description="build something complex", session_id=sid
        ), env=env)
        assert code == 2
        assert "BLOCKED" in stderr
        assert "Wait" in stderr

    def test_global_cooldown_allows(self, isolated_env_with_cooldown):
        """Two different-type spawns after cooldown expires → second allowed."""
        env, _, _ = isolated_env_with_cooldown
        sid = "cooldown-allow"
        # First spawn
        code, _, _ = run_guard(make_task_input(
            "general-purpose", description="do complex work", session_id=sid
        ), env=env)
        assert code == 0
        # Wait for cooldown (1s config + margin)
        time.sleep(1.5)
        # Second spawn should be allowed
        code, _, _ = run_guard(make_task_input(
            "master-coder", description="build something complex", session_id=sid
        ), env=env)
        assert code == 0


class TestBlockedAttempts:
    """Test that blocked attempts are persisted in state."""

    def test_blocked_attempts_persisted(self, isolated_env):
        """After a block, state file should contain blocked_attempts."""
        env, state_dir, _ = isolated_env
        sid = "blocked-persist"
        # Cause a block: spawn Explore twice
        run_guard(make_task_input("Explore", description="first", session_id=sid), env=env)
        run_guard(make_task_input("Explore", description="second attempt", session_id=sid), env=env)
        # Check state file
        with open(state_dir / f"{sid}.json", "r") as f:
            state = json.load(f)
        assert "blocked_attempts" in state
        assert len(state["blocked_attempts"]) >= 1
        assert state["blocked_attempts"][0]["type"] == "Explore"
        assert state["blocked_attempts"][0]["description"] == "second attempt"


class TestReportMode:
    """Test the --report analytics mode."""

    def test_report_mode(self, isolated_env):
        """--report should print analytics without crashing."""
        env, state_dir, _ = isolated_env
        # Create some audit data
        audit_file = state_dir / "audit.jsonl"
        entries = [
            {"ts": "2026-01-01T00:00:00", "event": "allow", "type": "Explore", "desc": "test", "session": "abc"},
            {"ts": "2026-01-01T00:00:01", "event": "block", "type": "Explore", "desc": "second", "session": "abc", "reason": "one_per_session limit"},
            {"ts": "2026-01-01T00:00:02", "event": "allow", "type": "general-purpose", "desc": "work", "session": "def"},
        ]
        audit_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        result = subprocess.run(
            ["python3", SCRIPT, "--report"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0
        assert "TOKEN GUARD ANALYTICS" in result.stdout
        assert "Allowed: 2" in result.stdout
        assert "Blocked: 1" in result.stdout

    def test_report_mode_no_data(self, isolated_env):
        """--report with no audit data should not crash."""
        env, _, _ = isolated_env
        result = subprocess.run(
            ["python3", SCRIPT, "--report"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0


class TestErrorResilience:
    """Test that the hook fails open under error conditions."""

    def test_save_state_failure_does_not_crash(self, tmp_path):
        """Unwritable state dir should exit 0 (fail-open), not crash."""
        state_dir = tmp_path / "session-state"
        state_dir.mkdir()
        config_path = tmp_path / "token-guard-config.json"
        config_path.write_text(json.dumps({
            "max_agents": 5,
            "global_cooldown_seconds": 0,
            "max_per_subagent_type": 1,
            "one_per_session": ["Explore", "Plan"],
            "always_allowed": ["claude-code-guide"],
        }))
        env = os.environ.copy()
        env["TOKEN_GUARD_STATE_DIR"] = str(state_dir)
        env["TOKEN_GUARD_CONFIG_PATH"] = str(config_path)

        # First spawn succeeds normally
        code, _, _ = run_guard(make_task_input("Explore", session_id="resilience"), env=env)
        assert code == 0

        # Make state dir read-only so save_state will fail
        os.chmod(str(state_dir), 0o444)
        try:
            # Second call should fail-open (exit 0), not crash (exit 1)
            code, _, _ = run_guard(make_task_input(
                "general-purpose", description="test resilience",
                session_id="resilience-2"
            ), env=env)
            assert code == 0, f"Hook should fail-open on state write failure, got exit {code}"
        finally:
            # Restore permissions for cleanup
            os.chmod(str(state_dir), 0o755)


class TestAuditRotation:
    """Test audit log rotation."""

    def test_audit_rotation(self, isolated_env):
        """Audit log with >10K lines should be rotated to .1 backup."""
        env, state_dir, _ = isolated_env
        audit_file = state_dir / "audit.jsonl"
        # Create an audit file with 10001 lines
        entry = json.dumps({"ts": "2026-01-01T00:00:00", "event": "allow", "type": "x", "desc": "t", "session": "s"})
        audit_file.write_text((entry + "\n") * 10001)

        # Run the guard — cleanup includes rotation
        run_guard(make_task_input("general-purpose", description="trigger rotation", session_id="rotation-test"), env=env)

        # Original should be gone or small (new entries only)
        backup = state_dir / "audit.jsonl.1"
        assert backup.exists(), "Backup file should exist after rotation"
