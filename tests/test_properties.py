"""
Property-based tests using hypothesis.

Tests invariants that must hold for ALL valid inputs, not just hand-picked examples.
These catch edge cases that unit tests miss — especially around numeric thresholds,
string parsing, and state management.

Requires: pip install hypothesis
"""

import json
import os
import sys
import tempfile

import pytest

try:
    from hypothesis import given, settings, assume, HealthCheck
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


# ============================================================
# Malformed payload fuzz tests (Phase 9.1)
# ============================================================

import subprocess

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_GUARD_SCRIPT = os.path.join(_REPO_ROOT, "token-guard.py")
READ_GUARD_SCRIPT = os.path.join(_REPO_ROOT, "read-efficiency-guard.py")


def _make_fuzz_env(tmp_dir):
    """Create isolated environment for fuzz tests (helper, not fixture)."""
    state_dir = os.path.join(tmp_dir, "state")
    os.makedirs(state_dir, exist_ok=True)
    config_path = os.path.join(tmp_dir, "config.json")
    with open(config_path, "w") as f:
        json.dump({
            "schema_version": 2, "max_agents": 5, "parallel_window_seconds": 30,
            "global_cooldown_seconds": 0, "max_per_subagent_type": 5,
            "state_ttl_hours": 24, "audit_log": False,
            "failure_mode": "fail_open", "sanitize_session_ids": True,
            "normalize_paths": True, "fault_audit": False,
            "max_string_field_length": 512, "metrics_correlation_window_seconds": 15,
            "one_per_session": ["Explore"], "always_allowed": ["claude-code-guide"],
        }, f)
    env = os.environ.copy()
    env["TOKEN_GUARD_STATE_DIR"] = state_dir
    env["TOKEN_GUARD_CONFIG_PATH"] = config_path
    return env


class TestMalformedPayloads:
    """Fuzz hooks with arbitrary JSON — must never exit 1 (crash)."""

    @given(st.dictionaries(
        keys=st.text(min_size=1, max_size=20),
        values=st.one_of(st.integers(), st.text(max_size=50), st.booleans(), st.none()),
        max_size=8,
    ))
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_token_guard_survives_any_json(self, payload):
        """token-guard must exit 0 or 2, never 1, for any JSON dict."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            env = _make_fuzz_env(tmp_dir)
            result = subprocess.run(
                ["python3", TOKEN_GUARD_SCRIPT],
                input=json.dumps(payload), capture_output=True, text=True,
                env=env, timeout=10,
            )
            assert result.returncode in (0, 2), (
                f"token-guard crashed (exit {result.returncode}) on: {payload}\nstderr: {result.stderr}"
            )

    @given(st.dictionaries(
        keys=st.text(min_size=1, max_size=20),
        values=st.one_of(st.integers(), st.text(max_size=50), st.booleans(), st.none()),
        max_size=8,
    ))
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_read_guard_survives_any_json(self, payload):
        """read-guard must exit 0 or 2, never 1, for any JSON dict."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            env = _make_fuzz_env(tmp_dir)
            result = subprocess.run(
                ["python3", READ_GUARD_SCRIPT],
                input=json.dumps(payload), capture_output=True, text=True,
                env=env, timeout=10,
            )
            assert result.returncode in (0, 2), (
                f"read-guard crashed (exit {result.returncode}) on: {payload}\nstderr: {result.stderr}"
            )

    def test_token_guard_survives_non_json(self):
        """token-guard must exit 0 on non-JSON input."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            env = _make_fuzz_env(tmp_dir)
            for bad_input in ["", "not json", "{{{", "null", "[]", "123"]:
                result = subprocess.run(
                    ["python3", TOKEN_GUARD_SCRIPT],
                    input=bad_input, capture_output=True, text=True,
                    env=env, timeout=10,
                )
                assert result.returncode == 0, (
                    f"token-guard crashed on non-JSON: {bad_input!r}"
                )

    def test_read_guard_survives_non_json(self):
        """read-guard must exit 0 on non-JSON input."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            env = _make_fuzz_env(tmp_dir)
            for bad_input in ["", "not json", "{{{", "null", "[]"]:
                result = subprocess.run(
                    ["python3", READ_GUARD_SCRIPT],
                    input=bad_input, capture_output=True, text=True,
                    env=env, timeout=10,
                )
                assert result.returncode == 0, (
                    f"read-guard crashed on non-JSON: {bad_input!r}"
                )


# ============================================================
# Normalization fuzz tests
# ============================================================

# Import normalization modules
sys.path.insert(0, _REPO_ROOT)
try:
    from guard_normalize import normalize_session_key, normalize_hook_payload, normalize_file_path
    from guard_contracts import build_audit_entry, build_metrics_lifecycle_entry, build_metrics_usage_entry
    HAS_NORMALIZE = True
except ImportError:
    HAS_NORMALIZE = False


