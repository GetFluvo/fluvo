"""Tests for the named connection registry (#363)."""

from pathlib import Path

import pytest

from fluvo.lib import registry
from fluvo.lib.core import ConnectError, InputError

_FILE_ENTRY = """
schema = 1

[connections.test]
file = "conf/test.conf"
"""

_INLINE_ENTRY = """
schema = 1

[connections.prod]
hostname = "erp.example.com"
database = "prod"
login = "migration@example.com"
port = 443
protocol = "jsonrpcs"
password_env = "FLUVO_TEST_PROD_PW"
"""


def _write(tmp_path: Path, text: str, name: str = "fluvo-connections.toml") -> str:
    """Write a registry file and return its path."""
    path = tmp_path / name
    path.write_text(text)
    return str(path)


class TestDiscoverRegistry:
    """Shallow, explicit discovery with no home/parent search."""

    def test_explicit_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit --registry path is used verbatim."""
        monkeypatch.setenv("FLUVO_REGISTRY", "/env/path.toml")
        assert registry.discover_registry("/explicit.toml") == "/explicit.toml"

    def test_env_used_when_no_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """$FLUVO_REGISTRY is used when no explicit path is given."""
        monkeypatch.setenv("FLUVO_REGISTRY", "/env/path.toml")
        assert registry.discover_registry(None) == "/env/path.toml"

    def test_cwd_default_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """./fluvo-connections.toml is used when it exists and nothing else is set."""
        monkeypatch.delenv("FLUVO_REGISTRY", raising=False)
        _write(tmp_path, _FILE_ENTRY)
        monkeypatch.chdir(tmp_path)
        assert registry.discover_registry(None) == str(
            tmp_path / "fluvo-connections.toml"
        )

    def test_none_when_nothing_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discovery returns None rather than searching parents or home."""
        monkeypatch.delenv("FLUVO_REGISTRY", raising=False)
        monkeypatch.chdir(tmp_path)
        assert registry.discover_registry(None) is None


class TestResolveConnection:
    """Resolving a key into a file path or an inline config dict."""

    def test_file_entry_returns_path(self, tmp_path: Path) -> None:
        """A `file =` entry resolves to its path, reused as-is."""
        path = _write(tmp_path, _FILE_ENTRY)
        assert registry.resolve_connection("test", path) == "conf/test.conf"

    def test_inline_entry_resolves_secret_and_env_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An inline entry resolves its *_env secret and carries _env_name."""
        monkeypatch.setenv("FLUVO_TEST_PROD_PW", "s3cret")
        path = _write(tmp_path, _INLINE_ENTRY)
        config = registry.resolve_connection("prod", path)
        assert isinstance(config, dict)
        assert config["hostname"] == "erp.example.com"
        assert config["password"] == "s3cret"  # noqa: S105 - resolved from env
        assert config["_env_name"] == "prod"  # fail-file dir follows the key
        assert "password_env" not in config  # the reference name is not leaked

    def test_unset_secret_env_raises_connect_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unset referenced env var is a ConnectError before any network call."""
        monkeypatch.delenv("FLUVO_TEST_PROD_PW", raising=False)
        path = _write(tmp_path, _INLINE_ENTRY)
        with pytest.raises(ConnectError, match="unset or empty"):
            registry.resolve_connection("prod", path)

    def test_literal_password_rejected(self, tmp_path: Path) -> None:
        """A literal secret in the registry is refused."""
        text = _INLINE_ENTRY.replace(
            'password_env = "FLUVO_TEST_PROD_PW"', 'password = "hunter2"'
        )
        path = _write(tmp_path, text)
        with pytest.raises(InputError, match="literal 'password' is not allowed"):
            registry.resolve_connection("prod", path)

    def test_missing_required_field(self, tmp_path: Path) -> None:
        """An inline entry missing a required field is rejected."""
        text = _INLINE_ENTRY.replace('database = "prod"\n', "")
        path = _write(tmp_path, text)
        with pytest.raises(InputError, match="missing required field.*database"):
            registry.resolve_connection("prod", path)

    def test_two_secret_refs_rejected(self, tmp_path: Path) -> None:
        """Exactly one of password_env / api_key_env is required."""
        text = _INLINE_ENTRY.replace(
            'password_env = "FLUVO_TEST_PROD_PW"',
            'password_env = "A"\napi_key_env = "B"',
        )
        path = _write(tmp_path, text)
        with pytest.raises(InputError, match="exactly one of `password_env`"):
            registry.resolve_connection("prod", path)

    def test_file_and_inline_mix_rejected(self, tmp_path: Path) -> None:
        """A `file` entry cannot also carry inline fields."""
        text = '[connections.x]\nfile = "a.conf"\nhostname = "h"\n'
        path = _write(tmp_path, "schema = 1\n" + text)
        with pytest.raises(InputError, match="cannot be combined with inline"):
            registry.resolve_connection("x", path)

    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        """An unknown inline key is rejected."""
        text = _INLINE_ENTRY + 'extra = "x"\n'
        path = _write(tmp_path, text)
        with pytest.raises(InputError, match="unknown key 'extra'"):
            registry.resolve_connection("prod", path)

    def test_wrong_schema_rejected(self, tmp_path: Path) -> None:
        """Only schema 1 is accepted."""
        path = _write(tmp_path, _FILE_ENTRY.replace("schema = 1", "schema = 2"))
        with pytest.raises(InputError, match="`schema` must be 1"):
            registry.resolve_connection("test", path)

    def test_unknown_connection_key(self, tmp_path: Path) -> None:
        """Requesting an undefined connection names the defined ones."""
        path = _write(tmp_path, _FILE_ENTRY)
        with pytest.raises(InputError, match="not defined"):
            registry.resolve_connection("nope", path)
