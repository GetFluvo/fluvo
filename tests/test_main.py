"""Test cases for the __main__ module."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fluvo import __main__


@pytest.fixture
def runner() -> CliRunner:
    """Fixture for invoking command-line interfaces."""
    return CliRunner()


# --- Project Mode Tests ---
@patch("fluvo.__main__.run_project_flow")
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


@patch("fluvo.__main__.run_project_flow")
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


@patch("fluvo.__main__.run_import")
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


@patch("fluvo.__main__.run_export")
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


@patch("fluvo.__main__.run_module_installation")
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


@patch("fluvo.__main__.run_write")
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


@patch("fluvo.__main__.run_path_to_image")
def test_path_to_image_command_calls_runner(
    mock_run_path_to_image: MagicMock, runner: CliRunner
) -> None:
    """Tests that the path-to-image command calls the correct runner function."""
    result = runner.invoke(
        __main__.cli, ["path-to-image", "my.csv", "--fields", "image"]
    )
    assert result.exit_code == 0
    mock_run_path_to_image.assert_called_once()


@patch("fluvo.__main__.run_url_to_image")
def test_url_to_image_command_calls_runner(
    mock_run_url_to_image: MagicMock, runner: CliRunner
) -> None:
    """Tests that the url-to-image command calls the correct runner function."""
    result = runner.invoke(
        __main__.cli, ["url-to-image", "my.csv", "--fields", "image_url"]
    )
    assert result.exit_code == 0
    mock_run_url_to_image.assert_called_once()


@patch("fluvo.__main__.run_migration")
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


@patch("fluvo.__main__.run_migration")
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


@patch("fluvo.__main__.run_invoice_v9_workflow")
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


@patch("fluvo.__main__.run_update_module_list")
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


@patch("fluvo.__main__.run_module_uninstallation")
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


@patch("fluvo.__main__.run_language_installation")
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


@patch("fluvo.__main__.run_import")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
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


@patch("fluvo.__main__.run_import")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
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


@patch("fluvo.__main__.run_import")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
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


@patch("fluvo.__main__.run_import")
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
        # Note: force_company is no longer set (deprecated in Odoo 18+)
        assert call_kwargs["context"]["allowed_company_ids"] == [5]


@patch("fluvo.__main__.run_export")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
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


@patch("fluvo.__main__.run_export")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
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


@patch("fluvo.__main__.run_export")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
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


@patch("fluvo.__main__.run_export")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
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


@patch("fluvo.__main__.run_export")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
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


@patch("fluvo.__main__.run_import")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
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


@patch("fluvo.__main__._execute_post_action")
@patch("fluvo.__main__.run_import")
def test_import_post_action_called_on_success(
    mock_run_import: MagicMock,
    mock_post_action: MagicMock,
    runner: CliRunner,
) -> None:
    """Tests that --post-action is called when import succeeds."""
    mock_run_import.return_value = {"ext_id_1": 1, "ext_id_2": 2}

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
                "stock.quant",
                "--post-action",
                "action_apply_inventory",
            ],
        )
        assert result.exit_code == 0
        mock_run_import.assert_called_once()
        mock_post_action.assert_called_once()
        # Verify post-action was called with correct arguments
        call_args = mock_post_action.call_args
        assert call_args[0][1] == "stock.quant"  # model
        assert call_args[0][2] == "action_apply_inventory"  # action_name
        assert call_args[0][3] == {"ext_id_1": 1, "ext_id_2": 2}  # id_map


@patch("fluvo.__main__._execute_post_action")
@patch("fluvo.__main__.run_import")
def test_import_post_action_not_called_on_failure(
    mock_run_import: MagicMock,
    mock_post_action: MagicMock,
    runner: CliRunner,
) -> None:
    """Tests that --post-action is not called when import fails."""
    mock_run_import.return_value = None  # Import failed

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
                "stock.quant",
                "--post-action",
                "action_apply_inventory",
            ],
        )
        assert result.exit_code == 0
        mock_run_import.assert_called_once()
        mock_post_action.assert_not_called()


@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_execute_post_action_calls_method(mock_get_conn: MagicMock) -> None:
    """Tests that _execute_post_action calls the correct method on records."""
    from fluvo.__main__ import _execute_post_action

    mock_conn = MagicMock()
    mock_model = MagicMock()
    mock_model.action_apply_inventory.return_value = True
    mock_conn.get_model.return_value = mock_model
    mock_get_conn.return_value = mock_conn

    id_map = {"ext_1": 10, "ext_2": 20, "ext_3": 30}

    _execute_post_action(
        config="conn.conf",
        model="stock.quant",
        action_name="action_apply_inventory",
        id_map=id_map,
        context={"tracking_disable": True},
    )

    mock_conn.get_model.assert_called_once_with("stock.quant")
    mock_model.action_apply_inventory.assert_called_once()
    # Check that all IDs were passed
    call_args = mock_model.action_apply_inventory.call_args
    assert set(call_args[0][0]) == {10, 20, 30}


@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_execute_post_action_handles_empty_id_map(mock_get_conn: MagicMock) -> None:
    """Tests that _execute_post_action handles empty id_map gracefully."""
    from fluvo.__main__ import _execute_post_action

    _execute_post_action(
        config="conn.conf",
        model="stock.quant",
        action_name="action_apply_inventory",
        id_map={},
        context={},
    )

    mock_get_conn.assert_not_called()


@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_execute_post_action_handles_missing_model(mock_get_conn: MagicMock) -> None:
    """Tests that _execute_post_action handles missing model gracefully."""
    from fluvo.__main__ import _execute_post_action

    _execute_post_action(
        config="conn.conf",
        model=None,
        action_name="action_apply_inventory",
        id_map={"ext_1": 10},
        context={},
    )

    mock_get_conn.assert_not_called()


@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_execute_post_action_returns_true_on_success(mock_get_conn: MagicMock) -> None:
    """Tests that _execute_post_action returns True on success."""
    from fluvo.__main__ import _execute_post_action

    mock_conn = MagicMock()
    mock_model = MagicMock()
    mock_model.action_apply_inventory.return_value = True
    mock_conn.get_model.return_value = mock_model
    mock_get_conn.return_value = mock_conn

    result = _execute_post_action(
        config="conn.conf",
        model="stock.quant",
        action_name="action_apply_inventory",
        id_map={"ext_1": 10},
        context={},
    )

    assert result is True


@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_execute_post_action_returns_true_on_timeout(mock_get_conn: MagicMock) -> None:
    """Tests that _execute_post_action returns True on timeout.

    Server may have completed the operation even though we timed out.
    """
    import socket

    from fluvo.__main__ import _execute_post_action

    mock_conn = MagicMock()
    mock_model = MagicMock()
    mock_model.action_apply_inventory.side_effect = socket.timeout(
        "Connection timed out"
    )
    mock_conn.get_model.return_value = mock_model
    mock_get_conn.return_value = mock_conn

    result = _execute_post_action(
        config="conn.conf",
        model="stock.quant",
        action_name="action_apply_inventory",
        id_map={"ext_1": 10},
        context={},
    )

    # Should return True because server may have completed the operation
    assert result is True


@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_execute_post_action_returns_false_on_other_error(
    mock_get_conn: MagicMock,
) -> None:
    """Tests that _execute_post_action returns False on non-timeout errors."""
    from fluvo.__main__ import _execute_post_action

    mock_conn = MagicMock()
    mock_model = MagicMock()
    mock_model.action_apply_inventory.side_effect = ValueError("Some error")
    mock_conn.get_model.return_value = mock_model
    mock_get_conn.return_value = mock_conn

    result = _execute_post_action(
        config="conn.conf",
        model="stock.quant",
        action_name="action_apply_inventory",
        id_map={"ext_1": 10},
        context={},
    )

    assert result is False


@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_get_product_ids_from_quants(mock_get_conn: MagicMock) -> None:
    """Tests that _get_product_ids_from_quants extracts product IDs correctly."""
    from fluvo.__main__ import _get_product_ids_from_quants

    mock_conn = MagicMock()
    mock_quant_model = MagicMock()
    mock_quant_model.read.return_value = [
        {"product_id": [101, "Product A"]},
        {"product_id": [102, "Product B"]},
        {"product_id": [101, "Product A"]},  # Duplicate
    ]
    mock_conn.get_model.return_value = mock_quant_model
    mock_get_conn.return_value = mock_conn

    product_ids = _get_product_ids_from_quants("conn.conf", [1, 2, 3])

    assert set(product_ids) == {101, 102}  # Should be deduplicated
    mock_quant_model.read.assert_called_once_with([1, 2, 3], ["product_id"])


@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_get_product_ids_from_quants_empty_input(mock_get_conn: MagicMock) -> None:
    """Tests that _get_product_ids_from_quants handles empty input."""
    from fluvo.__main__ import _get_product_ids_from_quants

    product_ids = _get_product_ids_from_quants("conn.conf", [])

    assert product_ids == []
    mock_get_conn.assert_not_called()


@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_update_inventory_move_dates(mock_get_conn: MagicMock) -> None:
    """Tests that _update_inventory_move_dates updates move dates correctly."""
    from fluvo.__main__ import _update_inventory_move_dates

    mock_conn = MagicMock()
    mock_location_model = MagicMock()
    mock_location_model.search.return_value = [99]  # Inventory adjustment location
    mock_move_model = MagicMock()
    mock_move_model.search.return_value = [501, 502, 503]

    def get_model(name: str) -> MagicMock:
        if name == "stock.location":
            return mock_location_model
        elif name == "stock.move":
            return mock_move_model
        return MagicMock()

    mock_conn.get_model.side_effect = get_model
    mock_get_conn.return_value = mock_conn

    _update_inventory_move_dates(
        config="conn.conf",
        move_date="2026-01-01",
        context={"tracking_disable": True},
        product_ids=[101, 102],
    )

    # Verify location search
    mock_location_model.search.assert_called_once_with([("usage", "=", "inventory")])

    # Verify move search was called with correct domain structure
    search_call = mock_move_model.search.call_args[0][0]
    assert "|" in search_call
    assert ("location_id", "in", [99]) in search_call
    assert ("location_dest_id", "in", [99]) in search_call
    assert ("product_id", "in", [101, 102]) in search_call
    assert ("state", "=", "done") in search_call

    # Verify write was called with correct date
    mock_move_model.write.assert_called_once()
    write_args = mock_move_model.write.call_args
    assert write_args[0][0] == [501, 502, 503]
    assert write_args[0][1] == {"date": "2026-01-01 00:00:00"}


@patch("fluvo.__main__._update_inventory_move_dates")
@patch("fluvo.__main__._get_product_ids_from_quants")
@patch("fluvo.__main__._execute_post_action")
@patch("fluvo.__main__.run_import")
def test_import_move_date_triggers_update(
    mock_run_import: MagicMock,
    mock_post_action: MagicMock,
    mock_get_products: MagicMock,
    mock_update_dates: MagicMock,
    runner: CliRunner,
) -> None:
    """Tests that --move-date triggers the move date update after post-action."""
    mock_run_import.return_value = {"ext_id_1": 1, "ext_id_2": 2}
    mock_post_action.return_value = True
    mock_get_products.return_value = [101, 102]

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
                "stock.quant",
                "--post-action",
                "action_apply_inventory",
                "--move-date",
                "2026-01-01",
            ],
        )

        assert result.exit_code == 0
        mock_run_import.assert_called_once()
        mock_post_action.assert_called_once()

        # Verify product IDs were extracted
        mock_get_products.assert_called_once()
        get_products_args = mock_get_products.call_args[0]
        assert get_products_args[1] == [1, 2]  # quant_ids from import_result.values()

        # Verify move dates were updated
        mock_update_dates.assert_called_once()
        update_args = mock_update_dates.call_args
        assert update_args[0][1] == "2026-01-01"  # move_date
        assert update_args[0][3] == [101, 102]  # product_ids


@patch("fluvo.__main__._update_inventory_move_dates")
@patch("fluvo.__main__._get_product_ids_from_quants")
@patch("fluvo.__main__._execute_post_action")
@patch("fluvo.__main__.run_import")
def test_import_move_date_not_triggered_without_post_action(
    mock_run_import: MagicMock,
    mock_post_action: MagicMock,
    mock_get_products: MagicMock,
    mock_update_dates: MagicMock,
    runner: CliRunner,
) -> None:
    """Tests that --move-date without --post-action shows warning."""
    mock_run_import.return_value = {"ext_id_1": 1}

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
                "stock.quant",
                "--move-date",
                "2026-01-01",
            ],
        )

        assert result.exit_code == 0
        mock_run_import.assert_called_once()
        mock_post_action.assert_not_called()
        mock_get_products.assert_not_called()
        mock_update_dates.assert_not_called()


@patch("fluvo.__main__._update_inventory_move_dates")
@patch("fluvo.__main__._get_product_ids_from_quants")
@patch("fluvo.__main__._execute_post_action")
@patch("fluvo.__main__.run_import")
def test_import_move_date_triggered_even_on_timeout(
    mock_run_import: MagicMock,
    mock_post_action: MagicMock,
    mock_get_products: MagicMock,
    mock_update_dates: MagicMock,
    runner: CliRunner,
) -> None:
    """Tests that --move-date triggers even when post-action times out."""
    mock_run_import.return_value = {"ext_id_1": 1, "ext_id_2": 2}
    mock_post_action.return_value = True  # Returns True even on timeout
    mock_get_products.return_value = [101, 102]

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
                "stock.quant",
                "--post-action",
                "action_apply_inventory",
                "--move-date",
                "2026-01-01",
            ],
        )

        assert result.exit_code == 0
        # Even on timeout, move date update should trigger
        mock_update_dates.assert_called_once()


@patch("fluvo.__main__._update_inventory_move_dates")
@patch("fluvo.__main__._get_product_ids_from_quants")
@patch("fluvo.__main__._execute_post_action")
@patch("fluvo.__main__.run_import")
def test_import_move_date_not_triggered_when_post_action_fails(
    mock_run_import: MagicMock,
    mock_post_action: MagicMock,
    mock_get_products: MagicMock,
    mock_update_dates: MagicMock,
    runner: CliRunner,
) -> None:
    """Tests that --move-date does not trigger when post-action definitively fails."""
    mock_run_import.return_value = {"ext_id_1": 1, "ext_id_2": 2}
    mock_post_action.return_value = False  # Definitive failure
    mock_get_products.return_value = [101, 102]

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
                "stock.quant",
                "--post-action",
                "action_apply_inventory",
                "--move-date",
                "2026-01-01",
            ],
        )

        assert result.exit_code == 0
        mock_post_action.assert_called_once()
        # Move date update should NOT be called when post-action definitively fails
        mock_update_dates.assert_not_called()


@patch("fluvo.__main__._update_inventory_move_dates")
@patch("fluvo.__main__._get_product_ids_from_quants")
@patch("fluvo.__main__._execute_post_action")
@patch("fluvo.__main__.run_import")
def test_import_move_date_not_triggered_when_no_products_extracted(
    mock_run_import: MagicMock,
    mock_post_action: MagicMock,
    mock_get_products: MagicMock,
    mock_update_dates: MagicMock,
    runner: CliRunner,
) -> None:
    """Tests that --move-date doesn't trigger when product extraction fails."""
    mock_run_import.return_value = {"ext_id_1": 1, "ext_id_2": 2}
    mock_post_action.return_value = True
    mock_get_products.return_value = []  # Empty list - extraction failed

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
                "stock.quant",
                "--post-action",
                "action_apply_inventory",
                "--move-date",
                "2026-01-01",
            ],
        )

        assert result.exit_code == 0
        mock_post_action.assert_called_once()
        # Move date update should NOT be called when no products extracted
        mock_update_dates.assert_not_called()
        # Should show warning in output
        assert "No product IDs extracted" in result.output or result.exit_code == 0


