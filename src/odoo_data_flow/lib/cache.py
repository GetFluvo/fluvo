"""Handles caching of import metadata, such as id_maps."""

import configparser
import hashlib
import json
from pathlib import Path
from typing import Any, Optional, cast

import polars as pl

from ..logging_config import log


def get_cache_dir(config_file: str) -> Optional[Path]:
    """Generates a unique, connection-specific cache directory path.

    Args:
        config_file: Path to the Odoo connection configuration file.

    Returns:
        A Path object to the unique cache directory, or None on failure.
    """
    try:
        config = configparser.ConfigParser()
        config.read(config_file)
        connection_str = (
            f"{config.get('Connection', 'hostname')}"
            f"{config.get('Connection', 'port')}"
            f"{config.get('Connection', 'database')}"
        )
        hash_id = hashlib.sha256(connection_str.encode()).hexdigest()
        cache_dir = Path(".odf_cache") / hash_id
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir
    except Exception as e:
        log.error(f"Could not create or access cache directory: {e}")
        return None


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


def load_id_map(config_file: str, model: str) -> Optional[pl.DataFrame]:
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


def load_fields_get_cache(config_file: str, model: str) -> Optional[dict[str, Any]]:
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
