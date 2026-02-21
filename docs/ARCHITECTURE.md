# Architecture Deep Dive

## Three-Layer Defense-in-Depth

Claude Token Guard uses the same defense-in-depth pattern found in nuclear safety systems, Anthropic's Constitutional AI, and OpenAI's safety pipeline:

```
Layer 1: Advisory     (settings.json prompt hook)     — reminds the AI of rules
Layer 2: Enforcement  (token-guard.py)                — hard-blocks agent spawns
Layer 3: Enforcement  (read-efficiency-guard.py)       — hard-blocks wasteful reads
Layer 4: Observability (audit.jsonl)                   — tracks everything for tuning
Layer 5: Self-Healing  (self-heal.py)                  — validates and repairs on startup
```

### Why Three Layers?

Behavioral instructions (CLAUDE.md) are suggestions. LLMs can rationalize past suggestions — "this case is different," "the user needs this," etc. Mechanical enforcement (`sys.exit(2)`) cannot be rationalized. The tool call simply never happens.

But enforcement alone isn't enough. You need:
- **Advisory** to catch issues at decision time (before the LLM even tries)
- **Enforcement** to block what slips through
- **Observability** to tune the system over time

## Component Architecture

### hook_utils.py — Shared Infrastructure

The foundation that both hooks import from. Exists to eliminate DRY violations so bug fixes propagate automatically.

| Function | Purpose |
|----------|---------|
| `lock(f)` / `unlock(f)` | Portable file locking (fcntl on Unix, msvcrt on Windows) |
| `load_json_state(path, default_factory)` | Load JSON with graceful fallback |
| `save_json_state(path, state)` | Atomic write via tempfile + os.replace |
| `locked_append(path, line)` | File-locked append for audit log |
| `read_jsonl_fault_tolerant(path)` | Per-line error handling for JSONL |
| `DEFAULT_CONFIG` | Single source of truth for configuration defaults |

### token-guard.py — Agent Spawning Enforcement

Runs as a PreToolUse hook on every `Task` tool call. Seven rules, evaluated in order:

| Rule | What It Checks | Action |
|------|----------------|--------|
| 1 | One-per-session types (Explore, Plan, etc.) | Block |
| 2 | General type cap (max N of any single type) | Block |
| 3 | Session agent cap (max total agents) | Block |
| 4 | Parallel window (no same-type within 30s) | Block |
| 5 | Necessity scoring (10 regex patterns) | Block |
| 6 | Type-switching (similarity >0.6 to blocked attempt) | Block |
| 7 | Global cooldown (no rapid-fire spawns) | Block |

**Special handling:**
- `resume` param → always allowed (continuing existing work)
- `team_name` param → bypasses rules 1-7 but counts toward session cap
- `always_allowed` types → skip all checks entirely

**Necessity Scoring** uses 10 regex patterns to detect tasks that should use direct tools:
```python
DIRECT_TOOL_PATTERNS = [
    (r'\b(search|find|grep|look for|locate)\b.*\b(file|function|class)\b',
     "Use Grep to search for code patterns directly.",
     "search_grep"),
    # ... 9 more patterns
]
```

Each pattern has a `pattern_name` (3rd element) that gets logged to the audit log, creating a feedback loop for tuning.

**Type-Switching Detection** uses `difflib.SequenceMatcher` to catch evasion attempts:
```
Blocked: Explore "investigate auth architecture"
Attempted: general-purpose "investigate the auth architecture thoroughly"
→ Similarity: 0.82 > 0.6 threshold → BLOCKED
```

### read-efficiency-guard.py — Read Pattern Enforcement

Runs as a PreToolUse hook on every `Read` tool call. Three checks:

| Check | Threshold | Action |
|-------|-----------|--------|
| Duplicate file | 3+ reads of same path | Block |
| Sequential reads | 4 in 120s | Warn |
| Sequential reads | 15 in 120s | Block |
| Post-Explore overlap | File in Explore'd dir | Warn (advisory) |

**Escalation is unconditional.** Time-based suppression applies only to warnings (to avoid spam), never to blocks. This was a bug that was fixed — the original code had a 60-second "free pass" after each block.

**Cross-hook coordination:** Reads the token-guard state file (with file locking) to find directories mapped by Explore agents. If you read a file inside an Explore'd directory, you get an advisory warning.

### self-heal.py — Startup Validation

Runs as a SessionStart hook. Five phases:

1. **Structural integrity** — all files exist, config valid, state dir writable
2. **Smoke tests** — pipe valid JSON through hooks in isolated temp dirs
3. **State health** — find and clean corrupted/orphaned/stale files
4. **Auto-repair** — fix permissions, recreate missing dirs, regenerate config
5. **Report** — summary to stdout

Always exits 0 (never blocks session start). Self-contained — imports DEFAULT_CONFIG from hook_utils with a fallback inline copy in case hook_utils itself is broken.

## State Management

### Atomic Writes

All state writes use the tempfile + os.replace pattern:

```python
fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
with os.fdopen(fd, "w") as f:
    json.dump(state, f, indent=2)
os.replace(tmp_path, path)  # Atomic on both POSIX and Windows
```

If the process crashes mid-write, the original file is untouched. Orphaned .tmp files are cleaned up by self-heal.

### File Locking

All shared state access is protected by exclusive file locks:

```python
with open(lock_file, "w") as lf:
    lock(lf)        # Blocks until lock acquired
    try:
        # ... read/modify/write state ...
    finally:
        unlock(lf)  # Always releases, even on sys.exit(2)
```

### TTL Pruning

All arrays have bounded growth:
- Read records: 300s TTL (pruned on every read-guard call)
- Blocked attempts: 300s TTL (pruned on every token-guard call)
- Session state files: 24h TTL (pruned on every token-guard call)

### Audit Log

JSONL format, one entry per line. Locked appends prevent interleaving. Rotation to `.1` backup at 10K lines (handled by self-heal on session start, not on the hot path).

## Configuration

Single source of truth: `DEFAULT_CONFIG` in `hook_utils.py`. All three consumers (token-guard, self-heal, config file on disk) derive from this.

Runtime config loaded from `~/.claude/hooks/token-guard-config.json` with type coercion via `_safe_int()` — handles None, strings, floats, and negatives gracefully.

## Testing Strategy

96 tests across 4 files:
- **test_token_guard.py** — all 7 rules, config edge cases, advisories, anti-evasion, cooldown, report mode
- **test_read_efficiency_guard.py** — duplicate blocking, sequential escalation, post-Explore detection
- **test_integration.py** — cross-hook state coordination, concurrent access, audit integrity
- **test_self_heal.py** — all 5 phases, repair actions, audit rotation

All tests use subprocess invocation (not direct imports) to match real-world execution. Isolated via `tmp_path` fixtures and environment variable overrides.
