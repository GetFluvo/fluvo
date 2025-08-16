"""Test the configuration and connection handling."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from odoo_data_flow.lib.conf_lib import (
    get_connection_from_config,
    get_connection_from_dict,
)


# --- Tests for file-based configuration ---
@patch("odoo_data_flow.lib.conf_lib.odoolib.get_connection")
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
@patch("odoo_data_flow.lib.conf_lib.odoolib.get_connection")
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


@patch("odoo_data_flow.lib.conf_lib.odoolib.get_connection")
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
