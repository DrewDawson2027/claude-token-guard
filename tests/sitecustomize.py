"""Enable coverage tracking in subprocess-spawned Python processes.

When COVERAGE_PROCESS_START is set, this module (auto-loaded by Python)
calls coverage.process_startup() to begin tracking coverage in the
subprocess. This allows us to measure coverage in token-guard.py and
read-efficiency-guard.py when they're invoked via subprocess in tests.
"""
try:
    import coverage
    coverage.process_startup()
except ImportError:
    pass
