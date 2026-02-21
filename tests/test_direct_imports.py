"""
Direct-import tests for mutation testing effectiveness.

These tests call hook functions directly (in-process) rather than via subprocess,
so mutmut can detect source mutations. They complement the subprocess-based
integration tests in test_token_guard.py and test_read_efficiency_guard.py.
"""

import os
import tempfile
import time

import read_guard

# These are loaded by conftest.py via importlib (hyphenated filenames)
import token_guard

import hook_utils

# ---------------------------------------------------------------------------
# token_guard.check_necessity — the core blocking decision
# ---------------------------------------------------------------------------

class TestCheckNecessityDirect:
    """Direct calls to check_necessity for mutation testing coverage."""

    def test_blocks_search_for_function(self):
        blocked, suggestion, pattern = token_guard.check_necessity(
            "search for the handleAuth function", ""
        )
        assert blocked is True
        assert "Grep" in suggestion or "grep" in suggestion.lower()

    def test_blocks_find_file(self):
        blocked, suggestion, pattern = token_guard.check_necessity(
            "find the config file in the project", ""
        )
        assert blocked is True

    def test_blocks_read_file(self):
        blocked, suggestion, pattern = token_guard.check_necessity(
            "read this file and tell me what it does", ""
        )
        assert blocked is True

    def test_blocks_grep_for_pattern(self):
        blocked, suggestion, pattern = token_guard.check_necessity(
            "grep for all usages of useState", ""
        )
        assert blocked is True

    def test_allows_complex_refactoring(self):
        blocked, suggestion, pattern = token_guard.check_necessity(
            "refactor the authentication system across 12 microservices to use OAuth2 with PKCE flow",
            ""
        )
        assert blocked is False

    def test_allows_architectural_analysis(self):
        blocked, suggestion, pattern = token_guard.check_necessity(
            "design a caching layer that integrates with our event-driven architecture",
            ""
        )
        assert blocked is False

    def test_empty_inputs_never_block(self):
        blocked, suggestion, pattern = token_guard.check_necessity("", "")
        assert blocked is False

    def test_returns_pattern_name(self):
        blocked, suggestion, pattern = token_guard.check_necessity(
            "search for the login function", ""
        )
        assert blocked is True
        assert pattern != ""  # pattern name should be set


# ---------------------------------------------------------------------------
# token_guard.check_type_switching — evasion detection
# ---------------------------------------------------------------------------

class TestCheckTypeSwitchingDirect:
    """Direct calls to check_type_switching for mutation testing coverage."""

    def test_detects_evasion_different_type_same_desc(self):
        state = {
            "blocked_attempts": [
                {
                    "type": "Explore",
                    "description": "map the codebase structure",
                    "time": __import__("time").time(),
                }
            ]
        }
        is_evasion, msg = token_guard.check_type_switching(
            state, "map the codebase structure", "general-purpose"
        )
        assert is_evasion is True

    def test_same_type_never_triggers(self):
        state = {
            "blocked_attempts": [
                {
                    "type": "Explore",
                    "description": "map the codebase structure",
                    "time": __import__("time").time(),
                }
            ]
        }
        is_evasion, msg = token_guard.check_type_switching(
            state, "map the codebase structure", "Explore"
        )
        assert is_evasion is False

    def test_empty_blocked_attempts(self):
        state = {"blocked_attempts": []}
        is_evasion, msg = token_guard.check_type_switching(
            state, "anything", "Explore"
        )
        assert is_evasion is False


# ---------------------------------------------------------------------------
# token_guard._safe_int — numeric coercion
# ---------------------------------------------------------------------------

class TestSafeIntDirect:
    def test_returns_int_for_valid_string(self):
        assert token_guard._safe_int("42", 0) == 42

    def test_returns_default_for_invalid(self):
        assert token_guard._safe_int("abc", 5) == 5

    def test_returns_default_for_none(self):
        assert token_guard._safe_int(None, 10) == 10

    def test_returns_int_for_int(self):
        assert token_guard._safe_int(7, 0) == 7


# ---------------------------------------------------------------------------
# token_guard.extract_target_dirs — path extraction from prompts
# ---------------------------------------------------------------------------

class TestExtractTargetDirsDirect:
    def test_extracts_start_directive(self):
        dirs = token_guard.extract_target_dirs("START: /src/components\nDo the work")
        assert "/src/components" in dirs

    def test_extracts_absolute_paths(self):
        dirs = token_guard.extract_target_dirs("Look at /usr/local/bin and /etc/config")
        assert any("/usr/local/bin" in d for d in dirs)

    def test_empty_prompt_returns_empty(self):
        dirs = token_guard.extract_target_dirs("")
        assert dirs == []


# ---------------------------------------------------------------------------
# hook_utils — state management
# ---------------------------------------------------------------------------

