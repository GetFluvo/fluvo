"""Test the language installation workflow."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fluvo.lib.actions.language_installer import (
    _wait_for_languages_to_be_active,
    run_language_installation,
)


class TestLanguageInstaller:
    """Tests for the language installation action."""

    @patch("fluvo.lib.actions.language_installer.time.sleep")
    def test_wait_for_languages_success(self, mock_sleep: MagicMock) -> None:
        """Tests the success path of the language polling function."""
        mock_lang_model = MagicMock()
        mock_lang_model.search_read.side_effect = [
            [{"code": "de_DE"}],
            [{"code": "de_DE"}, {"code": "fr_FR"}],
        ]
        mock_connection = MagicMock()
        mock_connection.get_model.return_value = mock_lang_model

        result = _wait_for_languages_to_be_active(
            mock_connection, ["de_DE", "fr_FR"], timeout=10
        )

        assert result is True
        assert mock_lang_model.search_read.call_count == 2

    @patch("fluvo.lib.actions.language_installer.time.sleep")
    def test_wait_for_languages_timeout(self, mock_sleep: MagicMock) -> None:
        """Tests the timeout path of the language polling function."""
        mock_lang_model = MagicMock()
        mock_lang_model.search_read.return_value = [{"code": "de_DE"}]
        mock_connection = MagicMock()
        mock_connection.get_model.return_value = mock_lang_model

        result = _wait_for_languages_to_be_active(
            mock_connection, ["de_DE", "fr_FR"], timeout=1
        )
        assert result is False

    @patch("fluvo.lib.actions.language_installer.time.sleep")
    def test_wait_for_languages_rpc_error(self, mock_sleep: MagicMock) -> None:
        """Tests that the polling function handles an RPC error."""
        mock_lang_model = MagicMock()
        mock_lang_model.search_read.side_effect = Exception("RPC Error")
        mock_connection = MagicMock()
        mock_connection.get_model.return_value = mock_lang_model

        result = _wait_for_languages_to_be_active(mock_connection, ["de_DE"], timeout=1)
        assert result is False

    @patch("fluvo.lib.odoo_lib.get_odoo_version")
    @patch("fluvo.lib.conf_lib.get_connection_from_config")
    @pytest.mark.parametrize(
        "version, expected_create_payload",
        [
            # Odoo <= 15: single 'lang' Selection.
            (14, {"lang": "de_DE", "overwrite": False}),
            # 15 is the boundary whose behavior changed (old code wrongly used the
            # modern 'langs' branch here); it must still use the single 'lang' key.
            (15, {"lang": "de_DE", "overwrite": False}),
            # Odoo 16+: 'lang_ids' many2many (verified against real Odoo 16 — there
            # is no 'langs' field, which the previous <17 branch wrongly used).
            (16, {"lang_ids": [(6, 0, [42])], "overwrite": False}),
            (18, {"lang_ids": [(6, 0, [42])], "overwrite": False}),
        ],
    )
    def test_run_installation_for_all_versions(
        self,
        mock_get_conn: MagicMock,
        mock_get_version: MagicMock,
        version: int,
        expected_create_payload: dict[str, Any],
    ) -> None:
        """Tests the correct installation payload for each Odoo version boundary."""
        mock_get_version.return_value = version

        mock_lang_model = MagicMock()
        mock_installer_model = MagicMock()
        mock_lang_model.search.return_value = [42]
        mock_installer_model.create.return_value = 123

        def get_model_side_effect(model_name: str) -> Any:
            if model_name == "res.lang":
                return mock_lang_model
            if model_name == "base.language.install":
                return mock_installer_model
            return MagicMock()

        mock_get_conn.return_value.get_model.side_effect = get_model_side_effect

        result = run_language_installation("dummy.conf", ["de_DE"])

        assert result is True
        mock_installer_model.create.assert_called_once_with(expected_create_payload)
        # lang_install is called with the wizard id positionally (RPC style).
        mock_installer_model.lang_install.assert_called_once_with([123])

    @patch("fluvo.lib.odoo_lib.get_odoo_version")
    @patch("fluvo.lib.conf_lib.get_connection_from_config")
    def test_installation_fails_if_language_not_found(
        self, mock_get_conn: MagicMock, mock_get_version: MagicMock
    ) -> None:
        """Test that the function returns False if a language code is invalid."""
        mock_get_version.return_value = 18

        mock_lang_model = MagicMock()
        # Simulate that the language code does not exist in Odoo.
        mock_lang_model.search.return_value = []

        def get_model_side_effect(model_name: str) -> Any:
            if model_name == "res.lang":
                return mock_lang_model
            return MagicMock()

        mock_get_conn.return_value.get_model.side_effect = get_model_side_effect

        result = run_language_installation("dummy.conf", ["xx_XX"])

        assert result is False

    @patch("fluvo.lib.conf_lib.get_connection_from_config")
    def test_installation_fails_gracefully_on_rpc_error(
        self, mock_get_conn: MagicMock
    ) -> None:
        """Test that the function returns False if an RPC error occurs."""
        mock_get_conn.side_effect = Exception("Connection refused")

        result = run_language_installation("dummy.conf", ["de_DE"])

        assert result is False

    @pytest.mark.parametrize("version", [14, 18])
    @patch("fluvo.lib.odoo_lib.get_odoo_version")
    @patch("fluvo.lib.conf_lib.get_connection_from_config")
    def test_run_installation_with_partial_failure(
        self,
        mock_get_conn: MagicMock,
        mock_get_version: MagicMock,
        version: int,
    ) -> None:
        """Test installation with partial failure.

        If one of several languages fails, the process continues but the final
        result is False.
        """
        mock_get_version.return_value = version
        languages_to_install = ["de_DE", "fr_FR"]  # one succeeds, one fails

        mock_lang_model = MagicMock()
        mock_lang_model.search.return_value = [42]
        mock_installer_model = MagicMock()

        def create_side_effect(vals: dict[str, Any]) -> int:
            if "lang_ids" in vals:
                # Modern payload can't say which lang; fail the second create.
                if mock_installer_model.create.call_count == 2:
                    raise Exception("RPC error on create for fr_FR")
            elif vals.get("lang") == "fr_FR":
                raise Exception("RPC error on create for fr_FR")
            return 123  # success for de_DE

        mock_installer_model.create.side_effect = create_side_effect

        def get_model_side_effect(model_name: str) -> Any:
            return {
                "res.lang": mock_lang_model,
                "base.language.install": mock_installer_model,
            }.get(model_name)

        mock_get_conn.return_value.get_model.side_effect = get_model_side_effect

        result = run_language_installation("dummy.conf", languages_to_install)

        assert result is False  # overall failure
        assert mock_installer_model.create.call_count == 2  # both attempted
        mock_installer_model.lang_install.assert_called_once_with([123])

    @pytest.mark.parametrize("version", [14, 18])
    @patch("fluvo.lib.odoo_lib.get_odoo_version")
    @patch("fluvo.lib.conf_lib.get_connection_from_config")
    def test_run_installation_fails_on_install_step(
        self,
        mock_get_conn: MagicMock,
        mock_get_version: MagicMock,
        version: int,
    ) -> None:
        """A failure on the `lang_install` RPC call is handled correctly."""
        mock_get_version.return_value = version
        mock_lang_model = MagicMock()
        mock_installer_model = MagicMock()

        mock_lang_model.search.return_value = [42]
        mock_installer_model.create.return_value = 123
        mock_installer_model.lang_install.side_effect = Exception("Execution error")

        def get_model_side_effect(model_name: str) -> Any:
            return {
                "res.lang": mock_lang_model,
                "base.language.install": mock_installer_model,
            }.get(model_name)

        mock_get_conn.return_value.get_model.side_effect = get_model_side_effect

        result = run_language_installation("dummy.conf", ["de_DE"])

        assert result is False
        mock_installer_model.create.assert_called_once()
        mock_installer_model.lang_install.assert_called_once()
