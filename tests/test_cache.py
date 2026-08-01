"""Tests for the caching logic."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import polars as pl
from _pytest.monkeypatch import MonkeyPatch
from polars.testing import assert_frame_equal

from fluvo.lib import cache


@patch("configparser.ConfigParser")
def test_get_cache_dir_creates_unique_directory(
    mock_config_parser: MagicMock, tmp_path: Path
) -> None:
    """Verify that a unique, hashed directory is created."""
    # Arrange
    mock_instance = mock_config_parser.return_value
    mock_instance.get.side_effect = ["localhost", 8069, "test_db"]
    expected_hash = "a1b2c3d4e5f6..."  # A known hash for the test data
    with patch("hashlib.sha256") as mock_sha256:
        mock_sha256.return_value.hexdigest.return_value = expected_hash
        with patch.object(Path, "cwd", return_value=tmp_path):
            # Act
            cache_dir = cache.get_cache_dir("dummy.conf")

            # Assert
            assert cache_dir is not None
            assert cache_dir.name == expected_hash
            assert cache_dir.exists()


@patch("fluvo.lib.cache.get_cache_dir")
def test_save_and_load_id_map(mock_get_cache_dir: "MagicMock", tmp_path: Path) -> None:
    """Verify that an id_map can be saved and loaded correctly."""
    # Arrange
    mock_get_cache_dir.return_value = tmp_path
    model = "res.partner"
    id_map = {"partner_a": 101, "partner_b": 102}

    # Act
    cache.save_id_map("dummy.conf", model, id_map)
    loaded_df = cache.load_id_map("dummy.conf", model)

    # Assert
    assert loaded_df is not None
    expected_df = pl.DataFrame(
        {"external_id": ["partner_a", "partner_b"], "db_id": [101, 102]}
    )
    assert_frame_equal(loaded_df, expected_df)


def test_load_id_map_returns_none_if_not_found(tmp_path: Path) -> None:
    """Verify that loading a non-existent map returns None."""
    with patch("fluvo.lib.cache.get_cache_dir", return_value=tmp_path):
        loaded_df = cache.load_id_map("dummy.conf", "non.existent.model")
        assert loaded_df is None


@patch("configparser.ConfigParser")
def test_get_cache_dir_handles_exception(
    mock_config_parser: MagicMock, caplog: "MagicMock"
) -> None:
    """Verify that get_cache_dir handles exceptions gracefully."""
    mock_instance = mock_config_parser.return_value
    mock_instance.get.side_effect = Exception("Test exception")
    cache_dir = cache.get_cache_dir("dummy.conf")
    assert cache_dir is None
    # get_cache_dir now delegates to resolve_cache_dir; the fingerprint step
    # reports the failure.
    assert "Could not fingerprint connection config" in caplog.text


@patch("fluvo.lib.cache.get_cache_dir", return_value=None)
def test_save_id_map_handles_no_cache_dir(
    mock_get_cache_dir: MagicMock, caplog: "MagicMock"
) -> None:
    """Verify save_id_map handles no cache directory."""
    cache.save_id_map("dummy.conf", "res.partner", {"a": 1})
    assert "Saved id_map for model" not in caplog.text


def test_save_id_map_handles_empty_id_map(tmp_path: Path, caplog: "MagicMock") -> None:
    """Verify save_id_map handles an empty id_map."""
    with patch("fluvo.lib.cache.get_cache_dir", return_value=tmp_path):
        cache.save_id_map("dummy.conf", "res.partner", {})
        assert "Saved id_map for model" not in caplog.text


@patch("fluvo.lib.cache.get_cache_dir")
@patch("polars.DataFrame.write_parquet")
def test_save_id_map_handles_write_error(
    mock_write_parquet: MagicMock,
    mock_get_cache_dir: MagicMock,
    tmp_path: Path,
    caplog: "MagicMock",
) -> None:
    """Verify save_id_map handles write errors."""
    mock_get_cache_dir.return_value = tmp_path
    mock_write_parquet.side_effect = Exception("Write error")
    cache.save_id_map("dummy.conf", "res.partner", {"a": 1})
    assert "Failed to save id_map for model 'res.partner'" in caplog.text


@patch("fluvo.lib.cache.get_cache_dir", return_value=None)
def test_load_id_map_handles_no_cache_dir(mock_get_cache_dir: MagicMock) -> None:
    """Verify load_id_map handles no cache directory."""
    result = cache.load_id_map("dummy.conf", "res.partner")
    assert result is None


@patch("fluvo.lib.cache.get_cache_dir")
@patch("polars.read_parquet")
def test_load_id_map_handles_read_error(
    mock_read_parquet: MagicMock,
    mock_get_cache_dir: MagicMock,
    tmp_path: Path,
    caplog: "MagicMock",
) -> None:
    """Verify load_id_map handles read errors."""
    mock_get_cache_dir.return_value = tmp_path
    (tmp_path / "res.partner.id_map.parquet").touch()
    mock_read_parquet.side_effect = Exception("Read error")
    result = cache.load_id_map("dummy.conf", "res.partner")
    assert result is None
    assert "Failed to load id_map for model 'res.partner'" in caplog.text


@patch("fluvo.lib.cache.get_cache_dir")
def test_save_and_load_fields_get_cache(
    mock_get_cache_dir: MagicMock, tmp_path: Path
) -> None:
    """Verify that a fields_get result can be saved and loaded."""
    # Arrange
    mock_get_cache_dir.return_value = tmp_path
    model = "res.users"
    fields_data = {
        "name": {"type": "char", "string": "Name"},
        "email": {"type": "char", "string": "Email"},
    }

    # Act
    cache.save_fields_get_cache("dummy.conf", model, fields_data)
    loaded_data = cache.load_fields_get_cache("dummy.conf", model)

    # Assert
    assert loaded_data == fields_data


def test_load_fields_get_cache_returns_none_if_not_found(tmp_path: Path) -> None:
    """Verify that loading a non-existent fields_get cache returns None."""
    with patch("fluvo.lib.cache.get_cache_dir", return_value=tmp_path):
        loaded_data = cache.load_fields_get_cache("dummy.conf", "non.existent.model")
        assert loaded_data is None


@patch("fluvo.lib.cache.get_cache_dir", return_value=None)
def test_save_fields_get_cache_handles_no_cache_dir(
    mock_get_cache_dir: MagicMock, caplog: "MagicMock"
) -> None:
    """Verify save_fields_get_cache handles no cache directory."""
    cache.save_fields_get_cache("dummy.conf", "res.partner", {"field": "data"})
    assert "Saved fields_get cache for model" not in caplog.text


def test_save_fields_get_cache_handles_empty_data(
    tmp_path: Path, caplog: "MagicMock"
) -> None:
    """Verify save_fields_get_cache handles empty data."""
    with patch("fluvo.lib.cache.get_cache_dir", return_value=tmp_path):
        cache.save_fields_get_cache("dummy.conf", "res.partner", {})
        assert "Saved fields_get cache for model" not in caplog.text


@patch("fluvo.lib.cache.get_cache_dir")
@patch("json.dump")
def test_save_fields_get_cache_handles_write_error(
    mock_json_dump: MagicMock,
    mock_get_cache_dir: MagicMock,
    tmp_path: Path,
    caplog: "MagicMock",
) -> None:
    """Verify save_fields_get_cache handles write errors."""
    mock_get_cache_dir.return_value = tmp_path
    mock_json_dump.side_effect = Exception("Write error")
    cache.save_fields_get_cache("dummy.conf", "res.partner", {"field": "data"})
    assert "Failed to save fields_get cache for model 'res.partner'" in caplog.text


@patch("fluvo.lib.cache.get_cache_dir")
@patch("json.load")
def test_load_fields_get_cache_handles_read_error(
    mock_json_load: MagicMock,
    mock_get_cache_dir: MagicMock,
    tmp_path: Path,
    caplog: "MagicMock",
) -> None:
    """Verify load_fields_get_cache handles read errors."""
    mock_get_cache_dir.return_value = tmp_path
    (tmp_path / "res.partner.fields.json").touch()
    mock_json_load.side_effect = Exception("Read error")
    result = cache.load_fields_get_cache("dummy.conf", "res.partner")
    assert result is None
    assert "Failed to load fields_get cache for model 'res.partner'" in caplog.text


def test_generate_session_id_is_consistent() -> None:
    """Verify that the session ID is consistent for the same inputs."""
    # Arrange
    model = "res.partner"
    domain = [("is_company", "=", True), ("customer", "=", True)]
    fields = ["name", "email", "phone"]

    # Act
    session_id1 = cache.generate_session_id(model, domain, fields)
    session_id2 = cache.generate_session_id(model, domain, fields)

    # Assert
    assert session_id1 == session_id2


def test_generate_session_id_is_sensitive_to_model() -> None:
    """Verify that the session ID changes with the model."""
    # Arrange
    domain = [("is_company", "=", True)]
    fields = ["name"]

    # Act
    session_id1 = cache.generate_session_id("res.partner", domain, fields)
    session_id2 = cache.generate_session_id("res.users", domain, fields)

    # Assert
    assert session_id1 != session_id2


def test_generate_session_id_is_sensitive_to_domain() -> None:
    """Verify that the session ID changes with the domain."""
    # Arrange
    model = "res.partner"
    fields = ["name"]

    # Act
    session_id1 = cache.generate_session_id(model, [("is_company", "=", True)], fields)
    session_id2 = cache.generate_session_id(model, [("is_company", "=", False)], fields)

    # Assert
    assert session_id1 != session_id2


def test_generate_session_id_is_sensitive_to_fields() -> None:
    """Verify that the session ID changes with the fields."""
    # Arrange
    model = "res.partner"
    domain = [("is_company", "=", True)]

    # Act
    session_id1 = cache.generate_session_id(model, domain, ["name", "email"])
    session_id2 = cache.generate_session_id(model, domain, ["name", "phone"])

    # Assert
    assert session_id1 != session_id2


def test_generate_session_id_is_order_agnostic() -> None:
    """Verify that the session ID is not sensitive to the order of items."""
    # Arrange
    model = "res.partner"
    domain1 = [("is_company", "=", True), ("customer", "=", True)]
    domain2 = [("customer", "=", True), ("is_company", "=", True)]
    fields1 = ["name", "email", "phone"]
    fields2 = ["phone", "name", "email"]

    # Act
    session_id1 = cache.generate_session_id(model, domain1, fields1)
    session_id2 = cache.generate_session_id(model, domain2, fields2)

    # Assert
    assert session_id1 == session_id2


def test_generate_session_id_handles_unsortable_domain() -> None:
    """Verify session ID generation with unsortable domain falls back."""
    # Arrange
    model = "res.partner"
    # Domain with mixed types that can't be sorted
    domain = [("name", "=", "test"), ("id", "in", [1, 2]), 1]
    fields = ["name"]

    # Act
    session_id = cache.generate_session_id(model, domain, fields)

    # Assert
    assert isinstance(session_id, str)
    assert len(session_id) == 16


def test_get_session_dir_creates_directory(tmp_path: Path) -> None:
    """Verify that a session directory is created correctly."""
    # Arrange
    session_id = "test_session_123"
    with patch.object(Path, "cwd", return_value=tmp_path):
        # Act
        session_dir = cache.get_session_dir(session_id)

        # Assert
        assert session_dir is not None
        assert session_dir.name == session_id
        assert session_dir.parent.name == "sessions"
        assert session_dir.exists()


@patch("pathlib.Path.mkdir")
def test_get_session_dir_handles_exception(
    mock_mkdir: MagicMock, caplog: "MagicMock"
) -> None:
    """Verify get_session_dir handles exceptions gracefully."""
    mock_mkdir.side_effect = Exception("Test exception")
    session_dir = cache.get_session_dir("test_session")
    assert session_dir is None
    assert "Could not create or access session directory" in caplog.text


def test_resolve_cache_dir_from_dict(
    tmp_path: Path, monkeypatch: "MonkeyPatch"
) -> None:
    """resolve_cache_dir builds a unique cache dir from a connection dict."""
    monkeypatch.chdir(tmp_path)
    d = cache.resolve_cache_dir({"hostname": "h", "port": 8069, "database": "db"})
    assert d is not None
    assert d.exists()
    assert d.parent.name == ".fluvo_cache"


def test_resolve_cache_dir_from_conf_file(
    tmp_path: Path, monkeypatch: "MonkeyPatch"
) -> None:
    """resolve_cache_dir also accepts a .conf file path (fingerprint via parser)."""
    monkeypatch.chdir(tmp_path)
    conf = tmp_path / "c.conf"
    conf.write_text("[Connection]\nhostname = h\nport = 8069\ndatabase = db\n")
    d = cache.resolve_cache_dir(str(conf))
    assert d is not None and d.exists()


def test_resolve_cache_dir_unfingerprintable_returns_none() -> None:
    """A config that can't be fingerprinted yields no cache dir."""
    assert cache.resolve_cache_dir("/no/such/file.conf") is None


