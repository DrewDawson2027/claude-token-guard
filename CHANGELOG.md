# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-02-21

### Added
- v2 schema contracts (`guard_contracts.py`) with builders for audit, lifecycle, usage, and decision-id records
- Normalization pipeline (`guard_normalize.py`) — session key hashing, file path resolution, text truncation
- Event logging module (`guard_events.py`) — locked JSONL append with atomic writes
- Install manifest (`.manifest.json`) with SHA256 checksums for all hook files
- CLI commands: `verify` (post-install validation), `drift` (manifest comparison), `benchmark` (latency profiling)
- SubagentStart/SubagentStop hook registration in `settings.json` via `install` command
- `fail_closed` strict mode (opt-in) — blocks tool calls when guard state is degraded
- Architecture documentation (`docs/ARCHITECTURE.md`) with data flow, module graph, schema formats
- Security documentation (`docs/security.md`) with failure modes, input sanitization, strict mode
- Compatibility matrix (`docs/compatibility.md`) with Python/OS support and migration guide
- Examples: `examples/local-hooks-setup.md`, `examples/custom-config.md`
- Fuzz tests with Hypothesis for malformed payloads and hostile strings
- Concurrency stress tests (100 parallel JSONL appends, 50 parallel state writes)
- p95 latency gate tests with CI-appropriate thresholds
- Packaging tests: import smoke, version consistency, hook file existence, build artifacts

### Fixed
- Non-dict JSON input (null, array, scalar) no longer crashes hooks — now fails-open gracefully
- Unicode surrogate characters in state files no longer crash `load_json_state`
- `datetime.utcnow()` deprecation warning on Python 3.12+

### Changed
- Version bump: 1.0.0 → 1.1.0
- HOOK_FILES expanded: 6 → 11 files (added `guard_contracts.py`, `guard_normalize.py`, `guard_events.py`, `agent-lifecycle.sh`, `agent-metrics.py`)
- Test count: 140+ → 205+
- CI matrix now sets `PYTHONIOENCODING=utf-8` for cross-platform Unicode safety

## [1.0.0] - 2026-02-20

### Added
- 7 enforcement rules for agent spawning control (one-per-session, type cap, session cap, parallel window, necessity scoring, type-switching detection, global cooldown)
- Read efficiency guard with duplicate file blocking (3+ reads) and sequential read throttling (15 reads in 120s)
- Self-healing session-start hook with 5-phase validation and auto-repair
- Fuzzy matching for necessity detection using word-level SequenceMatcher (50 canonical task descriptions)
- Property-based testing with hypothesis (17 tests verifying invariants)
- Performance benchmarks with pytest-benchmark (subprocess + direct function latency)
- PyPI packaging with CLI (`claude-token-guard install/report/health/version/uninstall`)
- `--usage` command for shareable testimonials from audit data
- `--report` command for cross-session analytics
- Code coverage reporting in CI with Codecov integration
- Type hints on all public functions
- Windows CI testing
- CHANGELOG.md

### Technical
- Zero runtime dependencies (stdlib only)
- Atomic state writes (tempfile.mkstemp + os.replace)
- Portable file locking (fcntl on Unix, msvcrt on Windows)
- Fail-open design (errors exit 0, never blocks legitimate work)
- 140+ tests across 8 test files
- CI matrix: Python 3.8/3.10/3.12 x Ubuntu/macOS/Windows