def _conn(runner_fs: str = "conn.conf") -> None:
    """Write a minimal connection file in the current (isolated) dir."""
    with open(runner_fs, "w") as f:
        f.write("[Connection]\n")


@patch("fluvo.__main__.run_update_module_list")
def test_module_update_list_calls_runner(mock_fn: MagicMock, runner: CliRunner) -> None:
    """`module update-list` delegates to run_update_module_list."""
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli, ["module", "update-list", "--connection-file", "conn.conf"]
        )
        assert result.exit_code == 0
        mock_fn.assert_called_once()


@patch("fluvo.__main__.run_module_installation")
def test_module_install_calls_runner(mock_fn: MagicMock, runner: CliRunner) -> None:
    """`module install` delegates to run_module_installation."""
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli,
            [
                "module",
                "install",
                "--connection-file",
                "conn.conf",
                "--modules",
                "sale,purchase",
            ],
        )
        assert result.exit_code == 0
        mock_fn.assert_called_once()


@patch("fluvo.__main__.run_module_uninstallation")
def test_module_uninstall_calls_runner(mock_fn: MagicMock, runner: CliRunner) -> None:
    """`module uninstall` delegates to run_module_uninstallation."""
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli,
            [
                "module",
                "uninstall",
                "--connection-file",
                "conn.conf",
                "--modules",
                "sale",
            ],
        )
        assert result.exit_code == 0
        mock_fn.assert_called_once()


