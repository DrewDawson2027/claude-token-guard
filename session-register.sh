#!/bin/bash
# Universal Session Registry — registers EVERY Claude Code session with full metadata
# Triggered by SessionStart hook
# Captures transcript_path so the lead can read other sessions' conversations
INPUT=$(cat)

# Debug logging — gated behind CLAUDE_DEBUG env var
mkdir -p ~/.claude/terminals
if [ "${CLAUDE_DEBUG:-}" = "1" ]; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) RAW_INPUT: $INPUT" >> ~/.claude/terminals/debug-session-register.log
fi

SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"')
CWD=$(echo "$INPUT" | jq -r '.cwd // "unknown"')
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // "unknown"')
SOURCE=$(echo "$INPUT" | jq -r '.source // "startup"')

if [ "${CLAUDE_DEBUG:-}" = "1" ]; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) PARSED: session=$SESSION_ID cwd=$CWD source=$SOURCE" >> ~/.claude/terminals/debug-session-register.log
fi

PROJECT=$(basename "$CWD")
BRANCH=$(cd "$CWD" 2>/dev/null && git branch --show-current 2>/dev/null || echo "none")

# Append to session log (safe JSON via jq --arg to prevent injection)
jq -c -n \
  --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg session "${SESSION_ID:0:8}" \
  --arg source "$SOURCE" \
  --arg project "$PROJECT" \
  --arg branch "$BRANCH" \
  --arg cwd "$CWD" \
  --arg transcript "$TRANSCRIPT" \
  '{ts:$ts,session:$session,event:"start",source:$source,project:$project,branch:$branch,cwd:$cwd,transcript:$transcript}' \
  >> ~/.claude/terminals/sessions.jsonl

# Capture TTY for reliable tab targeting by coord_wake_session
# Hooks run in pipe context so tty always fails — use ps to get parent's TTY
RAW_TTY=$(ps -o tty= -p $PPID 2>/dev/null | sed 's/ //g')
TTY=""
[ -n "$RAW_TTY" ] && [ "$RAW_TTY" != "??" ] && TTY="/dev/$RAW_TTY"

# Write per-session status file for quick lookup by lead (safe JSON via jq)
jq -c -n \
  --arg session "${SESSION_ID:0:8}" \
  --arg project "$PROJECT" \
  --arg branch "$BRANCH" \
  --arg cwd "$CWD" \
  --arg transcript "$TRANSCRIPT" \
  --arg started "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg last_active "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{session:$session,status:"active",project:$project,branch:$branch,cwd:$cwd,transcript:$transcript,started:$started,last_active:$last_active}' \
  > ~/.claude/terminals/session-${SESSION_ID:0:8}.json

# Add TTY if available (conditional jq merge)
if [ -n "$TTY" ]; then
  TMP=$(mktemp)
  jq --arg tty "$TTY" '. + {tty: $tty}' ~/.claude/terminals/session-${SESSION_ID:0:8}.json > "$TMP" && \
    mv "$TMP" ~/.claude/terminals/session-${SESSION_ID:0:8}.json
fi

# Auto-truncate sessions log
LINES=$(wc -l < ~/.claude/terminals/sessions.jsonl 2>/dev/null || echo 0)
if [ "$LINES" -gt 200 ]; then
  tail -150 ~/.claude/terminals/sessions.jsonl > ~/.claude/terminals/sessions.tmp
  mv ~/.claude/terminals/sessions.tmp ~/.claude/terminals/sessions.jsonl
fi

# Bootstrap session cache for cross-agent context sharing
CACHE_DIR="$HOME/.claude/session-cache"
mkdir -p "$CACHE_DIR"

# Create cache files with schema headers if they don't exist
if [ ! -f "$CACHE_DIR/coder-context.md" ]; then
  cat > "$CACHE_DIR/coder-context.md" << 'SCHEMA'
# Session Cache: coder-context.md
# Agents write findings here for cross-agent reuse.

## Files Read

## Patterns Found

## Architecture Notes
SCHEMA
fi
if [ ! -f "$CACHE_DIR/research-cache.md" ]; then
  cat > "$CACHE_DIR/research-cache.md" << 'SCHEMA'
# Session Cache: research-cache.md
# Agents write findings here for cross-agent reuse.

## Queries Used

## Sources Found

## Key Findings
SCHEMA
fi
if [ ! -f "$CACHE_DIR/design-decisions.md" ]; then
  cat > "$CACHE_DIR/design-decisions.md" << 'SCHEMA'
# Session Cache: design-decisions.md
# Agents write findings here for cross-agent reuse.

## ADRs

## Tech Selections

## Trade-offs
SCHEMA
fi

# Clean stale cache entries (older than 24h)
find "$CACHE_DIR" -name "*.md" -mmin +1440 -exec sh -c 'echo "# Session Cache: $(basename {})" > {}' \; 2>/dev/null

# Fix 1: Set terminal tab title to session ID for wake targeting by coord_wake_session
printf '\e]0;claude-%s\a' "${SESSION_ID:0:8}"

exit 0
