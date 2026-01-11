"""Test cases for the __main__ module."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from odoo_data_flow import __main__


@pytest.fixture
def runner() -> CliRunner:
    """Fixture for invoking command-line interfaces."""
    return CliRunner()


# --- Project Mode Tests ---
@patch("odoo_data_flow.__main__.run_project_flow")
def test_project_mode_with_explicit_flow_file(
    mock_run_flow: MagicMock, runner: CliRunner
) -> None:
    """It should run project mode when --flow-file is explicitly provided."""
    with runner.isolated_filesystem():
        with open("test_flow.yml", "w") as f:
            f.write("flow: content")
        result = runner.invoke(__main__.cli, ["--flow-file", "test_flow.yml"])
        assert result.exit_code == 0
        mock_run_flow.assert_called_once_with("test_flow.yml", None)


@patch("odoo_data_flow.__main__.run_project_flow")
def test_project_mode_with_default_flow_file(
    mock_run_flow: MagicMock, runner: CliRunner
) -> None:
    """It should use flows.yml by default if it exists and no command is given."""
    with runner.isolated_filesystem():
        with open("flows.yml", "w") as f:
            f.write("default flow")
        result = runner.invoke(__main__.cli)
        assert result.exit_code == 0
        mock_run_flow.assert_called_once_with("flows.yml", None)


def test_shows_help_when_no_command_or_flow_file(runner: CliRunner) -> None:
    """It should show the help message when no command or flow file is found."""
    with runner.isolated_filesystem():
        result = runner.invoke(__main__.cli)
        assert result.exit_code == 0
        assert "Usage: cli" in result.output


def test_main_shows_version(runner: CliRunner) -> None:
    """It shows the version of the package when --version is used."""
    result = runner.invoke(__main__.cli, ["--version"])
    assert result.exit_code == 0
    assert "version" in result.output


# --- Single-Action Mode Tests (Refactored) ---


def test_import_fails_without_required_options(runner: CliRunner) -> None:
    """The import command should fail if required options are missing."""
    result = runner.invoke(__main__.cli, ["import"])
    assert result.exit_code != 0
    assert "Missing option" in result.output
    assert "--connection-file" in result.output


@patch("odoo_data_flow.__main__.run_import")
def test_import_command_calls_runner(
    mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """Tests that the import command calls the correct runner function."""
    with runner.isolated_filesystem():
        with open("conn.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli,
            [
                "import",
                "--connection-file",
                "conn.conf",
                "--file",
                "my.csv",
                "--model",
                "res.partner",
            ],
        )
        assert result.exit_code == 0
        mock_run_import.assert_called_once()
        call_kwargs = mock_run_import.call_args.kwargs
        assert call_kwargs["config"] == "conn.conf"
        assert call_kwargs["filename"] == "my.csv"
        assert call_kwargs["model"] == "res.partner"


@patch("odoo_data_flow.__main__.run_export")
def test_export_command_calls_runner(
    mock_run_export: MagicMock, runner: CliRunner
) -> None:
    """Tests that the export command calls the correct runner function."""
    with runner.isolated_filesystem():
        with open("conn.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli,
            [
                "export",
                "--connection-file",
                "conn.conf",
                "--output",
                "my.csv",
                "--model",
                "res.partner",
                "--fields",
                "id,name",
            ],
        )
        assert result.exit_code == 0
        mock_run_export.assert_called_once()
        call_kwargs = mock_run_export.call_args[1]
        assert call_kwargs["config"] == "conn.conf"


@patch("odoo_data_flow.__main__.run_module_installation")
def test_module_install_command(mock_run_install: MagicMock, runner: CliRunner) -> None:
    """Tests the 'module install' command with the new connection file."""
    with runner.isolated_filesystem():
        with open("conn.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli,
            [
                "module",
                "install",
                "--connection-file",
                "conn.conf",
                "--modules",
                "sale,mrp",
            ],
        )
        assert result.exit_code == 0
        mock_run_install.assert_called_once_with(
            config="conn.conf", modules=["sale", "mrp"]
        )


@patch("odoo_data_flow.__main__.run_write")
def test_write_command_calls_runner(
    mock_run_write: MagicMock, runner: CliRunner
) -> None:
    """Tests that the write command calls the correct runner function."""
    with runner.isolated_filesystem():
        with open("conn.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli,
            [
                "write",
                "--connection-file",
                "conn.conf",
                "--file",
                "my.csv",
                "--model",
                "res.partner",
            ],
        )
        assert result.exit_code == 0
        mock_run_write.assert_called_once()
        call_kwargs = mock_run_write.call_args.kwargs
        assert call_kwargs["config"] == "conn.conf"


@patch("odoo_data_flow.__main__.run_path_to_image")
def test_path_to_image_command_calls_runner(
    mock_run_path_to_image: MagicMock, runner: CliRunner
) -> None:
    """Tests that the path-to-image command calls the correct runner function."""
    result = runner.invoke(
        __main__.cli, ["path-to-image", "my.csv", "--fields", "image"]
    )
    assert result.exit_code == 0
    mock_run_path_to_image.assert_called_once()


@patch("odoo_data_flow.__main__.run_url_to_image")
def test_url_to_image_command_calls_runner(
    mock_run_url_to_image: MagicMock, runner: CliRunner
) -> None:
    """Tests that the url-to-image command calls the correct runner function."""
    result = runner.invoke(
        __main__.cli, ["url-to-image", "my.csv", "--fields", "image_url"]
    )
    assert result.exit_code == 0
    mock_run_url_to_image.assert_called_once()


@patch("odoo_data_flow.__main__.run_migration")
def test_migrate_command_bad_mapping_syntax(
    mock_run_migration: MagicMock, runner: CliRunner
) -> None:
    """Tests that the migrate command handles a bad mapping string."""
    result = runner.invoke(
        __main__.cli,
        [
            "migrate",
            "--config-export",
            "src.conf",
            "--config-import",
            "dest.conf",
            "--model",
            "res.partner",
            "--fields",
            "id,name",
            "--mapping",
            "this-is-not-a-dict",
        ],
    )
    assert result.exit_code == 0
    assert "Invalid mapping provided" in result.output
    mock_run_migration.assert_not_called()


@patch("odoo_data_flow.__main__.run_migration")
def test_migrate_command_mapping_not_a_dict(
    mock_run_migration: MagicMock, runner: CliRunner
) -> None:
    """Tests that migrate command handles a valid literal that is not a dict."""
    result = runner.invoke(
        __main__.cli,
        [
            "migrate",
            "--config-export",
            "src.conf",
            "--config-import",
            "dest.conf",
            "--model",
            "res.partner",
            "--fields",
            "id,name",
            "--mapping",
            "['this', 'is', 'a', 'list']",  # Valid literal, but not a dict
        ],
    )
    assert result.exit_code == 0
    assert "Mapping must be a dictionary" in result.output
    mock_run_migration.assert_not_called()


@patch("odoo_data_flow.__main__.run_invoice_v9_workflow")
def test_workflow_command_calls_runner(
    mock_run_workflow: MagicMock, runner: CliRunner
) -> None:
    """Tests that the workflow command calls the correct runner function."""
    with runner.isolated_filesystem():
        with open("my.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli,
            [
                "workflow",
                "invoice-v9",
                "--connection-file",
                "my.conf",
                "--field",
                "x_status",
                "--status-map",
                "{}",
                "--paid-date-field",
                "x_date",
                "--payment-journal",
                "1",
            ],
        )
        assert result.exit_code == 0
        mock_run_workflow.assert_called_once()
        call_kwargs = mock_run_workflow.call_args.kwargs
        assert call_kwargs["config"] == "my.conf"


@patch("odoo_data_flow.__main__.run_update_module_list")
def test_module_update_list_command(
    mock_run_update: MagicMock, runner: CliRunner
) -> None:
    """Tests that the 'module update-list' command calls the correct function."""
    with runner.isolated_filesystem():
        with open("c.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli, ["module", "update-list", "--connection-file", "c.conf"]
        )
        assert result.exit_code == 0
        mock_run_update.assert_called_once_with(config="c.conf")


@patch("odoo_data_flow.__main__.run_module_uninstallation")
def test_module_uninstall_command(
    mock_run_uninstall: MagicMock, runner: CliRunner
) -> None:
    """Tests that the 'module uninstall' command calls the correct function."""
    with runner.isolated_filesystem():
        with open("conn.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli,
            [
                "module",
                "uninstall",
                "--connection-file",
                "conn.conf",
                "--modules",
                "sale,purchase",
            ],
        )
        assert result.exit_code == 0
        mock_run_uninstall.assert_called_once_with(
            config="conn.conf", modules=["sale", "purchase"]
        )


@patch("odoo_data_flow.__main__.run_language_installation")
def test_module_install_languages_command(
    mock_run_install: MagicMock, runner: CliRunner
) -> None:
    """Tests that the 'module install-languages' command calls the correct function."""
    with runner.isolated_filesystem():
        with open("conn.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli,
            [
                "module",
                "install-languages",
                "--connection-file",
                "conn.conf",
                "--languages",
                "en_US,fr_FR",
            ],
        )
        assert result.exit_code == 0
        mock_run_install.assert_called_once_with(
            config="conn.conf", languages=["en_US", "fr_FR"]
        )


# --- All-Companies Flag Tests ---


@patch("odoo_data_flow.__main__.run_import")
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_all_companies_flag_sets_context(
    mock_get_conn: MagicMock, mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """Tests that --all-companies fetches user companies and sets context."""
    # Mock the connection and user data
    mock_conn = MagicMock()
    mock_conn.user_id = 2
    mock_user_model = MagicMock()
    mock_user_model.read.return_value = {"company_ids": [1, 2, 3]}
    mock_conn.get_model.return_value = mock_user_model
    mock_get_conn.return_value = mock_conn

    with runner.isolated_filesystem():
        with open("conn.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli,
            [
                "import",
                "--connection-file",
                "conn.conf",
                "--file",
                "my.csv",
                "--model",
                "res.partner",
                "--all-companies",
            ],
        )
        assert result.exit_code == 0
        mock_run_import.assert_called_once()
        call_kwargs = mock_run_import.call_args.kwargs
        # Verify allowed_company_ids was set in context
        assert call_kwargs["context"]["allowed_company_ids"] == [1, 2, 3]


@patch("odoo_data_flow.__main__.run_import")
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_all_companies_flag_handles_empty_companies(
    mock_get_conn: MagicMock, mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """Tests that --all-companies handles users with no company access gracefully."""
    mock_conn = MagicMock()
    mock_conn.user_id = 2
    mock_user_model = MagicMock()
    mock_user_model.read.return_value = {"company_ids": []}
    mock_conn.get_model.return_value = mock_user_model
    mock_get_conn.return_value = mock_conn

    with runner.isolated_filesystem():
        with open("conn.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli,
            [
                "import",
                "--connection-file",
                "conn.conf",
                "--file",
                "my.csv",
                "--model",
                "res.partner",
                "--all-companies",
            ],
        )
        assert result.exit_code == 0
        # Should still proceed, just without allowed_company_ids
        mock_run_import.assert_called_once()
        assert "No company access found" in result.output


@patch("odoo_data_flow.__main__.run_import")
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_all_companies_flag_handles_connection_error(
    mock_get_conn: MagicMock, mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """Tests that --all-companies handles connection errors gracefully."""
    mock_get_conn.side_effect = Exception("Connection failed")

    with runner.isolated_filesystem():
        with open("conn.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli,
            [
                "import",
                "--connection-file",
                "conn.conf",
                "--file",
                "my.csv",
                "--model",
                "res.partner",
                "--all-companies",
            ],
        )
        assert result.exit_code == 0
        # Should still proceed, just without allowed_company_ids
        mock_run_import.assert_called_once()
        assert "Failed to fetch user companies" in result.output


@patch("odoo_data_flow.__main__.run_import")
def test_company_id_flag_sets_context(
    mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """Tests that --company-id sets allowed_company_ids in context."""
    with runner.isolated_filesystem():
        with open("conn.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli,
            [
                "import",
                "--connection-file",
                "conn.conf",
                "--file",
                "my.csv",
                "--model",
                "res.partner",
                "--company-id",
                "5",
            ],
        )
        assert result.exit_code == 0
        mock_run_import.assert_called_once()
        call_kwargs = mock_run_import.call_args.kwargs
        # Verify allowed_company_ids was set to single company
        assert call_kwargs["context"]["allowed_company_ids"] == [5]
        assert call_kwargs["context"]["force_company"] == 5


@patch("odoo_data_flow.__main__.run_export")
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_export_all_companies_flag_sets_context_and_domain(
    mock_get_conn: MagicMock, mock_run_export: MagicMock, runner: CliRunner
) -> None:
    """Tests that export --all-companies sets context and adds company domain filter."""
    # Mock the connection and user data
    mock_conn = MagicMock()
    mock_conn.user_id = 2
    mock_user_model = MagicMock()
    mock_user_model.read.return_value = {"company_ids": [1, 2, 3]}
    mock_target_model = MagicMock()
    mock_target_model.fields_get.return_value = {"company_id": {"type": "many2one"}}

    def get_model(name: str) -> MagicMock:
        if name == "res.users":
            return mock_user_model
        return mock_target_model

    mock_conn.get_model.side_effect = get_model
    mock_get_conn.return_value = mock_conn

    with runner.isolated_filesystem():
        with open("conn.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli,
            [
                "export",
                "--connection-file",
                "conn.conf",
                "--output",
                "out.csv",
                "--model",
                "res.partner",
                "--fields",
                "id,name",
                "--all-companies",
            ],
        )
        assert result.exit_code == 0
        mock_run_export.assert_called_once()
        call_kwargs = mock_run_export.call_args.kwargs
        # Verify allowed_company_ids was set in context
        assert call_kwargs["context"]["allowed_company_ids"] == [1, 2, 3]
        # Verify domain includes company filter
        expected_domain = (
            "['|', ('company_id', '=', False), ('company_id', 'in', [1, 2, 3])]"
        )
        assert call_kwargs["domain"] == expected_domain


@patch("odoo_data_flow.__main__.run_export")
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_export_all_companies_flag_handles_empty_companies(
    mock_get_conn: MagicMock, mock_run_export: MagicMock, runner: CliRunner
) -> None:
    """Tests that export --all-companies handles users with no company access."""
    mock_conn = MagicMock()
    mock_conn.user_id = 2
    mock_user_model = MagicMock()
    mock_user_model.read.return_value = {"company_ids": []}
    mock_conn.get_model.return_value = mock_user_model
    mock_get_conn.return_value = mock_conn

    with runner.isolated_filesystem():
        with open("conn.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli,
            [
                "export",
                "--connection-file",
                "conn.conf",
                "--output",
                "out.csv",
                "--model",
                "res.partner",
                "--fields",
                "id,name",
                "--all-companies",
            ],
        )
        assert result.exit_code == 0
        # Should still proceed, just without allowed_company_ids
        mock_run_export.assert_called_once()
        assert "No company access found" in result.output


@patch("odoo_data_flow.__main__.run_export")
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_export_all_companies_flag_handles_connection_error(
    mock_get_conn: MagicMock, mock_run_export: MagicMock, runner: CliRunner
) -> None:
    """Tests that export --all-companies handles connection errors gracefully."""
    mock_get_conn.side_effect = Exception("Connection failed")

    with runner.isolated_filesystem():
        with open("conn.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli,
            [
                "export",
                "--connection-file",
                "conn.conf",
                "--output",
                "out.csv",
                "--model",
                "res.partner",
                "--fields",
                "id,name",
                "--all-companies",
            ],
        )
        assert result.exit_code == 0
        # Should still proceed, just without allowed_company_ids
        mock_run_export.assert_called_once()
        assert "Failed to fetch user companies" in result.output


@patch("odoo_data_flow.__main__.run_export")
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_export_all_companies_flag_combines_with_existing_domain(
    mock_get_conn: MagicMock, mock_run_export: MagicMock, runner: CliRunner
) -> None:
    """Tests that --all-companies combines company filter with existing domain."""
    mock_conn = MagicMock()
    mock_conn.user_id = 2
    mock_user_model = MagicMock()
    mock_user_model.read.return_value = {"company_ids": [1, 2]}
    mock_target_model = MagicMock()
    mock_target_model.fields_get.return_value = {"company_id": {"type": "many2one"}}

    def get_model(name: str) -> MagicMock:
        if name == "res.users":
            return mock_user_model
        return mock_target_model

    mock_conn.get_model.side_effect = get_model
    mock_get_conn.return_value = mock_conn

    with runner.isolated_filesystem():
        with open("conn.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli,
            [
                "export",
                "--connection-file",
                "conn.conf",
                "--output",
                "out.csv",
                "--model",
                "mrp.bom",
                "--fields",
                "id,product_id",
                "--domain",
                "[('active', '=', True)]",
                "--all-companies",
            ],
        )
        assert result.exit_code == 0
        mock_run_export.assert_called_once()
        call_kwargs = mock_run_export.call_args.kwargs
        # Verify domain combines company filter with existing filter
        expected_domain = (
            "['|', ('company_id', '=', False), ('company_id', 'in', [1, 2]), "
            "('active', '=', True)]"
        )
        assert call_kwargs["domain"] == expected_domain


@patch("odoo_data_flow.__main__.run_export")
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_export_sudo_flag_disables_and_reenables_rules(
    mock_get_conn: MagicMock, mock_run_export: MagicMock, runner: CliRunner
) -> None:
    """Tests that --sudo temporarily disables record rules during export."""
    mock_conn = MagicMock()
    mock_ir_model = MagicMock()
    mock_ir_model.search.return_value = [123]  # Model ID
    mock_ir_rule = MagicMock()
    mock_ir_rule.search.return_value = [456, 789]  # Rule IDs

    def get_model(name: str) -> MagicMock:
        if name == "ir.model":
            return mock_ir_model
        elif name == "ir.rule":
            return mock_ir_rule
        return MagicMock()

    mock_conn.get_model.side_effect = get_model
    mock_get_conn.return_value = mock_conn

    with runner.isolated_filesystem():
        with open("conn.conf", "w") as f:
            f.write("[Connection]")
        result = runner.invoke(
            __main__.cli,
            [
                "export",
                "--connection-file",
                "conn.conf",
                "--output",
                "out.csv",
                "--model",
                "res.partner",
                "--fields",
                "id,name",
                "--sudo",
            ],
        )
        assert result.exit_code == 0
        # Verify rules were disabled then re-enabled
        assert mock_ir_rule.write.call_count == 2
        # First call: disable rules
        mock_ir_rule.write.assert_any_call([456, 789], {"active": False})
        # Second call: re-enable rules
        mock_ir_rule.write.assert_any_call([456, 789], {"active": True})
        mock_run_export.assert_called_once()


@patch("odoo_data_flow.__main__.run_import")
@patch("odoo_data_flow.lib.conf_lib.get_connection_from_config")
def test_import_sudo_flag_disables_and_reenables_rules(
    mock_get_conn: MagicMock, mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """Tests that --sudo temporarily disables record rules during import."""
    mock_conn = MagicMock()
    mock_ir_model = MagicMock()
    mock_ir_model.search.return_value = [123]  # Model ID
    mock_ir_rule = MagicMock()
    mock_ir_rule.search.return_value = [456, 789]  # Rule IDs

    def get_model(name: str) -> MagicMock:
        if name == "ir.model":
            return mock_ir_model
        elif name == "ir.rule":
            return mock_ir_rule
        return MagicMock()

    mock_conn.get_model.side_effect = get_model
    mock_get_conn.return_value = mock_conn

    with runner.isolated_filesystem():
        with open("conn.conf", "w") as f:
            f.write("[Connection]")
        with open("data.csv", "w") as f:
            f.write("id;name\n1;Test")
        result = runner.invoke(
            __main__.cli,
            [
                "import",
                "--connection-file",
                "conn.conf",
                "--file",
                "data.csv",
                "--model",
                "res.partner",
                "--sudo",
            ],
        )
        assert result.exit_code == 0
        # Verify rules were disabled then re-enabled
        assert mock_ir_rule.write.call_count == 2
        # First call: disable rules
        mock_ir_rule.write.assert_any_call([456, 789], {"active": False})
        # Second call: re-enable rules
        mock_ir_rule.write.assert_any_call([456, 789], {"active": True})
        mock_run_import.assert_called_once()
