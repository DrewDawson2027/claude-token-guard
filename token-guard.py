#!/usr/bin/env python3
"""
Token Guard — PreToolUse hook that enforces agent spawning limits for Claude Code.

Part of the Token Management System (three-layer architecture):
  Layer 1: Prompt hook in settings.json (decision-time prevention)
  Layer 2: This hook — mechanical enforcement that hard-blocks violations
  Layer 3: Read efficiency guard — prevents wasteful read patterns

How it works:
  Claude Code calls this script before every Task tool invocation.
  Exit 0 = allow the agent spawn. Exit 2 = block it with feedback.

Rules enforced:
  1. One-per-session types (Explore, Plan, etc.) — max 1 of each, ever
  2. General type cap — max N of any single subagent_type (default 1)
  3. Session agent cap — max total agents per session (default 5)
  4. Parallel window — no same-type spawns within 30s (catches same-turn dupes)
  5. Necessity scoring — blocks tasks that should use direct tools
  6. Type-switching detection — catches re-attempts with different agent type
  7. Global cooldown — prevents rapid-fire spawns of any type

Special handling:
  - Resume detection: Resuming existing agents always allowed
  - Team detection: Team spawns bypass rules 1-7 but count toward session cap
  - First-spawn advisory: Non-blocking reminder on first agent
  - Model cost advisory: Non-blocking warning when opus requested

Config: ~/.claude/hooks/token-guard-config.json
State:  ~/.claude/hooks/session-state/{session_id}.json
Audit:  ~/.claude/hooks/session-state/audit.jsonl

Cross-platform: Works on macOS, Linux, and Windows (portable file locking).

Usage:
  python3 token-guard.py           # Normal hook mode (reads JSON from stdin)
  python3 token-guard.py --report  # Print cross-session analytics
  python3 token-guard.py --usage   # Print shareable usage summary
"""

import difflib
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Tuple

# Shared infrastructure — locking, state, audit, config
from hook_utils import (
    DEFAULT_CONFIG,
    lock,
    unlock,
    load_json_state,
    save_json_state,
    locked_append,
    read_jsonl_fault_tolerant,
)

STATE_DIR = os.environ.get("TOKEN_GUARD_STATE_DIR", os.path.expanduser("~/.claude/hooks/session-state"))
CONFIG_PATH = os.environ.get("TOKEN_GUARD_CONFIG_PATH", os.path.expanduser("~/.claude/hooks/token-guard-config.json"))
AUDIT_LOG = os.path.join(STATE_DIR, "audit.jsonl")

BLOCKED_ATTEMPTS_TTL = 300  # Prune blocked attempts older than 5 minutes

## DEFAULT_CONFIG is imported from hook_utils (single source of truth)

# Patterns that indicate a task should use direct tools instead of an agent
DIRECT_TOOL_PATTERNS = [
    (r'\b(search|find|grep|look for|locate)\b.*\b(file|function|class|import|usage|pattern)\b',
     "Use Grep to search for code patterns directly.",
     "search_grep"),
    (r'\bread\b.*\b(file|config|settings|code)\b',
     "Use Read tool to read files directly.",
     "read_file"),
    (r'\b(check|verify|confirm)\b.*\b(exists?|status|version|content)\b',
     "Use Grep or Bash to check directly.",
     "check_verify"),
    (r'\b(edit|fix|change|update|modify)\b.*\b(line|bug|typo|value)\b',
     "Use Read + Edit to fix directly.",
     "edit_fix"),
    (r'\b(analyze|look at|examine|inspect)\b.*\b(file|code|function|method)\b',
     "Use Read to analyze the file directly.",
     "analyze_inspect"),
    (r'\bwhat does\b.*\b(function|method|class|file|module)\b',
     "Use Read to understand the code directly.",
     "what_does"),
    (r'\b(list|show|display)\b.*\b(files|directories|imports|dependencies)\b',
     "Use Grep or Glob to list matches directly.",
     "list_show"),
    (r'\b(count|how many)\b.*\b(files|functions|classes|tests|lines)\b',
     "Use Grep with count mode to count directly.",
     "count_how_many"),
    (r'\b(compare|diff)\b.*\b(files?|versions?)\b',
     "Use Read on both files or Bash diff directly.",
     "compare_diff"),
    (r'\b(run|execute|test)\b.*\b(script|command|test)\b',
     "Use Bash to run directly.",
     "run_execute"),
]

