"""Handles caching of import metadata, such as id_maps."""

import configparser
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import polars as pl

from ..logging_config import log

_cache_enabled = True
_uuid_by_fingerprint: dict[str, str | None] = {}


def set_cache_enabled(enabled: bool) -> None:
    """Enable or disable the on-disk cache process-wide (the --no-cache switch).

    Args:
        enabled: False disables all cache reads and writes; True (default)
            restores normal caching.
    """
    global _cache_enabled
    _cache_enabled = enabled


# Folding the Odoo database.uuid into the cache key invalidates the cache when the
# target database is rebuilt, or a *different* dump is restored under the same
# host/db name — the record ids change but host+port+db alone would not. A
# minimal-privilege API user may lack read access to ir.config_parameter; on any
# failure _database_uuid returns None and the caller falls back to the host+port+db
# key (restore-detection is simply unavailable for that user).
def _database_uuid(config: str | dict[str, Any], base_fingerprint: str) -> str | None:
    """Best-effort Odoo ``database.uuid``, memoized per connection fingerprint."""
    if base_fingerprint in _uuid_by_fingerprint:
        return _uuid_by_fingerprint[base_fingerprint]
    uuid: str | None = None
    try:
        from . import conf_lib  # local import avoids an import cycle

        connection = (
            conf_lib.get_connection_from_dict(config)
            if isinstance(config, dict)
            else conf_lib.get_connection_from_config(config)
        )
        value = connection.get_model("ir.config_parameter").get_param("database.uuid")
        if value:
            uuid = str(value)
    except Exception as e:
        log.debug(
            "Could not read database.uuid for the cache key "
            f"(restore-detection unavailable for this user): {e}"
        )
    _uuid_by_fingerprint[base_fingerprint] = uuid
    return uuid


def _connection_fingerprint(config: str | dict[str, Any]) -> str | None:
    """Return a stable host+port+db(+database.uuid) fingerprint for a config.

    Accepts either a path to a connection .conf file or a connection dict, so the
    cache works regardless of how the caller supplied the connection. The database
    uuid is appended when readable, so a rebuilt/restored database gets a fresh
    cache instead of reusing stale id mappings (see :func:`_database_uuid`).

    Args:
        config: Connection file path or a connection dict.

    Returns:
        A short string fingerprinting the target server/db, or None on failure.
    """
    try:
        if isinstance(config, dict):
            base = (
                f"{config.get('hostname')}{config.get('port')}{config.get('database')}"
            )
        else:
            parser = configparser.ConfigParser()
            parser.read(config)
            base = (
                f"{parser.get('Connection', 'hostname')}"
                f"{parser.get('Connection', 'port')}"
                f"{parser.get('Connection', 'database')}"
            )
    except Exception as e:  # pragma: no cover - defensive
        log.error(f"Could not fingerprint connection config: {e}")
        return None

    uuid = _database_uuid(config, base)
    return f"{base}{uuid}" if uuid else base


def resolve_cache_dir(config: str | dict[str, Any]) -> Path | None:
    """Cache directory for a connection, accepting a file path or a dict.

    Args:
        config: Connection file path or a connection dict.

    Returns:
        A Path to the unique cache directory, or None on failure.
    """
    if not _cache_enabled:
        return None
    fingerprint = _connection_fingerprint(config)
    if fingerprint is None:
        return None
    try:
        hash_id = hashlib.sha256(fingerprint.encode()).hexdigest()
        cache_dir = Path(".fluvo_cache") / hash_id
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir
    except Exception as e:  # pragma: no cover - defensive
        log.error(f"Could not create or access cache directory: {e}")
        return None


def get_cache_dir(config_file: str) -> Path | None:
    """Generates a unique, connection-specific cache directory path.

    Delegates to :func:`resolve_cache_dir` so both entry points share the same
    fingerprint (host+port+db+database.uuid) and the --no-cache switch.

    Args:
        config_file: Path to the Odoo connection configuration file.

    Returns:
        A Path object to the unique cache directory, or None on failure.
    """
    return resolve_cache_dir(config_file)


