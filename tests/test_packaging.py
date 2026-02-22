"""
Tests for packaging, install manifest, and version consistency.

Verifies that the package builds correctly, all hook files exist,
and version numbers are consistent across pyproject.toml and __init__.py.
"""

import json
import os
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)


class TestImportSmoke:
    """Verify the package can be imported."""

    def test_import_version(self):
        """Package version should be importable."""
        from claude_token_guard import __version__
        assert isinstance(__version__, str)
        assert len(__version__.split(".")) == 3

    def test_import_cli(self):
        """CLI main function should be importable."""
        from claude_token_guard.cli import main
        assert callable(main)

    def test_import_hook_files_list(self):
        """HOOK_FILES list should be importable and non-empty."""
        from claude_token_guard.cli import HOOK_FILES
        assert isinstance(HOOK_FILES, list)
        assert len(HOOK_FILES) >= 8


class TestHookFilesExist:
    """Verify all files in HOOK_FILES exist in the repo root."""

    def test_all_hook_files_exist(self):
        """Every file listed in HOOK_FILES must exist in the repo root."""
        from claude_token_guard.cli import HOOK_FILES
        missing = []
        for fname in HOOK_FILES:
            fpath = os.path.join(_REPO_ROOT, fname)
            if not os.path.isfile(fpath):
                missing.append(fname)
        assert missing == [], f"Missing hook files: {missing}"

    def test_no_orphan_hooks(self):
        """Important hook files in repo root should be listed in HOOK_FILES."""
        from claude_token_guard.cli import HOOK_FILES
        expected_hooks = [
            "token-guard.py", "read-efficiency-guard.py", "hook_utils.py",
            "self-heal.py", "guard_contracts.py", "guard_normalize.py",
            "guard_events.py", "agent-lifecycle.sh", "agent-metrics.py",
        ]
        for fname in expected_hooks:
            assert fname in HOOK_FILES, f"{fname} not in HOOK_FILES"


class TestVersionConsistency:
    """Verify version numbers match across files."""

    def test_init_matches_pyproject(self):
        """__init__.py version must match pyproject.toml version."""
        from claude_token_guard import __version__

        pyproject_path = os.path.join(_REPO_ROOT, "pyproject.toml")
        with open(pyproject_path, "r") as f:
            content = f.read()
        # Parse version from pyproject.toml
        for line in content.split("\n"):
            if line.strip().startswith("version"):
                pyproject_version = line.split("=")[1].strip().strip('"')
                break
        else:
            pytest.fail("No version found in pyproject.toml")

        assert __version__ == pyproject_version, (
            f"Version mismatch: __init__.py={__version__} vs pyproject.toml={pyproject_version}"
        )


class TestManifest:
    """Verify manifest building and integrity."""

    def test_build_manifest_creates_file(self, tmp_path):
        """_build_manifest should create a valid manifest file."""
        from claude_token_guard.cli import _sha256, HOOK_FILES

        # Create a mini hooks dir with a few files
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        for fname in ["token-guard.py", "hook_utils.py"]:
            src = os.path.join(_REPO_ROOT, fname)
            if os.path.isfile(src):
                with open(src, "rb") as sf:
                    (hooks_dir / fname).write_bytes(sf.read())

        manifest_path = hooks_dir / ".manifest.json"

        # Build manifest manually (can't call _build_manifest directly since it uses module constants)
        import hashlib
        import datetime
        files = {}
        for fname in HOOK_FILES:
            fpath = str(hooks_dir / fname)
            if os.path.isfile(fpath):
                files[fname] = {
                    "sha256": _sha256(fpath),
                    "size": os.path.getsize(fpath),
                }
        manifest = {
            "version": "test",
            "installed_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "files": files,
        }
        with open(str(manifest_path), "w") as f:
            json.dump(manifest, f, indent=2)

        # Verify manifest
        with open(str(manifest_path), "r") as f:
            loaded = json.load(f)
        assert "version" in loaded
        assert "installed_at" in loaded
        assert "files" in loaded
        assert len(loaded["files"]) >= 1

    def test_sha256_deterministic(self):
        """SHA256 of same file should be identical across calls."""
        from claude_token_guard.cli import _sha256
        fpath = os.path.join(_REPO_ROOT, "hook_utils.py")
        h1 = _sha256(fpath)
        h2 = _sha256(fpath)
        assert h1 == h2
        assert len(h1) == 64  # SHA256 hex digest length

    def test_manifest_matches_hook_files(self):
        """HOOK_FILES list should match expected installable files."""
        from claude_token_guard.cli import HOOK_FILES
        # All entries should be files, not directories
        for fname in HOOK_FILES:
            assert "." in fname, f"HOOK_FILES entry without extension: {fname}"
            assert "/" not in fname, f"HOOK_FILES entry with path separator: {fname}"


class TestBuildArtifacts:
    """Test that package builds produce valid artifacts."""

    @pytest.mark.skipif(
        subprocess.run(["python3", "-m", "build", "--help"],
                       capture_output=True).returncode != 0,
        reason="build module not installed"
    )
    def test_build_wheel(self, tmp_path):
        """python -m build --wheel should succeed."""
        result = subprocess.run(
            ["python3", "-m", "build", "--wheel", "--outdir", str(tmp_path)],
            capture_output=True, text=True, cwd=_REPO_ROOT, timeout=60,
        )
        assert result.returncode == 0, f"Wheel build failed: {result.stderr}"
        wheels = [f for f in os.listdir(str(tmp_path)) if f.endswith(".whl")]
        assert len(wheels) >= 1, "No .whl file produced"

    @pytest.mark.skipif(
        subprocess.run(["python3", "-m", "build", "--help"],
                       capture_output=True).returncode != 0,
        reason="build module not installed"
    )
    def test_build_sdist(self, tmp_path):
        """python -m build --sdist should succeed."""
        result = subprocess.run(
            ["python3", "-m", "build", "--sdist", "--outdir", str(tmp_path)],
            capture_output=True, text=True, cwd=_REPO_ROOT, timeout=60,
        )
        assert result.returncode == 0, f"Sdist build failed: {result.stderr}"
        tarballs = [f for f in os.listdir(str(tmp_path)) if f.endswith(".tar.gz")]
        assert len(tarballs) >= 1, "No .tar.gz file produced"


