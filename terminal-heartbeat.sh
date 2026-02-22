#!/bin/bash
# Universal Terminal Heartbeat v2.1 — rate-limited, self-healing, versioned, injection-safe
# Triggered by PostToolUse on Edit|Write|Bash|Read
# Tracks: activity log, session liveness, files touched, tool counts, recent ops
#
# RATE LIMIT: Max 1 full heartbeat per 5 seconds per session.
# Between beats, only the activity log is appended (cheap).
#
# All jq calls use --arg for safe value passing (no string interpolation in filters).

INPUT=$(cat)

SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"')
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // "unknown"')
if [ "$TOOL_NAME" = "Bash" ]; then
  FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.command // "unknown"' | head -1 | cut -c1-80)
else
  FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // "unknown"')
fi
CWD=$(echo "$INPUT" | jq -r '.cwd // "unknown"')
PROJECT=$(basename "$CWD")
SID8="${SESSION_ID:0:8}"
FILE_BASE=$(basename "$FILE_PATH")

mkdir -p ~/.claude/terminals

NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ─── ACTIVITY LOG (always fires, very cheap) ───
jq -c -n --arg ts "$NOW" --arg session "$SID8" --arg tool "$TOOL_NAME" \
      --arg file "$FILE_BASE" --arg path "$FILE_PATH" --arg project "$PROJECT" \
      '{ts:$ts,session:$session,tool:$tool,file:$file,path:$path,project:$project}' \
  >> ~/.claude/terminals/activity.jsonl

# ─── RATE LIMIT CHECK ───
# Use a lock file with mtime as the rate limiter (5-second cooldown)
LOCK_FILE="/tmp/claude-heartbeat-${SID8}.lock"
COOLDOWN=5  # seconds

if [ -f "$LOCK_FILE" ]; then
  LOCK_AGE=$(( $(date +%s) - $(stat -f %m "$LOCK_FILE" 2>/dev/null || stat -c %Y "$LOCK_FILE" 2>/dev/null || echo 0) ))
  if [ "$LOCK_AGE" -lt "$COOLDOWN" ]; then
    exit 0  # Skip full heartbeat, activity log already written
  fi
fi
touch "$LOCK_FILE"

# ─── FULL HEARTBEAT (rate-limited to 1 per 5s) ───

# Capture TTY
RAW_TTY=$(ps -o tty= -p $PPID 2>/dev/null | sed 's/ //g')
CURR_TTY=""
[ -n "$RAW_TTY" ] && [ "$RAW_TTY" != "??" ] && CURR_TTY="/dev/$RAW_TTY"
HOST_PID="$PPID"

SESSION_FILE=~/.claude/terminals/session-${SID8}.json
SCHEMA_VERSION=2  # Increment when adding new fields

# Refresh iTerm2 tab name (once per ~60s, macOS only)
TAB_NAME_LOCK="/tmp/claude-tabname-${SID8}.lock"
DO_TABNAME=false
if [ ! -f "$TAB_NAME_LOCK" ]; then
  DO_TABNAME=true
else
  TAB_AGE=$(( $(date +%s) - $(stat -f %m "$TAB_NAME_LOCK" 2>/dev/null || echo 0) ))
  [ "$TAB_AGE" -gt 60 ] && DO_TABNAME=true
fi

if $DO_TABNAME && [ -n "$CURR_TTY" ] && command -v osascript &>/dev/null; then
  touch "$TAB_NAME_LOCK"
  TAB_NAME=$(osascript << APPLESCRIPT 2>/dev/null
tell application "iTerm2"
  repeat with w in windows
    repeat with aTab in tabs of w
      repeat with s in sessions of aTab
        if tty of s is "$CURR_TTY" then return name of s
      end repeat
    end repeat
  end repeat
end tell
APPLESCRIPT
)
  if [ -n "$TAB_NAME" ] && [ -f "$SESSION_FILE" ]; then
    TMP=$(mktemp)
    jq --arg name "$TAB_NAME" '.tab_name = $name' "$SESSION_FILE" > "$TMP" && mv "$TMP" "$SESSION_FILE"
  fi
fi

