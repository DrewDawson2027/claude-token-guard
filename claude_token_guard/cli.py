#!/usr/bin/env python3
"""CLI for Claude Token Guard: install, report, health, version, status, uninstall."""

import json
import os
import shutil
import subprocess
import sys

from claude_token_guard import __version__

HOOKS_DIR = os.path.expanduser("~/.claude/hooks")
SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
VERSION_FILE = os.path.join(HOOKS_DIR, ".version")

# Files to install (relative to package data or source root)
HOOK_FILES = [
    "token-guard.py",
    "read-efficiency-guard.py",
    "hook_utils.py",
    "self-heal.py",
    "health-check.sh",
    "token-guard-config.json",
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

    # Remove version stamp
    if os.path.isfile(VERSION_FILE):
        os.unlink(VERSION_FILE)

    # Unpatch settings.json
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r") as f:
                settings = json.load(f)
            hooks = settings.get("hooks", {})
            for key in ["PreToolUse", "SessionStart"]:
                if key in hooks:
                    hooks[key] = [
                        e for e in hooks[key]
                        if not any(h in str(e) for h in ["token-guard", "read-efficiency-guard", "self-heal"])
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
        print("\nUpdate available! Run: claude-token-guard install")


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
        print("  report     Show token usage analytics")
        print("  health     Run self-heal diagnostics")
        print("  version    Show version")
        sys.exit(0)

    cmd = sys.argv[1].lower()
    commands = {
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
        "report": cmd_report,
        "health": cmd_health,
        "version": cmd_version,
    }

    if cmd in commands:
        commands[cmd]()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Available: install, uninstall, status, report, health, version", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