@pytest.mark.skipif(not HAS_NORMALIZE, reason="guard_normalize not importable")
class TestNormalizationFuzz:
    """Fuzz normalization functions — must never crash."""

    @given(st.text(min_size=0, max_size=200))
    @settings(max_examples=100)
    def test_normalize_session_key_never_crashes(self, session_id):
        """normalize_session_key must return a string for any input."""
        result = normalize_session_key(session_id)
        assert isinstance(result, str)
        assert len(result) <= 16

    @given(st.dictionaries(
        keys=st.text(min_size=1, max_size=30),
        values=st.one_of(st.text(max_size=100), st.integers(), st.none()),
        max_size=10,
    ))
    @settings(max_examples=100)
    def test_normalize_hook_payload_never_crashes(self, data):
        """normalize_hook_payload must return a dict for any dict input."""
        result = normalize_hook_payload(data)
        assert isinstance(result, dict)

    @given(st.text(min_size=0, max_size=200))
    @settings(max_examples=100)
    def test_normalize_file_path_never_crashes(self, path):
        """normalize_file_path must return a string for any input."""
        result = normalize_file_path(path)
        assert isinstance(result, str)


@pytest.mark.skipif(not HAS_NORMALIZE, reason="guard_contracts not importable")
class TestContractBuilderFuzz:
    """Fuzz contract builders — must never crash."""

    @given(
        st.text(min_size=0, max_size=50),
        st.text(min_size=0, max_size=50),
        st.text(min_size=0, max_size=100),
        st.text(min_size=0, max_size=50),
    )
    @settings(max_examples=100)
    def test_build_audit_entry_never_crashes(self, event_type, subagent_type, description, session_id):
        """build_audit_entry must return a dict for any string inputs."""
        result = build_audit_entry(
            event_type=event_type,
            subagent_type=subagent_type,
            description=description,
            session_id=session_id,
        )
        assert isinstance(result, dict)
        assert "schema_version" in result
        assert result["schema_version"] == 2

    @given(
        st.text(min_size=0, max_size=50),
        st.text(min_size=0, max_size=50),
        st.text(min_size=0, max_size=50),
        st.text(min_size=0, max_size=50),
    )
    @settings(max_examples=50)
    def test_build_metrics_lifecycle_never_crashes(self, event, agent_type, agent_id, session_id):
        """build_metrics_lifecycle_entry must return a dict."""
        result = build_metrics_lifecycle_entry(
            event=event, agent_type=agent_type,
            agent_id=agent_id, session_id=session_id,
        )
        assert isinstance(result, dict)

    @given(
        st.text(min_size=0, max_size=50),
        st.text(min_size=0, max_size=50),
        st.text(min_size=0, max_size=50),
        st.integers(min_value=0, max_value=1000000),
        st.integers(min_value=0, max_value=1000000),
    )
    @settings(max_examples=50)
    def test_build_metrics_usage_never_crashes(self, agent_type, agent_id, session_id, input_tok, output_tok):
        """build_metrics_usage_entry must return a dict."""
        totals = {"input_tokens": input_tok, "output_tokens": output_tok}
        result = build_metrics_usage_entry(
            agent_type=agent_type, agent_id=agent_id,
            session_id=session_id,
            totals=totals, cost_usd=0.0,
        )
        assert isinstance(result, dict)


# ============================================================
# Hostile string tests (Phase 9.2)
# ============================================================

@pytest.mark.skipif(not HAS_NORMALIZE, reason="guard_normalize not importable")
class TestHostileStrings:
    """Test normalization functions against adversarial inputs."""

    def test_null_bytes_removed(self):
        """Null bytes in session_id must not appear in output."""
        result = normalize_session_key("abc\x00def")
        assert "\x00" not in result

    def test_path_traversal_sanitized(self):
        """Path traversal components must be resolved."""
        result = normalize_file_path("../../../etc/passwd")
        assert "../" not in result

    def test_very_long_string_truncated(self):
        """100KB string must be truncated by normalize_hook_payload."""
        huge = "x" * 100_000
        data = {"description": huge}
        result = normalize_hook_payload(data)
        assert len(result.get("description", "")) <= 1024

    def test_control_characters_handled(self):
        """Control characters must not crash normalization."""
        control_str = "".join(chr(i) for i in range(32))
        result = normalize_session_key(control_str)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_json_injection_in_string(self):
        """JSON strings embedded in values must not cause issues."""
        data = {"description": '{"key": "value"}'}
        result = normalize_hook_payload(data)
        assert isinstance(result, dict)

    def test_newline_injection_safe(self):
        """Newlines in strings must not corrupt JSONL when serialized."""
        data = {"description": "line1\nline2\nline3"}
        result = normalize_hook_payload(data)
        serialized = json.dumps(result)
        assert "\n" not in serialized  # JSON escapes newlines as \\n

    def test_unicode_bmp_characters(self):
        """BMP Unicode characters must not crash normalization."""
        unicode_str = "emoji: \u2603 \u2764 \u2602 CJK: \u4e16\u754c"
        result = normalize_session_key(unicode_str)
        assert isinstance(result, str)

    def test_empty_string_handled(self):
        """Empty string must not crash any normalizer."""
        assert isinstance(normalize_session_key(""), str)
        assert isinstance(normalize_file_path(""), str)
        assert isinstance(normalize_hook_payload({}), dict)
