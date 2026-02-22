#!/bin/bash
# Hook Health Check — validates all hooks are working and provides audit stats
#
# Usage:
#   bash ~/.claude/hooks/health-check.sh           # Full health check
#   bash ~/.claude/hooks/health-check.sh --stats   # Token guard audit stats
#   bash ~/.claude/hooks/health-check.sh --cleanup  # Prune stale session state
#
# Part of the Token Management System:
#   token-guard.py          → blocks illegal agent spawns (PreToolUse)
#   read-efficiency-guard.py → blocks wasteful reads (PreToolUse, matcher: Read)
#   health-check.sh         → validates + reports (manual)

STATE_DIR="$HOME/.claude/hooks/session-state"
AUDIT_LOG="$STATE_DIR/audit.jsonl"

# --cleanup: remove stale session state files (>24h old)
if [ "$1" = "--cleanup" ]; then
  COUNT=$(find "$STATE_DIR" -name "*.json" -not -name "audit.jsonl" -mtime +1 2>/dev/null | wc -l | tr -d ' ')
  find "$STATE_DIR" -name "*.json" -not -name "audit.jsonl" -mtime +1 -delete 2>/dev/null
  find "$STATE_DIR" -name "*.lock" -mtime +1 -delete 2>/dev/null
  echo "Cleaned $COUNT stale session state files"
  exit 0
fi

# --stats: show token guard audit statistics
if [ "$1" = "--stats" ]; then
  echo ""
  echo "=== Token Guard Audit Stats ==="
  echo ""

  if [ ! -f "$AUDIT_LOG" ]; then
    echo "  No audit log found. Stats will appear after token-guard.py runs."
    echo "  Expected location: $AUDIT_LOG"
    exit 0
  fi

  TOTAL=$(wc -l < "$AUDIT_LOG" | tr -d ' ')
  BLOCKS=$(grep -c '"event": "block"' "$AUDIT_LOG" 2>/dev/null || echo 0)
  ALLOWS=$(grep -c '"event": "allow"' "$AUDIT_LOG" 2>/dev/null || echo 0)

  if [ "$TOTAL" -gt 0 ]; then
    RATE=$((BLOCKS * 100 / TOTAL))
  else
    RATE=0
  fi

  # Most blocked type
  if [ "$BLOCKS" -gt 0 ]; then
    MOST_BLOCKED=$(grep '"event": "block"' "$AUDIT_LOG" | \
      grep -o '"type": "[^"]*"' | sort | uniq -c | sort -rn | head -1 | \
      awk '{print $3 " (" $1 ")"}' | tr -d '"')
  else
    MOST_BLOCKED="none"
  fi

  # Unique sessions
  SESSIONS=$(grep -o '"session": "[^"]*"' "$AUDIT_LOG" | sort -u | wc -l | tr -d ' ')

  # Last 7 days only
  WEEK_AGO=$(date -v-7d +%Y-%m-%d 2>/dev/null || date -d "7 days ago" +%Y-%m-%d 2>/dev/null || echo "0000-00-00")
  RECENT_BLOCKS=$(awk -v cutoff="$WEEK_AGO" -F'"ts": "' '{split($2,a,"\""); if(a[1] >= cutoff) print}' "$AUDIT_LOG" | grep -c '"event": "block"' 2>/dev/null || echo 0)
  RECENT_ALLOWS=$(awk -v cutoff="$WEEK_AGO" -F'"ts": "' '{split($2,a,"\""); if(a[1] >= cutoff) print}' "$AUDIT_LOG" | grep -c '"event": "allow"' 2>/dev/null || echo 0)
  RECENT_TOTAL=$((RECENT_BLOCKS + RECENT_ALLOWS))

  if [ "$RECENT_TOTAL" -gt 0 ]; then
    RECENT_RATE=$((RECENT_BLOCKS * 100 / RECENT_TOTAL))
  else
    RECENT_RATE=0
  fi

  echo "  All Time:"
  echo "    Total decisions:  $TOTAL"
  echo "    Blocks:           $BLOCKS"
  echo "    Allows:           $ALLOWS"
  echo "    Block rate:       ${RATE}%"
  echo "    Most blocked:     $MOST_BLOCKED"
  echo "    Sessions tracked: $SESSIONS"
  echo ""
  echo "  Last 7 Days:"
  echo "    Decisions:        $RECENT_TOTAL"
  echo "    Blocks:           $RECENT_BLOCKS"
  echo "    Allows:           $RECENT_ALLOWS"
  echo "    Block rate:       ${RECENT_RATE}%"
  echo ""

  # Show recent blocks detail
  if [ "$BLOCKS" -gt 0 ]; then
    echo "  Recent Blocks (last 5):"
    grep '"event": "block"' "$AUDIT_LOG" | tail -5 | while read -r line; do
      TS=$(echo "$line" | grep -o '"ts": "[^"]*"' | cut -d'"' -f4)
      TYPE=$(echo "$line" | grep -o '"type": "[^"]*"' | cut -d'"' -f4)
      REASON=$(echo "$line" | grep -o '"reason": "[^"]*"' | cut -d'"' -f4)
      echo "    $TS  $TYPE  ($REASON)"
    done
  fi

  echo ""
  exit 0
