"""Tests for the geonames module."""

import tempfile
import zipfile
from pathlib import Path
from unittest import mock

import polars as pl
import pytest

from odoo_data_flow.lib import geonames


class TestConstants:
    """Tests for geonames constants."""

    def test_datasets_available(self) -> None:
        """Test that DATASETS constant contains expected datasets."""
        assert "cities15000" in geonames.DATASETS
        assert "cities5000" in geonames.DATASETS
        assert "cities1000" in geonames.DATASETS
        assert "cities500" in geonames.DATASETS
        assert "alternateNamesV2" in geonames.DATASETS

    def test_datasets_have_urls(self) -> None:
        """Test that each dataset has a URL."""
        for name, info in geonames.DATASETS.items():
            assert "url" in info, f"Dataset {name} missing url"
            assert info["url"].startswith("https://"), f"Dataset {name} has invalid url"


class TestCacheDir:
    """Tests for cache directory handling."""

    def test_get_cache_dir_creates_directory(self) -> None:
        """Test that get_cache_dir creates the directory."""
        with mock.patch.object(Path, "mkdir") as mock_mkdir:
            geonames.get_cache_dir()
            mock_mkdir.assert_called_once_with(parents=True, exist_ok=True)

    def test_get_cache_dir_returns_path(self) -> None:
        """Test that get_cache_dir returns a Path."""
        result = geonames.get_cache_dir()
        assert isinstance(result, Path)
        assert "geonames" in str(result)


class TestLoadCities:
    """Tests for loading cities data."""

    @pytest.fixture
    def sample_cities_file(self, tmp_path: Path) -> Path:
        """Create a sample cities file for testing."""
        content = (
            "2759794\tAmsterdam\tAmsterdam\tAmsterdam,Амстердам\t52.37403\t4.88969\t"
            "P\tPPLA\tNL\t\t07\t\t\t\t872680\t-2\t13\tEurope/Amsterdam\t2023-01-01\n"
            "2968815\tParis\tParis\tParis,Parigi,Париж\t48.85341\t2.3488\t"
            "P\tPPLC\tFR\t\t11\t75\t751\t75056\t2102650\t\t42\tEurope/Paris\t2023-01-01\n"
            "2643743\tLondon\tLondon\tLondon,Londra,Лондон\t51.50853\t-0.12574\t"
            "P\tPPLC\tGB\t\tENG\t\t\t\t8961989\t\t25\tEurope/London\t2023-01-01\n"
        )
        cities_file = tmp_path / "cities15000.txt"
        cities_file.write_text(content)
        return cities_file

    def test_load_cities_returns_dataframe(self, sample_cities_file: Path) -> None:
        """Test that load_cities returns a DataFrame."""
        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_cities_file
        ):
            df = geonames.load_cities()
            assert isinstance(df, pl.DataFrame)
            assert len(df) == 3

    def test_load_cities_has_expected_columns(self, sample_cities_file: Path) -> None:
        """Test that DataFrame has expected columns."""
        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_cities_file
        ):
            df = geonames.load_cities()
            assert "name" in df.columns
            assert "country_code" in df.columns
            assert "latitude" in df.columns
            assert "longitude" in df.columns
            assert "population" in df.columns

    def test_load_cities_min_population_filter(
        self, sample_cities_file: Path
    ) -> None:
        """Test population filtering."""
        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_cities_file
        ):
            df = geonames.load_cities(min_population=1000000)
            assert len(df) == 2  # Only Paris and London have pop > 1M

    def test_load_cities_data_types(self, sample_cities_file: Path) -> None:
        """Test that columns have correct data types."""
        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_cities_file
        ):
            df = geonames.load_cities()
            assert df["latitude"].dtype == pl.Float64
            assert df["longitude"].dtype == pl.Float64
            assert df["population"].dtype == pl.Int64


