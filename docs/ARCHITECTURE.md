# Architecture Deep Dive

## Three-Layer Defense-in-Depth

Claude Token Guard uses defense-in-depth — the same pattern found in nuclear safety systems and AI alignment pipelines:

```
Layer 1: Advisory      (settings.json prompt hook)      — reminds the AI of rules
Layer 2: Enforcement   (token-guard.py)                 — hard-blocks agent spawns
Layer 3: Enforcement   (read-efficiency-guard.py)        — hard-blocks wasteful reads
Layer 4: Lifecycle     (agent-lifecycle.sh, agent-metrics.py) — tracks agent start/stop/cost
Layer 5: Observability (audit.jsonl, metrics.jsonl)      — tracks everything for tuning
Layer 6: Self-Healing  (self-heal.py)                    — validates and repairs on startup
```

### Why Defense-in-Depth?

Behavioral instructions (CLAUDE.md) are suggestions. LLMs can rationalize past suggestions — "this case is different," "the user needs this," etc. Mechanical enforcement (`sys.exit(2)`) cannot be rationalized. The tool call simply never happens.

But enforcement alone isn't enough. You need:
- **Advisory** to catch issues at decision time (before the LLM even tries)
- **Enforcement** to block what slips through
- **Lifecycle tracking** to correlate decisions with outcomes
- **Observability** to tune the system over time

## Data Flow

```
Tool Call (stdin JSON)
  |
  +-> token-guard.py (PreToolUse:Task)
  |     |-> Read/write session state ({session_key}.json)
  |     |-> Append audit decision (audit.jsonl)
  |     |-> Write pending_spawns for lifecycle correlation
  |     +-> Exit 0 (allow) or Exit 2 (block)
  |
  +-> read-efficiency-guard.py (PreToolUse:Read)
  |     |-> Read/write read state ({session_key}-reads.json)
  |     |-> Read token-guard state (cross-hook coordination)
  |     |-> Append audit on blocks (audit.jsonl)
  |     +-> Exit 0 (allow) or Exit 2 (block)
  |
  +-> agent-lifecycle.sh (SubagentStart/SubagentStop)
  |     |-> Consume pending_spawns (correlate decision_id)
  |     +-> Append lifecycle events (metrics.jsonl)
  |
  +-> agent-metrics.py (SubagentStop)
  |     |-> Parse agent transcript (if available)
  |     |-> Calculate token usage and cost
  |     +-> Append usage metrics (metrics.jsonl)
  |
  +-> self-heal.py (SessionStart)
        |-> Validate config, state, permissions
        |-> Repair corrupted/orphaned files
        |-> Detect runtime drift (file checksum changes)
        +-> Always exit 0
```

## Module Dependency Graph

```
hook_utils.py (shared infrastructure)
  ^  ^  ^  ^
  |  |  |  |
  |  |  |  +-- self-heal.py (imports DEFAULT_CONFIG, load/save functions)
  |  |  +-- read-efficiency-guard.py (imports lock/state/append functions)
  |  +-- token-guard.py (imports lock/state/append/read_jsonl functions)
  +-- guard_events.py (imports locked_append)

guard_contracts.py (v2 schema builders, no imports from other hook modules)
  ^  ^  ^
  |  |  +-- agent-metrics.py (imports build_metrics_usage_entry)
  |  +-- read-efficiency-guard.py (imports build_audit_entry)
  +-- token-guard.py (imports build_audit_entry, build_decision_id)

guard_normalize.py (normalization helpers, no imports from other hook modules)
  ^  ^  ^  ^
  |  |  |  +-- agent-lifecycle.sh (calls normalize_hook_payload via inline python)
  |  |  +-- agent-metrics.py (imports normalize_hook_payload, normalize_session_key)
  |  +-- read-efficiency-guard.py (imports normalize_file_path, normalize_session_key)
  +-- token-guard.py (imports normalize_session_key, normalize_hook_payload)

guard_events.py (JSONL append wrapper)
  ^  ^
  |  +-- agent-metrics.py (imports append_jsonl)
  +-- token-guard.py (imports append_jsonl)
```

## Component Architecture

### hook_utils.py — Shared Infrastructure

The foundation that all hooks import from. Exists to eliminate DRY violations so bug fixes propagate automatically.

| Function | Purpose |
|----------|---------|
| `lock(f)` / `unlock(f)` | Portable file locking (fcntl on Unix, msvcrt on Windows) |
| `load_json_state(path, default_factory)` | Load JSON with graceful fallback |
| `save_json_state(path, state)` | Atomic write via tempfile + os.replace |
| `locked_append(path, line)` | File-locked append for audit log |
| `read_jsonl_fault_tolerant(path)` | Per-line error handling for JSONL |
| `DEFAULT_CONFIG` | Single source of truth for configuration defaults |

### guard_contracts.py — Schema v2 Builders

Canonical builders for all structured records. Enforces schema_version, timestamps, field truncation.

| Builder | Output Record Type |
|---------|-------------------|
| `build_audit_entry(...)` | Audit decision (allow/block/warn) |
| `build_metrics_lifecycle_entry(...)` | Agent start/stop lifecycle event |
| `build_metrics_usage_entry(...)` | Token usage and cost per agent |
| `build_decision_id()` | 12-char hex decision correlation ID |

### guard_normalize.py — Input Normalization

Sanitizes all user-controlled inputs before persistence.

