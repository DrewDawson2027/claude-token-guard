#!/bin/bash
# Agent Lifecycle Metrics — logs subagent start/stop for duration tracking and cost analysis
# Triggered by SubagentStart and SubagentStop hooks
# Part of the Master Agent System's observability layer
set -u
INPUT=$(cat)

HOOKS_DIR="$HOME/.claude/hooks"
METRICS_DIR="$HOME/.claude/hooks/session-state"
METRICS_FILE="$METRICS_DIR/agent-metrics.jsonl"
mkdir -p "$METRICS_DIR"

# Normalize all fields via Python contracts module (single source of truth)
NORMALIZED=$(python3 - <<'PY' "$INPUT"
import json, sys
sys.path.insert(0, sys.argv[0].rsplit('/', 1)[0] if '/' in sys.argv[0] else '.')
import os
sys.path.insert(0, os.path.expanduser('~/.claude/hooks'))
from guard_normalize import normalize_hook_payload
try:
    raw = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
except (json.JSONDecodeError, ValueError):
    raw = {}
p = normalize_hook_payload(raw)
print(json.dumps(p))
PY
2>/dev/null)

if [ -z "$NORMALIZED" ]; then
  # Fallback: extract raw fields if Python normalization fails
  EVENT=$(echo "$INPUT" | jq -r '.hook_event_name // "unknown"')
  AGENT_TYPE_SAFE="unknown"
  AGENT_ID_SAFE="unknown"
  SESSION_KEY="unknown"
  SESSION_ID="unknown"
else
  EVENT=$(echo "$NORMALIZED" | jq -r '.hook_event_name // "unknown"')
  AGENT_TYPE_SAFE=$(echo "$NORMALIZED" | jq -r '.agent_type // "unknown"')
  AGENT_ID_SAFE=$(echo "$NORMALIZED" | jq -r '.agent_id // "unknown"')
  SESSION_KEY=$(echo "$NORMALIZED" | jq -r '.session_key // "unknown"')
  SESSION_ID=$(echo "$NORMALIZED" | jq -r '.session_id // "unknown"')
fi

consume_pending_decision() {
  python3 - "$HOME" "$SESSION_KEY" "$AGENT_TYPE_SAFE" <<'PY'
import json, os, sys, time
home, session_key, agent_type = sys.argv[1], sys.argv[2], sys.argv[3]
state_file = os.path.join(home, '.claude', 'hooks', 'session-state', f'{session_key}.json')
try:
    with open(state_file, 'r') as f:
        state = json.load(f)
except Exception:
    print('')
    raise SystemExit(0)
spawns = state.get('pending_spawns') or []
chosen = None
for spawn in reversed(spawns):
    if spawn.get('consumed'):
        continue
    if str(spawn.get('type', '')) != str(agent_type):
        continue
    chosen = spawn
    break
if chosen is None:
    print('')
    raise SystemExit(0)
chosen['consumed'] = True
chosen['agent_id'] = os.environ.get('AGENT_ID_FOR_CONSUME', '')
chosen['consumed_ts'] = time.time()
try:
    with open(state_file, 'w') as f:
        json.dump(state, f, indent=2)
except Exception:
    pass
print(str(chosen.get('decision_id', ''))[:32])
PY
}

lookup_start_decision() {
  if [ ! -f "$METRICS_FILE" ]; then
    echo ""
    return
  fi
  grep '"event":"start"' "$METRICS_FILE" 2>/dev/null | grep "\"agent_id\":\"$AGENT_ID_SAFE\"" | tail -1 | jq -r '.decision_id // ""' 2>/dev/null || echo ""
}

if [ "$EVENT" = "SubagentStart" ]; then
  export AGENT_ID_FOR_CONSUME="$AGENT_ID_SAFE"
  DECISION_ID=$(consume_pending_decision)
  jq -c -n \
    --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg event "start" \
    --arg agent_type "$AGENT_TYPE_SAFE" \
    --arg agent_id "$AGENT_ID_SAFE" \
    --arg session "$SESSION_KEY" \
    --arg session_key "$SESSION_KEY" \
    --arg decision_id "${DECISION_ID:-}" \
    '{schema_version:2,record_type:"lifecycle",ts:$ts,event:$event,agent_type:$agent_type,agent_id:$agent_id,session:$session,session_key:$session_key,decision_id:$decision_id}' \
    >> "$METRICS_FILE"

elif [ "$EVENT" = "SubagentStop" ]; then
  # Calculate duration if we have a start timestamp
  START_TS=$(grep "\"agent_id\":\"$AGENT_ID_SAFE\"" "$METRICS_FILE" 2>/dev/null | grep '"event":"start"' | tail -1 | jq -r '.ts // empty')
  DURATION=""
  DURATION_KNOWN=false
  if [ -n "$START_TS" ]; then
    START_EPOCH=$(date -jf "%Y-%m-%dT%H:%M:%SZ" "$START_TS" "+%s" 2>/dev/null || date -d "$START_TS" "+%s" 2>/dev/null || echo "")
    END_EPOCH=$(date -u "+%s")
    if [ -n "$START_EPOCH" ]; then
      DURATION=$((END_EPOCH - START_EPOCH))
      DURATION_KNOWN=true
    fi
  fi
  DECISION_ID=$(lookup_start_decision)

  if [ "$DURATION_KNOWN" = true ]; then
    jq -c -n \
      --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --arg event "stop" \
      --arg agent_type "$AGENT_TYPE_SAFE" \
      --arg agent_id "$AGENT_ID_SAFE" \
      --arg session "$SESSION_KEY" \
      --arg session_key "$SESSION_KEY" \
      --arg decision_id "${DECISION_ID:-}" \
      --argjson duration "$DURATION" \
      '{schema_version:2,record_type:"lifecycle",ts:$ts,event:$event,agent_type:$agent_type,agent_id:$agent_id,session:$session,session_key:$session_key,decision_id:$decision_id,duration_seconds:$duration,duration_known:true}' \
      >> "$METRICS_FILE"
  else
    jq -c -n \
      --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --arg event "stop" \
      --arg agent_type "$AGENT_TYPE_SAFE" \
      --arg agent_id "$AGENT_ID_SAFE" \
      --arg session "$SESSION_KEY" \
      --arg session_key "$SESSION_KEY" \
      --arg decision_id "${DECISION_ID:-}" \
      '{schema_version:2,record_type:"lifecycle",ts:$ts,event:$event,agent_type:$agent_type,agent_id:$agent_id,session:$session,session_key:$session_key,decision_id:$decision_id,duration_seconds:"unknown",duration_known:false}' \
      >> "$METRICS_FILE"
  fi
fi

# Auto-truncate metrics log (keep last 500 entries)
if [ -f "$METRICS_FILE" ]; then
  LINES=$(wc -l < "$METRICS_FILE" 2>/dev/null | tr -d ' ')
  if [ "$LINES" -gt 500 ]; then
    tail -400 "$METRICS_FILE" > "$METRICS_FILE.tmp"
    mv "$METRICS_FILE.tmp" "$METRICS_FILE"
  fi
fi

exit 0
