#!/usr/bin/env python3
"""
Self-Heal — SessionStart hook that validates and repairs the token management system.

Runs on every session start (~50ms for a healthy system). Five phases:
  1. Structural integrity — all files exist, config valid, state dir writable
  2. Smoke tests — pipe valid JSON through hooks in isolated temp dirs
  3. State health — find and clean corrupted/orphaned/stale files
  4. Auto-repair — fix permissions, recreate missing dirs, regenerate config
  5. Report — summary to stdout, warnings to stderr

Always exits 0 (never blocks session start). Logs to session-state/self-heal.jsonl.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

# Import shared config (single source of truth — prevents config drift).
# Fallback to inline copy if hook_utils is broken (self-heal must be self-contained).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from hook_utils import DEFAULT_CONFIG
except (ImportError, SyntaxError):
    DEFAULT_CONFIG = {
        "max_agents": 5, "parallel_window_seconds": 30, "global_cooldown_seconds": 5,
        "max_per_subagent_type": 1, "state_ttl_hours": 24, "audit_log": True,
        "one_per_session": ["Explore", "master-coder", "master-researcher",
                            "master-architect", "master-workflow", "Plan"],
        "always_allowed": ["claude-code-guide", "statusline-setup", "haiku"],
    }

HOOKS_DIR = os.environ.get(
    "TOKEN_GUARD_HOOKS_DIR",
    os.path.expanduser("~/.claude/hooks"),
)
STATE_DIR = os.environ.get(
    "TOKEN_GUARD_STATE_DIR",
    os.path.expanduser("~/.claude/hooks/session-state"),
)
CONFIG_PATH = os.environ.get(
    "TOKEN_GUARD_CONFIG_PATH",
    os.path.expanduser("~/.claude/hooks/token-guard-config.json"),
)
HEAL_LOG = os.path.join(STATE_DIR, "self-heal.jsonl")

REQUIRED_HOOKS = {
    "token-guard.py": os.path.join(HOOKS_DIR, "token-guard.py"),
    "read-efficiency-guard.py": os.path.join(HOOKS_DIR, "read-efficiency-guard.py"),
    "hook_utils.py": os.path.join(HOOKS_DIR, "hook_utils.py"),
    "health-check.sh": os.path.join(HOOKS_DIR, "health-check.sh"),
}

AUDIT_MAX_LINES = 10000
STALE_LOCK_SECONDS = 300  # 5 minutes

MASTER_AGENTS_DIR = os.path.expanduser("~/.claude/master-agents")

# Mode files referenced by master agents — validated on session start
EXPECTED_MODE_FILES = {
    "coder": ["build-mode.md", "debug-mode.md", "review-mode.md", "refactor-mode.md", "atlas-mode.md"],
    "researcher": ["academic-mode.md", "market-mode.md", "technical-mode.md", "general-mode.md"],
    "architect": ["database-design.md", "api-design.md", "system-design.md", "frontend-design.md"],
    "workflow": ["gsd-exec.md", "feature-workflow.md", "git-workflow.md", "autonomous.md"],
}


def phase_mode_validation():
    """Phase 4b: Validate all mode files referenced by master agents exist."""
    checks = 0
    repairs = 0
    actions = []

    if not os.path.isdir(MASTER_AGENTS_DIR):
        return checks, repairs, actions

    for agent, modes in EXPECTED_MODE_FILES.items():
        agent_dir = os.path.join(MASTER_AGENTS_DIR, agent)
        for mode_file in modes:
            checks += 1
            mode_path = os.path.join(agent_dir, mode_file)
            if not os.path.isfile(mode_path):
                actions.append(f"MISSING MODE: {agent}/{mode_file}")
                repairs += 1
                print(
                    f"WARNING: Mode file missing: {agent}/{mode_file}. "
                    f"Agent will fall back to default mode.",
                    file=sys.stderr,
                )

    # Also validate ref card directories exist
    for agent in EXPECTED_MODE_FILES:
        refs_dir = os.path.join(MASTER_AGENTS_DIR, agent, "refs")
        checks += 1
        if os.path.isdir(os.path.join(MASTER_AGENTS_DIR, agent)) and not os.path.isdir(refs_dir):
            try:
                os.makedirs(refs_dir, exist_ok=True)
                actions.append(f"created refs dir: {agent}/refs/")
                repairs += 1
            except OSError:
                pass

    return checks, repairs, actions


def main():
    # NOTE: DEFAULT_CONFIG is imported from hook_utils with fallback (see top of file).
    # self-heal remains self-contained even if hook_utils is broken.
    checks = 0
    repairs = 0
    actions = []

    # Phase 1: Structural integrity
    c, r, a = phase_structural()
    checks += c
    repairs += r
    actions.extend(a)

    # Phase 2: Smoke tests
    c, r, a = phase_smoke_tests()
    checks += c
    repairs += r
    actions.extend(a)

    # Phase 3: State health
    c, r, a = phase_state_health()
    checks += c
    repairs += r
    actions.extend(a)

    # Phase 4: Auto-repair (permissions, missing dirs)
    c, r, a = phase_auto_repair()
    checks += c
    repairs += r
    actions.extend(a)

    # Phase 4b: Master agent mode file validation
    c, r, a = phase_mode_validation()
    checks += c
    repairs += r
    actions.extend(a)

    # Phase 5: Report
    status = "healthy" if repairs == 0 else "repaired"
    log_entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "checks": checks,
        "repairs": repairs,
        "status": status,
    }
    if actions:
        log_entry["actions"] = actions

    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(HEAL_LOG, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
    except OSError:
        pass

    summary = f"Self-heal: {'OK' if repairs == 0 else 'REPAIRED'} ({checks} checks, {repairs} repairs)"
    print(summary)
    if repairs > 0:
        print(f"  Repairs: {', '.join(actions)}", file=sys.stderr)

    sys.exit(0)


def phase_structural():
    """Phase 1: Verify all hook files exist and config is valid JSON."""
    checks = 0
    repairs = 0
    actions = []

    # Check hook files exist
    for name, path in REQUIRED_HOOKS.items():
        checks += 1
        if not os.path.isfile(path):
            actions.append(f"MISSING: {name}")
            repairs += 1

    # Check config is valid JSON with expected keys
    checks += 1
    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
        if not isinstance(config, dict):
            actions.append("config is not a JSON object")
            repairs += 1
        elif "max_agents" not in config:
            actions.append("config missing max_agents key")
            # Will be auto-repaired in phase 4
    except FileNotFoundError:
        actions.append("config file missing")
        # Will be auto-repaired in phase 4
    except json.JSONDecodeError:
        actions.append("config is corrupted JSON")
        repairs += 1
        # Will be auto-repaired in phase 4

    # Check state directory exists and is writable
    checks += 1
    if not os.path.isdir(STATE_DIR):
        actions.append("state directory missing")
        # Will be auto-repaired in phase 4
    else:
        try:
            test_file = os.path.join(STATE_DIR, ".write-test")
            with open(test_file, "w") as f:
                f.write("test")
            os.unlink(test_file)
        except OSError:
            actions.append("state directory not writable")
            repairs += 1

    return checks, repairs, actions


def phase_smoke_tests():
    """Phase 2: Pipe valid JSON through hooks in isolated temp env."""
    checks = 0
    repairs = 0
    actions = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        smoke_state = os.path.join(tmp_dir, "state")
        os.makedirs(smoke_state)
        smoke_config = os.path.join(tmp_dir, "config.json")
        with open(smoke_config, "w") as f:
            json.dump(DEFAULT_CONFIG, f)

        smoke_env = os.environ.copy()
        smoke_env["TOKEN_GUARD_STATE_DIR"] = smoke_state
        smoke_env["TOKEN_GUARD_CONFIG_PATH"] = smoke_config

        # Task input for token-guard (tests the enforcement path, not just boot)
        valid_task_input = json.dumps({
            "tool_name": "Task",
            "tool_input": {
                "subagent_type": "general-purpose",
                "description": "refactor authentication across multiple services",
            },
            "session_id": "smoke-test",
        })
        # Read input for read-efficiency-guard
        valid_read_input = json.dumps({
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/test.py"},
            "session_id": "smoke-test",
        })

        # Smoke test token-guard.py
        tg_path = REQUIRED_HOOKS.get("token-guard.py", "")
        if os.path.isfile(tg_path):
            checks += 1
            try:
                result = subprocess.run(
                    ["python3", tg_path],
                    input=valid_task_input,
                    capture_output=True,
                    text=True,
                    env=smoke_env,
                    timeout=5,
                )
                if result.returncode not in (0, 2):
                    actions.append(f"token-guard smoke test failed (exit {result.returncode})")
                    repairs += 1
            except (subprocess.TimeoutExpired, OSError) as e:
                actions.append(f"token-guard smoke test error: {type(e).__name__}")
                repairs += 1

        # Smoke test read-efficiency-guard.py
        reg_path = REQUIRED_HOOKS.get("read-efficiency-guard.py", "")
        if os.path.isfile(reg_path):
            checks += 1
            try:
                result = subprocess.run(
                    ["python3", reg_path],
                    input=valid_read_input,
                    capture_output=True,
                    text=True,
                    env=smoke_env,
                    timeout=5,
                )
                if result.returncode != 0:
                    actions.append(f"read-efficiency-guard smoke test failed (exit {result.returncode})")
                    repairs += 1
            except (subprocess.TimeoutExpired, OSError) as e:
                actions.append(f"read-efficiency-guard smoke test error: {type(e).__name__}")
                repairs += 1

        # Syntax check health-check.sh
        hc_path = REQUIRED_HOOKS.get("health-check.sh", "")
        if os.path.isfile(hc_path):
            checks += 1
            try:
                result = subprocess.run(
                    ["bash", "-n", hc_path],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode != 0:
                    actions.append("health-check.sh syntax error")
                    repairs += 1
            except (subprocess.TimeoutExpired, OSError) as e:
                actions.append(f"health-check.sh syntax check error: {type(e).__name__}")
                repairs += 1

    return checks, repairs, actions


def phase_state_health():
    """Phase 3: Clean corrupted, orphaned, and stale files in state dir."""
    checks = 0
    repairs = 0
    actions = []

    if not os.path.isdir(STATE_DIR):
        return checks, repairs, actions

    now = time.time()

    for fname in os.listdir(STATE_DIR):
        fpath = os.path.join(STATE_DIR, fname)
        if not os.path.isfile(fpath):
            continue

        # Check for corrupted JSON state files
        if fname.endswith(".json") and fname != "audit.jsonl":
            checks += 1
            try:
                with open(fpath, "r") as f:
                    json.load(f)
            except (json.JSONDecodeError, ValueError):
                try:
                    os.unlink(fpath)
                    actions.append(f"deleted corrupted {fname}")
                    repairs += 1
                except OSError:
                    pass

        # Check for orphaned .tmp files (crashed atomic writes)
        elif fname.endswith(".tmp"):
            checks += 1
            try:
                os.unlink(fpath)
                actions.append(f"deleted orphaned {fname}")
                repairs += 1
            except OSError:
                pass

        # Check for stale .lock files (older than 5 minutes)
        elif fname.endswith(".lock"):
            checks += 1
            try:
                if now - os.stat(fpath).st_mtime > STALE_LOCK_SECONDS:
                    os.unlink(fpath)
                    actions.append(f"deleted stale {fname}")
                    repairs += 1
            except OSError:
                pass

    # Check audit.jsonl size — rotate to .1 backup (same strategy as token-guard)
    audit_path = os.path.join(STATE_DIR, "audit.jsonl")
    if os.path.isfile(audit_path):
        checks += 1
        try:
            with open(audit_path, "r") as f:
                line_count = sum(1 for _ in f)
            if line_count > AUDIT_MAX_LINES:
                backup = audit_path + ".1"
                if os.path.exists(backup):
                    os.unlink(backup)
                os.rename(audit_path, backup)
                actions.append(f"rotated audit.jsonl ({line_count} lines) to .1 backup")
                repairs += 1
        except OSError:
            pass

    return checks, repairs, actions


def phase_auto_repair():
    """Phase 4: Fix permissions, recreate missing dirs, regenerate config."""
    checks = 0
    repairs = 0
    actions = []

    # Missing state directory
    checks += 1
    if not os.path.isdir(STATE_DIR):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            actions.append("recreated state directory")
            repairs += 1
        except OSError:
            actions.append("FAILED to recreate state directory")
            repairs += 1

    # .sh files not executable
    for name, path in REQUIRED_HOOKS.items():
        if path.endswith(".sh") and os.path.isfile(path):
            checks += 1
            if not os.access(path, os.X_OK):
                try:
                    os.chmod(path, 0o755)
                    actions.append(f"chmod +x {name}")
                    repairs += 1
                except OSError:
                    pass

    # Corrupted or missing config — regenerate from defaults
    checks += 1
    needs_regen = False
    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
        if not isinstance(config, dict):
            needs_regen = True
    except (FileNotFoundError, json.JSONDecodeError):
        needs_regen = True

    if needs_regen:
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            actions.append("regenerated config from defaults")
            repairs += 1
        except OSError:
            actions.append("FAILED to regenerate config")
            repairs += 1

    return checks, repairs, actions


if __name__ == "__main__":
    main()