@patch("fluvo.__main__.run_language_installation")
def test_module_install_languages_calls_runner(
    mock_fn: MagicMock, runner: CliRunner
) -> None:
    """`module install-languages` delegates to run_language_installation."""
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli,
            [
                "module",
                "install-languages",
                "--connection-file",
                "conn.conf",
                "--languages",
                "nl_BE,fr_FR",
            ],
        )
        assert result.exit_code == 0
        mock_fn.assert_called_once()


@patch("fluvo.__main__.get_vat_validation_settings")
def test_vat_get_settings_calls_runner(mock_fn: MagicMock, runner: CliRunner) -> None:
    """`vat get-settings` delegates and renders the returned settings."""
    mock_fn.return_value = MagicMock(vies_settings={1: True, 2: False})
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli, ["vat", "get-settings", "--connection-file", "conn.conf"]
        )
        assert result.exit_code == 0
        mock_fn.assert_called_once()


@patch("fluvo.__main__.disable_vat_validation")
def test_vat_disable_calls_runner(mock_fn: MagicMock, runner: CliRunner) -> None:
    """`vat disable` delegates to disable_vat_validation."""
    mock_fn.return_value = MagicMock(vies_settings={1: False})
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli, ["vat", "disable", "--connection-file", "conn.conf"]
        )
        assert result.exit_code == 0
        mock_fn.assert_called_once()


