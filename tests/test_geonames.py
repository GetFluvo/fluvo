"""Tests for the geonames module."""

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
        cities_file.write_text(content, encoding="utf-8")
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

    def test_load_cities_min_population_filter(self, sample_cities_file: Path) -> None:
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
        # GeoNames TSV format - lines are intentionally long
        content = (
            "2759794\tAmsterdam\tAmsterdam\t"
            "Amsterdam,Mokum,'s-Gravenhage\t52.37403\t4.88969\t"
            "P\tPPLA\tNL\t\t07\t\t\t\t872680\t-2\t13\tEurope/Amsterdam\t2023-01-01\n"
            "2747373\tThe Hague\tThe Hague\t"
            "Den Haag,'s-Gravenhage,La Haye\t52.07667\t4.29861\t"
            "P\tPPLC\tNL\t\t11\t\t\t\t514861\t\t5\tEurope/Amsterdam\t2023-01-01\n"
            "2968815\tParis\tParis\tParis,Parigi\t48.85341\t2.3488\t"
            "P\tPPLC\tFR\t\t11\t75\t751\t75056\t2102650\t\t42\tEurope/Paris\t2023-01-01\n"
        )
        cities_file = tmp_path / "cities15000.txt"
        cities_file.write_text(content, encoding="utf-8")
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
        cached_file.write_text("cached content", encoding="utf-8")

        with mock.patch.object(geonames, "get_cache_dir", return_value=tmp_path):
            result = geonames.download_dataset("cities15000")
            assert result == cached_file

    def test_download_dataset_force_redownload(self, tmp_path: Path) -> None:
        """Test that force=True re-downloads."""
        cached_file = tmp_path / "cities15000.txt"
        cached_file.write_text("old content", encoding="utf-8")

        # Create a mock zip file with new content
        zip_content = b"PK..."  # Minimal zip header

        with (
            mock.patch.object(geonames, "get_cache_dir", return_value=tmp_path),
            mock.patch("httpx.Client") as mock_client,
        ):
            # Setup mock response
            mock_response = mock.MagicMock()
            mock_response.iter_bytes.return_value = [zip_content]
            client_enter = mock_client.return_value.__enter__.return_value
            client_enter.stream.return_value.__enter__.return_value = mock_response

            # Should attempt to download even though cached
            with pytest.raises(zipfile.BadZipFile):
                # Fails because mock zip is invalid, but proves download attempted
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
        postal_file.write_text(content, encoding="utf-8")
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
        nl_file.write_text(nl_content, encoding="utf-8")
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
        cities_file.write_text(content, encoding="utf-8")
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
        cities_file.write_text(content, encoding="utf-8")
        return cities_file

    def test_cities_lookup_with_detect_country(self, sample_cities_file: Path) -> None:
        """Test using geonames lookup with clean.detect_country."""
        from odoo_data_flow.lib import clean

        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_cities_file
        ):
            cities = geonames.get_cities_lookup()

            assert clean.detect_country(city="Amsterdam", cities=cities) == "NL"
            assert clean.detect_country(city="Paris", cities=cities) == "FR"
            assert clean.detect_country(city="Mokum", cities=cities) == "NL"


class TestGetCachedFile:
    """Tests for _get_cached_file function."""

    def test_returns_path_when_file_exists(self, tmp_path: Path) -> None:
        """Test that _get_cached_file returns path when txt file exists."""
        # Create cached txt file
        txt_file = tmp_path / "cities15000.txt"
        txt_file.write_text("cached content", encoding="utf-8")

        with mock.patch.object(geonames, "get_cache_dir", return_value=tmp_path):
            result = geonames._get_cached_file("cities15000")
            assert result == txt_file

    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        """Test that _get_cached_file returns None when file doesn't exist."""
        with mock.patch.object(geonames, "get_cache_dir", return_value=tmp_path):
            result = geonames._get_cached_file("cities15000")
            assert result is None


class TestLoadCitiesDownload:
    """Tests for load_cities when download is needed."""

    def test_load_cities_triggers_download(self, tmp_path: Path) -> None:
        """Test that load_cities triggers download when cache is empty."""
        # Create a cities file to be "downloaded"
        cities_content = (
            "2759794\tAmsterdam\tAmsterdam\t\t52.37403\t4.88969\t"
            "P\tPPLA\tNL\t\t07\t\t\t\t872680\t-2\t13\tEurope/Amsterdam\t2023-01-01\n"
        )
        cities_file = tmp_path / "cities15000.txt"

        def mock_download(dataset: str, cache_dir: Path | None = None) -> Path:
            cities_file.write_text(cities_content, encoding="utf-8")
            return cities_file

        with (
            mock.patch.object(geonames, "_get_cached_file", return_value=None),
            mock.patch.object(geonames, "download_dataset", side_effect=mock_download),
        ):
            df = geonames.load_cities()
            assert len(df) == 1
            assert df["name"][0] == "Amsterdam"