# Canonical "direct tool" task descriptions for fuzzy matching.
# Catches paraphrases that regex misses (e.g., "find where X is called" vs "search for X").
# Uses difflib.SequenceMatcher — zero dependencies, same technique as type-switching detection.
# Format: (canonical_description, pattern_name, suggestion)
CANONICAL_DIRECT_TASKS = [
    # search/grep tasks
    ("search for function in the codebase", "search_grep",
     "Use Grep to search for code patterns directly."),
    ("find where this function is called", "search_grep",
     "Use Grep to search for code patterns directly."),
    ("locate the definition of this class", "search_grep",
     "Use Grep to search for code patterns directly."),
    ("grep for all usages of this import", "search_grep",
     "Use Grep to search for code patterns directly."),
    ("find all references to this variable", "search_grep",
     "Use Grep to search for code patterns directly."),
    # read/inspect tasks
    ("read the config file and understand it", "read_file",
     "Use Read tool to read files directly."),
    ("look at the contents of this module", "read_file",
     "Use Read tool to read files directly."),
    ("open the file and check what it does", "read_file",
     "Use Read tool to read files directly."),
    ("examine this source code file", "read_file",
     "Use Read tool to read files directly."),
    ("read through the settings and configuration", "read_file",
     "Use Read tool to read files directly."),
    # check/verify tasks
    ("check if this file exists and verify contents", "check_verify",
     "Use Grep or Bash to check directly."),
    ("verify the version number in package json", "check_verify",
     "Use Grep or Bash to check directly."),
    ("confirm that the dependency is installed", "check_verify",
     "Use Grep or Bash to check directly."),
    ("check the status of the git repository", "check_verify",
     "Use Grep or Bash to check directly."),
    ("see if this environment variable is set", "check_verify",
     "Use Grep or Bash to check directly."),
    # edit/fix tasks
    ("fix the typo in this line of code", "edit_fix",
     "Use Read + Edit to fix directly."),
    ("change this value in the config file", "edit_fix",
     "Use Read + Edit to fix directly."),
    ("update the version number in the file", "edit_fix",
     "Use Read + Edit to fix directly."),
    ("modify this single function to fix the bug", "edit_fix",
     "Use Read + Edit to fix directly."),
    ("edit the import statement at the top", "edit_fix",
     "Use Read + Edit to fix directly."),
    # analyze/inspect tasks
    ("analyze this file and tell me what it does", "analyze_inspect",
     "Use Read to analyze the file directly."),
    ("look at this function and explain it", "analyze_inspect",
     "Use Read to analyze the file directly."),
    ("inspect the error handling in this module", "analyze_inspect",
     "Use Read to analyze the file directly."),
    ("examine how this class is structured", "analyze_inspect",
     "Use Read to analyze the file directly."),
    ("review this code and explain the logic", "analyze_inspect",
     "Use Read to analyze the file directly."),
    # what-does tasks
    ("what does this function do exactly", "what_does",
     "Use Read to understand the code directly."),
    ("explain what this module is responsible for", "what_does",
     "Use Read to understand the code directly."),
    ("understand what this class handles", "what_does",
     "Use Read to understand the code directly."),
    ("figure out what this method returns", "what_does",
     "Use Read to understand the code directly."),
    ("tell me what this file is used for", "what_does",
     "Use Read to understand the code directly."),
    # list/show tasks
    ("list all the files in this directory", "list_show",
     "Use Grep or Glob to list matches directly."),
    ("show me all the imports in this file", "list_show",
     "Use Grep or Glob to list matches directly."),
    ("display all test files in the project", "list_show",
     "Use Grep or Glob to list matches directly."),
    ("find all python files in this folder", "list_show",
     "Use Grep or Glob to list matches directly."),
    ("list the dependencies in requirements", "list_show",
     "Use Grep or Glob to list matches directly."),
    # count tasks
    ("count how many test functions we have", "count_how_many",
     "Use Grep with count mode to count directly."),
    ("how many files are in this directory", "count_how_many",
     "Use Grep with count mode to count directly."),
    ("count the number of classes in the project", "count_how_many",
     "Use Grep with count mode to count directly."),
    ("how many lines of code in this file", "count_how_many",
     "Use Grep with count mode to count directly."),
    ("count all the todo comments in the codebase", "count_how_many",
     "Use Grep with count mode to count directly."),
    # compare/diff tasks
    ("compare these two files and show differences", "compare_diff",
     "Use Read on both files or Bash diff directly."),
    ("diff the current version against the previous", "compare_diff",
     "Use Read on both files or Bash diff directly."),
    ("check what changed between these two files", "compare_diff",
     "Use Read on both files or Bash diff directly."),
    ("compare the old and new configuration files", "compare_diff",
     "Use Read on both files or Bash diff directly."),
    ("see the differences in these two modules", "compare_diff",
     "Use Read on both files or Bash diff directly."),
    # run/execute tasks
    ("run the test suite and check results", "run_execute",
     "Use Bash to run directly."),
    ("execute this python script and see output", "run_execute",
     "Use Bash to run directly."),
    ("run the linter on this file", "run_execute",
     "Use Bash to run directly."),
    ("execute the build command for the project", "run_execute",
     "Use Bash to run directly."),
    ("run this shell command and report the output", "run_execute",
     "Use Bash to run directly."),
]

