"""
Property-based tests using hypothesis.

Tests invariants that must hold for ALL valid inputs, not just hand-picked examples.
These catch edge cases that unit tests miss — especially around numeric thresholds,
string parsing, and state management.

Requires: pip install hypothesis
"""

import json
import os
import tempfile

import pytest

try:
    from hypothesis import assume, given, settings
    from hypothesis import strategies as st
    HAS_HYPOTHESIS = True
except ImportError:
    HAS_HYPOTHESIS = False
    # Provide no-op stubs so class bodies parse without errors
    def given(*a, **kw):
        return lambda f: f
    def settings(**kw):
        return lambda f: f
    def assume(x):
        pass
    class st:
        @staticmethod
        def one_of(*a, **kw): return None
        @staticmethod
        def integers(**kw): return None
        @staticmethod
        def floats(**kw): return None
        @staticmethod
        def text(**kw): return None
        @staticmethod
        def none(): return None
        @staticmethod
        def booleans(): return None
        @staticmethod
        def from_regex(*a, **kw): return None
        @staticmethod
        def characters(**kw): return None
        @staticmethod
        def dictionaries(**kw): return None

# Modules are registered in sys.modules by conftest.py's dynamic loader
import token_guard

import hook_utils

pytestmark = pytest.mark.skipif(not HAS_HYPOTHESIS, reason="hypothesis not installed")


# ============================================================
# _safe_int properties
# ============================================================

class TestSafeIntProperties:
    """Properties of _safe_int: always returns int, never raises."""

    @given(st.one_of(st.integers(), st.floats(allow_nan=False, allow_infinity=False),
                     st.text(), st.none(), st.booleans()))
    @settings(max_examples=200)
    def test_always_returns_int(self, val):
        """_safe_int(val, 42) always returns an int, regardless of input type."""
        result = token_guard._safe_int(val, 42)
        assert isinstance(result, int)

    @given(st.integers())
    @settings(max_examples=100)
    def test_returns_value_for_ints(self, val):
        """For actual integers, _safe_int returns the value itself."""
        result = token_guard._safe_int(val, 99)
        assert result == val

    @given(st.text())
    @settings(max_examples=100)
    def test_returns_default_for_strings(self, val):
        """For non-numeric strings, _safe_int returns the default."""
        try:
            int(val)
            assume(False)  # Skip strings that are valid ints
        except (ValueError, TypeError):
            pass
        result = token_guard._safe_int(val, 42)
        assert result == 42


# ============================================================
# extract_target_dirs properties
# ============================================================

class TestExtractTargetDirsProperties:
    """Properties of extract_target_dirs: paths are valid, absolute."""

    @given(st.text(min_size=0, max_size=500))
    @settings(max_examples=200)
    def test_never_crashes(self, prompt):
        """extract_target_dirs never raises, regardless of input."""
        result = token_guard.extract_target_dirs(prompt)
        assert isinstance(result, list)

    @given(st.text(min_size=0, max_size=500))
    @settings(max_examples=200)
    def test_all_paths_absolute(self, prompt):
        """Every extracted path must be absolute (starts with /)."""
        result = token_guard.extract_target_dirs(prompt)
        for path in result:
            assert path.startswith("/"), f"Relative path leaked: {path}"

    @given(st.from_regex(r"START: /[a-z]+/[a-z]+", fullmatch=True))
    @settings(max_examples=50)
    def test_start_directive_extracted(self, prompt):
        """Paths in START: directives are always extracted."""
        result = token_guard.extract_target_dirs(prompt)
        assert len(result) >= 1, f"START: directive not extracted from: {prompt}"

    @given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=0, max_size=200))
    @settings(max_examples=100)
    def test_no_paths_without_slashes(self, prompt):
        """Text without slashes should never produce paths."""
        result = token_guard.extract_target_dirs(prompt)
        assert result == [], f"Got paths from non-path text: {result}"


# ============================================================
# check_necessity properties
# ============================================================