| Function | What It Does |
|----------|-------------|
| `normalize_session_key(session_id)` | UUID → 12-char hex key (filesystem-safe) |
| `normalize_hook_payload(data)` | Truncate strings, strip nulls |
| `normalize_file_path(path)` | expanduser → normpath → realpath |

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

### read-efficiency-guard.py — Read Pattern Enforcement

Runs as a PreToolUse hook on every `Read` tool call.

| Check | Threshold | Action |
|-------|-----------|--------|
| Duplicate file | 3+ reads of same path | Block |
| Sequential reads | 4 in 120s | Warn |
| Sequential reads | 15 in 120s | Block |
| Post-Explore overlap | File in Explore'd dir | Warn (advisory) |

**Path normalization:** All file paths are canonicalized via `normalize_file_path()` which calls `os.path.expanduser()` → `os.path.normpath()` → `os.path.realpath()`. This prevents bypass via `../`, `~/`, or symlinks.

### self-heal.py — Startup Validation

Runs as a SessionStart hook. Six phases:

1. **Structural integrity** — all files exist, config valid (including v2 fields), state dir writable
2. **Smoke tests** — pipe valid JSON through hooks in isolated temp dirs
3. **State health** — find and clean corrupted/orphaned/stale files, validate filename patterns
4. **Data quality** — sample last 20 audit/metrics entries, check for schema violations
5. **Runtime drift** — SHA256 checksums of hook files, warn on changes between sessions
6. **Report** — summary to stdout

Always exits 0 (never blocks session start).

## Schema v2 Record Formats

### audit_decision (audit.jsonl)

```json
{
  "schema_version": 2,
  "ts": "2026-02-21T22:30:00Z",
  "event": "allow|block|warn",
  "type": "Explore",
  "description": "search for auth patterns",
  "session_key": "b501172ca133",
  "reason": "one_per_session",
  "message": "Max 1 Explore per session",
  "decision_id": "a1b2c3d4e5f6"
}
```

### lifecycle (metrics.jsonl)

```json
{
  "schema_version": 2,
  "ts": "2026-02-21T22:30:05Z",
  "event": "agent_started|agent_stopped",
  "agent_type": "Explore",
  "agent_id": "af59e681fbcbea278",
  "session_key": "b501172ca133",
  "decision_id": "a1b2c3d4e5f6"
}
```

### usage (metrics.jsonl)

```json
{
  "schema_version": 2,
  "ts": "2026-02-21T22:31:00Z",
  "event": "agent_completed",
  "agent_type": "Explore",
  "agent_id": "af59e681fbcbea278",
  "session_key": "b501172ca133",
  "input_tokens": 12345,
  "output_tokens": 4567,
  "cost_usd": 0.15,
  "correlated": true,
  "decision_id": "a1b2c3d4e5f6",
  "transcript_found": true
}
```

## State File Formats

### Session state ({session_key}.json)

```json
{
  "agent_count": 2,
  "agents": [
    {
      "type": "Explore",
      "description": "search codebase",
      "timestamp": 1740200000.0,
      "target_dirs": ["/home/user/project"],
      "decision_id": "a1b2c3d4e5f6"
    }
  ],
  "blocked_attempts": [
    {
      "type": "Explore",
      "description": "another explore",
      "timestamp": 1740200010.0
    }
  ],
  "pending_spawns": {
    "a1b2c3d4e5f6": {
      "type": "Explore",
      "decision_id": "a1b2c3d4e5f6",
      "timestamp": 1740200000.0
    }
  }
}
```

### Read state ({session_key}-reads.json)

```json
{
  "reads": [
    {
      "path": "/home/user/project/main.py",
      "count": 2,
      "timestamps": [1740200000.0, 1740200030.0]
    }
  ]
}
```

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
- Audit log: rotated at 10K lines (by self-heal)
- Metrics log: truncated at 500 entries (by agent-metrics.py)

### Decision Correlation

Token-guard writes `pending_spawns` entries keyed by `decision_id`. When an agent starts, `agent-lifecycle.sh` consumes the matching entry and propagates the `decision_id` to the lifecycle event. When the agent stops, `agent-metrics.py` correlates the usage record back to the original decision.

## Configuration

Single source of truth: `DEFAULT_CONFIG` in `hook_utils.py`. All consumers (token-guard, self-heal, config file on disk) derive from this.

Runtime config loaded from `~/.claude/hooks/token-guard-config.json` with type coercion via `_safe_int()` — handles None, strings, floats, and negatives gracefully.

## Testing Strategy

170+ tests across 9 files:
- **test_token_guard.py** — all 7 rules, config edge cases, advisories, anti-evasion, cooldown, report mode
- **test_read_efficiency_guard.py** — duplicate blocking, sequential escalation, post-Explore detection, path alias evasion
- **test_integration.py** — cross-hook state coordination, concurrent access, audit integrity, concurrency stress
- **test_self_heal.py** — all 6 repair phases, audit rotation, runtime drift
- **test_guard_contracts.py** — v2 schema builders, field truncation, decision ID format
- **test_properties.py** — hypothesis property-based invariant testing, fuzz, hostile strings
- **test_performance.py** — subprocess + function latency benchmarks, p95 gates
- **test_packaging.py** — build/import/manifest/version consistency
- **test_direct_imports.py** — direct function calls for mutation testing

All tests use subprocess invocation (not direct imports) to match real-world execution, except where direct function testing is needed. Isolated via `tmp_path` fixtures and environment variable overrides.
