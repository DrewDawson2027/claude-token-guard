# Manual Installation (Without pip)

If you prefer not to use pip, you can install Token Guard manually by copying files and editing your settings.

## Step 1: Clone the Repository

```bash
git clone https://github.com/DrewDawson2027/claude-token-guard.git
cd claude-token-guard
```

## Step 2: Create the Hooks Directory

```bash
mkdir -p ~/.claude/hooks/session-state
chmod 700 ~/.claude/hooks/session-state
```

## Step 3: Copy Hook Files

```bash
cp token-guard.py \
   read-efficiency-guard.py \
   hook_utils.py \
   self-heal.py \
   health-check.sh \
   token-guard-config.json \
   guard_contracts.py \
   guard_normalize.py \
   guard_events.py \
   agent-lifecycle.sh \
   agent-metrics.py \
   ~/.claude/hooks/

chmod +x ~/.claude/hooks/health-check.sh ~/.claude/hooks/agent-lifecycle.sh
```

## Step 4: Register Hooks in settings.json

Edit `~/.claude/settings.json` (create it if it doesn't exist):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "type": "command",
        "command": "python3 ~/.claude/hooks/token-guard.py"
      },
      {
        "type": "command",
        "command": "python3 ~/.claude/hooks/read-efficiency-guard.py"
      }
    ],
    "SessionStart": [
      {
        "type": "command",
        "command": "python3 ~/.claude/hooks/self-heal.py"
      }
    ],
    "SubagentStart": [
      {
        "type": "command",
        "command": "bash ~/.claude/hooks/agent-lifecycle.sh"
      }
    ],
    "SubagentStop": [
      {
        "type": "command",
        "command": "bash ~/.claude/hooks/agent-lifecycle.sh"
      },
      {
        "type": "command",
        "command": "python3 ~/.claude/hooks/agent-metrics.py"
      }
    ]
  }
}
```

## Step 5: Verify

```bash
# Run the test suite
cd claude-token-guard
python3 -m pytest tests/ -v

# Run self-heal to validate the installation
python3 ~/.claude/hooks/self-heal.py

# Test token-guard with a passthrough
echo '{"tool_name":"Read","tool_input":{"file_path":"/tmp/test"},"session_id":"test"}' | python3 ~/.claude/hooks/token-guard.py
# Should exit 0 (no output = allowed)
```

## Step 6: Restart Claude Code

The hooks take effect on the next session start.

## Updating

To update, pull the latest code and re-copy the files:

```bash
cd claude-token-guard
git pull
cp token-guard.py read-efficiency-guard.py hook_utils.py self-heal.py \
   health-check.sh token-guard-config.json guard_contracts.py \
   guard_normalize.py guard_events.py agent-lifecycle.sh agent-metrics.py \
   ~/.claude/hooks/
```
