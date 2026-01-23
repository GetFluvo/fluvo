"""GeoNames data utilities for geographic lookups.

This module provides utilities to download, cache, and query GeoNames data
for city-to-country mapping, postal code validation, and geographic lookups.

Data is downloaded from https://download.geonames.org/export/dump/ and cached
locally in ~/.cache/odoo-data-flow/geonames/ for reuse across environments.

Example::

    from odoo_data_flow.lib import geonames, clean

    # Load cities (downloads and caches on first use)
    cities = geonames.get_cities_lookup()

    # Use with detect_country
    clean.detect_country(city="Amsterdam", cities=cities)
    # Returns: 'NL'

    # Or get full city data with coordinates
    df = geonames.load_cities()
    df.filter(pl.col("name") == "Amsterdam")
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import polars as pl

if TYPE_CHECKING:
    pass

__all__ = [
    # Constants
    "DATASETS",
    # Download utilities
    "download_dataset",
    "get_cache_dir",
    # Lookup builders
    "get_cities_lookup",
    "get_postal_lookup",
    "load_alternate_names",
    # Data loading
    "load_cities",
    "load_postal_codes",
]

# =============================================================================
# CONSTANTS
# =============================================================================

GEONAMES_BASE_URL = "https://download.geonames.org/export/dump/"

# Available datasets with their URLs and descriptions
DATASETS: dict[str, dict[str, str]] = {
    # City datasets (by population threshold)
    "cities500": {
        "url": f"{GEONAMES_BASE_URL}cities500.zip",
        "description": "All cities with population > 500 (~200k cities, ~50MB)",
    },
    "cities1000": {
        "url": f"{GEONAMES_BASE_URL}cities1000.zip",
        "description": "All cities with population > 1000 (~150k cities, ~35MB)",
    },
    "cities5000": {
        "url": f"{GEONAMES_BASE_URL}cities5000.zip",
        "description": "All cities with population > 5000 (~50k cities, ~10MB)",
    },
    "cities15000": {
        "url": f"{GEONAMES_BASE_URL}cities15000.zip",
        "description": "All cities with population > 15000 (~25k cities, ~5MB)",
    },
    # Other datasets
    "alternateNamesV2": {
        "url": f"{GEONAMES_BASE_URL}alternateNamesV2.zip",
        "description": "Alternate names for all features (~15M names, ~400MB)",
    },
    "allCountries": {
        "url": f"{GEONAMES_BASE_URL}allCountries.zip",
        "description": "All GeoNames features (~12M records, ~1.5GB)",
    },
}

# GeoNames cities file columns (tab-separated)
CITIES_COLUMNS = [
    "geonameid",
    "name",
    "asciiname",
    "alternatenames",
    "latitude",
    "longitude",
    "feature_class",
    "feature_code",
    "country_code",
    "cc2",
    "admin1_code",
    "admin2_code",
    "admin3_code",
    "admin4_code",
    "population",
    "elevation",
    "dem",
    "timezone",
    "modification_date",
]

# Alternate names file columns
ALTERNATE_NAMES_COLUMNS = [
    "alternatenameid",
    "geonameid",
    "isolanguage",
    "alternate_name",
    "isPreferredName",
    "isShortName",
    "isColloquial",
    "isHistoric",
    "from",
    "to",
]

# Postal codes file columns
POSTAL_COLUMNS = [
    "country_code",
    "postal_code",
    "place_name",
    "admin1_name",
    "admin1_code",
    "admin2_name",
    "admin2_code",
    "admin3_name",
    "admin3_code",
    "latitude",
    "longitude",
    "accuracy",
]

# Default cache directory
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "odoo-data-flow" / "geonames"


# =============================================================================
# CACHE UTILITIES
# =============================================================================


def get_cache_dir() -> Path:
    """Get the GeoNames cache directory, creating it if needed.

    Returns:
        Path to cache directory (~/.cache/odoo-data-flow/geonames/)
    """
    cache_dir = DEFAULT_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _get_cached_file(dataset: str) -> Path | None:
    """Check if a dataset is already cached.

    Args:
        dataset: Dataset name (e.g., "cities15000")

    Returns:
        Path to cached file if exists, None otherwise.
    """
    cache_dir = get_cache_dir()

    # Check for extracted txt file
    txt_file = cache_dir / f"{dataset}.txt"
    if txt_file.exists():
        return txt_file

    return None


# =============================================================================
# DOWNLOAD UTILITIES
# =============================================================================


def download_dataset(
    dataset: str = "cities15000",
    cache_dir: Path | None = None,
    force: bool = False,
) -> Path:
    """Download and extract a GeoNames dataset.

    Args:
        dataset: Dataset name. One of: cities500, cities1000, cities5000,
                 cities15000, alternateNamesV2, allCountries
        cache_dir: Directory to cache files.
                   Defaults to ~/.cache/odoo-data-flow/geonames/
        force: Force re-download even if cached.

    Returns:
        Path to the extracted txt file.

    Raises:
        ValueError: If dataset is not recognized.
        httpx.HTTPError: If download fails.
    """
    import httpx

    if dataset not in DATASETS:
        available = ", ".join(DATASETS.keys())
        msg = f"Unknown dataset '{dataset}'. Available: {available}"
        raise ValueError(msg)

    cache_dir = cache_dir or get_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    txt_file = cache_dir / f"{dataset}.txt"

    # Return cached file if exists and not forcing
    if txt_file.exists() and not force:
        return txt_file

    # Download
    url = DATASETS[dataset]["url"]
    zip_file = cache_dir / f"{dataset}.zip"

    with httpx.Client(follow_redirects=True, timeout=300.0) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with open(zip_file, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=8192):
                    f.write(chunk)

    # Extract
    with zipfile.ZipFile(zip_file, "r") as zf:
        # Find the main txt file in the archive
        txt_names = [n for n in zf.namelist() if n.endswith(".txt")]
        if txt_names:
            # Extract and rename to consistent name
            zf.extract(txt_names[0], cache_dir)
            extracted = cache_dir / txt_names[0]
            if extracted != txt_file:
                extracted.rename(txt_file)

    # Clean up zip file
    zip_file.unlink(missing_ok=True)

    return txt_file


# =============================================================================
# DATA LOADING
# =============================================================================


def load_cities(
    dataset: str = "cities15000",
    min_population: int = 0,
    cache_dir: Path | None = None,
) -> pl.DataFrame:
    """Load cities data as a Polars DataFrame.

    Downloads and caches the dataset on first use.

    Args:
        dataset: Dataset name (cities500, cities1000, cities5000, cities15000).
        min_population: Filter cities with population >= this value.
        cache_dir: Custom cache directory.

    Returns:
        Polars DataFrame with columns:
        - geonameid: GeoNames ID
        - name: City name (UTF-8)
        - asciiname: ASCII-only name
        - alternatenames: Comma-separated alternate names
        - latitude, longitude: Coordinates
        - country_code: ISO 2-letter country code
        - population: Population count
        - timezone: Timezone string
        - And more...

    Example::

        df = load_cities(min_population=100000)
        df.filter(pl.col("country_code") == "NL").select("name", "population")
    """
    # Ensure data is downloaded
    txt_file = _get_cached_file(dataset)
    if txt_file is None:
        txt_file = download_dataset(dataset, cache_dir)

    # Read with Polars (fast!)
    df = pl.read_csv(
        txt_file,
        separator="\t",
        has_header=False,
        new_columns=CITIES_COLUMNS,
        schema_overrides={
            "geonameid": pl.Int64,
            "latitude": pl.Float64,
            "longitude": pl.Float64,
            "population": pl.Int64,
            "elevation": pl.Int32,
            "dem": pl.Int32,
        },
        null_values=[""],
    )

    # Filter by population
    if min_population > 0:
        df = df.filter(pl.col("population") >= min_population)

    return df


def load_alternate_names(
    cache_dir: Path | None = None,
    languages: list[str] | None = None,
) -> pl.DataFrame:
    """Load alternate names data as a Polars DataFrame.

    This is a large dataset (~15M rows). Consider filtering by language.

    Args:
        cache_dir: Custom cache directory.
        languages: Filter to specific language codes (e.g., ["en", "nl", "de"]).
                   Use "" for names without language code.

    Returns:
        Polars DataFrame with alternate names linked to geonameid.
    """
    txt_file = _get_cached_file("alternateNamesV2")
    if txt_file is None:
        txt_file = download_dataset("alternateNamesV2", cache_dir)

    df = pl.read_csv(
        txt_file,
        separator="\t",
        has_header=False,
        new_columns=ALTERNATE_NAMES_COLUMNS,
        schema_overrides={
            "alternatenameid": pl.Int64,
            "geonameid": pl.Int64,
            "isPreferredName": pl.Int8,
            "isShortName": pl.Int8,
            "isColloquial": pl.Int8,
            "isHistoric": pl.Int8,
        },
        null_values=[""],
    )

    if languages:
        df = df.filter(pl.col("isolanguage").is_in(languages))

    return df


def load_postal_codes(
    country: str | None = None,
    cache_dir: Path | None = None,
) -> pl.DataFrame:
    """Load postal codes data as a Polars DataFrame.

    Postal code data must be downloaded per country from:
    https://download.geonames.org/export/zip/{country_code}.zip

    Args:
        country: ISO 2-letter country code (e.g., "NL", "BE").
        cache_dir: Custom cache directory.

    Returns:
        Polars DataFrame with postal code data including coordinates.
    """
    import httpx

    cache_dir = cache_dir or get_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    if country:
        country = country.upper()
        txt_file = cache_dir / f"postal_{country}.txt"

        if not txt_file.exists():
            # Download country-specific postal data
            url = f"https://download.geonames.org/export/zip/{country}.zip"
            zip_file = cache_dir / f"postal_{country}.zip"

            with httpx.Client(follow_redirects=True, timeout=60.0) as client:
                response = client.get(url)
                response.raise_for_status()
                zip_file.write_bytes(response.content)

            # Extract
            with zipfile.ZipFile(zip_file, "r") as zf:
                txt_names = [n for n in zf.namelist() if n.endswith(".txt")]
                if txt_names:
                    zf.extract(txt_names[0], cache_dir)
                    extracted = cache_dir / txt_names[0]
                    if extracted != txt_file:
                        extracted.rename(txt_file)

            zip_file.unlink(missing_ok=True)
    else:
        msg = "Country code is required for postal code data"
        raise ValueError(msg)

    df = pl.read_csv(
        txt_file,
        separator="\t",
        has_header=False,
        new_columns=POSTAL_COLUMNS,
        schema_overrides={
            "latitude": pl.Float64,
            "longitude": pl.Float64,
            "accuracy": pl.Int8,
        },
        null_values=[""],
    )

    return df


# =============================================================================
# LOOKUP BUILDERS
# =============================================================================


def get_cities_lookup(
    dataset: str = "cities15000",
    min_population: int = 0,
    include_alternates: bool = True,
    cache_dir: Path | None = None,
) -> dict[str, str]:
    """Build a city name to country code lookup dictionary.

    This is the main function for use with `clean.detect_country()`.

    Args:
        dataset: Dataset name (cities500, cities1000, cities5000, cities15000).
        min_population: Filter cities with population >= this value.
        include_alternates: Include alternate names from the alternatenames column.
        cache_dir: Custom cache directory.

    Returns:
        Dict mapping lowercase city names to ISO country codes.
        Includes primary names, ASCII names, and optionally alternate names.

    Example::

        cities = get_cities_lookup()
        cities["amsterdam"]      # Returns: 'NL'
        cities["den haag"]       # Alternate name -> 'NL'
        cities["the hague"]      # English alternate -> 'NL'
    """
    df = load_cities(dataset, min_population, cache_dir)

    cities: dict[str, str] = {}

    # Process each row
    for row in df.iter_rows(named=True):
        country = row["country_code"]
        if not country:
            continue

        # Primary name
        name = row["name"]
        if name:
            cities[name.lower()] = country

        # ASCII name
        asciiname = row["asciiname"]
        if asciiname and asciiname != name:
            cities[asciiname.lower()] = country

        # Alternate names (comma-separated in the data)
        if include_alternates:
            alternates = row["alternatenames"]
            if alternates:
                for alt in alternates.split(","):
                    alt = alt.strip()
                    if alt:
                        # Don't overwrite primary names with alternates
                        alt_lower = alt.lower()
                        if alt_lower not in cities:
                            cities[alt_lower] = country

    return cities


def get_postal_lookup(
    countries: list[str],
    cache_dir: Path | None = None,
) -> dict[str, dict[str, str]]:
    """Build a postal code lookup dictionary for multiple countries.

    Args:
        countries: List of ISO 2-letter country codes.
        cache_dir: Custom cache directory.

    Returns:
        Dict mapping country codes to dicts of postal_code -> place_name.

    Example::

        lookup = get_postal_lookup(["NL", "BE"])
        lookup["NL"]["1012AB"]  # Returns: 'Amsterdam'
    """
    result: dict[str, dict[str, str]] = {}

    for country in countries:
        country = country.upper()
        df = load_postal_codes(country, cache_dir)

        # Build lookup: postal_code -> place_name
        postal_dict: dict[str, str] = {}
        for row in df.iter_rows(named=True):
            postal = row["postal_code"]
            place = row["place_name"]
            if postal and place:
                # Normalize postal code (remove spaces)
                postal_norm = postal.replace(" ", "").upper()
                postal_dict[postal_norm] = place

        result[country] = postal_dict

    return result


def get_city_coordinates(
    city: str,
    country: str | None = None,
    dataset: str = "cities15000",
    cache_dir: Path | None = None,
) -> tuple[float, float] | None:
    """Get latitude/longitude for a city.

    Args:
        city: City name (case-insensitive).
        country: Optional ISO country code to disambiguate.
        dataset: Dataset to search.
        cache_dir: Custom cache directory.

    Returns:
        Tuple of (latitude, longitude) or None if not found.

    Example::

        get_city_coordinates("Amsterdam")
        # Returns: (52.37403, 4.88969)

        get_city_coordinates("Paris", "FR")
        # Returns: (48.85341, 2.3488)
    """
    df = load_cities(dataset, cache_dir=cache_dir)

    # Filter by name (case-insensitive)
    city_lower = city.lower()
    matches = df.filter(
        (pl.col("name").str.to_lowercase() == city_lower)
        | (pl.col("asciiname").str.to_lowercase() == city_lower)
    )

    # Filter by country if specified
    if country:
        matches = matches.filter(pl.col("country_code") == country.upper())

    if matches.is_empty():
        return None

    # Return coordinates of largest city (by population) if multiple matches
    row = matches.sort("population", descending=True).row(0, named=True)
    return (row["latitude"], row["longitude"])