@patch("fluvo.lib.cache.resolve_cache_dir")
@patch("fluvo.lib.conf_lib.get_connection_from_dict")
def test_export_id_map_exports_caches_and_reuses(
    mock_conn: MagicMock, mock_resolve: MagicMock, tmp_path: Path
) -> None:
    """export_id_map builds the id-map, writes parquet, and reuses the cache."""
    mock_resolve.return_value = tmp_path
    conn = MagicMock()
    main_model, imd_model = MagicMock(), MagicMock()
    conn.get_model.side_effect = lambda m: (
        imd_model if m == "ir.model.data" else main_model
    )
    # country_id comes back as [id, name] -> exercises the list-value key path.
    main_model.search_read.return_value = [
        {"id": 5, "country_id": [1, "Belgium"]},
        {"id": 6, "country_id": [2, "France"]},
    ]
    imd_model.search_read.return_value = [
        {"res_id": 5, "module": "base", "name": "rec5"},
    ]
    mock_conn.return_value = conn

    cfg = {"hostname": "h", "port": 1, "database": "d"}
    df = cache.export_id_map(cfg, "res.partner", "country_id")
    assert df is not None and df.height == 2
    assert set(df.columns) == {"key", "xmlid", "db_id"}
    assert (tmp_path / "res.partner.idmap__country_id.parquet").exists()

    # Second call is served from the parquet cache (no new connection).
    mock_conn.reset_mock()
    df2 = cache.export_id_map(cfg, "res.partner", "country_id")
    assert df2 is not None and df2.height == 2
    mock_conn.assert_not_called()


