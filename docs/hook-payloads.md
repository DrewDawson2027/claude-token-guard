# Hook Payload Shapes

Documented from live runtime observation. These are the JSON objects piped to hooks via stdin.

## PreToolUse (token-guard.py, read-efficiency-guard.py)

```json
{
  "tool_name": "Task",
  "tool_input": {
    "subagent_type": "Explore",
    "description": "Search codebase for patterns",
    "prompt": "GOAL: Find all API endpoints...",
    "model": "sonnet",
    "resume": "agent-id-string",
    "team_name": "my-team",
    "run_in_background": true
  },
  "session_id": "b501172c-a133-49da-be99-46aff354ae2d"
}
```

**Notes:**
- `tool_input` shape varies by `tool_name` — above is for `Task`
- For `Read`: `tool_input = {"file_path": "/absolute/path"}`
- `session_id` is a raw UUID — must be sanitized before persisting

## SubagentStart (agent-lifecycle.sh)

```json
{
  "hook_event_name": "SubagentStart",
  "agent_type": "Explore",
  "agent_id": "af59e681fbcbea278",
  "session_id": "b501172c-a133-49da-be99-46aff354ae2d"
}
```

**Known gaps:**
- `agent_type` is sometimes empty string (Claude Code doesn't always propagate it)

## SubagentStop (agent-lifecycle.sh, agent-metrics.py)

```json
{
  "hook_event_name": "SubagentStop",
  "agent_type": "",
  "agent_id": "af59e681fbcbea278",
  "session_id": "b501172c-a133-49da-be99-46aff354ae2d",
  "agent_transcript_path": "/path/to/transcript.jsonl"
}
```

**Known gaps:**
- `agent_type` is frequently empty in SubagentStop (105/106 observed records)
- `agent_transcript_path` is not always present
- When transcript exists, it may yield 0 parseable usage records

## SessionStart (self-heal.py)

No stdin payload — triggered on session boot. Uses environment only.

## PreCompact (pre-compact-save.sh)

No stdin payload documented. Triggered before context compaction.

## Read Guard Thresholds

Current enforcement values in `read-efficiency-guard.py`:

| Parameter | Value | Effect |
|-----------|-------|--------|
| `DUPLICATE_FILE_LIMIT` | 3 | BLOCK after 3 reads of the same file path |
| `SEQUENTIAL_THRESHOLD` | 4 | WARN after 4 reads within the window |
| `ESCALATION_THRESHOLD` | 15 | BLOCK after 15 reads within the window |
| `SEQUENTIAL_WINDOW` | 120s | Time window for sequential read counting |
| `READ_TTL` | 300s | Prune read records older than 5 minutes |

**Path normalization:** All file paths are canonicalized via `normalize_file_path()` which
calls `os.path.expanduser()` → `os.path.normpath()` → `os.path.realpath()`. This handles:
- `~/file.py` → `/Users/<user>/file.py`
- `/a/b/../c/file.py` → `/a/c/file.py`
- `/tmp/x` → `/private/tmp/x` (macOS symlink resolution)
- Symlinked paths → resolved to real target