FUZZY_THRESHOLD = 0.55  # Word-level matching — lower than char-level because words are coarser


def _safe_int(val: Any, default: int) -> int:
    """Safely coerce a value to int, returning default on failure."""
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def load_config() -> Dict:
    """Load config from JSON file, falling back to defaults on any error."""
    config = DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_PATH, "r") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                config.update({k: v for k, v in loaded.items() if v is not None})
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    config["max_agents"] = _safe_int(config.get("max_agents"), DEFAULT_CONFIG["max_agents"])
    config["parallel_window_seconds"] = _safe_int(config.get("parallel_window_seconds"), DEFAULT_CONFIG["parallel_window_seconds"])
    config["global_cooldown_seconds"] = _safe_int(config.get("global_cooldown_seconds"), DEFAULT_CONFIG["global_cooldown_seconds"])
    config["max_per_subagent_type"] = _safe_int(config.get("max_per_subagent_type"), DEFAULT_CONFIG["max_per_subagent_type"])
    config["state_ttl_hours"] = _safe_int(config.get("state_ttl_hours"), DEFAULT_CONFIG["state_ttl_hours"])
    config["audit_log"] = bool(config.get("audit_log", DEFAULT_CONFIG["audit_log"]))
    config["one_per_session"] = set(config.get("one_per_session", DEFAULT_CONFIG["one_per_session"]))
    config["always_allowed"] = set(config.get("always_allowed", DEFAULT_CONFIG["always_allowed"]))
    return config


def cleanup_stale_state(ttl_hours: int) -> None:
    """Remove session state files older than ttl_hours. Self-cleaning on every run."""
    cutoff = time.time() - (ttl_hours * 3600)
    try:
        for fname in os.listdir(STATE_DIR):
            if fname == "audit.jsonl" or fname == "audit.jsonl.1":
                continue  # Never auto-delete audit logs
            fpath = os.path.join(STATE_DIR, fname)
            try:
                if os.path.isfile(fpath) and os.stat(fpath).st_mtime < cutoff:
                    os.unlink(fpath)
            except OSError:
                pass
    except OSError:
        pass
    # Audit log rotation is handled by self-heal.py on session start,
    # not here on the hot path. See self-heal.py phase_state_health().


