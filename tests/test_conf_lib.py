"""Test the configuration and connection handling."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluvo.lib.conf_lib import (
    _read_config_file,
    get_connection_from_config,
    get_connection_from_dict,
)


# --- Tests for file-based configuration ---
@patch("fluvo.lib.conf_lib.odoolib.get_connection")
def test_get_connection_from_config_success(
    mock_get_connection: MagicMock, tmp_path: Path
) -> None:
    """Tests successful connection configuration parsing.

    Verifies that it reads a valid config file and calls the underlying
    connection library with correctly parsed and typed parameters.
    """
    # 1. Setup: Create a valid temporary config file
    config_file = tmp_path / "connection.conf"
    config_content = """
[Connection]
hostname = test-server
port = 8070
database = test-db
login = test-user
password = test-pass
uid = 2
"""
    config_file.write_text(config_content)
    get_connection_from_config(str(config_file))
    mock_get_connection.assert_called_once()
    call_kwargs = mock_get_connection.call_args.kwargs
    assert call_kwargs.get("hostname") == "test-server"
    assert call_kwargs.get("port") == 8070
    assert call_kwargs.get("user_id") == 2


def test_get_connection_file_not_found() -> None:
    """Tests that a FileNotFoundError is raised if the config file does not exist."""
    with pytest.raises(FileNotFoundError):
        get_connection_from_config("non_existent_file.conf")


def test_get_connection_missing_key_from_file(tmp_path: Path) -> None:
    """Tests that a KeyError is raised if a required key is missing from a file."""
    config_file = tmp_path / "missing_key.conf"
    config_file.write_text("[Connection]\nhostname = test-server\n")
    with pytest.raises(KeyError):
        get_connection_from_config(str(config_file))


# --- Tests for dictionary-based configuration ---
@patch("fluvo.lib.conf_lib.odoolib.get_connection")
def test_get_connection_from_dict_success(mock_get_connection: MagicMock) -> None:
    """Tests successful connection configuration parsing from a dictionary."""
    config_dict = {
        "hostname": "dict-server",
        "port": "8080",  # Test string-to-int conversion
        "database": "dict-db",
        "login": "dict-user",
        "password": "dict-password",
        "uid": "3",  # Test string-to-int conversion
    }
    get_connection_from_dict(config_dict)
    mock_get_connection.assert_called_once()
    call_kwargs = mock_get_connection.call_args.kwargs
    assert call_kwargs.get("hostname") == "dict-server"
    assert call_kwargs.get("port") == 8080
    assert call_kwargs.get("user_id") == 3
    assert "uid" not in call_kwargs


def test_get_connection_missing_key_from_dict() -> None:
    """Tests that a KeyError is raised if a required key is missing from a dict."""
    config_dict = {"hostname": "test-server"}  # Missing database, login, etc.
    with pytest.raises(KeyError, match="'database'"):
        get_connection_from_dict(config_dict)


def test_get_connection_malformed_value_from_dict() -> None:
    """Tests that a ValueError is raised for a malformed value from a dict."""
    config_dict = {
        "hostname": "test-server",
        "database": "test-db",
        "login": "admin",
        "password": "admin",
        "port": "not-a-number",
    }
    with pytest.raises(ValueError):
        get_connection_from_dict(config_dict)


@patch("fluvo.lib.conf_lib.odoolib.get_connection")
def test_get_connection_from_dict_generic_exception(
    mock_get_connection: MagicMock,
) -> None:
    """Tests that a generic Exception from the lib is caught and re-raised."""
    config_dict = {
        "hostname": "test-server",
        "database": "test-db",
        "login": "admin",
        "password": "admin",
    }
    mock_get_connection.side_effect = Exception("Generic connection error")
    with pytest.raises(Exception, match="Generic connection error"):
        get_connection_from_dict(config_dict)


# --- Tests for _config_file handling ---
@patch("fluvo.lib.conf_lib.odoolib.get_connection")
def test_get_connection_from_dict_with_config_file_override(
    mock_get_connection: MagicMock, tmp_path: Path
) -> None:
    """Tests that _config_file key loads base config and merges with overrides."""
    # Create a base config file
    config_file = tmp_path / "base.conf"
    config_content = """