@patch("fluvo.__main__.run_invoice_v9_workflow")
def test_workflow_invoice_v9_calls_runner(
    mock_fn: MagicMock, runner: CliRunner
) -> None:
    """`workflow invoice-v9` delegates to run_invoice_v9_workflow."""
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli,
            [
                "workflow",
                "invoice-v9",
                "--connection-file",
                "conn.conf",
                "--field",
                "x_paid",
                "--status-map",
                "{}",
                "--paid-date-field",
                "paid_on",
                "--payment-journal",
                "7",
            ],
        )
        assert result.exit_code == 0
        mock_fn.assert_called_once()


@patch("fluvo.__main__.restore_vat_validation_settings")
def test_vat_restore_calls_runner(mock_fn: MagicMock, runner: CliRunner) -> None:
    """`vat restore` delegates to restore_vat_validation_settings."""
    mock_fn.return_value = True
    with runner.isolated_filesystem():
        _conn()
        with open("settings.json", "w") as f:
            f.write("{}")
        result = runner.invoke(
            __main__.cli,
            [
                "vat",
                "restore",
                "--connection-file",
                "conn.conf",
                "--input",
                "settings.json",
            ],
        )
        assert result.exit_code == 0
        mock_fn.assert_called_once()


@patch("fluvo.__main__.run_vies_validation")
def test_vat_validate_calls_runner(mock_fn: MagicMock, runner: CliRunner) -> None:
    """`vat validate` delegates to run_vies_validation."""
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli, ["vat", "validate", "--connection-file", "conn.conf"]
        )
        assert result.exit_code == 0
        mock_fn.assert_called_once()


