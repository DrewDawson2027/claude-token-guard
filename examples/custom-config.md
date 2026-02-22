# Custom Configuration Examples

Edit `~/.claude/hooks/token-guard-config.json` to customize behavior.

## Default Configuration

```json
{
  "schema_version": 2,
  "max_agents": 5,
  "parallel_window_seconds": 30,
  "global_cooldown_seconds": 5,
  "max_per_subagent_type": 1,
  "state_ttl_hours": 24,
  "audit_log": true,
  "failure_mode": "fail_open",
  "sanitize_session_ids": true,
  "normalize_paths": true,
  "fault_audit": true,
  "max_string_field_length": 512,
  "metrics_correlation_window_seconds": 15,
  "one_per_session": ["Explore", "Plan"],
  "always_allowed": ["claude-code-guide", "statusline-setup"]
}
```

## Common Customizations

### Allow More Agents Per Session

For complex multi-agent workflows:

```json
{
  "max_agents": 10,
  "max_per_subagent_type": 3
}
```

### Stricter Enforcement

For cost-conscious users:

```json
{
  "max_agents": 3,
  "max_per_subagent_type": 1,
  "global_cooldown_seconds": 10,
  "one_per_session": [
    "Explore", "Plan", "master-coder",
    "master-researcher", "master-architect", "master-workflow"
  ]
}
```

### Disable Audit Logging

If you don't want session data written to disk:

```json
{
  "audit_log": false,
  "fault_audit": false
}
```

### Bypass for Specific Agent Types

Always allow certain agent types without any checks:

```json
{
  "always_allowed": [
    "claude-code-guide",
    "statusline-setup",
    "haiku"
  ]
}
```

### Strict Mode (Fail-Closed)

For power users who want enforcement even when the guard's state is degraded:

```json
{
  "failure_mode": "fail_closed"
}
```

In this mode, if the guard can't create its state directory or lock files, it blocks the tool call (exit 2) instead of allowing it through. Stdin parse errors still fail-open — the guard can't enforce rules without valid input. See `docs/security.md` for details.

### Shorter State Retention

Clean up state files more aggressively:

```json
{
  "state_ttl_hours": 4
}
```

## Configuration Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `schema_version` | int | 2 | Schema version for records |
| `max_agents` | int | 5 | Maximum total agents per session |
| `parallel_window_seconds` | int | 30 | Block same-type spawns within this window |
| `global_cooldown_seconds` | int | 5 | Minimum seconds between any spawns |
| `max_per_subagent_type` | int | 1 | Max instances of any single agent type |
| `state_ttl_hours` | int | 24 | Auto-cleanup session state after this |
| `audit_log` | bool | true | Enable/disable audit logging |
| `failure_mode` | string | "fail_open" | Error handling mode: "fail_open" or "fail_closed" |
| `sanitize_session_ids` | bool | true | Hash session IDs before use as filenames |
| `normalize_paths` | bool | true | Resolve symlinks and normalize file paths |
| `fault_audit` | bool | true | Log internal errors to fault log |
| `max_string_field_length` | int | 512 | Truncate strings in audit/metrics records |
| `metrics_correlation_window_seconds` | int | 15 | Window for correlating decisions with lifecycle events |
| `one_per_session` | list | ["Explore", "Plan"] | Types limited to exactly 1 per session |
| `always_allowed` | list | ["claude-code-guide", ...] | Types that bypass all enforcement |
