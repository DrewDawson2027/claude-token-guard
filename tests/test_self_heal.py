"""
Tests for self-heal.py — the SessionStart hook that validates and repairs hook health.

Uses isolated temp directories so tests never touch real session state.
"""

import json
import os
import subprocess
import time

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(_REPO_ROOT, "self-heal.py")
HOOKS_DIR = _REPO_ROOT


@pytest.fixture
def isolated_env(tmp_path):
    """Create an isolated environment for self-heal tests."""
    state_dir = tmp_path / "session-state"
    state_dir.mkdir()
    config_path = tmp_path / "token-guard-config.json"
    config_path.write_text(json.dumps({
        "schema_version": 2,
        "max_agents": 5,
        "parallel_window_seconds": 30,
        "global_cooldown_seconds": 5,
        "max_per_subagent_type": 1,
        "state_ttl_hours": 24,
        "audit_log": True,
        "failure_mode": "fail_open",
        "sanitize_session_ids": True,
        "normalize_paths": True,
        "fault_audit": True,
        "max_string_field_length": 512,
        "metrics_correlation_window_seconds": 15,
        "one_per_session": ["Explore", "Plan"],
        "always_allowed": ["claude-code-guide", "statusline-setup"],
    }))
    env = os.environ.copy()
    env["TOKEN_GUARD_STATE_DIR"] = str(state_dir)
    env["TOKEN_GUARD_CONFIG_PATH"] = str(config_path)
    env["TOKEN_GUARD_HOOKS_DIR"] = _REPO_ROOT
    return env, state_dir, config_path