@patch("fluvo.lib.cache.resolve_cache_dir")
@patch("fluvo.lib.conf_lib.get_connection_from_dict")
def test_export_id_map_no_records(
    mock_conn: MagicMock, mock_resolve: MagicMock, tmp_path: Path
) -> None:
    """No source records yields an empty (typed) DataFrame, not an error."""
    mock_resolve.return_value = tmp_path
    conn = MagicMock()
    conn.get_model.return_value.search_read.return_value = []
    mock_conn.return_value = conn
    df = cache.export_id_map({"hostname": "h"}, "res.partner", "name")
    assert df is not None and df.height == 0


@patch("fluvo.lib.cache.resolve_cache_dir", return_value=None)
@patch(
    "fluvo.lib.conf_lib.get_connection_from_dict",
    side_effect=Exception("boom"),
)
def test_export_id_map_handles_connection_error(
    mock_conn: MagicMock, mock_resolve: MagicMock
) -> None:
    """A connection failure is swallowed and returns None."""
    assert cache.export_id_map({"hostname": "h"}, "res.partner", "name") is None


def test_set_cache_enabled_false_disables_cache_dir() -> None:
    """--no-cache (set_cache_enabled(False)) yields no cache dir -> no read/write."""
    cache.set_cache_enabled(False)
    try:
        cfg = {"hostname": "h", "port": 8069, "database": "d"}
        assert cache.resolve_cache_dir(cfg) is None
        assert cache.get_cache_dir("dummy.conf") is None
    finally:
        cache.set_cache_enabled(True)