def save_id_map(config_file: str, model: str, id_map: dict[str, int]) -> None:
    """Saves an id_map dictionary to a Parquet file in the cache.

    Args:
        config_file: Path to the Odoo connection configuration file.
        model: The Odoo model name (e.g., 'res.partner').
        id_map: The dictionary mapping external IDs to database IDs.
    """
    cache_dir = get_cache_dir(config_file)
    if not cache_dir or not id_map:
        return

    try:
        df = pl.DataFrame({"external_id": id_map.keys(), "db_id": id_map.values()})
        file_path = cache_dir / f"{model}.id_map.parquet"
        df.write_parquet(file_path)
        log.info(f"Saved id_map for model '{model}' to cache: {file_path}")
    except Exception as e:
        log.error(f"Failed to save id_map for model '{model}': {e}")


def load_id_map(config_file: str, model: str) -> pl.DataFrame | None:
    """Loads an id_map from the cache into a Polars DataFrame.

    Args:
        config_file: Path to the Odoo connection configuration file.
        model: The Odoo model name to load the map for.

    Returns:
        A Polars DataFrame with 'external_id' and 'db_id' columns, or None.
    """
    cache_dir = get_cache_dir(config_file)
    if not cache_dir:
        return None

    file_path = cache_dir / f"{model}.id_map.parquet"
    if not file_path.exists():
        log.warning(f"No cache file found for model '{model}' at {file_path}")
        return None

    try:
        log.info(f"Loading id_map for model '{model}' from cache.")
        return pl.read_parquet(file_path)
    except Exception as e:
        log.error(f"Failed to load id_map for model '{model}': {e}")
        return None


def export_id_map(
    config: str | dict[str, Any],
    model: str,
    key_field: str,
    domain: list[Any] | None = None,
    *,
    force_refresh: bool = False,
) -> pl.DataFrame | None:
    """Export and cache an id-map for ``model`` keyed by a natural field.

    Reads every (or domain-filtered) record of ``model`` once and resolves each to
    its XML ID (via ``ir.model.data``), producing a Polars DataFrame with columns
    ``key`` (the stringified ``key_field`` value), ``xmlid`` and ``db_id``. The
    result is cached to parquet per (connection, model, key_field) and reused on
    subsequent runs, so relation pre-resolution joins (see
    :meth:`transform.Processor.resolve_relation`) never pay an Odoo ``name_search``.

    Args:
        config: Connection file path or connection dict.
        model: The related Odoo model to map (e.g. ``res.country``).
        key_field: The natural-key field the source data references (e.g. ``name``,
            ``code``, ``ref``).
        domain: Optional domain to limit which records are mapped.
        force_refresh: If True, ignore any cached copy and re-export.

    Returns:
        DataFrame ``[key, xmlid, db_id]``, or None on failure.
    """
    from . import conf_lib  # local import avoids any import cycle

    cache_dir = resolve_cache_dir(config)
    safe_key = key_field.replace("/", "_")
    file_path = cache_dir / f"{model}.idmap__{safe_key}.parquet" if cache_dir else None

    if file_path and file_path.exists() and not force_refresh:
        try:
            log.info(f"Loading cached id-map for '{model}' (key={key_field}).")
            return pl.read_parquet(file_path)
        except Exception as e:  # pragma: no cover - defensive
            log.warning(f"Could not read cached id-map ({file_path}): {e}")

    try:
        connection = (
            conf_lib.get_connection_from_dict(config)
            if isinstance(config, dict)
            else conf_lib.get_connection_from_config(config)
        )
        records = connection.get_model(model).search_read(domain or [], [key_field])
        if not records:
            log.warning(f"id-map export for '{model}' returned no records.")
            return pl.DataFrame(
                schema={"key": pl.String, "xmlid": pl.String, "db_id": pl.Int64}
            )

        db_ids = [r["id"] for r in records]
        # Bulk-resolve XML IDs for these records in one round trip.
        imd = connection.get_model("ir.model.data").search_read(
            [["model", "=", model], ["res_id", "in", db_ids]],
            ["res_id", "module", "name"],
        )
        xmlid_by_res: dict[int, str] = {}
        for rec in imd:
            # First XML ID wins if a record somehow has several.
            xmlid_by_res.setdefault(
                int(rec["res_id"]), f"{rec['module']}.{rec['name']}"
            )

        def _key_str(value: Any) -> str | None:
            # m2o values come back as [id, name]; take the display part.
            if isinstance(value, (list, tuple)):
                return str(value[1]) if len(value) > 1 else None
            return None if value is False or value is None else str(value)

        df = pl.DataFrame(
            {
                "key": [_key_str(r.get(key_field)) for r in records],
                "xmlid": [xmlid_by_res.get(int(r["id"])) for r in records],
                "db_id": [int(r["id"]) for r in records],
            }
        )
        if file_path:
            df.write_parquet(file_path)
            log.info(
                f"Cached id-map for '{model}' (key={key_field}, "
                f"{df.height} rows) -> {file_path}"
            )
        return df
    except Exception as e:
        log.error(f"Failed to export id-map for model '{model}': {e}")
        return None