class TestLoadAlternateNames:
    """Tests for load_alternate_names function."""

    @pytest.fixture
    def sample_alternate_names_file(self, tmp_path: Path) -> Path:
        """Create a sample alternate names file."""
        content = (
            "1\t2759794\ten\tAmsterdam\t1\t0\t0\t0\t\t\n"
            "2\t2759794\tnl\tMokum\t0\t1\t0\t0\t\t\n"
            "3\t2968815\tfr\tParis\t1\t0\t0\t0\t\t\n"
            "4\t2968815\tit\tParigi\t0\t0\t0\t0\t\t\n"
        )
        alt_file = tmp_path / "alternateNamesV2.txt"
        alt_file.write_text(content, encoding="utf-8")
        return alt_file

    def test_load_alternate_names_returns_dataframe(
        self, sample_alternate_names_file: Path
    ) -> None:
        """Test that load_alternate_names returns a DataFrame."""
        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_alternate_names_file
        ):
            df = geonames.load_alternate_names()
            assert isinstance(df, pl.DataFrame)
            assert len(df) == 4

    def test_load_alternate_names_filter_languages(
        self, sample_alternate_names_file: Path
    ) -> None:
        """Test filtering by language."""
        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_alternate_names_file
        ):
            df = geonames.load_alternate_names(languages=["en", "nl"])
            assert len(df) == 2
            assert set(df["isolanguage"].to_list()) == {"en", "nl"}

    def test_load_alternate_names_triggers_download(self, tmp_path: Path) -> None:
        """Test that load_alternate_names triggers download when cache is empty."""
        alt_content = "1\t2759794\ten\tAmsterdam\t1\t0\t0\t0\t\t\n"
        alt_file = tmp_path / "alternateNamesV2.txt"

        def mock_download(dataset: str, cache_dir: Path | None = None) -> Path:
            alt_file.write_text(alt_content, encoding="utf-8")
            return alt_file

        with (
            mock.patch.object(geonames, "_get_cached_file", return_value=None),
            mock.patch.object(geonames, "download_dataset", side_effect=mock_download),
        ):
            df = geonames.load_alternate_names()
            assert len(df) == 1


class TestLoadPostalCodesDownload:
    """Tests for load_postal_codes download functionality."""

    def test_load_postal_codes_downloads_when_not_cached(self, tmp_path: Path) -> None:
        """Test that postal codes are downloaded when not cached."""
        postal_content = (
            "NL\t1012\tAmsterdam\tNoord-Holland\tNH\t\t\t\t\t52.3731\t4.8932\t4\n"
        )

        # Create a valid zip file
        zip_path = tmp_path / "postal_NL.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("NL.txt", postal_content)

        mock_response = mock.MagicMock()
        mock_response.content = zip_path.read_bytes()

        with (
            mock.patch.object(geonames, "get_cache_dir", return_value=tmp_path),
            mock.patch("httpx.Client") as mock_client,
        ):
            client_ctx = mock_client.return_value.__enter__.return_value
            client_ctx.get.return_value = mock_response

            df = geonames.load_postal_codes("NL", cache_dir=tmp_path)
            assert isinstance(df, pl.DataFrame)
            assert len(df) == 1