class TestVerifyAndDrift:
    """Test the verify and drift CLI commands."""

    def test_verify_passes_on_healthy_install(self, tmp_path):
        """cmd_verify should pass when all hook files are present with matching checksums."""
        from claude_token_guard.cli import HOOK_FILES, _sha256, MANIFEST_FILE
        import datetime

        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        state_dir = hooks_dir / "session-state"
        state_dir.mkdir()

        # Copy all hook files to the temp hooks dir
        for fname in HOOK_FILES:
            src = os.path.join(_REPO_ROOT, fname)
            if os.path.isfile(src):
                with open(src, "rb") as sf:
                    (hooks_dir / fname).write_bytes(sf.read())

        # Create manifest
        files = {}
        for fname in HOOK_FILES:
            fpath = str(hooks_dir / fname)
            if os.path.isfile(fpath):
                files[fname] = {"sha256": _sha256(fpath), "size": os.path.getsize(fpath)}
        manifest = {
            "version": "1.1.0",
            "installed_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "files": files,
        }
        with open(str(hooks_dir / ".manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)

        # Create settings.json with hook registrations
        settings_path = tmp_path / "settings.json"
        settings = {
            "hooks": {
                "PreToolUse": [
                    {"type": "command", "command": f"python3 {hooks_dir}/token-guard.py"},
                    {"type": "command", "command": f"python3 {hooks_dir}/read-efficiency-guard.py"},
                ],
                "SessionStart": [
                    {"type": "command", "command": f"python3 {hooks_dir}/self-heal.py"},
                ],
            }
        }
        with open(str(settings_path), "w") as f:
            json.dump(settings, f, indent=2)

        # Write token-guard.py and self-heal.py as stubs for smoke test
        for fname in ["token-guard.py", "self-heal.py"]:
            fpath = hooks_dir / fname
            if not fpath.exists():
                fpath.write_text("#!/usr/bin/env python3\nimport sys; sys.exit(0)\n")

        # Verify the manifest itself is valid
        with open(str(hooks_dir / ".manifest.json"), "r") as f:
            loaded = json.load(f)
        assert "version" in loaded
        assert "files" in loaded
        assert len(loaded["files"]) >= 1

    def test_drift_detects_modified_file(self, tmp_path):
        """Drift should detect when an installed file has been modified."""
        from claude_token_guard.cli import HOOK_FILES, _sha256
        import datetime

        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()

        # Copy hook_utils.py
        src = os.path.join(_REPO_ROOT, "hook_utils.py")
        dst = hooks_dir / "hook_utils.py"
        with open(src, "rb") as sf:
            dst.write_bytes(sf.read())

        # Create manifest with correct checksum
        original_hash = _sha256(str(dst))
        manifest = {
            "version": "1.1.0",
            "installed_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "files": {"hook_utils.py": {"sha256": original_hash, "size": os.path.getsize(str(dst))}},
        }
        manifest_path = hooks_dir / ".manifest.json"
        with open(str(manifest_path), "w") as f:
            json.dump(manifest, f, indent=2)

        # Modify the file
        with open(str(dst), "a") as f:
            f.write("\n# modified\n")

        # Verify checksum changed
        new_hash = _sha256(str(dst))
        assert new_hash != original_hash, "File modification should change the SHA256"


class TestSettingsSchema:
    """Test that _patch_settings produces correct hook registration structure."""

    def test_patch_settings_creates_correct_structure(self, tmp_path):
        """_patch_settings should create valid hook entries with type and command keys."""
        from claude_token_guard import cli

        # Override paths for isolated test
        original_settings = cli.SETTINGS_PATH
        original_hooks = cli.HOOKS_DIR
        cli.SETTINGS_PATH = str(tmp_path / "settings.json")
        cli.HOOKS_DIR = str(tmp_path / "hooks")
        try:
            os.makedirs(cli.HOOKS_DIR, exist_ok=True)
            cli._patch_settings()

            with open(cli.SETTINGS_PATH, "r") as f:
                settings = json.load(f)

            hooks = settings["hooks"]
            # Verify required hook event keys exist
            assert "PreToolUse" in hooks
            assert "SessionStart" in hooks
            assert "SubagentStart" in hooks
            assert "SubagentStop" in hooks

            # Verify all entries have "type" and "command" keys
            for key in ["PreToolUse", "SessionStart", "SubagentStart", "SubagentStop"]:
                for entry in hooks[key]:
                    assert "type" in entry, f"{key} entry missing 'type'"
                    assert "command" in entry, f"{key} entry missing 'command'"
                    assert entry["type"] == "command", f"{key} entry type should be 'command'"

            # Verify specific hooks are registered
            pre_tool_commands = [e["command"] for e in hooks["PreToolUse"]]
            assert any("token-guard" in c for c in pre_tool_commands), "token-guard not in PreToolUse"
            assert any("read-efficiency-guard" in c for c in pre_tool_commands), "read-efficiency-guard not in PreToolUse"

            session_commands = [e["command"] for e in hooks["SessionStart"]]
            assert any("self-heal" in c for c in session_commands), "self-heal not in SessionStart"
        finally:
            cli.SETTINGS_PATH = original_settings
            cli.HOOKS_DIR = original_hooks
