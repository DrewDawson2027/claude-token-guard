#!/bin/bash
# PreToolUse inbox check — surfaces messages from lead/other terminals
# Runs before EVERY tool call. If inbox has messages, prints them so the model sees them.
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"')
INBOX=~/.claude/terminals/inbox/${SESSION_ID:0:8}.jsonl

if [ -f "$INBOX" ] && [ -s "$INBOX" ]; then
  echo "--- INCOMING MESSAGES FROM COORDINATOR ---"
  cat "$INBOX"
  echo "--- END MESSAGES ---"
  # Move to .processed instead of truncating — recoverable if Claude crashes
  mv "$INBOX" "${INBOX}.processed"
fi

# Fix 2: Check for completed workers and notify lead
RESULTS_DIR=~/.claude/terminals/results
for donefile in "$RESULTS_DIR"/*.meta.json.done; do
  [ -f "$donefile" ] || continue
  TASK_ID=$(basename "$donefile" .meta.json.done)
  REPORTED="$RESULTS_DIR/${TASK_ID}.reported"
  if [ ! -f "$REPORTED" ]; then
    echo "--- WORKER COMPLETED: $TASK_ID ---"
    cat "$donefile"
    # Show last 20 lines of output
    tail -20 "$RESULTS_DIR/${TASK_ID}.txt" 2>/dev/null
    echo "--- END WORKER RESULT ---"
    touch "$REPORTED"
  fi
done

exit 0