def audit(event_type: str, subagent_type: str, description: str, session_id: str, reason: str = "", matched_pattern: str = "") -> None:
    """Append a single JSON line to the audit log with file locking. Non-critical."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "event": event_type,
        "type": subagent_type,
        "desc": description[:80],
        "session": session_id[:12],
    }
    if reason:
        entry["reason"] = reason[:120]
    if matched_pattern:
        entry["pattern"] = matched_pattern
    locked_append(AUDIT_LOG, json.dumps(entry) + "\n")


def check_necessity(description: str, prompt_text: str) -> Tuple[bool, str, str]:
    """Score whether this task could be handled by direct tools.

    Two-pass detection:
      1. Fast path: regex patterns (existing, <1ms)
      2. Slow path: fuzzy matching against canonical bank (~50 comparisons, <5ms)

    Returns (should_block, suggestion, pattern_name).
    pattern_name is logged to audit for tuning. Fuzzy matches get a "fuzzy_" prefix.
    """
    combined = f"{description} {prompt_text}".lower()

    # Fast path: regex (high confidence, <1ms)
    for pattern, suggestion, pattern_name in DIRECT_TOOL_PATTERNS:
        if re.search(pattern, combined):
            return True, suggestion, pattern_name

    # Slow path: word-level fuzzy matching against canonical bank
    # Word-level comparison is more robust than character-level because specific
    # identifiers (handleAuth, myFile.py) don't dilute the structural similarity.
    input_words = combined[:200].split()
    best_score = 0
    best_match = None
    for canonical, pattern_name, suggestion in CANONICAL_DIRECT_TASKS:
        score = difflib.SequenceMatcher(None, input_words, canonical.split()).ratio()
        if score > best_score:
            best_score = score
            best_match = (suggestion, pattern_name)

    if best_score >= FUZZY_THRESHOLD and best_match:
        return True, best_match[0], f"fuzzy_{best_match[1]}"

    return False, "", ""


def check_type_switching(state: Dict, description: str, subagent_type: str) -> Tuple[bool, str]:
    """Detect if new spawn resembles a previously blocked spawn with different type."""
    for attempt in state.get("blocked_attempts", []):
        similarity = difflib.SequenceMatcher(
            None, description.lower(), attempt["description"].lower()
        ).ratio()
        if similarity > 0.6 and attempt["type"] != subagent_type:
            return True, attempt["type"]
    return False, ""


def default_state() -> Dict:
    """Return the default empty state for a new session."""
    return {"agent_count": 0, "agents": [], "blocked_attempts": []}


def main():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except OSError:
        sys.exit(0)  # Can't create state dir — fail-open

    config = load_config()
    max_agents = config["max_agents"]
    parallel_window_seconds = config["parallel_window_seconds"]
    global_cooldown = config["global_cooldown_seconds"]
    max_per_subagent_type = config["max_per_subagent_type"]
    one_per_session = config["one_per_session"]
    always_allowed = config["always_allowed"]
    audit_enabled = config["audit_log"]

    # Self-clean stale state files on every invocation
    cleanup_stale_state(config["state_ttl_hours"])

    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        sys.exit(0)  # Can't parse input — fail-open, not fail-closed

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    session_id = input_data.get("session_id", "unknown")

    # Only gate Task tool calls
    if tool_name != "Task":
        sys.exit(0)

    subagent_type = tool_input.get("subagent_type", "")
    description = tool_input.get("description", "")

    # Skip gating for lightweight agents
    if subagent_type in always_allowed:
        sys.exit(0)

    # RESUME DETECTION — continuing existing work, not new spawn
    if tool_input.get("resume"):
        if audit_enabled:
            audit("resume", subagent_type or "resumed", description, session_id)
        sys.exit(0)  # Always allow resumes

    state_file = os.path.join(STATE_DIR, f"{session_id}.json")

    # File-locked state access (prevents race conditions from parallel tool calls)
    lock_file = state_file + ".lock"
    try:
        lf = open(lock_file, "w")
    except OSError:
        sys.exit(0)  # Can't create lock file — fail-open
    try:
        lock(lf)
        try:
            state = load_json_state(state_file, default_state)
            now = time.time()

            # Prune stale blocked_attempts (5-minute TTL)
            state["blocked_attempts"] = [
                a for a in state.get("blocked_attempts", [])
                if now - a.get("timestamp", 0) < BLOCKED_ATTEMPTS_TTL
            ]

            # TEAM DETECTION — team spawns bypass rules but count toward session cap
            if tool_input.get("team_name"):
                if state["agent_count"] >= max_agents:
                    reason = (
                        f"BLOCKED: Agent cap reached ({max_agents}/session) even for team spawns. "
                        f"Reduce team size or increase max_agents in config."
                    )
                    if audit_enabled:
                        audit("block", subagent_type, description, session_id, "team_session_cap")
                    block(reason)
                # Record and allow
                state["agent_count"] += 1
                state["agents"].append({
                    "type": subagent_type, "description": description,
                    "timestamp": now, "team": tool_input["team_name"],
                })
                save_json_state(state_file, state)
                if audit_enabled:
                    audit("allow_team", subagent_type, description, session_id)
                sys.exit(0)

            # RULE 1: One-per-session types (Explore, Plan, master-coder, etc.)
            if subagent_type in one_per_session:
                existing = [a for a in state["agents"] if a["type"] == subagent_type]
                if existing:
                    reason = (
                        f"BLOCKED: Already spawned a {subagent_type} agent this session. "
                        f"Max 1 per session. Merge your queries into one agent, or use "
                        f"Grep/Read/WebSearch directly instead of spawning another."
                    )
                    state.setdefault("blocked_attempts", []).append({
                        "type": subagent_type, "description": description, "timestamp": now
                    })
                    save_json_state(state_file, state)
                    if audit_enabled:
                        audit("block", subagent_type, description, session_id, "one_per_session limit")
                    block(reason)

            # RULE 2: No duplicate subagent_types (for types NOT already covered by Rule 1)
            elif len([a for a in state["agents"] if a["type"] == subagent_type]) >= max_per_subagent_type:
                count = len([a for a in state["agents"] if a["type"] == subagent_type])
                reason = (
                    f"BLOCKED: Already {count} {subagent_type} agent(s) this session. "
                    f"Max {max_per_subagent_type} of any type. Use tools directly instead."
                )
                state.setdefault("blocked_attempts", []).append({
                    "type": subagent_type, "description": description, "timestamp": now
                })
                save_json_state(state_file, state)
                if audit_enabled:
                    audit("block", subagent_type, description, session_id, "max_per_type limit")
                block(reason)

            # RULE 3: Session agent cap
            if state["agent_count"] >= max_agents:
                reason = (
                    f"BLOCKED: Agent cap reached ({max_agents}/session). "
                    f"You've spawned {state['agent_count']} agents already. "
                    f"Use Grep/Read/WebSearch tools directly instead of spawning agents."
                )
                state.setdefault("blocked_attempts", []).append({
                    "type": subagent_type, "description": description, "timestamp": now
                })
                save_json_state(state_file, state)
                if audit_enabled:
                    audit("block", subagent_type, description, session_id, "session_cap limit")
                block(reason)

            # RULE 4: No spawns within window of same type (catches same-turn parallel spawns)
            recent_same = [
                a for a in state["agents"]
                if a["type"] == subagent_type
                and (now - a["timestamp"]) < parallel_window_seconds
            ]
            if recent_same:
                elapsed = now - recent_same[0]["timestamp"]
                reason = (
                    f"BLOCKED: Another {subagent_type} agent was spawned {elapsed:.0f}s ago. "
                    f"Wait or merge into one agent. Overlap Check: combine queries into a single prompt."
                )
                state.setdefault("blocked_attempts", []).append({
                    "type": subagent_type, "description": description, "timestamp": now
                })
                save_json_state(state_file, state)
                if audit_enabled:
                    audit("block", subagent_type, description, session_id, "parallel_window limit")
                block(reason)

            # RULE 5: Necessity check — block obviously simple tasks
            should_block, suggestion, pattern_name = check_necessity(description, tool_input.get("prompt", ""))
            if should_block:
                reason = (
                    f"BLOCKED: This task can be handled with direct tools. "
                    f"{suggestion} "
                    f"Agents cost ~50k tokens. Direct tools cost ~2-10k."
                )
                state.setdefault("blocked_attempts", []).append({
                    "type": subagent_type, "description": description, "timestamp": now
                })
                save_json_state(state_file, state)
                if audit_enabled:
                    audit("block", subagent_type, description, session_id, "necessity_check", pattern_name)
                block(reason)

            # RULE 6: Type-switching detection
            is_evasion, blocked_type = check_type_switching(state, description, subagent_type)
            if is_evasion:
                reason = (
                    f"BLOCKED: This {subagent_type} resembles a previously blocked "
                    f"{blocked_type} attempt. Use Grep/Read directly."
                )
                state.setdefault("blocked_attempts", []).append({
                    "type": subagent_type, "description": description, "timestamp": now
                })
                save_json_state(state_file, state)
                if audit_enabled:
                    audit("block", subagent_type, description, session_id, "type_switching")
                block(reason)

            # RULE 7: Global cooldown — prevent rapid-fire spawns of any type
            # Skip cooldown for non-team agents only (team spawns exempt — they need fast setup)
            non_team_agents = [a for a in state["agents"] if not a.get("team")]
            if non_team_agents:
                last_any = max(a["timestamp"] for a in non_team_agents)
                elapsed = now - last_any
                if elapsed < global_cooldown:
                    reason = (
                        f"BLOCKED: Agent spawned {elapsed:.0f}s ago. "
                        f"Wait {global_cooldown}s between spawns."
                    )
                    state.setdefault("blocked_attempts", []).append({
                        "type": subagent_type, "description": description, "timestamp": now
                    })
                    save_json_state(state_file, state)
                    if audit_enabled:
                        audit("block", subagent_type, description, session_id, "global_cooldown")
                    block(reason)

            # ADVISORY: First-spawn reminder (non-blocking)
            if state["agent_count"] == 0:
                print(
                    f"FIRST AGENT THIS SESSION: {subagent_type} ({description[:60]}). "
                    f"Cost: ~50k tokens. Confirm Direct-First Rule compliance.",
                    file=sys.stderr
                )

            # ADVISORY: Model cost check (non-blocking)
            requested_model = tool_input.get("model", "")
            if requested_model == "opus":
                print(
                    f"MODEL COST: opus requested for {subagent_type}. "
                    f"Rule: ALL agents default to sonnet. Opus costs ~3x more. "
                    f"Only for genuinely hard reasoning.",
                    file=sys.stderr
                )

            # ALLOWED — record and proceed
            agent_record = {
                "type": subagent_type,
                "description": description,
                "timestamp": now,
            }

            # For Explore agents, extract target directories from the prompt
            # so read-efficiency-guard.py can detect duplicate reads
            if subagent_type == "Explore":
                prompt = tool_input.get("prompt", "")
                target_dirs = extract_target_dirs(prompt)
                if target_dirs:
                    agent_record["target_dirs"] = target_dirs

            state["agent_count"] += 1
            state["agents"].append(agent_record)
            save_json_state(state_file, state)

            if audit_enabled:
                audit("allow", subagent_type, description, session_id)

        finally:
            unlock(lf)
    finally:
        lf.close()

    sys.exit(0)  # Allow


def block(reason: str) -> None:
    """Block the tool call with feedback to Claude."""
    print(reason, file=sys.stderr)
    sys.exit(2)


def extract_target_dirs(prompt: str) -> List[str]:
    """Extract directory paths from an Explore agent's prompt.

    Uses general path patterns rather than hardcoded directory names,
    so it works with any path structure (~/Projects, /tmp, /Documents, etc.).
    """
    dirs = []
    patterns = [
        r'(?:START:\s*)(~?/[^\s\n,]+)',            # START: /any/path
        r'(?:^|\s)(~/[^\s\n,]+)',                   # Any ~/ path
        r'(?:^|\s)(/[^\s\n,]+/[^\s\n,]+)',         # Any /foo/bar multi-segment absolute path
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, prompt):
            path = match.group(1).rstrip("/").rstrip(")")
            path = os.path.expanduser(path)
            # Include if it looks like a directory (no file extension) or actually is one
            _, ext = os.path.splitext(path)
            if not ext or os.path.isdir(path):
                if path not in dirs:
                    dirs.append(path)
    return dirs


def report() -> None:
    """Print cross-session analytics from audit log."""
    from collections import Counter

    entries = read_jsonl_fault_tolerant(AUDIT_LOG)
    if not entries:
        print("No audit data found.")
        return

    allows = [e for e in entries if e.get("event") == "allow"]
    blocks = [e for e in entries if e.get("event") == "block"]
    resumes = [e for e in entries if e.get("event") == "resume"]
    teams = [e for e in entries if e.get("event") == "allow_team"]
    total = len(allows) + len(blocks)

    print(f"\n{'='*40}")
    print(f"  TOKEN GUARD ANALYTICS")
    print(f"{'='*40}")
    print(f"Total attempts: {total}")
    print(f"Allowed: {len(allows)} ({len(allows)/max(total,1)*100:.0f}%)")
    print(f"Blocked: {len(blocks)} ({len(blocks)/max(total,1)*100:.0f}%)")
    print(f"Resumes: {len(resumes)}")
    print(f"Team spawns: {len(teams)}")
    print(f"\nTop agent types:")
    for t, c in Counter(e.get("type", "?") for e in allows).most_common(5):
        print(f"  {t}: {c}")
    print(f"\nBlock reasons:")
    for r, c in Counter(e.get("reason", "?") for e in blocks).most_common(5):
        print(f"  {r}: {c}")

    # Necessity pattern breakdown (feedback loop for tuning)
    necessity_blocks = [e for e in blocks if e.get("reason") == "necessity_check"]
    if necessity_blocks:
        print(f"\nNecessity patterns triggered:")
        for p, c in Counter(e.get("pattern", "?") for e in necessity_blocks).most_common(10):
            print(f"  {p}: {c}")

    # Estimated token cost (heuristic: ~50k per agent, split ~70/30 input/output)
    EST_INPUT_PER_AGENT = 35000   # ~70% is input (system prompt + mode file + context)
    EST_OUTPUT_PER_AGENT = 15000  # ~30% is output (agent's response + tool calls)
    SONNET_COST_PER_1K_INPUT = 0.003  # $3/M input tokens
    SONNET_COST_PER_1K_OUTPUT = 0.015  # $15/M output tokens
    est_input_cost = len(allows) * EST_INPUT_PER_AGENT * SONNET_COST_PER_1K_INPUT / 1000
    est_output_cost = len(allows) * EST_OUTPUT_PER_AGENT * SONNET_COST_PER_1K_OUTPUT / 1000
    est_cost = est_input_cost + est_output_cost
    est_tokens = len(allows) * (EST_INPUT_PER_AGENT + EST_OUTPUT_PER_AGENT)
    savings_input = len(blocks) * EST_INPUT_PER_AGENT * SONNET_COST_PER_1K_INPUT / 1000
    savings_output = len(blocks) * EST_OUTPUT_PER_AGENT * SONNET_COST_PER_1K_OUTPUT / 1000
    savings_cost = savings_input + savings_output
    savings_tokens = len(blocks) * (EST_INPUT_PER_AGENT + EST_OUTPUT_PER_AGENT)

    print(f"\nEstimated impact:")
    print(f"  Tokens used by agents: ~{est_tokens:,}")
    print(f"  Tokens SAVED by blocks: ~{savings_tokens:,}")
    print(f"  Est. cost (agents): ~${est_cost:.2f}")
    print(f"  Est. savings (blocks): ~${savings_cost:.2f}")
    print(f"  Block rate: {len(blocks)/max(total,1)*100:.0f}%")

    # Real metrics from transcript parsing (agent-metrics.py)
    metrics_file = os.path.join(STATE_DIR, "agent-metrics.jsonl")
    if os.path.isfile(metrics_file):
        real_metrics = read_jsonl_fault_tolerant(metrics_file)
        completed = [m for m in real_metrics if m.get("event") == "agent_completed"]
        if completed:
            real_input = sum(m.get("input_tokens", 0) for m in completed)
            real_output = sum(m.get("output_tokens", 0) for m in completed)
            real_cache = sum(m.get("cache_read_tokens", 0) for m in completed)
            real_cost = sum(m.get("cost_usd", 0) for m in completed)
            print(f"\nReal metrics (from transcript parsing):")
            print(f"  Agents metered: {len(completed)}")
            print(f"  Input tokens: {real_input:,}")
            print(f"  Output tokens: {real_output:,}")
            print(f"  Cache reads: {real_cache:,} ({real_cache/max(real_input,1)*100:.0f}% cache hit)")
            print(f"  Actual cost: ${real_cost:.4f}")

    # Warn/allow breakdown
    warns = [e for e in entries if e.get("event") == "warn"]
    if warns:
        print(f"\nWarnings (non-blocking): {len(warns)}")
        for r, c in Counter(e.get("reason", "?") for e in warns).most_common(5):
            print(f"  {r}: {c}")

    print(f"\nUnique sessions: {len(set(e.get('session', '?') for e in entries))}")
    print(f"{'='*40}\n")


def usage() -> None:
    """Print shareable usage summary from audit data."""
    from collections import Counter

    entries = read_jsonl_fault_tolerant(AUDIT_LOG)
    if not entries:
        print("No usage data yet. Token Guard will start tracking on your next session.")
        return

    allows = [e for e in entries if e.get("event") == "allow"]
    blocks = [e for e in entries if e.get("event") == "block"]
    total = len(allows) + len(blocks)
    sessions = len(set(e.get("session", "?") for e in entries))

    # Find earliest timestamp
    timestamps = [e.get("ts", "") for e in entries if e.get("ts")]
    active_since = min(timestamps)[:10] if timestamps else "unknown"

    # Estimated savings
    EST_TOKENS_PER_AGENT = 50000
    COST_PER_AGENT = 0.33  # ~$0.33 per agent at Sonnet rates
    saved_tokens = len(blocks) * EST_TOKENS_PER_AGENT
    saved_cost = len(blocks) * COST_PER_AGENT

    # Top block reasons
    reason_counts = Counter(e.get("reason", "?") for e in blocks).most_common(3)

    print(f"\n{'='*40}")
    print(f"  YOUR TOKEN GUARD USAGE")
    print(f"{'='*40}")
    print(f"Active since: {active_since}")
    print(f"Sessions tracked: {sessions}")
    print(f"Total agent attempts: {total}")
    print(f"Agents blocked: {len(blocks)} ({len(blocks)/max(total,1)*100:.0f}%)")
    print(f"Estimated tokens saved: ~{saved_tokens:,}")
    print(f"Estimated cost saved: ~${saved_cost:.2f}")
    if reason_counts:
        print(f"Top block reasons:")
        for reason, count in reason_counts:
            print(f"  {reason}: {count}")
    print(f"{'='*40}")
    print(f"\nShare this as a testimonial:")
    print(f'"Token Guard saved me ~${saved_cost:.2f} across {sessions} sessions '
          f'by blocking {len(blocks)} wasteful agent spawns."')
    print()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--report":
        report()
    elif len(sys.argv) > 1 and sys.argv[1] == "--usage":
        usage()
    else:
        main()