class TestGetCitiesLookup:
    """Tests for building city lookup dictionary."""

    @pytest.fixture
    def sample_cities_file(self, tmp_path: Path) -> Path:
        """Create a sample cities file for testing."""
        content = (
            "2759794\tAmsterdam\tAmsterdam\tAmsterdam,Mokum,'s-Gravenhage\t52.37403\t4.88969\t"
            "P\tPPLA\tNL\t\t07\t\t\t\t872680\t-2\t13\tEurope/Amsterdam\t2023-01-01\n"
            "2747373\tThe Hague\tThe Hague\tDen Haag,'s-Gravenhage,La Haye\t52.07667\t4.29861\t"
            "P\tPPLC\tNL\t\t11\t\t\t\t514861\t\t5\tEurope/Amsterdam\t2023-01-01\n"
            "2968815\tParis\tParis\tParis,Parigi\t48.85341\t2.3488\t"
            "P\tPPLC\tFR\t\t11\t75\t751\t75056\t2102650\t\t42\tEurope/Paris\t2023-01-01\n"
        )
        cities_file = tmp_path / "cities15000.txt"
        cities_file.write_text(content)
        return cities_file

    def test_get_cities_lookup_returns_dict(self, sample_cities_file: Path) -> None:
        """Test that get_cities_lookup returns a dictionary."""
        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_cities_file
        ):
            cities = geonames.get_cities_lookup()
            assert isinstance(cities, dict)

    def test_get_cities_lookup_lowercase_keys(self, sample_cities_file: Path) -> None:
        """Test that keys are lowercase."""
        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_cities_file
        ):
            cities = geonames.get_cities_lookup()
            assert "amsterdam" in cities
            assert "Amsterdam" not in cities

    def test_get_cities_lookup_includes_alternates(
        self, sample_cities_file: Path
    ) -> None:
        """Test that alternate names are included."""
        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_cities_file
        ):
            cities = geonames.get_cities_lookup(include_alternates=True)
            # Primary names
            assert cities["amsterdam"] == "NL"
            assert cities["paris"] == "FR"
            # Alternate names
            assert cities["den haag"] == "NL"
            assert cities["la haye"] == "NL"
            assert cities["parigi"] == "FR"

    def test_get_cities_lookup_without_alternates(
        self, sample_cities_file: Path
    ) -> None:
        """Test that alternate names can be excluded."""
        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_cities_file
        ):
            cities = geonames.get_cities_lookup(include_alternates=False)
            assert cities["amsterdam"] == "NL"
            # These should not be present
            assert "mokum" not in cities


class TestDownloadDataset:
    """Tests for downloading datasets."""

    def test_download_dataset_invalid_name(self) -> None:
        """Test that invalid dataset name raises error."""
        with pytest.raises(ValueError, match="Unknown dataset"):
            geonames.download_dataset("invalid_dataset")

    def test_download_dataset_uses_cache(self, tmp_path: Path) -> None:
        """Test that cached files are reused."""
        # Create a cached file
        cached_file = tmp_path / "cities15000.txt"
        cached_file.write_text("cached content")

        with mock.patch.object(geonames, "get_cache_dir", return_value=tmp_path):
            result = geonames.download_dataset("cities15000")
            assert result == cached_file

    def test_download_dataset_force_redownload(self, tmp_path: Path) -> None:
        """Test that force=True re-downloads."""
        cached_file = tmp_path / "cities15000.txt"
        cached_file.write_text("old content")

        # Create a mock zip file with new content
        zip_content = b"PK..."  # Minimal zip header

        with (
            mock.patch.object(geonames, "get_cache_dir", return_value=tmp_path),
            mock.patch("httpx.Client") as mock_client,
        ):
            # Setup mock response
            mock_response = mock.MagicMock()
            mock_response.iter_bytes.return_value = [zip_content]
            mock_client.return_value.__enter__.return_value.stream.return_value.__enter__.return_value = (
                mock_response
            )

            # Should attempt to download even though cached
            with pytest.raises(zipfile.BadZipFile):
                # Will fail because our mock zip is invalid, but proves download attempted
                geonames.download_dataset("cities15000", force=True)


class TestLoadPostalCodes:
    """Tests for loading postal code data."""

    def test_load_postal_codes_requires_country(self) -> None:
        """Test that country parameter is required."""
        with pytest.raises(ValueError, match="Country code is required"):
            geonames.load_postal_codes()

    @pytest.fixture
    def sample_postal_file(self, tmp_path: Path) -> Path:
        """Create a sample postal codes file."""
        content = (
            "NL\t1012\tAmsterdam\tNoord-Holland\tNH\t\t\t\t\t52.3731\t4.8932\t4\n"
            "NL\t1013\tAmsterdam\tNoord-Holland\tNH\t\t\t\t\t52.3880\t4.8770\t4\n"
            "NL\t3011\tRotterdam\tZuid-Holland\tZH\t\t\t\t\t51.9225\t4.4792\t4\n"
        )
        postal_file = tmp_path / "postal_NL.txt"
        postal_file.write_text(content)
        return postal_file

    def test_load_postal_codes_returns_dataframe(
        self, sample_postal_file: Path, tmp_path: Path
    ) -> None:
        """Test that load_postal_codes returns a DataFrame."""
        with mock.patch.object(geonames, "get_cache_dir", return_value=tmp_path):
            # File already exists, so no download needed
            df = geonames.load_postal_codes("NL", cache_dir=tmp_path)
            assert isinstance(df, pl.DataFrame)
            assert len(df) == 3


