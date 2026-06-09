"""Config File Handler.

This module handles creating Odoo connections from configuration,
supporting both file-based and dictionary-based setups.

Supported protocols (via odoolib):
- xmlrpc / xmlrpcs: XML-RPC (default, compatible with all Odoo versions)
- jsonrpc / jsonrpcs: JSON-RPC (recommended for Odoo 10+, ~30% faster)
- json2 / json2s: JSON-2 API (Odoo 19+, requires API key instead of password)
"""

import configparser
from typing import Any

import odoolib

from ..logging_config import log
from . import rpc_transport

# Replace odoo-client-lib's per-call httpx.post transport with a pooled client
# (one SSL context, keep-alive, generous timeout). Critical for large imports.
rpc_transport.install()

_connection_cache: dict[str, Any] = {}


def get_connection_from_dict(config_dict: dict[str, Any]) -> Any:
    """Establishes a connection to Odoo from a dictionary.

    Args:
        config_dict: A dictionary with connection details.
            Required: hostname, database, login, password
            Optional: port, protocol (xmlrpc|jsonrpc|json2), uid

    Returns:
        An initialized and connected Odoo client object.
    """
    try:
        # Handle special _config_file key for protocol override
        config_file = config_dict.pop("_config_file", None)
        if config_file:
            # Load base config from file and merge with overrides
            file_config = _read_config_file(config_file)
            # Overrides from dict take precedence
            file_config.update(config_dict)
            config_dict = file_config

        # Explicitly check for required keys before proceeding.
        required_keys = ["hostname", "database", "login", "password"]
        for key in required_keys:
            if key not in config_dict:
                raise KeyError(f"Required key '{key}' not found in config dict.")

        # Ensure port and uid are integers if they exist
        if "port" in config_dict:
            config_dict["port"] = int(config_dict["port"])
        if "uid" in config_dict:
            # The OdooClient expects the user ID as 'user_id'
            config_dict["user_id"] = int(config_dict.pop("uid"))

        # Log protocol being used
        protocol = config_dict.get("protocol", "xmlrpc")
        log.info(
            f"Connecting to Odoo server at {config_dict.get('hostname')} "
            f"using {protocol} protocol..."
        )

        # Use odoo-client-lib to establish the connection
        connection = odoolib.get_connection(**config_dict)
        return connection

    except (KeyError, ValueError) as e:
        log.error(f"Connection config dict is missing a key or has a bad value: {e}")
        raise
    except Exception as e:
        log.error(f"An unexpected error occurred while connecting to Odoo: {e}")
        raise


def _read_config_file(config_file: str) -> dict[str, Any]:
    """Reads a config file and returns its contents as a dictionary.

    Args:
        config_file: The path to the connection.conf file.

    Returns:
        A dictionary with the connection details from the file.
    """
    config = configparser.ConfigParser()
    if not config.read(config_file):
        log.error(f"Configuration file not found or is empty: {config_file}")
        raise FileNotFoundError(f"Configuration file not found: {config_file}")

    return dict(config["Connection"])


def get_connection_from_config(config_file: str) -> Any:
    """Reads a config file and returns an Odoo connection.

    It caches connections based on the file path to reuse them.

    Args:
        config_file: The path to the connection.conf file.

    Returns:
        An initialized and connected Odoo client object.
    """
    if config_file in _connection_cache:
        log.debug(f"Reusing cached connection for {config_file}")
        return _connection_cache[config_file]

    config = configparser.ConfigParser()
    if not config.read(config_file):
        log.error(f"Configuration file not found or is empty: {config_file}")
        raise FileNotFoundError(f"Configuration file not found: {config_file}")

    conn_details: dict[str, Any] = dict(config["Connection"])

    # The core logic is now in get_connection_from_dict
    connection = get_connection_from_dict(conn_details)

    _connection_cache[config_file] = connection
    return connection