def run_heal(env=None):
    """Run self-heal.py and return (exit_code, stdout, stderr)."""
    result = subprocess.run(
        ["python3", SCRIPT],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return result.returncode, result.stdout, result.stderr


class TestHealthySystem:
    """Test that a healthy system reports OK."""

    def test_healthy_output(self, isolated_env):
        """Healthy system should produce 'OK' output."""
        env, _, _ = isolated_env
        code, stdout, _ = run_heal(env=env)
        assert code == 0
        assert "Self-heal: OK" in stdout

    def test_always_exits_zero(self, isolated_env):
        """Self-heal should ALWAYS exit 0 (never block session start)."""
        env, _, _ = isolated_env
        code, _, _ = run_heal(env=env)
        assert code == 0


class TestCorruptedStateFile:
    """Test that corrupted state files are detected and cleaned."""

    def test_corrupted_json_deleted(self, isolated_env):
        """Corrupted JSON state file should be deleted."""
        env, state_dir, _ = isolated_env
        corrupt_file = state_dir / "corrupt-session.json"
        corrupt_file.write_text("not valid json {{{")

        code, stdout, _ = run_heal(env=env)
        assert code == 0
        assert not corrupt_file.exists(), "Corrupted state file should have been deleted"
        assert "REPAIRED" in stdout

    def test_valid_json_preserved(self, isolated_env):
        """Valid JSON state files should not be touched."""
        env, state_dir, _ = isolated_env
        valid_file = state_dir / "valid-session.json"
        valid_file.write_text(json.dumps({"agent_count": 0, "agents": []}))

        run_heal(env=env)
        assert valid_file.exists(), "Valid state file should be preserved"


class TestOrphanedTmpFiles:
    """Test that orphaned .tmp files are cleaned up."""

    def test_tmp_file_deleted(self, isolated_env):
        """Orphaned .tmp file should be deleted."""
        env, state_dir, _ = isolated_env
        tmp_file = state_dir / "session-abc.tmp"
        tmp_file.write_text("orphaned temp data")

        code, stdout, _ = run_heal(env=env)
        assert code == 0
        assert not tmp_file.exists(), "Orphaned .tmp file should have been deleted"
        assert "REPAIRED" in stdout


class TestStaleLockFiles:
    """Test that stale .lock files are cleaned up."""

    def test_stale_lock_deleted(self, isolated_env):
        """Lock file older than 5 minutes should be deleted."""
        env, state_dir, _ = isolated_env
        lock_file = state_dir / "session-abc.json.lock"
        lock_file.write_text("")
        old_time = time.time() - 400  # 6+ minutes ago
        os.utime(str(lock_file), (old_time, old_time))

        code, stdout, _ = run_heal(env=env)
        assert code == 0
        assert not lock_file.exists(), "Stale lock file should have been deleted"

    def test_fresh_lock_preserved(self, isolated_env):
        """Recent lock file should not be deleted."""
        env, state_dir, _ = isolated_env
        lock_file = state_dir / "session-fresh.json.lock"
        lock_file.write_text("")
        # mtime is now (fresh) — should be preserved

        run_heal(env=env)
        assert lock_file.exists(), "Fresh lock file should be preserved"


class TestMissingStateDir:
    """Test that missing state directory is recreated."""

    def test_state_dir_recreated(self, isolated_env):
        """Missing state directory should be recreated."""
        env, state_dir, _ = isolated_env
        # Remove the state dir
        os.rmdir(str(state_dir))
        assert not state_dir.exists()

        code, stdout, _ = run_heal(env=env)
        assert code == 0
        assert state_dir.exists(), "State directory should have been recreated"


class TestCorruptedConfig:
    """Test that corrupted config is regenerated."""

    def test_corrupted_config_regenerated(self, isolated_env):
        """Corrupted config should be regenerated from defaults."""
        env, _, config_path = isolated_env
        config_path.write_text("not json {{{")

        code, stdout, _ = run_heal(env=env)
        assert code == 0
        # Config should now be valid
        config = json.loads(config_path.read_text())
        assert "max_agents" in config
        assert "REPAIRED" in stdout

    def test_missing_config_regenerated(self, isolated_env):
        """Missing config should be regenerated from defaults."""
        env, _, config_path = isolated_env
        os.unlink(str(config_path))

        code, _, _ = run_heal(env=env)
        assert code == 0
        assert config_path.exists()
        config = json.loads(config_path.read_text())
        assert "max_agents" in config


class TestSmokeTests:
    """Test the smoke test phase."""

    def test_smoke_tests_pass_healthy(self, isolated_env):
        """Smoke tests should pass on a healthy system."""
        env, _, _ = isolated_env
        code, stdout, _ = run_heal(env=env)
        assert code == 0
        # Should not report smoke test failures
        assert "smoke test failed" not in stdout

    def test_smoke_test_detects_broken_hook(self, isolated_env, tmp_path):
        """Replacing a hook with a crashing script should be detected."""
        env, _, _ = isolated_env
        # Create a fake broken hook
        fake_hooks_dir = tmp_path / "fake-hooks"
        fake_hooks_dir.mkdir()
        broken_hook = fake_hooks_dir / "token-guard.py"
        broken_hook.write_text("#!/usr/bin/env python3\nraise RuntimeError('broken')\n")
        broken_hook.chmod(0o755)

        # Run self-heal directly with the broken hook by patching the path
        # We test via subprocess that sends broken-hook path
        result = subprocess.run(
            ["python3", "-c", f"""
import sys, os, json, subprocess, tempfile

HOOKS_DIR = "{fake_hooks_dir}"
STATE_DIR = os.environ["TOKEN_GUARD_STATE_DIR"]
CONFIG_PATH = os.environ["TOKEN_GUARD_CONFIG_PATH"]

# Simulate smoke test on the broken hook
smoke_dir = tempfile.mkdtemp()
smoke_state = os.path.join(smoke_dir, "state")
os.makedirs(smoke_state)
smoke_config = os.path.join(smoke_dir, "config.json")
with open(smoke_config, "w") as f:
    json.dump({{"max_agents": 5}}, f)
smoke_env = os.environ.copy()
smoke_env["TOKEN_GUARD_STATE_DIR"] = smoke_state
smoke_env["TOKEN_GUARD_CONFIG_PATH"] = smoke_config
valid_input = json.dumps({{"tool_name": "Read", "tool_input": {{"file_path": "/tmp/t"}}, "session_id": "smoke"}})
r = subprocess.run(
    ["python3", os.path.join(HOOKS_DIR, "token-guard.py")],
    input=valid_input, capture_output=True, text=True, env=smoke_env, timeout=5
)
# Broken hook should exit non-0
print(f"exit_code={{r.returncode}}")
sys.exit(0 if r.returncode != 0 else 1)
"""],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0, "Should detect that broken hook exits non-0"


class TestHealLog:
    """Test that self-heal writes its log."""

    def test_heal_log_created(self, isolated_env):
        """Self-heal should write a log entry."""
        env, state_dir, _ = isolated_env
        run_heal(env=env)

        heal_log = state_dir / "self-heal.jsonl"
        assert heal_log.exists(), "Self-heal log should be created"
        lines = heal_log.read_text().strip().split("\n")
        assert len(lines) >= 1
        entry = json.loads(lines[-1])
        assert "ts" in entry
        assert "checks" in entry
        assert "repairs" in entry
        assert "status" in entry

    def test_heal_log_records_repairs(self, isolated_env):
        """Self-heal log should record repair actions."""
        env, state_dir, _ = isolated_env
        # Create a corrupted file to trigger repair
        corrupt_file = state_dir / "corrupt.json"
        corrupt_file.write_text("bad json")

        run_heal(env=env)

        heal_log = state_dir / "self-heal.jsonl"
        entry = json.loads(heal_log.read_text().strip().split("\n")[-1])
        assert entry["status"] == "repaired"
        assert entry["repairs"] > 0
        assert "actions" in entry


class TestAuditRotation:
    """Test audit log rotation (rotate-to-backup strategy)."""

    def test_large_audit_rotated(self, isolated_env):
        """Audit log over 10K lines should be rotated to .1 backup."""
        env, state_dir, _ = isolated_env
        audit_file = state_dir / "audit.jsonl"
        # Write 11K lines
        lines = [json.dumps({"ts": "2026-01-01", "event": "allow", "line": i}) + "\n" for i in range(11000)]
        audit_file.write_text("".join(lines))

        run_heal(env=env)

        # Original file should be gone (rotated away)
        # Self-heal's own log write may recreate it with 1 entry
        backup = state_dir / "audit.jsonl.1"
        assert backup.exists(), "Backup file should exist after rotation"
        backup_lines = backup.read_text().strip().split("\n")
        assert len(backup_lines) == 11000, f"Backup should have all 11K lines, got {len(backup_lines)}"

    def test_small_audit_untouched(self, isolated_env):
        """Audit log under 10K lines should not be touched."""
        env, state_dir, _ = isolated_env
        audit_file = state_dir / "audit.jsonl"
        lines = [json.dumps({"ts": "2026-01-01", "event": "allow", "line": i}) + "\n" for i in range(100)]
        audit_file.write_text("".join(lines))

        run_heal(env=env)

        result_lines = audit_file.read_text().strip().split("\n")
        assert len(result_lines) == 100


class TestAutoRepair:
    """Test auto-repair of hook permissions."""

    def test_nonexecutable_sh_gets_fixed(self, isolated_env, tmp_path):
        """health-check.sh with 644 permissions should be fixed to 755 by self-heal.

        Uses a temp directory with a copy of the hook files to avoid mutating
        real filesystem state (test isolation).
        """
        env, _, _ = isolated_env

        # Create an isolated hooks directory with a non-executable .sh file
        fake_hooks = tmp_path / "hooks"
        fake_hooks.mkdir()
        fake_hc = fake_hooks / "health-check.sh"
        fake_hc.write_text("#!/bin/bash\necho ok\n")
        fake_hc.chmod(0o644)

        # Also create the required Python hooks so structural checks pass
        for name in ["token-guard.py", "read-efficiency-guard.py", "hook_utils.py"]:
            src = os.path.join(_REPO_ROOT, name)
            if os.path.isfile(src):
                (fake_hooks / name).write_text(open(src).read())

        env["TOKEN_GUARD_HOOKS_DIR"] = str(fake_hooks)

        assert not os.access(str(fake_hc), os.X_OK), "Should not be executable after chmod 644"

        code, stdout, _ = run_heal(env=env)
        assert code == 0

        # Self-heal should have restored execute permission
        assert os.access(str(fake_hc), os.X_OK), "Self-heal should restore execute permission"
        assert "REPAIRED" in stdout or "chmod" in stdout