class TestGetPostalLookup:
    """Tests for building postal code lookup."""

    @pytest.fixture
    def sample_postal_file(self, tmp_path: Path) -> Path:
        """Create sample postal files."""
        nl_content = (
            "NL\t1012 AB\tAmsterdam\tNoord-Holland\tNH\t\t\t\t\t52.3731\t4.8932\t4\n"
            "NL\t3011 AA\tRotterdam\tZuid-Holland\tZH\t\t\t\t\t51.9225\t4.4792\t4\n"
        )
        nl_file = tmp_path / "postal_NL.txt"
        nl_file.write_text(nl_content)
        return nl_file

    def test_get_postal_lookup_normalizes_codes(
        self, sample_postal_file: Path, tmp_path: Path
    ) -> None:
        """Test that postal codes are normalized (no spaces, uppercase)."""
        with mock.patch.object(geonames, "get_cache_dir", return_value=tmp_path):
            lookup = geonames.get_postal_lookup(["NL"], cache_dir=tmp_path)
            assert "1012AB" in lookup["NL"]  # Space removed, uppercase
            assert lookup["NL"]["1012AB"] == "Amsterdam"


class TestGetCityCoordinates:
    """Tests for getting city coordinates."""

    @pytest.fixture
    def sample_cities_file(self, tmp_path: Path) -> Path:
        """Create a sample cities file."""
        content = (
            "2759794\tAmsterdam\tAmsterdam\t\t52.37403\t4.88969\t"
            "P\tPPLA\tNL\t\t07\t\t\t\t872680\t-2\t13\tEurope/Amsterdam\t2023-01-01\n"
            "2968815\tParis\tParis\t\t48.85341\t2.3488\t"
            "P\tPPLC\tFR\t\t11\t75\t751\t75056\t2102650\t\t42\tEurope/Paris\t2023-01-01\n"
        )
        cities_file = tmp_path / "cities15000.txt"
        cities_file.write_text(content)
        return cities_file

    def test_get_city_coordinates_found(self, sample_cities_file: Path) -> None:
        """Test getting coordinates for a city."""
        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_cities_file
        ):
            coords = geonames.get_city_coordinates("Amsterdam")
            assert coords is not None
            lat, lon = coords
            assert abs(lat - 52.37403) < 0.001
            assert abs(lon - 4.88969) < 0.001

    def test_get_city_coordinates_not_found(self, sample_cities_file: Path) -> None:
        """Test that None is returned for unknown city."""
        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_cities_file
        ):
            coords = geonames.get_city_coordinates("UnknownCity")
            assert coords is None

    def test_get_city_coordinates_with_country(self, sample_cities_file: Path) -> None:
        """Test filtering by country."""
        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_cities_file
        ):
            coords = geonames.get_city_coordinates("Paris", country="FR")
            assert coords is not None

            # Wrong country should return None
            coords = geonames.get_city_coordinates("Paris", country="NL")
            assert coords is None


class TestIntegrationWithClean:
    """Tests for integration with clean.detect_country."""

    @pytest.fixture
    def sample_cities_file(self, tmp_path: Path) -> Path:
        """Create a sample cities file."""
        content = (
            "2759794\tAmsterdam\tAmsterdam\tAmsterdam,Mokum\t52.37403\t4.88969\t"
            "P\tPPLA\tNL\t\t07\t\t\t\t872680\t-2\t13\tEurope/Amsterdam\t2023-01-01\n"
            "2968815\tParis\tParis\tParigi\t48.85341\t2.3488\t"
            "P\tPPLC\tFR\t\t11\t75\t751\t75056\t2102650\t\t42\tEurope/Paris\t2023-01-01\n"
        )
        cities_file = tmp_path / "cities15000.txt"
        cities_file.write_text(content)
        return cities_file

    def test_cities_lookup_with_detect_country(
        self, sample_cities_file: Path
    ) -> None:
        """Test using geonames lookup with clean.detect_country."""
        from odoo_data_flow.lib import clean

        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_cities_file
        ):
            cities = geonames.get_cities_lookup()

            assert clean.detect_country(city="Amsterdam", cities=cities) == "NL"
            assert clean.detect_country(city="Paris", cities=cities) == "FR"
            assert clean.detect_country(city="Mokum", cities=cities) == "NL"
