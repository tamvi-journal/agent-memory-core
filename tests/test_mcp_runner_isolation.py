from pathlib import Path

import pytest

from tools.golden_mcp.generate import assert_startup_paths, isolated_environment


def test_mcp_oracle_environment_is_allowlisted_and_roots_are_temporary(tmp_path, monkeypatch):
    for key in ("HTTPS_PROXY", "TRAJECTA_WORK_ROOT", "TRAJECTA_IDENTITY_DATA_DIR", "TRAJECTA_IDENTITY_PROFILES"):
        monkeypatch.setenv(key, "synthetic-parent-value")
    env = isolated_environment(tmp_path, tmp_path / "clock")
    assert "HTTPS_PROXY" not in env
    assert "TRAJECTA_WORK_ROOT" not in env
    for key in ("HOME", "USERPROFILE", "XDG_DATA_HOME", "LOCALAPPDATA", "APPDATA",
                "TRAJECTA_IDENTITY_DATA_DIR", "TRAJECTA_IDENTITY_PROFILES"):
        assert Path(env[key]).is_relative_to(tmp_path)
    assert (Path(env["TRAJECTA_IDENTITY_PROFILES"]) / "example/profile.json").is_file()
    assert_startup_paths(tmp_path, env, "example", "store.sqlite3")
    with pytest.raises(AssertionError, match="unapproved child env"):
        isolated_environment(tmp_path, tmp_path / "clock", {"HTTPS_PROXY": "synthetic"})


def test_mcp_oracle_refuses_escaping_writable_paths(tmp_path):
    env = isolated_environment(tmp_path, tmp_path / "clock")
    with pytest.raises(AssertionError, match="escapes MCP fixture"):
        assert_startup_paths(tmp_path, env, "example", "../outside.sqlite3")
    with pytest.raises(AssertionError, match="escapes MCP fixture"):
        assert_startup_paths(tmp_path, {**env, "TRAJECTA_IDENTITY_DATA_DIR": str(tmp_path.parent)}, "example", "store.sqlite3")
    with pytest.raises(AssertionError, match="escapes MCP fixture"):
        assert_startup_paths(tmp_path, {**env, "TRAJECTA_WORK_ROOT": "../work"}, "example", "store.sqlite3")
    # Existing symlink ancestors are checked, even when the DB does not yet exist.
    link = tmp_path / "escape"
    try:
        link.symlink_to(tmp_path.parent, target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit test symlinks")
    with pytest.raises(AssertionError, match="escapes MCP fixture"):
        assert_startup_paths(tmp_path, env, "example", "escape/new.sqlite3")
    install = Path(env["TRAJECTA_IDENTITY_DATA_DIR"]) / "profiles/example"
    install.mkdir(parents=True)
    (install / "profile.json").symlink_to(tmp_path.parent / "synthetic-profile.json")
    with pytest.raises(AssertionError, match="escapes MCP fixture"):
        assert_startup_paths(tmp_path, env, "example", "store.sqlite3")