class TestCheckNecessityProperties:
    """Properties of check_necessity: safe with all inputs."""

    @given(st.text(min_size=0, max_size=1000), st.text(min_size=0, max_size=1000))
    @settings(max_examples=200)
    def test_never_crashes(self, description, prompt):
        """check_necessity never raises, regardless of input."""
        result = token_guard.check_necessity(description, prompt)
        assert isinstance(result, tuple)
        assert len(result) == 3
        should_block, suggestion, pattern_name = result
        assert isinstance(should_block, bool)
        assert isinstance(suggestion, str)
        assert isinstance(pattern_name, str)

    def test_empty_inputs_never_block(self):
        """Empty description + prompt should never trigger a block."""
        should_block, _, _ = token_guard.check_necessity("", "")
        assert not should_block

    @given(st.text(min_size=100, max_size=500))
    @settings(max_examples=20)
    def test_long_inputs_complete_quickly(self, text):
        """Moderately long inputs should not hang (truncation must work)."""
        import time
        start = time.monotonic()
        token_guard.check_necessity(text, text)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"check_necessity took {elapsed:.2f}s on {len(text)}-char input"


# ============================================================
# check_type_switching properties
# ============================================================

class TestCheckTypeSwitchingProperties:
    """Properties of check_type_switching: correct evasion detection."""

    @given(st.text(min_size=10, max_size=100))
    @settings(max_examples=100)
    def test_identical_desc_different_type_triggers(self, description):
        """Identical description with different type should always trigger (similarity=1.0)."""
        state = {
            "blocked_attempts": [
                {"type": "Explore", "description": description, "timestamp": 0}
            ]
        }
        is_evasion, blocked_type = token_guard.check_type_switching(
            state, description, "general-purpose"
        )
        assert is_evasion, f"Should detect evasion for identical desc: {description[:50]}"
        assert blocked_type == "Explore"

    @given(st.text(min_size=10, max_size=100))
    @settings(max_examples=100)
    def test_same_type_never_triggers(self, description):
        """Same type should never trigger, even with identical description."""
        state = {
            "blocked_attempts": [
                {"type": "Explore", "description": description, "timestamp": 0}
            ]
        }
        is_evasion, _ = token_guard.check_type_switching(
            state, description, "Explore"  # Same type
        )
        assert not is_evasion

    def test_empty_state_never_triggers(self):
        """Empty blocked_attempts should never trigger."""
        state = {"blocked_attempts": []}
        is_evasion, _ = token_guard.check_type_switching(
            state, "anything at all", "general-purpose"
        )
        assert not is_evasion


# ============================================================
# hook_utils properties
# ============================================================

class TestLoadJsonStateProperties:
    """Properties of load_json_state: always returns dict, never raises."""

    @given(st.text(alphabet=st.characters(blacklist_characters='\x00/'), min_size=1, max_size=50))
    @settings(max_examples=50)
    def test_nonexistent_path_returns_default(self, suffix):
        """Non-existent paths should return the default, never raise."""
        path = f"/tmp/nonexistent_{suffix}_test.json"
        result = hook_utils.load_json_state(path, lambda: {"default": True})
        assert result == {"default": True}

    def test_none_factory_returns_empty_dict(self):
        """None factory should return empty dict."""
        result = hook_utils.load_json_state("/tmp/nonexistent.json")
        assert result == {}


class TestSaveJsonStateProperties:
    """Properties of save_json_state: round-trip integrity."""

    @given(st.dictionaries(
        keys=st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnopqrstuvwxyz"),
        values=st.one_of(st.integers(), st.text(max_size=50), st.booleans(), st.none()),
        max_size=10,
    ))
    @settings(max_examples=100)
    def test_roundtrip_integrity(self, state):
        """If save returns True, the file contains valid JSON matching the input."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            success = hook_utils.save_json_state(path, state)
            if success:
                with open(path, "r") as f:
                    loaded = json.load(f)
                assert loaded == state
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


class TestLockedAppendProperties:
    """Properties of locked_append: append integrity."""

    @given(st.text(alphabet=st.characters(blacklist_characters='\r', blacklist_categories=('Cs',)), min_size=1, max_size=100))
    @settings(max_examples=50)
    def test_appended_line_present(self, line):
        """After successful append, the line should be in the file."""
        line_with_newline = line + "\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            success = hook_utils.locked_append(path, line_with_newline)
            if success:
                with open(path, "r") as f:
                    content = f.read()
                assert line_with_newline in content
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
            try:
                os.unlink(path + ".lock")
            except OSError:
                pass
