# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
