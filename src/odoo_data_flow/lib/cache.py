"""Handles caching of import metadata, such as id_maps."""

import hashlib
from pathlib import Path
from typing import Optional

import polars as pl

from ..logging_config import log
from . import conf_lib


def get_cache_dir(config_file: str) -> Optional[Path]:
    """Generates a unique, connection-specific cache directory path.

    Args:
        config_file: Path to the Odoo connection configuration file.

    Returns:
        A Path object to the unique cache directory, or None on failure.
    """
    try:
        config = conf_lib.get_connection_from_config(config_file)
        connection_str = f"{config.hostname}{config.port}{config.database}"
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
