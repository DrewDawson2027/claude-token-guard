#!/usr/bin/env python3
"""CLI for Claude Token Guard: install, report, health, version, status, uninstall."""

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys

from claude_token_guard import __version__

HOOKS_DIR = os.path.expanduser("~/.claude/hooks")
SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
VERSION_FILE = os.path.join(HOOKS_DIR, ".version")
MANIFEST_FILE = os.path.join(HOOKS_DIR, ".manifest.json")

# Files to install (relative to package data or source root)
HOOK_FILES = [
    "token-guard.py",
    "read-efficiency-guard.py",
    "hook_utils.py",
    "self-heal.py",
    "health-check.sh",
    "token-guard-config.json",
    "guard_contracts.py",
    "guard_normalize.py",
    "guard_events.py",
    "agent-lifecycle.sh",
    "agent-metrics.py",
]


def _find_source_dir():
    """Find the directory containing the hook source files."""
    candidates = [
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),  # repo root
        os.path.join(sys.prefix, "share", "claude-token-guard"),  # installed data
    ]
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, "token-guard.py")):
            return candidate
    return None


def _get_installed_version():
    """Read the installed version from the .version file, or None."""
    try:
        with open(VERSION_FILE, "r") as f:
            return f.read().strip()
    except (FileNotFoundError, OSError):
        return None


