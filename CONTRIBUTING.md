# Contributing to Claude Token Guard

Thanks for your interest in making Claude Code cheaper for everyone.

## Quick Start

```bash
git clone https://github.com/DrewDawson2027/claude-token-guard.git
cd claude-token-guard
python3 -m pytest tests/ -v
```

## How to Contribute

1. **Fork** the repo
2. **Create a branch** (`git checkout -b fix/your-fix`)
3. **Make your changes** — keep them focused
4. **Run the tests** — `python3 -m pytest tests/ -v` (all 96 must pass)
5. **Commit** with a clear message
6. **Open a PR** against `main`

## What We're Looking For

- **New detection patterns** for `DIRECT_TOOL_PATTERNS` (with tests)
- **Bug reports** with reproduction steps
- **Threshold tuning** backed by real audit data
- **Platform testing** (Windows, Linux — we test primarily on macOS)
- **Documentation improvements**

## Rules

- Every hook must **fail-open** (`exit 0` on error, never `exit 1`)
- Blocks use `exit 2` — never block silently
- All state writes must be **atomic** (use `save_json_state` from `hook_utils.py`)
- File locking is **mandatory** for shared state access
- New features need tests. No exceptions.

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full technical deep-dive.

## Tests

```bash
# Run all tests
python3 -m pytest tests/ -v

# Run a specific test file
python3 -m pytest tests/test_token_guard.py -v

# Run a specific test
python3 -m pytest tests/test_token_guard.py::TestNecessityScoring -v
```

## Questions?

Open an issue. We're friendly.
