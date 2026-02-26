#!/usr/bin/env python3
"""
Budget Guard — PreToolUse hook that enforces real-time spending limits.

Fires before EVERY tool call (matcher: ".*"), providing a hot enforcement layer
that blocks or warns when budget thresholds are exceeded.

Supports two plan modes:
  - "max": Claude Code Max plan ($200/mo subscription). Tracks against a monthly
    cap; per-tool blocking prevents runaway usage within the billing cycle.
  - "api": API key billing. Per-token cost; stricter daily tracking appropriate.

Fast path: reads ~/.claude/cost/cache.json (already written by cost_runtime statusline).
  - If cache mtime < ttl seconds: decision in <1ms (one JSON file read)
  - If cache is stale: subprocess call to cost_runtime.py to refresh (amortized)

Config: ~/.claude/hooks/token-guard-config.json → "budget_guard" section
State:  ~/.claude/cost/cache.json (read-only from this hook)

Exit codes: 0 = allow, 2 = block
Fail-open: any error → exit(0)
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

HOOKS_DIR = Path(__file__).parent
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
COST_DIR = Path.home() / ".claude" / "cost"
CONFIG_PATH = os.environ.get(
    "TOKEN_GUARD_CONFIG_PATH",
    str(Path.home() / ".claude" / "hooks" / "token-guard-config.json"),
)
CACHE_FILE = COST_DIR / "cache.json"

# How long a subprocess refresh may run before we give up and fail-open
REFRESH_TIMEOUT_SECONDS = 4

# Cooldown between refresh attempts to avoid subprocess pile-ups
REFRESH_COOLDOWN_FILE = "/tmp/budget-guard-refresh.ts"
REFRESH_COOLDOWN_SECONDS = 30


def load_config() -> dict:
    """Load budget_guard section from token-guard-config.json. Always returns a dict."""
    defaults = {
        "enabled": True,
        "plan_type": "max",  # "max" | "api"
        "monthly_usd": 200.0,  # hard cap for "max" plan
        "daily_usd": 0.0,  # 0 = use budgets.json global.dailyUSD
        "cache_ttl_seconds": 60,
        "fail_open": True,
        "block_on_critical": True,
        "warn_on_warning": True,
    }
    try:
        raw = json.loads(Path(CONFIG_PATH).read_text())
        section = raw.get("budget_guard") or {}
        return {**defaults, **section}
    except Exception:
        return defaults


def _refresh_cooldown_ok() -> bool:
    """Return True if enough time has passed since last refresh attempt."""
    try:
        last = float(Path(REFRESH_COOLDOWN_FILE).read_text().strip())
        return (time.time() - last) >= REFRESH_COOLDOWN_SECONDS
    except Exception:
        return True


def refresh_cache(config: dict) -> None:
    """Shell out to cost_runtime.py to write a fresh cache.json. Non-fatal on failure."""
    if not _refresh_cooldown_ok():
        return
    try:
        Path(REFRESH_COOLDOWN_FILE).write_text(str(time.time()))
    except Exception:
        pass
    cost_runtime = SCRIPTS_DIR / "cost_runtime.py"
    if not cost_runtime.exists():
        return
    try:
        subprocess.run(
            [sys.executable, str(cost_runtime), "statusline", "--json"],
            timeout=REFRESH_TIMEOUT_SECONDS,
            capture_output=True,
        )
    except Exception:
        pass  # fail-open — stale cache is fine


def fast_path_budget(config: dict) -> Tuple[str, Optional[float]]:
    """
    Return (level, pct) by reading cache.json.

    level: "ok" | "warning" | "critical" | "none"
    pct: percentage of limit used (0-100+), or None if unavailable.

    Falls back to (none, None) on any error so the hook fails open.
    """
    try:
        stat = CACHE_FILE.stat()
        ttl = float(config.get("cache_ttl_seconds", 60))
        if time.time() - stat.st_mtime > ttl:
            refresh_cache(config)
            # Re-stat after refresh attempt
            try:
                stat = CACHE_FILE.stat()
            except Exception:
                return "none", None

        data = json.loads(CACHE_FILE.read_text())
        windows = data.get("windows") or {}

        # Prefer today window; fall back to active_block
        today = windows.get("today") or windows.get("active_block") or {}
        budget = today.get("budget") or {}
        level = budget.get("level", "none")
        pct = budget.get("pct")

        # For "max" plan with a monthly_usd override, check month window too
        plan_type = config.get("plan_type", "max")
        if plan_type == "max":
            month = windows.get("month") or {}
            month_budget = month.get("budget") or {}
            monthly_usd = float(config.get("monthly_usd", 200.0))
            month_spent = (month.get("totals") or {}).get("totalUSD")
            if month_spent is not None and monthly_usd > 0:
                month_pct = (month_spent / monthly_usd) * 100.0
                # Use whichever is more severe
                month_level = _pct_to_level(month_pct)
                if _severity(month_level) > _severity(level):
                    level = month_level
                    pct = round(month_pct, 2)

        return level, pct if isinstance(pct, (int, float)) else None

    except Exception:
        return "none", None  # fail-open


def _pct_to_level(pct: float) -> str:
    """Convert a percentage to a severity level using standard thresholds."""
    if pct >= 95.0:
        return "critical"
    if pct >= 80.0:
        return "warning"
    return "ok"


def _severity(level: str) -> int:
    """Return numeric severity for comparison. Higher = more severe."""
    return {"none": 0, "ok": 1, "warning": 2, "critical": 3}.get(level, 0)


def main() -> None:
    try:
        from circuit_breaker import check_circuit, record_success, record_failure
        if not check_circuit("budget-guard"):
            sys.exit(0)
    except ImportError:
        pass

    config = load_config()

    if not config.get("enabled", True):
        sys.exit(0)

    level, pct = fast_path_budget(config)
    pct_str = f" ({pct:.0f}%)" if isinstance(pct, (int, float)) else ""

    if level == "critical" and config.get("block_on_critical", True):
        period = "monthly" if config.get("plan_type") == "max" else "daily"
        print(
            f"BUDGET EXCEEDED{pct_str}: {period.capitalize()} limit reached. "
            f"Run /cost to review. To override: set block_on_critical=false in "
            f"token-guard-config.json budget_guard section.",
            file=sys.stderr,
        )
        sys.exit(2)

    if level == "warning" and config.get("warn_on_warning", True):
        print(f"BUDGET WARNING{pct_str}: Approaching spending limit.", file=sys.stderr)
        # Non-blocking — continue

    try:
        record_success("budget-guard")
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            from circuit_breaker import record_failure
            record_failure("budget-guard")
        except Exception:
            pass
        sys.exit(0)  # fail-open
