#!/bin/bash
# Session End — marks session as closed with final stats preserved
# Triggered by SessionEnd hook
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"')

SESSION_FILE=~/.claude/terminals/session-${SESSION_ID:0:8}.json
if [ -f "$SESSION_FILE" ]; then
  TMP=$(mktemp)
  # Mark closed but preserve files_touched, tool_counts, recent_ops for lead review
  jq '.status = "closed" | .ended = "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"' "$SESSION_FILE" > "$TMP" && mv "$TMP" "$SESSION_FILE"
fi

exit 0