class TestGetCitiesLookupEdgeCases:
    """Tests for edge cases in get_cities_lookup."""

    @pytest.fixture
    def sample_cities_with_edge_cases(self, tmp_path: Path) -> Path:
        """Create a cities file with edge cases."""
        content = (
            # City with no country code (should be skipped)
            "1\tUnknownCity\tUnknownCity\t\t0.0\t0.0\t"
            "P\tPPLA\t\t\t\t\t\t\t1000\t\t\t\t2023-01-01\n"
            # City with no name (should be handled)
            "2\t\t\t\t52.0\t4.0\t"
            "P\tPPLA\tNL\t\t\t\t\t\t1000\t\t\t\t2023-01-01\n"
            # City with no alternatenames
            "3\tRotterdam\tRotterdam\t\t51.9\t4.5\t"
            "P\tPPLA\tNL\t\t\t\t\t\t600000\t\t\t\t2023-01-01\n"
            # City with same asciiname as name
            "4\tUtrecht\tUtrecht\tUtrecht City\t52.1\t5.1\t"
            "P\tPPLA\tNL\t\t\t\t\t\t350000\t\t\t\t2023-01-01\n"
            # City with empty alternate name in list
            "5\tEindhoven\tEindhoven\tEindhoven,,Lamp City\t51.4\t5.5\t"
            "P\tPPLA\tNL\t\t\t\t\t\t230000\t\t\t\t2023-01-01\n"
        )
        cities_file = tmp_path / "cities15000.txt"
        cities_file.write_text(content, encoding="utf-8")
        return cities_file

    def test_skips_cities_without_country(
        self, sample_cities_with_edge_cases: Path
    ) -> None:
        """Test that cities without country codes are skipped."""
        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_cities_with_edge_cases
        ):
            cities = geonames.get_cities_lookup()
            assert "unknowncity" not in cities

    def test_handles_empty_city_name(
        self, sample_cities_with_edge_cases: Path
    ) -> None:
        """Test that empty city names are handled gracefully."""
        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_cities_with_edge_cases
        ):
            cities = geonames.get_cities_lookup()
            # Should not raise and should have valid entries
            assert "rotterdam" in cities

    def test_handles_same_asciiname_as_name(
        self, sample_cities_with_edge_cases: Path
    ) -> None:
        """Test that duplicate asciiname=name is handled."""
        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_cities_with_edge_cases
        ):
            cities = geonames.get_cities_lookup()
            assert cities["utrecht"] == "NL"

    def test_handles_empty_alternate_names(
        self, sample_cities_with_edge_cases: Path
    ) -> None:
        """Test that empty alternate names in list are skipped."""
        with mock.patch.object(
            geonames, "_get_cached_file", return_value=sample_cities_with_edge_cases
        ):
            cities = geonames.get_cities_lookup()
            assert cities["eindhoven"] == "NL"
            assert cities["lamp city"] == "NL"
            # Empty string should not be a key
            assert "" not in cities


class TestGetPostalLookupMultipleCountries:
    """Tests for get_postal_lookup with multiple countries."""

    def test_get_postal_lookup_multiple_countries(self, tmp_path: Path) -> None:
        """Test building postal lookup for multiple countries."""
        # Create postal files for NL and BE (all 12 columns as per POSTAL_COLUMNS)
        nl_content = "NL\t1012 AB\tAmsterdam\tNoord-Holland\tNH\t\t\t\t\t52.37\t4.89\t4\n"
        be_content = "BE\tB-1000\tBrussels\tBrussels-Capital\tBRU\t\t\t\t\t50.85\t4.35\t4\n"

        (tmp_path / "postal_NL.txt").write_text(nl_content, encoding="utf-8")
        (tmp_path / "postal_BE.txt").write_text(be_content, encoding="utf-8")

        with mock.patch.object(geonames, "get_cache_dir", return_value=tmp_path):
            lookup = geonames.get_postal_lookup(["NL", "BE"], cache_dir=tmp_path)

            assert "NL" in lookup
            assert "BE" in lookup
            assert "1012AB" in lookup["NL"]
            assert "B-1000" in lookup["BE"]


class TestDownloadDatasetExtraction:
    """Tests for download_dataset zip extraction logic."""

    def test_download_extracts_and_renames_file(self, tmp_path: Path) -> None:
        """Test that download extracts txt file and renames it correctly."""
        cities_content = (
            "2759794\tAmsterdam\tAmsterdam\t\t52.37403\t4.88969\t"
            "P\tPPLA\tNL\t\t07\t\t\t\t872680\t-2\t13\tEurope/Amsterdam\t2023-01-01\n"
        )

        # Create a valid zip with a differently named txt file
        zip_content_path = tmp_path / "temp_cities.zip"
        with zipfile.ZipFile(zip_content_path, "w") as zf:
            zf.writestr("cities15000.txt", cities_content)

        mock_response = mock.MagicMock()
        mock_response.iter_bytes.return_value = [zip_content_path.read_bytes()]

        with (
            mock.patch.object(geonames, "get_cache_dir", return_value=tmp_path),
            mock.patch("httpx.Client") as mock_client,
        ):
            client_ctx = mock_client.return_value.__enter__.return_value
            client_ctx.stream.return_value.__enter__.return_value = mock_response

            result = geonames.download_dataset("cities15000", force=True)

            assert result.exists()
            assert result.name == "cities15000.txt"