def save_fields_get_cache(
    config_file: str, model: str, fields_data: dict[str, Any]
) -> None:
    """Saves the result of a 'fields_get' call to a JSON file in the cache.

    Args:
        config_file: Path to the Odoo connection configuration file.
        model: The Odoo model name.
        fields_data: The dictionary returned by the fields_get method.
    """
    cache_dir = get_cache_dir(config_file)
    if not cache_dir or not fields_data:
        return

    file_path = cache_dir / f"{model}.fields.json"
    try:
        with file_path.open("w") as f:
            json.dump(fields_data, f, indent=2)
        log.info(f"Saved fields_get cache for model '{model}' to {file_path}")
    except Exception as e:
        log.error(f"Failed to save fields_get cache for model '{model}': {e}")


def load_fields_get_cache(config_file: str, model: str) -> dict[str, Any] | None:
    """Loads a 'fields_get' result from the JSON cache file.

    Args:
        config_file: Path to the Odoo connection configuration file.
        model: The Odoo model name.

    Returns:
        The cached dictionary, or None if not found or on error.
    """
    cache_dir = get_cache_dir(config_file)
    if not cache_dir:
        return None

    file_path = cache_dir / f"{model}.fields.json"
    if not file_path.exists():
        return None

    try:
        with file_path.open("r") as f:
            log.info(f"Loading fields_get cache for model '{model}' from cache.")
            return cast(dict[str, Any], json.load(f))
    except Exception as e:
        log.error(f"Failed to load fields_get cache for model '{model}': {e}")
        return None


def generate_session_id(model: str, domain: list[Any], fields: list[Any]) -> str:
    """Generates a unique session ID for an export job.

    Args:
        model: The Odoo model name.
        domain: The domain filter for the export.
        fields: The list of fields to export.

    Returns:
        A unique hexadecimal session ID string.
    """
    # The domain can contain tuples or lists, so we convert lists to tuples
    # to make them hashable and ensure consistent sorting.
    try:
        stable_domain = sorted(
            tuple(item) if isinstance(item, list) else item for item in domain
        )
    except TypeError:
        # Fallback for un-sortable domains
        stable_domain = [
            tuple(item) if isinstance(item, list) else item for item in domain
        ]

    domain_str = str(stable_domain)
    fields_str = str(sorted(fields))
    session_str = f"{model}{domain_str}{fields_str}"
    return hashlib.sha256(session_str.encode()).hexdigest()[:16]


def get_session_dir(session_id: str) -> Path | None:
    """Creates and returns the path to a specific session directory.

    Args:
        session_id: The unique ID of the session.

    Returns:
        A Path object to the session directory, or None on failure.
    """
    try:
        # Session directories are stored in a common 'sessions' subdir to
        # distinguish them from other connection-specific caches.
        session_dir = Path(".fluvo_cache") / "sessions" / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir
    except Exception as e:
        log.error(f"Could not create or access session directory '{session_id}': {e}")
        return None