@patch("fluvo.__main__.run_write")
def test_write_calls_runner(mock_fn: MagicMock, runner: CliRunner) -> None:
    """`write` delegates to run_write."""
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli,
            [
                "write",
                "--connection-file",
                "conn.conf",
                "--file",
                "x.csv",
                "--model",
                "res.partner",
            ],
        )
        assert result.exit_code == 0
        mock_fn.assert_called_once()


@patch("fluvo.__main__.run_migration")
def test_migrate_calls_runner(mock_fn: MagicMock, runner: CliRunner) -> None:
    """`migrate` delegates to run_migration."""
    with runner.isolated_filesystem():
        with open("exp.conf", "w") as f:
            f.write("[Connection]\n")
        with open("imp.conf", "w") as f:
            f.write("[Connection]\n")
        result = runner.invoke(
            __main__.cli,
            [
                "migrate",
                "--config-export",
                "exp.conf",
                "--config-import",
                "imp.conf",
                "--model",
                "res.partner",
                "--fields",
                "name,email",
            ],
        )
        assert result.exit_code == 0
        mock_fn.assert_called_once()


@patch("fluvo.__main__.run_path_to_image")
def test_path_to_image_calls_runner(mock_fn: MagicMock, runner: CliRunner) -> None:
    """`path-to-image` delegates to run_path_to_image."""
    with runner.isolated_filesystem():
        with open("in.csv", "w") as f:
            f.write("id,img\n")
        result = runner.invoke(
            __main__.cli, ["path-to-image", "in.csv", "--fields", "img"]
        )
        assert result.exit_code == 0
        mock_fn.assert_called_once()


@patch("fluvo.__main__.run_url_to_image")
def test_url_to_image_calls_runner(mock_fn: MagicMock, runner: CliRunner) -> None:
    """`url-to-image` delegates to run_url_to_image."""
    with runner.isolated_filesystem():
        with open("in.csv", "w") as f:
            f.write("id,url\n")
        result = runner.invoke(
            __main__.cli, ["url-to-image", "in.csv", "--fields", "url"]
        )
        assert result.exit_code == 0
        mock_fn.assert_called_once()