if [ -f "$SESSION_FILE" ]; then
  TMP=$(mktemp)

  # Use jq --arg for all dynamic values (safe against special chars in filenames)
  jq --arg now "$NOW" \
     --arg tool "$TOOL_NAME" \
     --arg file_base "$FILE_BASE" \
     --arg file_path "$FILE_PATH" \
     --arg tty "$CURR_TTY" \
     --argjson host_pid "$HOST_PID" \
     --argjson schema "$SCHEMA_VERSION" \
     --arg is_write_edit "$([ "$TOOL_NAME" = "Write" ] || [ "$TOOL_NAME" = "Edit" ] && echo "yes" || echo "no")" \
     '
     .last_active = $now |
     .last_tool = $tool |
     .last_file = $file_base |
     .schema_version = $schema |
     .host_pid = $host_pid |
     (if $tty != "" then .tty = $tty else . end) |
     .tool_counts = ((.tool_counts // {}) | .[$tool] = ((.[$tool] // 0) + 1)) |
     (if $is_write_edit == "yes" then
       .files_touched = (((.files_touched // []) | map(select(. != $file_path))) + [$file_path])[-30:]
     else . end) |
     .recent_ops = (((.recent_ops // []) + [{"t": $now, "tool": $tool, "file": $file_base}])[-10:])
     ' "$SESSION_FILE" > "$TMP" 2>/dev/null && mv "$TMP" "$SESSION_FILE"
else
  # Fallback: create session file from PostToolUse context using jq (safe JSON construction)
  BRANCH=$(cd "$CWD" 2>/dev/null && git branch --show-current 2>/dev/null || echo "none")

  jq -n \
     --arg session "$SID8" \
     --arg project "$PROJECT" \
     --arg branch "$BRANCH" \
     --arg cwd "$CWD" \
     --arg now "$NOW" \
     --arg tool "$TOOL_NAME" \
     --arg file_base "$FILE_BASE" \
     --arg tty "$CURR_TTY" \
     --argjson host_pid "$HOST_PID" \
     --argjson schema "$SCHEMA_VERSION" \
     '
     {
       session: $session,
       status: "active",
       project: $project,
       branch: $branch,
       cwd: $cwd,
       transcript: "unknown",
       started: $now,
       last_active: $now,
       last_tool: $tool,
       last_file: $file_base,
       source: "heartbeat-fallback",
       host_pid: $host_pid,
       schema_version: $schema,
       tool_counts: {($tool): 1},
       files_touched: [],
       recent_ops: [{"t": $now, "tool": $tool, "file": $file_base}]
     } |
     (if $tty != "" then .tty = $tty else . end)
     ' > "$SESSION_FILE"
fi

# Track plan file writes (using --arg for safe path handling)
case "$FILE_PATH" in
  */.claude/plans/*.md)
    if [ -f "$SESSION_FILE" ]; then
      TMP=$(mktemp)
      jq --arg plan "$FILE_PATH" '.plan_file = $plan' "$SESSION_FILE" > "$TMP" && mv "$TMP" "$SESSION_FILE"
    fi
    ;;
esac

# ─── AUTO-STALE: Mark other sessions stale if inactive >1h ───
# Only check every 60s (not every heartbeat) by using a separate lock
STALE_LOCK="/tmp/claude-stale-check.lock"
STALE_COOLDOWN=60

DO_STALE=false
if [ ! -f "$STALE_LOCK" ]; then
  DO_STALE=true
else
  STALE_AGE=$(( $(date +%s) - $(stat -f %m "$STALE_LOCK" 2>/dev/null || stat -c %Y "$STALE_LOCK" 2>/dev/null || echo 0) ))
  [ "$STALE_AGE" -gt "$STALE_COOLDOWN" ] && DO_STALE=true
fi

if $DO_STALE; then
  touch "$STALE_LOCK"
  NOW_EPOCH=$(date +%s)
  for sf in ~/.claude/terminals/session-*.json; do
    [ -f "$sf" ] || continue
    [ "$sf" = "$SESSION_FILE" ] && continue

    SF_STATUS=$(jq -r '.status // "unknown"' "$sf" 2>/dev/null)
    [ "$SF_STATUS" != "active" ] && continue

    SF_LAST=$(jq -r '.last_active // "1970-01-01T00:00:00Z"' "$sf" 2>/dev/null)
    SF_EPOCH=$(date -jf "%Y-%m-%dT%H:%M:%SZ" "$SF_LAST" +%s 2>/dev/null || date -d "$SF_LAST" +%s 2>/dev/null || echo 0)

    AGE=$(( NOW_EPOCH - SF_EPOCH ))
    if [ "$AGE" -gt 3600 ]; then
      TMP=$(mktemp)
      jq '.status = "stale"' "$sf" > "$TMP" 2>/dev/null && mv "$TMP" "$sf"
    fi
  done
fi

# Auto-truncate activity log
LINES=$(wc -l < ~/.claude/terminals/activity.jsonl 2>/dev/null || echo 0)
[ "$LINES" -gt 600 ] && tail -500 ~/.claude/terminals/activity.jsonl > ~/.claude/terminals/activity.tmp && mv ~/.claude/terminals/activity.tmp ~/.claude/terminals/activity.jsonl

# Emit team hook events (TeammateIdle scan) based on session liveness.
python3 ~/.claude/scripts/team_runtime.py hook heartbeat --session-id "$SID8" >/dev/null 2>&1 || true

exit 0
