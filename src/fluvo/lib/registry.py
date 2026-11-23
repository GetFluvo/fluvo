"""Named connection registry: pick a connection by key, not a raw file (#363).

Today every command is handed a connection *file*. A registry lets a project
name its connections once, in a ``fluvo-connections.toml`` that is safe to commit
because it holds **no secrets** — an inline entry references an environment
variable by name (``password_env``), never an inline password. A run then selects
one with ``--connection <key>``.

The file is opt-in and versioned (``schema = 1``): nothing changes for a run that
keeps passing ``--connection-file``. Discovery is deliberately shallow — an
explicit ``--registry`` path, then ``$FLUVO_REGISTRY``, then
``./fluvo-connections.toml`` in the current directory — with **no** home- or
parent-directory search, so a stray file can never silently point a run at the
wrong database.

Example::

    schema = 1

    [connections.test]
    file = "conf/test.conf"            # a legacy connection file, reused as-is

    [connections.prod]
    hostname = "erp.example.com"
    database = "prod"
    login = "migration@example.com"
    port = 443
    protocol = "jsonrpcs"
    password_env = "FLUVO_PROD_PASSWORD"   # a reference; the secret stays in the env
"""

import os
from pathlib import Path
from typing import Any

from .core import ConnectError, InputError

REGISTRY_SCHEMA = 1
DEFAULT_REGISTRY_FILENAME = "fluvo-connections.toml"

# Keys an inline entry may carry. A secret is referenced by *_env (the name of an
# environment variable), never inlined.
_INLINE_OPTIONAL = {"port", "protocol", "user_agent"}
_INLINE_REQUIRED = {"hostname", "database", "login"}
_SECRET_REF_KEYS = {"password_env", "api_key_env"}
_INLINE_KEYS = _INLINE_REQUIRED | _INLINE_OPTIONAL | _SECRET_REF_KEYS
# A literal secret must never appear in a committed registry.
_FORBIDDEN_INLINE = {"password", "api_key"}


def _load_toml(path: str) -> dict[str, Any]:
    """Parse a TOML file, using stdlib ``tomllib`` or the ``tomli`` backport.

    Args:
        path: The registry file path.

    Returns:
        dict[str, Any]: The parsed document.

    Raises:
        InputError: If the file cannot be read or is not valid TOML.
    """
    try:
        import tomllib as toml_reader
    except ModuleNotFoundError:  # Python 3.10
        import tomli as toml_reader  # type: ignore[no-redef]

    try:
        with open(path, "rb") as handle:
            return toml_reader.load(handle)
    except OSError as exc:
        raise InputError(f"could not read connection registry '{path}': {exc}") from exc
    except toml_reader.TOMLDecodeError as exc:
        raise InputError(
            f"invalid TOML in connection registry '{path}': {exc}"
        ) from exc


def discover_registry(explicit: str | None) -> str | None:
    """Find the registry file to use, by shallow, explicit precedence.

    Args:
        explicit: The path given with ``--registry``, if any.

    Returns:
        str | None: ``explicit`` if given, else ``$FLUVO_REGISTRY`` if set, else
        ``./fluvo-connections.toml`` if it exists, else ``None``.
    """
    if explicit:
        return explicit
    env = os.environ.get("FLUVO_REGISTRY")
    if env:
        return env
    default = Path.cwd() / DEFAULT_REGISTRY_FILENAME
    return str(default) if default.is_file() else None


def _resolve_inline(key: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Resolve an inline connection entry into a connection dict.

    Args:
        key: The connection key (used as the fail-file environment name).
        entry: The ``[connections.<key>]`` table.

    Returns:
        dict[str, Any]: A config dict for ``get_connection_from_dict`` with the
        secret resolved from its environment variable and ``_env_name`` set.

    Raises:
        InputError: On a forbidden inline secret, a missing required field, the
            wrong number of secret references, or an unknown key.
        ConnectError: If the referenced environment variable is unset or empty.
    """
    forbidden = _FORBIDDEN_INLINE & set(entry)
    if forbidden:
        raise InputError(
            f"connection '{key}': a literal {sorted(forbidden)[0]!r} is not allowed "
            "in the registry; reference an environment variable with "
            "`password_env` / `api_key_env` instead."
        )
    unknown = set(entry) - _INLINE_KEYS
    if unknown:
        raise InputError(f"connection '{key}': unknown key '{sorted(unknown)[0]}'.")
    missing = _INLINE_REQUIRED - set(entry)
    if missing:
        raise InputError(
            f"connection '{key}': missing required field(s) {sorted(missing)}."
        )
    secret_refs = _SECRET_REF_KEYS & set(entry)
    if len(secret_refs) != 1:
        raise InputError(
            f"connection '{key}': needs exactly one of `password_env` or "
            f"`api_key_env`, got {sorted(secret_refs)}."
        )

    config: dict[str, Any] = {
        k: entry[k] for k in (_INLINE_REQUIRED | _INLINE_OPTIONAL) & set(entry)
    }
    ref_key = secret_refs.pop()
    env_var = str(entry[ref_key])
    secret = os.environ.get(env_var)
    if not secret:
        raise ConnectError(
            f"connection '{key}': environment variable '{env_var}' "
            f"(from `{ref_key}`) is unset or empty."
        )
    # Both password and api_key map to odoolib's password field downstream.
    config["password"] = secret
    # The fail-file environment directory follows the connection key, not a file.
    config["_env_name"] = key
    return config


def resolve_connection(key: str, registry_path: str) -> str | dict[str, Any]:
    """Resolve a connection key against a registry into a usable config.

    Args:
        key: The connection key requested with ``--connection``.
        registry_path: The registry file to read.

    Returns:
        str | dict[str, Any]: A connection-file path (for a ``file =`` entry) or a
        connection dict (for an inline entry), ready for the existing connection
        helpers. An inline entry's secret is resolved by :func:`_resolve_inline`,
        which raises ``ConnectError`` if the referenced environment variable is
        unset.

    Raises:
        InputError: On a bad schema, an unknown key, or an entry that mixes
            ``file`` with inline fields.
    """
    doc = _load_toml(registry_path)
    schema = doc.get("schema")
    if schema != REGISTRY_SCHEMA:
        raise InputError(
            f"connection registry '{registry_path}': `schema` must be "
            f"{REGISTRY_SCHEMA}, got {schema!r}."
        )
    connections = doc.get("connections")
    if not isinstance(connections, dict):
        raise InputError(
            f"connection registry '{registry_path}': a `[connections.<key>]` "
            "table is required."
        )
    entry = connections.get(key)
    if not isinstance(entry, dict):
        raise InputError(
            f"connection '{key}' is not defined in '{registry_path}'. "
            f"Defined: {sorted(connections)}."
        )
    if "file" in entry:
        if set(entry) != {"file"}:
            raise InputError(
                f"connection '{key}': `file` cannot be combined with inline "
                f"fields {sorted(set(entry) - {'file'})}."
            )
        return str(entry["file"])
    return _resolve_inline(key, entry)