fi

# Default: full health check
echo ""
echo "=== Claude Code Hook Health Check ==="
echo ""

PASS=0
FAIL=0
WARN=0

check() {
  local name="$1" file="$2" required="$3"
  if [ ! -f "$file" ]; then
    if [ "$required" = "required" ]; then
      echo "  FAIL  $name — file missing: $file"
      FAIL=$((FAIL + 1))
    else
      echo "  SKIP  $name — not installed"
    fi
    return
  fi
  if [ ! -x "$file" ] && [[ "$file" == *.sh ]]; then
    echo "  FAIL  $name — not executable: $file"
    FAIL=$((FAIL + 1))
    return
  fi
  # Check syntax
  if [[ "$file" == *.sh ]]; then
    if bash -n "$file" 2>/dev/null; then
      echo "  PASS  $name"
      PASS=$((PASS + 1))
    else
      echo "  FAIL  $name — syntax error"
      FAIL=$((FAIL + 1))
    fi
  elif [[ "$file" == *.py ]]; then
    if python3 -c "import py_compile, sys; py_compile.compile(sys.argv[1], doraise=True)" "$file" 2>/dev/null; then
      echo "  PASS  $name"
      PASS=$((PASS + 1))
    else
      echo "  FAIL  $name — syntax error"
      FAIL=$((FAIL + 1))
    fi
  elif [[ "$file" == *.js ]]; then
    if node --check "$file" 2>/dev/null; then
      echo "  PASS  $name"
      PASS=$((PASS + 1))
    else
      echo "  FAIL  $name — syntax error"
      FAIL=$((FAIL + 1))
    fi
  fi
}

echo "Hooks:"
check "terminal-heartbeat" ~/.claude/hooks/terminal-heartbeat.sh required
check "session-register" ~/.claude/hooks/session-register.sh required
check "check-inbox" ~/.claude/hooks/check-inbox.sh required
check "session-end" ~/.claude/hooks/session-end.sh required
check "token-guard" ~/.claude/hooks/token-guard.py required
check "read-efficiency-guard" ~/.claude/hooks/read-efficiency-guard.py required
check "hook-utils" ~/.claude/hooks/hook_utils.py required

echo ""
echo "MCP Coordinator:"
check "coordinator" ~/.claude/mcp-coordinator/index.js required

echo ""
echo "Token Management:"
if [ -f ~/.claude/hooks/token-guard-config.json ]; then
  if python3 -c "import json; json.load(open('$HOME/.claude/hooks/token-guard-config.json'))" 2>/dev/null; then
    MAX_AGENTS=$(python3 -c "import json; print(json.load(open('$HOME/.claude/hooks/token-guard-config.json')).get('max_agents', '?'))" 2>/dev/null)
    CONFIG_SCHEMA=$(python3 -c "import json; print(json.load(open('$HOME/.claude/hooks/token-guard-config.json')).get('schema_version', 1))" 2>/dev/null)
    echo "  PASS  config valid (schema_version=$CONFIG_SCHEMA, max_agents=$MAX_AGENTS)"
    PASS=$((PASS + 1))
    if [ "$CONFIG_SCHEMA" -lt 2 ] 2>/dev/null; then
      echo "  WARN  config schema_version < 2 (upgrade recommended)"
      WARN=$((WARN + 1))
    fi
  else
    echo "  FAIL  config is invalid JSON"
    FAIL=$((FAIL + 1))
  fi