@patch("fluvo.lib.cache._database_uuid")
def test_uuid_folds_into_fingerprint(mock_uuid: MagicMock) -> None:
    """The database uuid distinguishes otherwise-identical connections (#15)."""
    cfg = {"hostname": "h", "port": 8069, "database": "d"}
    mock_uuid.return_value = "uuid-A"
    fp_a = cache._connection_fingerprint(cfg)
    mock_uuid.return_value = "uuid-B"
    fp_b = cache._connection_fingerprint(cfg)
    assert fp_a is not None and fp_b is not None
    assert fp_a != fp_b  # a rebuild/restore (new uuid) -> fresh cache
    assert fp_a.endswith("uuid-A")
    assert fp_b.endswith("uuid-B")


@patch("fluvo.lib.cache._database_uuid", return_value=None)
def test_uuid_unavailable_falls_back_to_base(mock_uuid: MagicMock) -> None:
    """When the uuid can't be read, the fingerprint is just host+port+db."""
    fp = cache._connection_fingerprint({"hostname": "h", "port": 8069, "database": "d"})
    assert fp == "h8069d"


def test_database_uuid_none_on_read_failure() -> None:
    """A read failure (e.g. ACL) yields None -> graceful fallback (#15)."""
    cache._uuid_by_fingerprint.clear()
    with patch(
        "fluvo.lib.conf_lib.get_connection_from_dict", side_effect=Exception("acl")
    ):
        assert cache._database_uuid({"hostname": "h"}, "fp-x") is None


@patch("fluvo.lib.conf_lib.get_connection_from_dict")
def test_database_uuid_reads_config_parameter(mock_conn: MagicMock) -> None:
    """The uuid comes from ir.config_parameter.get_param('database.uuid')."""
    cache._uuid_by_fingerprint.clear()
    get_param = mock_conn.return_value.get_model.return_value.get_param
    get_param.return_value = "the-uuid"
    assert cache._database_uuid({"hostname": "h"}, "fp-y") == "the-uuid"
    mock_conn.return_value.get_model.assert_called_with("ir.config_parameter")
    get_param.assert_called_with("database.uuid")