class TestHookUtilsDirect:
    def test_load_json_state_missing_file(self):
        result = hook_utils.load_json_state("/nonexistent/path.json")
        assert result == {}

    def test_load_json_state_with_factory(self):
        result = hook_utils.load_json_state(
            "/nonexistent/path.json",
            default_factory=lambda: {"key": "value"}
        )
        assert result == {"key": "value"}

    def test_save_and_load_round_trip(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            path = f.name
        try:
            data = {"count": 42, "items": ["a", "b"]}
            assert hook_utils.save_json_state(path, data) is True
            loaded = hook_utils.load_json_state(path)
            assert loaded == data
        finally:
            os.unlink(path)

    def test_locked_append_creates_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            os.unlink(path)  # start fresh
            assert hook_utils.locked_append(path, '{"event":"test"}\n') is True
            with open(path) as f:
                assert '{"event":"test"}' in f.read()
        finally:
            if os.path.exists(path):
                os.unlink(path)
            lock_path = path + ".lock"
            if os.path.exists(lock_path):
                os.unlink(lock_path)

    def test_read_jsonl_fault_tolerant_skips_bad_lines(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"good": 1}\n')
            f.write('this is not json\n')
            f.write('{"also_good": 2}\n')
            path = f.name
        try:
            entries = hook_utils.read_jsonl_fault_tolerant(path)
            assert len(entries) == 2
            assert entries[0] == {"good": 1}
            assert entries[1] == {"also_good": 2}
        finally:
            os.unlink(path)

    def test_read_jsonl_missing_file(self):
        entries = hook_utils.read_jsonl_fault_tolerant("/nonexistent/file.jsonl")
        assert entries == []


# ---------------------------------------------------------------------------
# token_guard.load_config — config loading
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# token_guard.audit — audit log writing
# ---------------------------------------------------------------------------

class TestAuditDirect:
    def test_audit_writes_entry(self, tmp_path):
        audit_path = str(tmp_path / "audit.jsonl")
        # Temporarily override the AUDIT_LOG path
        original = token_guard.AUDIT_LOG
        token_guard.AUDIT_LOG = audit_path
        try:
            token_guard.audit("allowed", "Explore", "test desc", "test-session")
            entries = hook_utils.read_jsonl_fault_tolerant(audit_path)
            assert len(entries) == 1
            assert entries[0]["event"] == "allowed"
            assert entries[0]["type"] == "Explore"
        finally:
            token_guard.AUDIT_LOG = original

    def test_audit_with_reason(self, tmp_path):
        audit_path = str(tmp_path / "audit.jsonl")
        original = token_guard.AUDIT_LOG
        token_guard.AUDIT_LOG = audit_path
        try:
            token_guard.audit("blocked", "Explore", "test", "sess", reason="one_per_session")
            entries = hook_utils.read_jsonl_fault_tolerant(audit_path)
            assert entries[0]["reason"] == "one_per_session"
        finally:
            token_guard.AUDIT_LOG = original


# ---------------------------------------------------------------------------
# token_guard.cleanup_stale_state — TTL-based file cleanup
# ---------------------------------------------------------------------------

class TestCleanupStaleStateDirect:
    def test_removes_old_files(self, tmp_path):
        old_file = tmp_path / "old-session.json"
        old_file.write_text("{}")
        # Set modification time to 48 hours ago
        old_time = time.time() - 48 * 3600
        os.utime(str(old_file), (old_time, old_time))

        original = token_guard.STATE_DIR
        token_guard.STATE_DIR = str(tmp_path)
        try:
            token_guard.cleanup_stale_state(24)
            assert not old_file.exists()
        finally:
            token_guard.STATE_DIR = original

    def test_preserves_fresh_files(self, tmp_path):
        fresh_file = tmp_path / "fresh-session.json"
        fresh_file.write_text("{}")

        original = token_guard.STATE_DIR
        token_guard.STATE_DIR = str(tmp_path)
        try:
            token_guard.cleanup_stale_state(24)
            assert fresh_file.exists()
        finally:
            token_guard.STATE_DIR = original

    def test_preserves_audit_log(self, tmp_path):
        audit_file = tmp_path / "audit.jsonl"
        audit_file.write_text("{}")
        old_time = time.time() - 48 * 3600
        os.utime(str(audit_file), (old_time, old_time))

        original = token_guard.STATE_DIR
        token_guard.STATE_DIR = str(tmp_path)
        try:
            token_guard.cleanup_stale_state(24)
            assert audit_file.exists()  # audit.jsonl should never be deleted
        finally:
            token_guard.STATE_DIR = original


# ---------------------------------------------------------------------------
# read_guard utility functions
# ---------------------------------------------------------------------------

class TestReadGuardUtilsDirect:
    def test_default_read_state_structure(self):
        state = read_guard.default_read_state()
        assert "reads" in state
        assert "last_sequential_warn" in state
        assert isinstance(state["reads"], list)

    def test_get_explore_dirs_no_state(self, tmp_path):
        dirs = read_guard.get_explore_dirs("nonexistent-session-id-12345")
        assert dirs == [] or isinstance(dirs, list)


# ---------------------------------------------------------------------------
# token_guard.load_config — config loading
# ---------------------------------------------------------------------------

class TestLoadConfigDirect:
    def test_returns_dict_with_expected_keys(self):
        config = token_guard.load_config()
        assert isinstance(config, dict)
        assert "max_agents" in config
        assert "one_per_session" in config

    def test_max_agents_is_int(self):
        config = token_guard.load_config()
        assert isinstance(config["max_agents"], int)
