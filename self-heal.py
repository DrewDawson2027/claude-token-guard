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

import hashlib
import json
import os
import re
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
        "schema_version": 2,
        "max_agents": 5, "parallel_window_seconds": 30, "global_cooldown_seconds": 5,
        "max_per_subagent_type": 1, "state_ttl_hours": 24, "audit_log": True,
        "failure_mode": "fail_open", "sanitize_session_ids": True, "normalize_paths": True,
        "fault_audit": True, "max_string_field_length": 512, "metrics_correlation_window_seconds": 15,
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
    "coder": ["build-mode.md", "debug-mode.md", "review-mode.md", "refactor-mode.md"],
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


def phase_data_quality():
    """Phase 4c: Validate v2 data quality in audit and metrics logs.

    Advisory warnings only — never blocks session start. Identifies
    records that lack v2 schema markers or have known data gaps.
    """
    checks = 0
    repairs = 0
    actions = []

    # Check audit entries for schema_version and malformed lines
    audit_path = os.path.join(STATE_DIR, "audit.jsonl")
    if os.path.isfile(audit_path):
        checks += 1
        try:
            with open(audit_path, "r") as f:
                lines = f.readlines()
            sample_size = min(20, len(lines))
            tail = lines[-sample_size:] if sample_size > 0 else []
            v2_count = 0
            malformed = 0
            total_parsed = 0
            for line in tail:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    total_parsed += 1
                    if entry.get("schema_version") == 2:
                        v2_count += 1
                except json.JSONDecodeError:
                    malformed += 1
            if v2_count == 0 and total_parsed > 0:
                actions.append("audit: no v2 records in recent entries")
                print(
                    "WARNING: No schema_version=2 audit records found in recent entries. "
                    "New records should include schema_version.",
                    file=sys.stderr,
                )
            total_sampled = total_parsed + malformed
            if total_sampled > 0 and malformed / total_sampled > 0.5:
                actions.append(f"audit: {malformed}/{total_sampled} malformed JSON lines")
        except OSError:
            pass

    # Check metrics entries for empty agent_type, zero-token, and malformed lines
    metrics_path = os.path.join(STATE_DIR, "agent-metrics.jsonl")
    if os.path.isfile(metrics_path):
        checks += 1
        try:
            with open(metrics_path, "r") as f:
                lines = f.readlines()
            sample_size = min(20, len(lines))
            tail = lines[-sample_size:] if sample_size > 0 else []
            empty_type = 0
            zero_tok = 0
            no_schema = 0
            malformed = 0
            total_parsed = 0
            for line in tail:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    total_parsed += 1
                    at = entry.get("agent_type", "")
                    if not at or at == "unknown":
                        empty_type += 1
                    if not entry.get("schema_version"):
                        no_schema += 1
                    if (entry.get("event") == "agent_completed"
                            and entry.get("input_tokens", 0) == 0
                            and entry.get("output_tokens", 0) == 0):
                        zero_tok += 1
                except json.JSONDecodeError:
                    malformed += 1
            if empty_type > 0:
                actions.append(f"metrics: {empty_type}/{total_parsed} recent entries have empty agent_type")
            total_sampled = total_parsed + malformed
            if total_sampled > 0 and malformed / total_sampled > 0.5:
                actions.append(f"metrics: {malformed}/{total_sampled} malformed JSON lines")
            if total_parsed > 0 and no_schema / total_parsed > 0.5:
                actions.append(f"metrics: {no_schema}/{total_parsed} missing schema_version")
        except OSError:
            pass

    return checks, repairs, actions


_DRIFT_TRACKED_FILES = [
    "token-guard.py", "read-efficiency-guard.py", "hook_utils.py",
    "guard_contracts.py", "guard_normalize.py", "guard_events.py",
    "agent-lifecycle.sh", "agent-metrics.py", "self-heal.py",
]
_CHECKSUMS_FILE = os.path.join(STATE_DIR, "hook-checksums.json")


def phase_runtime_drift():
    """Phase 5: Detect changes to hook files between sessions (advisory only)."""
    checks = 0
    repairs = 0
    actions = []

    # Compute current checksums
    current = {}
    for fname in _DRIFT_TRACKED_FILES:
        fpath = os.path.join(HOOKS_DIR, fname)
        checks += 1
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, "rb") as f:
                current[fname] = hashlib.sha256(f.read()).hexdigest()[:16]
        except OSError:
            pass

    # Compare against stored checksums
    stored = {}
    try:
        with open(_CHECKSUMS_FILE, "r") as f:
            stored = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass  # First run or corrupted — will create fresh

    if stored:
        for fname, cur_hash in current.items():
            prev_hash = stored.get(fname)
            if prev_hash and prev_hash != cur_hash:
                actions.append(f"drift: {fname} changed since last session")
        for fname in stored:
            if fname not in current and fname in _DRIFT_TRACKED_FILES:
                actions.append(f"drift: {fname} was removed")

    # Save current checksums for next session
    try:
        with open(_CHECKSUMS_FILE, "w") as f:
            json.dump(current, f, indent=2)
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

    # Phase 4c: v2 data quality validation
    c, r, a = phase_data_quality()
    checks += c
    repairs += r
    actions.extend(a)

    # Phase 5: Runtime drift detection
    try:
        c, r, a = phase_runtime_drift()
        checks += c
        repairs += r
        actions.extend(a)
    except Exception:
        pass  # Drift detection must never block session start

    # Phase 6: Report
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
        else:
            try:
                schema_version = int(config.get("schema_version", 1) or 1)
            except (TypeError, ValueError):
                schema_version = 1
            if schema_version < 2:
                actions.append("config schema_version < 2 (upgrade recommended)")
            for required in ("failure_mode", "sanitize_session_ids", "normalize_paths"):
                if required not in config:
                    actions.append(f"config missing {required} key")
            # Auto-repair missing v2-required fields from defaults
            v2_fields = ("fault_audit", "max_string_field_length", "metrics_correlation_window_seconds")
            for field in v2_fields:
                if field not in config:
                    config[field] = DEFAULT_CONFIG.get(field)
                    actions.append(f"config: added missing {field}={config[field]}")
                    repairs += 1
            if repairs > 0:
                try:
                    with open(CONFIG_PATH, "w") as f:
                        json.dump(config, f, indent=2)
                except OSError:
                    pass
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

        # Check state file names match expected session_key pattern
        if fname.endswith(".json") and not fname.endswith(".jsonl"):
            checks += 1
            base = fname[:-5]  # strip .json
            if base.endswith("-reads"):
                base = base[:-6]  # strip -reads
            if not re.match(r'^[a-zA-Z0-9_-]{1,16}$', base) and base not in ("hook-checksums", "token-guard-config"):
                actions.append(f"unusual state filename: {fname}")

        # Check for corrupted JSON state files
        if fname.endswith(".json") and fname != "audit.jsonl":
            checks += 1
            try:
                with open(fpath, "r") as f:
                    parsed = json.load(f)
                # Validate session_key if present in v2 state
                if isinstance(parsed, dict) and "session_key" in parsed:
                    sk = str(parsed.get("session_key", ""))
                    if "/" in sk or "\\" in sk or ".." in sk:
                        actions.append(f"invalid session_key in {fname}")
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
