"""
Shared test configuration and dynamic module loading.

Loads hook modules with hyphens in their filenames (token-guard.py, read-efficiency-guard.py)
using importlib so they can be imported directly in tests. This enables:
  - Property-based testing (hypothesis) on individual functions
  - Performance benchmarks on individual functions
  - Mutation testing (mutmut) that can detect source mutations

Usage in tests:
    import token_guard
    result = token_guard.check_necessity("search for function", "")
"""

import importlib.util
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

# Add repo root to sys.path for hook_utils import resolution
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Enable subprocess coverage tracking: set COVERAGE_PROCESS_START so that
# when tests spawn `python3 token-guard.py`, the sitecustomize.py in the
# tests/ directory calls coverage.process_startup() to track those lines.
_coveragerc = os.path.join(_REPO_ROOT, ".coveragerc")
if os.path.isfile(_coveragerc) and "COVERAGE_PROCESS_START" not in os.environ:
    os.environ["COVERAGE_PROCESS_START"] = _coveragerc
    # Ensure the tests/ dir is in PYTHONPATH so subprocess Python finds sitecustomize.py
    pythonpath = os.environ.get("PYTHONPATH", "")
    if _TESTS_DIR not in pythonpath:
        os.environ["PYTHONPATH"] = _TESTS_DIR + os.pathsep + pythonpath if pythonpath else _TESTS_DIR


def _load_module(name, filename):
    """Dynamically load a Python module from a file with a non-importable name."""
    filepath = os.path.join(_REPO_ROOT, filename)
    spec = importlib.util.spec_from_file_location(name, filepath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Load modules with hyphenated filenames
token_guard = _load_module("token_guard", "token-guard.py")
read_guard = _load_module("read_guard", "read-efficiency-guard.py")

# hook_utils can be imported normally (no hyphens)
import hook_utils  # noqa: E402
