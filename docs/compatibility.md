# Compatibility Matrix

## Python Versions

| Version | Status | Notes |
|---------|--------|-------|
| 3.8 | Supported | Minimum version. Tested in CI (Ubuntu, macOS). |
| 3.9 | Supported | Not in CI matrix, but no 3.9-specific issues known. |
| 3.10 | Supported | Tested in CI (Ubuntu, macOS, Windows). |
| 3.11 | Supported | Not in CI matrix, but no 3.11-specific issues known. |
| 3.12 | Supported | Tested in CI (Ubuntu, macOS, Windows). |
| 3.13+ | Expected to work | No known incompatibilities. |

## Operating Systems

| OS | Status | Notes |
|----|--------|-------|
| macOS | Primary | Developed and tested here. Uses `fcntl` for file locking. |
| Linux | Supported | Tested in CI (Ubuntu). Uses `fcntl` for file locking. |
| Windows | Supported | Tested in CI. Uses `msvcrt` for file locking. Shell scripts use bash. |

## Claude Code Versions

Compatible with any version of Claude Code that supports `PreToolUse` hooks via `settings.json`. The hook protocol is simple:

1. Claude Code pipes JSON to stdin
2. Hook reads stdin, processes, writes to stderr/stdout
3. Exit 0 = allow, Exit 2 = block

No Claude Code version-specific features are used.

## Dependencies

**Zero runtime dependencies.** Uses only Python standard library modules:
- `json`, `os`, `sys`, `time`, `hashlib`, `difflib`, `re`, `tempfile`, `fcntl`/`msvcrt`

**Dev dependencies** (for testing only):
- `pytest` — test framework
- `hypothesis` — property-based testing
- `pytest-benchmark` — performance benchmarks (optional)
- `mutmut` — mutation testing (optional)

## Schema Migration

### v1 → v2

Schema v2 was introduced to add `schema_version`, `decision_id`, and normalized field names. Migration is automatic:

- **Readers** handle both v1 and v2 records (presence of `schema_version` field differentiates)
- **Writers** always produce v2 records
- **self-heal** auto-adds missing v2 config fields on session start
- No manual migration step required

### Config Migration

Pre-v2 configs are missing these fields:
- `fault_audit` (default: `true`)
- `max_string_field_length` (default: `512`)
- `metrics_correlation_window_seconds` (default: `15`)

self-heal detects missing fields and adds them with defaults. No user action needed.

## Migration: v1.0.0 → v1.1.0

### Quick Upgrade

```bash
pip install --upgrade claude-token-guard
claude-token-guard install --force
```

### What Happens

1. **New shared modules installed**: `guard_contracts.py`, `guard_normalize.py`, `guard_events.py` are required by the updated hooks. The `install --force` command copies them to `~/.claude/hooks/`.
2. **settings.json patched**: New `SubagentStart` and `SubagentStop` hook entries are added. Existing entries are not modified.
3. **Install manifest created**: `.manifest.json` with SHA256 checksums enables `verify` and `drift` commands.
4. **No config changes required**: `self-heal` auto-adds any missing v2 config fields (`fault_audit`, `max_string_field_length`, `metrics_correlation_window_seconds`) with defaults on next session start.

### Verification

```bash
claude-token-guard verify   # 5-step validation with checksums and smoke tests
claude-token-guard drift    # Compare installed files against manifest
claude-token-guard status   # Confirm version 1.1.0 installed
```

### Rollback

```bash
pip install claude-token-guard==1.0.0
claude-token-guard install --force
```

## Breaking Changes

| Version | Change | Impact |
|---------|--------|--------|
| 1.1.0 | Added `guard_contracts.py`, `guard_normalize.py`, `guard_events.py` to install | No breaking change. New modules are additive. |
| 1.1.0 | Added `SubagentStart`/`SubagentStop` hook registrations | No breaking change. New hook events, existing hooks unaffected. |
| 1.1.0 | Install manifest (`.manifest.json`) written on install | No breaking change. New file, not required for operation. |
| 1.1.0 | `fail_closed` strict mode added | No breaking change. Opt-in only, default remains `fail_open`. |
