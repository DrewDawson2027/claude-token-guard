# Security Posture

## What Token Guard Protects Against

- **Wasteful agent spawning** — blocks redundant, unnecessary, or evasive agent spawn attempts
- **Excessive file reads** — blocks duplicate reads and sequential read floods
- **State corruption** — atomic writes, file locking, and self-heal prevent and repair corruption
- **Config drift** — self-heal validates and auto-repairs configuration on every session start

## What Token Guard Does NOT Protect Against

- **Malicious users** — Token Guard assumes the user is trusted. It protects the user from the AI's inefficiency, not from malicious actors.
- **Network attacks** — Token Guard is entirely local. No network communication, no remote APIs.
- **Data encryption** — Audit logs, state files, and metrics are stored as plaintext JSON. They may contain file paths and agent descriptions from your sessions.
- **Denial of service** — File locking prevents corruption but a process that holds a lock indefinitely could stall subsequent hooks. Self-heal cleans stale locks (>5 min) on session start.

## Failure Modes

Every hook follows the **fail-open** principle:

| Scenario | Behavior |
|----------|----------|
| Can't read stdin | Exit 0 (allow the tool call) |
| Can't parse JSON input | Exit 0 (allow) |
| Can't create/read state directory | Exit 0 (allow) |
| Can't acquire file lock | Exit 0 (allow) |
| Can't write audit log | Allow the tool call, skip logging |
| Config file missing or corrupt | Use DEFAULT_CONFIG from hook_utils.py |
| self-heal encounters any error | Exit 0 (never block session start) |

A bug in the guard should **never** block legitimate work. False negatives (allowing a wasteful call) are strictly preferred over false positives (blocking needed work).

Exit codes:
- **0** — allow the tool call
- **2** — block the tool call (intentional enforcement decision)
- **1** — should never happen (all error paths exit 0)

## Rollback

### Automated
```bash
claude-token-guard uninstall
```
This removes all hook files from `~/.claude/hooks/` and unpatches `~/.claude/settings.json`.

### Manual
1. Delete hook files: `rm ~/.claude/hooks/{token-guard,read-efficiency-guard,self-heal,hook_utils,guard_*,agent-*}.{py,sh}`
2. Delete config: `rm ~/.claude/hooks/token-guard-config.json`
3. Delete state: `rm -rf ~/.claude/hooks/session-state/`
4. Remove hook entries from `~/.claude/settings.json` (under `hooks.PreToolUse`, `hooks.SessionStart`, `hooks.SubagentStart`, `hooks.SubagentStop`)
5. Restart Claude Code

## Input Sanitization

All user-controlled strings are normalized before persistence:

| Input | Normalization |
|-------|--------------|
| `session_id` (raw UUID) | `normalize_session_key()` — SHA256 hash truncated to 12 hex chars |
| File paths | `normalize_file_path()` — expanduser → normpath → realpath |
| Agent descriptions | Truncated to `max_string_field_length` (default 512 chars) |
| Hook payloads | `normalize_hook_payload()` — strips nulls, truncates all string fields |

This prevents:
- Path traversal in state file names (session keys are hex-only)
- Symlink bypass in read deduplication (realpath resolution)
- JSONL injection via newlines in strings (JSON serialization handles escaping)
- Memory exhaustion from very long strings (hard truncation)

## File Locking

- Uses `fcntl.flock` (Unix) or `msvcrt.locking` (Windows) for exclusive locks
- Lock files are `.lock` siblings of the data files
- Prevents interleaved writes from concurrent hook processes
- Self-heal cleans locks older than 5 minutes on session start
- Lock acquisition is blocking (waits until available), not try-once

## Data Retention

| Data | Retention | Mechanism |
|------|-----------|-----------|
| Session state | 24 hours | TTL pruning on every token-guard call |
| Read records | 5 minutes | TTL pruning on every read-guard call |
| Blocked attempts | 5 minutes | TTL pruning on every token-guard call |
| Audit log | 10,000 lines | Rotation to `.1` backup by self-heal |
| Metrics log | 500 entries | Truncation by agent-metrics.py |
| Self-heal log | Unbounded | Rotated with audit log |

## Strict Mode (`fail_closed`)

By default, Token Guard uses `fail_open` mode — any internal error (can't create state dir, can't acquire lock) results in exit 0, allowing the tool call through. This ensures a bug in the guard never blocks legitimate work.

For power users who prefer correctness over availability, `fail_closed` mode blocks tool calls when the guard can't verify state:

```json
{
  "failure_mode": "fail_closed"
}
```

**What changes in strict mode:**

| Error Path | `fail_open` (default) | `fail_closed` |
|------------|----------------------|---------------|
| Can't create state directory | Exit 0 (allow) | Exit 2 (block) |
| Can't create lock file | Exit 0 (allow) | Exit 2 (block) |
| Can't parse stdin | Exit 0 (allow) | Exit 0 (allow) |
| Non-dict JSON input | Exit 0 (allow) | Exit 0 (allow) |

Note: stdin parse errors and non-dict JSON always fail-open regardless of mode, because the guard can't enforce rules without valid input.

**When to use strict mode:**
- You're developing or testing the guard itself
- You want absolute certainty that every agent spawn was checked
- You have a healthy state directory with proper permissions

**When NOT to use strict mode:**
- In production sessions where blocking legitimate work is worse than allowing an extra agent
- On shared systems where file permissions may be restrictive

## Monitoring

Token Guard writes structured JSONL logs that can be monitored for operational health.

### Key Files

| File | Contents | What to Watch |
|------|----------|---------------|
| `session-state/audit.jsonl` | Agent spawn decisions | High block rates may indicate overly strict config |
| `session-state/self-heal.jsonl` | Repair actions | Frequent repairs suggest environmental issues |
| `session-state/agent-metrics.jsonl` | Real token usage | Cost trends and correlation rates |

### Quick Analysis

```bash
# Block rate over last 100 decisions
tail -100 ~/.claude/hooks/session-state/audit.jsonl | \
  python3 -c "import sys,json; lines=[json.loads(l) for l in sys.stdin if l.strip()]; \
  blocks=sum(1 for e in lines if e.get('event')=='block'); \
  print(f'Block rate: {blocks}/{len(lines)} ({blocks/max(len(lines),1)*100:.0f}%)')"

# Self-heal repair frequency
grep '"repaired"' ~/.claude/hooks/session-state/self-heal.jsonl | wc -l

# Top block reasons
python3 -c "import sys,json; from collections import Counter; \
  entries=[json.loads(l) for l in open('$HOME/.claude/hooks/session-state/audit.jsonl') if l.strip()]; \
  blocks=[e for e in entries if e.get('event')=='block']; \
  print('\n'.join(f'  {r}: {c}' for r,c in Counter(e.get('reason','?') for e in blocks).most_common(5)))"

# JSON report for programmatic consumption
python3 ~/.claude/hooks/token-guard.py --report --json | python3 -m json.tool
```

## Permissions

- State directory (`session-state/`) is created with mode 0700 (owner-only)
- Shell scripts (`.sh`) are installed with mode 0755 (executable)
- No elevated privileges required — runs as the current user
- No network access — entirely offline