else
  echo "  WARN  no config file (using defaults)"
  WARN=$((WARN + 1))
fi

STATE_COUNT=$(ls "$STATE_DIR"/*.json 2>/dev/null | grep -v audit.jsonl | wc -l | tr -d ' ')
echo "  INFO  $STATE_COUNT active session state files"

if [ -f "$AUDIT_LOG" ]; then
  AUDIT_LINES=$(wc -l < "$AUDIT_LOG" | tr -d ' ')
  echo "  INFO  audit log: $AUDIT_LINES entries"
else
  echo "  INFO  audit log: not yet created (will appear after first Task call)"
fi

METRICS_LOG="$STATE_DIR/agent-metrics.jsonl"
if [ -f "$AUDIT_LOG" ] || [ -f "$METRICS_LOG" ]; then
  DQ_OUT=$(python3 - <<PY 2>/dev/null
import json, os
state_dir = os.path.expanduser("$STATE_DIR")
audit = os.path.join(state_dir, "audit.jsonl")
metrics = os.path.join(state_dir, "agent-metrics.jsonl")
invalid_legacy_session = 0
v2_audit = 0
v1_audit = 0
faults = 0
empty_agent_type = 0
untagged_metrics = 0
def lines(path):
    if not os.path.isfile(path): return []
    out = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
for e in lines(audit):
    if int(e.get("schema_version", 1) or 1) >= 2:
        v2_audit += 1
    else:
        v1_audit += 1
    s = str(e.get("session", "")) if "session" in e else ""
    if ("/" in s or ".." in s or "\\" in s):
        invalid_legacy_session += 1
    if e.get("event") == "fault":
        faults += 1
for m in lines(metrics):
    if "record_type" not in m:
        untagged_metrics += 1
    if m.get("event") == "agent_completed" and not str(m.get("agent_type", "")).strip():
        empty_agent_type += 1
print(f"audit_v2={v2_audit} audit_v1={v1_audit} invalid_legacy_session={invalid_legacy_session} faults={faults} untagged_metrics={untagged_metrics} empty_agent_type={empty_agent_type}")
PY
)
  [ -n "$DQ_OUT" ] && echo "  INFO  data-quality: $DQ_OUT"
fi

REPO_ROOT="$HOME/Projects/claude-lead-system"
if [ -d "$REPO_ROOT/hooks" ]; then
  DRIFT_COUNT=$(python3 - <<PY 2>/dev/null
import filecmp, os
home = os.path.expanduser("~")
repo = os.path.join(home, "Projects", "claude-lead-system", "hooks")
live = os.path.join(home, ".claude", "hooks")
files = ["token-guard.py","read-efficiency-guard.py","agent-metrics.py","self-heal.py","health-check.sh","hook_utils.py","token-guard-config.json"]
drift = 0
for name in files:
    a = os.path.join(repo, name)
    b = os.path.join(live, name)
    if os.path.isfile(a) and os.path.isfile(b) and not filecmp.cmp(a, b, shallow=False):
        drift += 1
print(drift)
PY
)
  echo "  INFO  repo/live hook drift count: ${DRIFT_COUNT:-unknown}"
fi

echo ""
echo "Dependencies:"
if command -v jq &>/dev/null; then
  echo "  PASS  jq installed ($(jq --version 2>/dev/null))"
  PASS=$((PASS + 1))
else
  echo "  FAIL  jq not installed — heartbeat won't work"
  FAIL=$((FAIL + 1))
fi

if command -v node &>/dev/null; then
  echo "  PASS  node installed ($(node --version 2>/dev/null))"
  PASS=$((PASS + 1))
else
  echo "  FAIL  node not installed — MCP coordinator won't work"
  FAIL=$((FAIL + 1))
fi

echo ""
echo "Settings:"
if [ -f ~/.claude/settings.local.json ]; then
  # Check heartbeat is registered
  if jq -e '.hooks.PostToolUse[].hooks[]? | select(.command | contains("terminal-heartbeat"))' ~/.claude/settings.local.json &>/dev/null; then
    echo "  PASS  heartbeat registered in PostToolUse"
    PASS=$((PASS + 1))
  else
    echo "  FAIL  heartbeat NOT registered in PostToolUse"
    FAIL=$((FAIL + 1))
  fi
  if jq -e '.hooks.PreToolUse[].hooks[]? | select(.command | contains("check-inbox"))' ~/.claude/settings.local.json &>/dev/null; then
    echo "  PASS  inbox hook registered in PreToolUse"
    PASS=$((PASS + 1))
  else
    if jq -e '.hooks.PreToolUse[].hooks[]? | select(.command | contains("check-inbox"))' ~/.claude/settings.json &>/dev/null 2>/dev/null; then
      echo "  PASS  inbox hook registered in global settings"
      PASS=$((PASS + 1))
    else
      echo "  WARN  inbox hook not found (messaging may not work)"
      WARN=$((WARN + 1))
    fi
  fi
else
  echo "  FAIL  settings.local.json not found"
  FAIL=$((FAIL + 1))
fi

# Check token-guard is registered in global settings
if [ -f ~/.claude/settings.json ]; then
  if jq -e '.hooks.PreToolUse[].hooks[]? | select(.command? // "" | contains("token-guard"))' ~/.claude/settings.json &>/dev/null; then
    echo "  PASS  token-guard registered in PreToolUse"
    PASS=$((PASS + 1))
  else
    echo "  FAIL  token-guard NOT registered in PreToolUse"
    FAIL=$((FAIL + 1))
  fi
  if jq -e '.hooks.PreToolUse[].hooks[]? | select(.command? // "" | contains("read-efficiency-guard"))' ~/.claude/settings.json &>/dev/null; then
    echo "  PASS  read-efficiency-guard registered in PreToolUse"
    PASS=$((PASS + 1))
  else
    echo "  FAIL  read-efficiency-guard NOT registered in PreToolUse"
    FAIL=$((FAIL + 1))
  fi
fi

echo ""
echo "Master Agents:"
AGENT_PASS=0
AGENT_FAIL=0
for agent in master-coder master-researcher master-architect master-workflow; do
  if [ -f ~/.claude/agents/${agent}.md ]; then
    echo "  PASS  ${agent}.md"
    AGENT_PASS=$((AGENT_PASS + 1))
    PASS=$((PASS + 1))
  else
    echo "  FAIL  ${agent}.md — missing"
    AGENT_FAIL=$((AGENT_FAIL + 1))
    FAIL=$((FAIL + 1))
  fi
done
if [ -f ~/.claude/master-agents/MANIFEST.md ]; then
  echo "  PASS  MANIFEST.md"
  PASS=$((PASS + 1))
else
  echo "  FAIL  MANIFEST.md — missing"
  FAIL=$((FAIL + 1))
fi
MODE_COUNT=$(find ~/.claude/master-agents -name "*.md" -not -name "MANIFEST.md" -not -path "*/refs/*" 2>/dev/null | wc -l | tr -d ' ')
echo "  INFO  $MODE_COUNT mode files found (expected 17)"
if [ "$MODE_COUNT" -lt 17 ]; then
  echo "  WARN  some mode files may be missing"
  WARN=$((WARN + 1))
fi

echo ""
echo "Session Files:"
ACTIVE=$(ls ~/.claude/terminals/session-*.json 2>/dev/null | wc -l | tr -d ' ')
echo "  INFO  $ACTIVE session file(s) on disk"

echo ""
echo "Activity Log:"
if [ -f ~/.claude/terminals/activity.jsonl ]; then
  LINES=$(wc -l < ~/.claude/terminals/activity.jsonl | tr -d ' ')
  LAST=$(tail -1 ~/.claude/terminals/activity.jsonl 2>/dev/null | jq -r '.ts // "unknown"' 2>/dev/null)
  echo "  INFO  $LINES entries, last: $LAST"
else
  echo "  WARN  no activity log yet"
  WARN=$((WARN + 1))
fi

echo ""
echo "─────────────────────────────────"
echo "  Results: $PASS passed, $FAIL failed, $WARN warnings"
if [ "$FAIL" -gt 0 ]; then
  echo "  STATUS: UNHEALTHY — fix the failures above"
  exit 1
else
  echo "  STATUS: HEALTHY"
  exit 0
fi
