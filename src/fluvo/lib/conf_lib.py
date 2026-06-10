"""Config File Handler.

This module handles creating Odoo connections from configuration,
supporting both file-based and dictionary-based setups.

Supported protocols (via odoolib):
- xmlrpc / xmlrpcs: XML-RPC (default, compatible with all Odoo versions)
- jsonrpc / jsonrpcs: JSON-RPC (recommended for Odoo 10+, ~30% faster)
- json2 / json2s: JSON-2 API (Odoo 19+, requires API key instead of password)
"""

import configparser
import inspect
from typing import Any

import odoolib

from ..logging_config import log
from . import rpc_transport

# Replace odoo-client-lib's per-call httpx.post transport with a pooled client
# (one SSL context, keep-alive, generous timeout). Critical for large imports.
rpc_transport.install()

_connection_cache: dict[str, Any] = {}

# Parameters odoolib.get_connection() accepts, captured once from the real function
# at import time (before tests can patch it). Used to drop unknown [Connection]
# keys instead of crashing on them (#194).
try:
    _ODOOLIB_PARAMS = frozenset(inspect.signature(odoolib.get_connection).parameters)
except (TypeError, ValueError):  # pragma: no cover - non-introspectable signature
    _ODOOLIB_PARAMS = frozenset(
        {"hostname", "protocol", "port", "database", "login", "password", "user_id"}
    )


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

        # Per-connection User-Agent override (#193): WAFs such as Cloudflare
        # challenge the default client UA on the json2 endpoint. This is applied
        # to the pooled transport, not passed to odoolib.
        user_agent = config_dict.pop("user_agent", None)
        if user_agent:
            rpc_transport.set_user_agent(str(user_agent))

        # Log protocol being used
        protocol = config_dict.get("protocol", "xmlrpc")
        log.info(
            f"Connecting to Odoo server at {config_dict.get('hostname')} "
            f"using {protocol} protocol..."
        )

        # odoolib.get_connection() raises TypeError on any unknown kwarg (#194).
        # Connection files naturally accumulate extra keys (inline notes, old
        # credentials kept for reference, settings for other tooling); keep only
        # the parameters odoolib accepts and ignore the rest.
        ignored = sorted(k for k in config_dict if k not in _ODOOLIB_PARAMS)
        if ignored:
            log.debug(f"Ignoring non-connection config keys: {ignored}")
        clean = {k: v for k, v in config_dict.items() if k in _ODOOLIB_PARAMS}

        # Use odoo-client-lib to establish the connection
        connection = odoolib.get_connection(**clean)
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