def _sha256(path):
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_manifest():
    """Build and write install manifest with checksums to MANIFEST_FILE."""
    files = {}
    for fname in HOOK_FILES:
        fpath = os.path.join(HOOKS_DIR, fname)
        if os.path.isfile(fpath):
            files[fname] = {
                "sha256": _sha256(fpath),
                "size": os.path.getsize(fpath),
            }
    manifest = {
        "version": __version__,
        "installed_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": files,
    }
    with open(MANIFEST_FILE, "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def cmd_install():
    """Copy hooks to ~/.claude/hooks/ and patch settings.json."""
    force = "--force" in sys.argv

    # Check if already up to date
    installed_ver = _get_installed_version()
    if installed_ver == __version__ and not force:
        print(f"Token Guard {__version__} already installed and up to date.")
        print("Use --force to reinstall.")
        return

    source_dir = _find_source_dir()
    if not source_dir:
        print("ERROR: Cannot find hook source files.", file=sys.stderr)
        print("If installed via pip, try reinstalling.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(HOOKS_DIR, exist_ok=True)

    installed = []
    for fname in HOOK_FILES:
        src = os.path.join(source_dir, fname)
        dst = os.path.join(HOOKS_DIR, fname)
        if os.path.isfile(src):
            shutil.copy2(src, dst)
            if fname.endswith(".sh"):
                os.chmod(dst, 0o755)
            installed.append(fname)

    # Write version stamp
    with open(VERSION_FILE, "w") as f:
        f.write(__version__)

    # Create session-state directory with restricted permissions
    state_dir = os.path.join(HOOKS_DIR, "session-state")
    os.makedirs(state_dir, exist_ok=True)
    try:
        os.chmod(state_dir, 0o700)
    except OSError:
        pass

    # Patch settings.json with hook configuration
    _patch_settings()

    # Write install manifest with checksums
    _build_manifest()

    action = "Updated" if installed_ver else "Installed"
    print(f"{action} {len(installed)} files to {HOOKS_DIR}/ (v{__version__})")
    for f in installed:
        print(f"  + {f}")
    print(f"\nState directory: {state_dir}")
    print("Settings patched: ~/.claude/settings.json")
    print("\nToken Guard is active. Restart Claude Code to apply.")


def _patch_settings():
    """Add hook entries to ~/.claude/settings.json."""
    settings = {}
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    hooks = settings.setdefault("hooks", {})
    pre_tool = hooks.setdefault("PreToolUse", [])
    session_start = hooks.setdefault("SessionStart", [])

    # Add token-guard if not present
    tg_entry = {
        "type": "command",
        "command": f"python3 {HOOKS_DIR}/token-guard.py",
    }
    if not any("token-guard" in str(e) for e in pre_tool):
        pre_tool.append(tg_entry)

    # Add read-efficiency-guard if not present
    reg_entry = {
        "type": "command",
        "command": f"python3 {HOOKS_DIR}/read-efficiency-guard.py",
    }
    if not any("read-efficiency-guard" in str(e) for e in pre_tool):
        pre_tool.append(reg_entry)

    # Add self-heal if not present
    sh_entry = {
        "type": "command",
        "command": f"python3 {HOOKS_DIR}/self-heal.py",
    }
    if not any("self-heal" in str(e) for e in session_start):
        session_start.append(sh_entry)

    # Add agent lifecycle hooks (SubagentStart/SubagentStop)
    subagent_start = hooks.setdefault("SubagentStart", [])
    subagent_stop = hooks.setdefault("SubagentStop", [])

    lc_entry = {
        "type": "command",
        "command": f"bash {HOOKS_DIR}/agent-lifecycle.sh",
    }
    if not any("agent-lifecycle" in str(e) for e in subagent_start):
        subagent_start.append(lc_entry)
    if not any("agent-lifecycle" in str(e) for e in subagent_stop):
        subagent_stop.append(dict(lc_entry))

    am_entry = {
        "type": "command",
        "command": f"python3 {HOOKS_DIR}/agent-metrics.py",
    }
    if not any("agent-metrics" in str(e) for e in subagent_stop):
        subagent_stop.append(am_entry)

    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)


def cmd_uninstall():
    """Remove hooks and unpatch settings.json."""
    removed = []
    for fname in HOOK_FILES:
        path = os.path.join(HOOKS_DIR, fname)
        if os.path.isfile(path):
            os.unlink(path)
            removed.append(fname)

    # Remove version stamp and manifest
    for cleanup_file in [VERSION_FILE, MANIFEST_FILE]:
        if os.path.isfile(cleanup_file):
            os.unlink(cleanup_file)

    # Unpatch settings.json
    _hook_markers = ["token-guard", "read-efficiency-guard", "self-heal",
                     "agent-lifecycle", "agent-metrics"]
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r") as f:
                settings = json.load(f)
            hooks = settings.get("hooks", {})
            for key in ["PreToolUse", "SessionStart", "SubagentStart", "SubagentStop"]:
                if key in hooks:
                    hooks[key] = [
                        e for e in hooks[key]
                        if not any(h in str(e) for h in _hook_markers)
                    ]
            with open(SETTINGS_PATH, "w") as f:
                json.dump(settings, f, indent=2)
        except (json.JSONDecodeError, OSError):
            pass

    print(f"Removed {len(removed)} files from {HOOKS_DIR}/")
    print("Settings unpatched. Restart Claude Code to apply.")


def cmd_report():
    """Run token-guard.py --report for analytics."""
    tg_path = os.path.join(HOOKS_DIR, "token-guard.py")
    if not os.path.isfile(tg_path):
        print("Token Guard not installed. Run: claude-token-guard install", file=sys.stderr)
        sys.exit(1)
    subprocess.run(["python3", tg_path, "--report"])


def cmd_health():
    """Run self-heal.py and report status."""
    sh_path = os.path.join(HOOKS_DIR, "self-heal.py")
    if not os.path.isfile(sh_path):
        print("Self-heal not installed. Run: claude-token-guard install", file=sys.stderr)
        sys.exit(1)
    subprocess.run(["python3", sh_path])


def cmd_status():
    """Check installed version vs package version."""
    installed_ver = _get_installed_version()
    if not installed_ver:
        print("Token Guard is not installed.")
        print(f"Package version: {__version__}")
        print("\nRun: claude-token-guard install")
        return

    print(f"Installed version: {installed_ver}")
    print(f"Package version:   {__version__}")

    if installed_ver == __version__:
        print("\nUp to date.")
    else:
        print(f"\nUpdate available! Run: claude-token-guard install")


def cmd_verify():
    """Post-install verification: checksums, settings, smoke test."""
    ok = True
    checks = 0

    # 1. Check all HOOK_FILES exist
    print("Checking installed files...")
    for fname in HOOK_FILES:
        checks += 1
        fpath = os.path.join(HOOKS_DIR, fname)
        if os.path.isfile(fpath):
            print(f"  OK  {fname}")
        else:
            print(f"  MISSING  {fname}")
            ok = False

    # 2. Compare checksums against manifest
    print("\nChecking manifest checksums...")
    if os.path.isfile(MANIFEST_FILE):
        try:
            with open(MANIFEST_FILE, "r") as f:
                manifest = json.load(f)
            for fname, meta in manifest.get("files", {}).items():
                checks += 1
                fpath = os.path.join(HOOKS_DIR, fname)
                if not os.path.isfile(fpath):
                    print(f"  MISSING  {fname}")
                    ok = False
                    continue
                current = _sha256(fpath)
                if current == meta["sha256"]:
                    print(f"  OK  {fname}")
                else:
                    print(f"  CHANGED  {fname}")
                    ok = False
        except (json.JSONDecodeError, OSError, KeyError) as e:
            print(f"  ERROR reading manifest: {e}")
            ok = False
    else:
        print("  No manifest found. Run: claude-token-guard install")
        ok = False

    # 3. Verify settings.json has hook registrations
    print("\nChecking settings.json registrations...")
    checks += 1
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r") as f:
                settings = json.load(f)
            hooks = settings.get("hooks", {})
            for key, marker in [("PreToolUse", "token-guard"), ("PreToolUse", "read-efficiency-guard"),
                                ("SessionStart", "self-heal")]:
                checks += 1
                entries = hooks.get(key, [])
                if any(marker in str(e) for e in entries):
                    print(f"  OK  {key}/{marker}")
                else:
                    print(f"  MISSING  {key}/{marker}")
                    ok = False
        except (json.JSONDecodeError, OSError):
            print("  ERROR reading settings.json")
            ok = False
    else:
        print("  settings.json not found")
        ok = False

    # 4. Run self-heal and check exit code
    print("\nRunning self-heal smoke test...")
    checks += 1
    sh_path = os.path.join(HOOKS_DIR, "self-heal.py")
    if os.path.isfile(sh_path):
        result = subprocess.run(["python3", sh_path], capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            print(f"  OK  self-heal exited 0")
        else:
            print(f"  FAIL  self-heal exited {result.returncode}")
            ok = False
    else:
        print("  SKIP  self-heal.py not found")

    # 5. Pipe test input through token-guard
    print("\nRunning token-guard smoke test...")
    checks += 1
    tg_path = os.path.join(HOOKS_DIR, "token-guard.py")
    if os.path.isfile(tg_path):
        test_input = json.dumps({
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/test"},
            "session_id": "verify-smoke",
        })
        result = subprocess.run(
            ["python3", tg_path], input=test_input,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            print(f"  OK  token-guard passthrough exited 0")
        else:
            print(f"  FAIL  token-guard exited {result.returncode}")
            ok = False
    else:
        print("  SKIP  token-guard.py not found")

    # Summary
    status = "PASS" if ok else "FAIL"
    print(f"\nVerification: {status} ({checks} checks)")
    if not ok:
        sys.exit(1)


def cmd_drift():
    """Compare installed files against manifest checksums."""
    if not os.path.isfile(MANIFEST_FILE):
        print("No manifest found. Run: claude-token-guard install")
        sys.exit(1)

    try:
        with open(MANIFEST_FILE, "r") as f:
            manifest = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR reading manifest: {e}", file=sys.stderr)
        sys.exit(1)

    manifest_files = manifest.get("files", {})
    changed = []
    missing = []
    extra = []

    # Check manifest entries against current files
    for fname, meta in manifest_files.items():
        fpath = os.path.join(HOOKS_DIR, fname)
        if not os.path.isfile(fpath):
            missing.append(fname)
        elif _sha256(fpath) != meta["sha256"]:
            changed.append(fname)

    # Check for extra hook files not in manifest
    for fname in HOOK_FILES:
        if fname not in manifest_files:
            fpath = os.path.join(HOOKS_DIR, fname)
            if os.path.isfile(fpath):
                extra.append(fname)

    print(f"Manifest version: {manifest.get('version', 'unknown')}")
    print(f"Installed at: {manifest.get('installed_at', 'unknown')}")
    print(f"Tracked files: {len(manifest_files)}")

    if not changed and not missing and not extra:
        print("\nNo drift detected. All files match manifest.")
    else:
        if changed:
            print(f"\nChanged ({len(changed)}):")
            for f in changed:
                print(f"  ~ {f}")
        if missing:
            print(f"\nMissing ({len(missing)}):")
            for f in missing:
                print(f"  - {f}")
        if extra:
            print(f"\nExtra ({len(extra)}):")
            for f in extra:
                print(f"  + {f}")
        print(f"\nDrift detected. Run: claude-token-guard install --force")
        sys.exit(1)


def cmd_benchmark():
    """Run latency benchmarks against hook scripts."""
    import statistics
    import tempfile
    import time as _time

    # Load benchmark inputs
    fixtures_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tests", "fixtures", "benchmark_inputs.json",
    )
    if os.path.isfile(fixtures_path):
        with open(fixtures_path, "r") as f:
            benchmarks = json.load(f)
    else:
        # Fallback: simple passthrough test
        benchmarks = [{
            "name": "passthrough",
            "description": "Non-Task tool call (passthrough)",
            "input": {"tool_name": "Grep", "tool_input": {"pattern": "x"}, "session_id": "bench"},
            "expected_exit": 0,
        }]

    tg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "token-guard.py",
    )
    reg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "read-efficiency-guard.py",
    )

    iterations = 20  # Enough for stable percentiles without being slow
    all_latencies = []

    print(f"\n{'='*50}")
    print(f"  CLAUDE TOKEN GUARD BENCHMARK")
    print(f"{'='*50}")
    print(f"Iterations per input: {iterations}")
    print()

    for bench in benchmarks:
        name = bench["name"]
        payload = json.dumps(bench["input"])
        tool_name = bench["input"].get("tool_name", "")

        # Pick the right script
        if tool_name == "Read":
            script = reg_path
        else:
            script = tg_path

        if not os.path.isfile(script):
            print(f"  SKIP  {name} — script not found: {script}")
            continue

        latencies = []
        for i in range(iterations):
            with tempfile.TemporaryDirectory() as tmp_dir:
                state_dir = os.path.join(tmp_dir, "session-state")
                os.makedirs(state_dir)
                config_path = os.path.join(tmp_dir, "config.json")
                with open(config_path, "w") as cf:
                    json.dump({"max_agents": 50, "global_cooldown_seconds": 0,
                               "parallel_window_seconds": 0, "max_per_subagent_type": 50,
                               "audit_log": False}, cf)

                # Pre-seed if needed
                if "pre_seed" in bench:
                    env = os.environ.copy()
                    env["TOKEN_GUARD_STATE_DIR"] = state_dir
                    env["TOKEN_GUARD_CONFIG_PATH"] = config_path
                    subprocess.run(
                        ["python3", script], input=json.dumps(bench["pre_seed"]),
                        capture_output=True, text=True, env=env, timeout=10,
                    )

                env = os.environ.copy()
                env["TOKEN_GUARD_STATE_DIR"] = state_dir
                env["TOKEN_GUARD_CONFIG_PATH"] = config_path

                t0 = _time.monotonic()
                subprocess.run(
                    ["python3", script], input=payload,
                    capture_output=True, text=True, env=env, timeout=10,
                )
                elapsed_ms = (_time.monotonic() - t0) * 1000
                latencies.append(elapsed_ms)

        latencies.sort()
        all_latencies.extend(latencies)
        p50 = statistics.median(latencies)
        p95 = latencies[int(len(latencies) * 0.95)]
        p99 = latencies[int(len(latencies) * 0.99)]

        print(f"  {name}:")
        print(f"    min={latencies[0]:.0f}ms  p50={p50:.0f}ms  p95={p95:.0f}ms  "
              f"p99={p99:.0f}ms  max={latencies[-1]:.0f}ms")

    # Overall summary
    if all_latencies:
        all_latencies.sort()
        overall_p95 = all_latencies[int(len(all_latencies) * 0.95)]
        budget = 500  # ms — subprocess overhead budget
        status = "PASS" if overall_p95 <= budget else "OVER BUDGET"
        print(f"\n  Overall p95: {overall_p95:.0f}ms (budget: {budget}ms) — {status}")

    print(f"{'='*50}\n")


def cmd_version():
    """Print version."""
    print(f"claude-token-guard {__version__}")


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print(f"claude-token-guard {__version__}")
        print("\nUsage: claude-token-guard <command>")
        print("\nCommands:")
        print("  install    Copy hooks to ~/.claude/hooks/ and patch settings")
        print("  uninstall  Remove hooks and unpatch settings")
        print("  status     Check installed vs package version")
        print("  verify     Post-install verification (checksums + smoke tests)")
        print("  drift      Compare installed files against manifest")
        print("  benchmark  Run latency benchmarks against hook scripts")
        print("  report     Show token usage analytics")
        print("  health     Run self-heal diagnostics")
        print("  version    Show version")
        sys.exit(0)

    cmd = sys.argv[1].lower()
    commands = {
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
        "verify": cmd_verify,
        "drift": cmd_drift,
        "benchmark": cmd_benchmark,
        "report": cmd_report,
        "health": cmd_health,
        "version": cmd_version,
    }

    if cmd in commands:
        commands[cmd]()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Available: install, uninstall, status, verify, drift, benchmark, report, health, version", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