[Connection]
hostname = base-server
port = 8069
database = base-db
login = base-user
password = base-pass
"""
    config_file.write_text(config_content)

    # Pass config dict with _config_file and override some values
    config_dict = {
        "_config_file": str(config_file),
        "hostname": "override-server",  # This should override base
        "password": "override-pass",  # This should override base
    }

    get_connection_from_dict(config_dict)
    mock_get_connection.assert_called_once()
    call_kwargs = mock_get_connection.call_args.kwargs
    # Overridden values
    assert call_kwargs.get("hostname") == "override-server"
    assert call_kwargs.get("password") == "override-pass"
    # Values from base config
    assert call_kwargs.get("database") == "base-db"
    assert call_kwargs.get("login") == "base-user"


# --- Tests for connection caching ---
@patch("fluvo.lib.conf_lib.odoolib.get_connection")
def test_get_connection_from_config_caches_connection(
    mock_get_connection: MagicMock, tmp_path: Path
) -> None:
    """Tests that connections are cached and reused."""
    from fluvo.lib.conf_lib import _connection_cache

    # Clear cache first
    _connection_cache.clear()

    config_file = tmp_path / "connection.conf"
    config_content = """
[Connection]
hostname = test-server
database = test-db
login = test-user
password = test-pass
"""
    config_file.write_text(config_content)

    mock_connection = MagicMock()
    mock_get_connection.return_value = mock_connection

    # First call creates connection
    conn1 = get_connection_from_config(str(config_file))
    assert mock_get_connection.call_count == 1

    # Second call should use cache
    conn2 = get_connection_from_config(str(config_file))
    assert mock_get_connection.call_count == 1  # Not called again
    assert conn1 is conn2

    # Clear cache after test
    _connection_cache.clear()


# --- Tests for _read_config_file ---
def test_read_config_file_not_found() -> None:
    """Tests that _read_config_file raises FileNotFoundError for missing file.

    Covers lines 86-87 in conf_lib.py.
    """
    with pytest.raises(FileNotFoundError, match="Configuration file not found"):
        _read_config_file("nonexistent_config_file.conf")


@patch("fluvo.lib.conf_lib.odoolib.get_connection")
def test_get_connection_ignores_unknown_keys(mock_get_connection: MagicMock) -> None:
    """Unknown [Connection] keys are dropped instead of crashing odoolib (#194)."""
    config_dict = {
        "hostname": "h",
        "database": "d",
        "login": "l",
        "password": "p",
        "old_password": "kept-for-reference",  # not an odoolib parameter
        "note": "some metadata",
    }
    get_connection_from_dict(config_dict)
    call_kwargs = mock_get_connection.call_args.kwargs
    assert "old_password" not in call_kwargs
    assert "note" not in call_kwargs
    assert call_kwargs["hostname"] == "h"


@patch("fluvo.lib.conf_lib.rpc_transport.set_user_agent")
@patch("fluvo.lib.conf_lib.odoolib.get_connection")
def test_get_connection_applies_user_agent(
    mock_get_connection: MagicMock, mock_set_ua: MagicMock
) -> None:
    """A user_agent key configures the transport and is not passed to odoolib (#193)."""
    config_dict = {
        "hostname": "h",
        "database": "d",
        "login": "l",
        "password": "p",
        "user_agent": "Mozilla/5.0 custom",
    }
    get_connection_from_dict(config_dict)
    mock_set_ua.assert_called_once_with("h", "Mozilla/5.0 custom")
    assert "user_agent" not in mock_get_connection.call_args.kwargs


@patch("fluvo.lib.conf_lib.odoolib.get_connection")
def test_get_connection_does_not_mutate_input(mock_get_connection: MagicMock) -> None:
    """get_connection_from_dict must not mutate the caller's dict (#194 review)."""
    config_dict = {
        "hostname": "h",
        "database": "d",
        "login": "l",
        "password": "p",
        "uid": "2",
        "user_agent": "UA",
        "note": "keep",
    }
    snapshot = dict(config_dict)
    get_connection_from_dict(config_dict)
    assert config_dict == snapshot  # original dict untouched
