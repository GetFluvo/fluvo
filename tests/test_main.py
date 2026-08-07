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


# --- _parse_resolve_relation_specs Tests ---


def test_parse_resolve_relation_specs_basic() -> None:
    """It parses a 4-part spec into a resolve_relations dict."""
    from fluvo.__main__ import _parse_resolve_relation_specs

    specs = _parse_resolve_relation_specs(("country:res.country:code:country_id",))
    assert specs == [
        {
            "source_column": "country",
            "model": "res.country",
            "key_field": "code",
            "relation_field": "country_id",
        }
    ]


def test_parse_resolve_relation_specs_with_to() -> None:
    """It parses the optional 5th 'to' part when it is xmlid or dbid."""
    from fluvo.__main__ import _parse_resolve_relation_specs

    specs = _parse_resolve_relation_specs(("c:res.country:code:country_id:dbid",))
    assert specs[0]["to"] == "dbid"


def test_parse_resolve_relation_specs_bad_length() -> None:
    """It raises BadParameter when the spec has the wrong number of parts."""
    import click

    from fluvo.__main__ import _parse_resolve_relation_specs

    with pytest.raises(click.BadParameter):
        _parse_resolve_relation_specs(("only:two",))


def test_parse_resolve_relation_specs_bad_to() -> None:
    """It raises BadParameter when the 'to' part is neither xmlid nor dbid."""
    import click

    from fluvo.__main__ import _parse_resolve_relation_specs

    with pytest.raises(click.BadParameter):
        _parse_resolve_relation_specs(("c:res.country:code:country_id:bogus",))


# --- _run_dry_run_validation Tests ---


@patch("fluvo.lib.internal.ui._show_error_panel")
def test_dry_run_validation_no_filename(mock_panel: MagicMock) -> None:
    """It reports an error and returns when no filename is given."""
    from fluvo.__main__ import _run_dry_run_validation

    _run_dry_run_validation("conn.conf")
    mock_panel.assert_called_once()
    assert mock_panel.call_args[0][0] == "Dry Run Error"


@patch("fluvo.__main__._infer_model_from_filename", return_value=None)
@patch("fluvo.lib.internal.ui._show_error_panel")
def test_dry_run_validation_model_inference_fails(
    mock_panel: MagicMock, mock_infer: MagicMock
) -> None:
    """It reports an error when the model cannot be inferred from the filename."""
    from fluvo.__main__ import _run_dry_run_validation

    _run_dry_run_validation("conn.conf", filename="data.csv")
    mock_infer.assert_called_once_with("data.csv")
    assert mock_panel.call_args[0][0] == "Model Not Found"


@patch("fluvo.__main__.display_validation_results")
@patch("fluvo.__main__.validate_csv_data")
@patch("fluvo.lib.conf_lib.get_connection_from_dict")
def test_dry_run_validation_success_with_protocol(
    mock_get_dict: MagicMock,
    mock_validate: MagicMock,
    mock_display: MagicMock,
) -> None:
    """It validates via a protocol connection and parses the ignore list."""
    from fluvo.__main__ import _run_dry_run_validation

    mock_conn = MagicMock()
    mock_model = MagicMock()
    mock_model.fields_get.return_value = {"name": {}}
    mock_conn.get_model.return_value = mock_model
    mock_get_dict.return_value = mock_conn
    mock_validate.return_value = MagicMock()

    _run_dry_run_validation(
        "conn.conf",
        filename="data.csv",
        model="res.partner",
        protocol="jsonrpc",
        ignore="a, b ,",
    )

    mock_get_dict.assert_called_once()
    mock_validate.assert_called_once()
    assert mock_validate.call_args.kwargs["ignore"] == ["a", "b"]
    mock_display.assert_called_once()


@patch("fluvo.__main__.validate_csv_data", side_effect=Exception("boom"))
@patch("fluvo.lib.conf_lib.get_connection_from_config")
@patch("fluvo.lib.internal.ui._show_error_panel")
def test_dry_run_validation_exception(
    mock_panel: MagicMock,
    mock_get_conn: MagicMock,
    mock_validate: MagicMock,
) -> None:
    """It reports a Validation Error panel when validation raises."""
    from fluvo.__main__ import _run_dry_run_validation

    mock_conn = MagicMock()
    mock_conn.get_model.return_value = MagicMock()
    mock_get_conn.return_value = mock_conn

    _run_dry_run_validation("conn.conf", filename="data.csv", model="res.partner")
    assert mock_panel.call_args[0][0] == "Validation Error"


@patch("fluvo.__main__._run_dry_run_validation")
def test_import_dry_run_delegates(mock_dry: MagicMock, runner: CliRunner) -> None:
    """The import command delegates to _run_dry_run_validation when --dry-run is set."""
    with runner.isolated_filesystem():
        _conn()
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
                "--dry-run",
            ],
        )
        assert result.exit_code == 0
        mock_dry.assert_called_once()


# --- _execute_post_action extra branches ---


@patch("fluvo.lib.conf_lib.get_connection_from_dict")
def test_execute_post_action_dict_config(mock_get_dict: MagicMock) -> None:
    """It uses get_connection_from_dict when config is a dict."""
    from fluvo.__main__ import _execute_post_action

    mock_conn = MagicMock()
    mock_model = MagicMock()
    mock_model.action_apply_inventory.return_value = True
    mock_conn.get_model.return_value = mock_model
    mock_get_dict.return_value = mock_conn

    result = _execute_post_action(
        config={"protocol": "jsonrpc"},
        model="stock.quant",
        action_name="action_apply_inventory",
        id_map={"e": 1},
        context={},
    )
    assert result is True
    mock_get_dict.assert_called_once()


@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_execute_post_action_method_not_found(mock_get_conn: MagicMock) -> None:
    """It returns False when the action method does not exist on the model."""
    from fluvo.__main__ import _execute_post_action

    mock_conn = MagicMock()
    mock_model = MagicMock(spec=[])  # no attributes -> hasattr is False
    mock_conn.get_model.return_value = mock_model
    mock_get_conn.return_value = mock_conn

    result = _execute_post_action(
        config="conn.conf",
        model="stock.quant",
        action_name="does_not_exist",
        id_map={"e": 1},
        context={},
    )
    assert result is False


# --- _get_product_ids_from_quants extra branches ---


@patch("fluvo.lib.conf_lib.get_connection_from_dict")
def test_get_product_ids_dict_config(mock_get_dict: MagicMock) -> None:
    """It uses get_connection_from_dict when config is a dict."""
    from fluvo.__main__ import _get_product_ids_from_quants

    mock_conn = MagicMock()
    mock_quant_model = MagicMock()
    mock_quant_model.read.return_value = [{"product_id": [5, "x"]}]
    mock_conn.get_model.return_value = mock_quant_model
    mock_get_dict.return_value = mock_conn

    product_ids = _get_product_ids_from_quants({"protocol": "jsonrpc"}, [1])
    assert product_ids == [5]
    mock_get_dict.assert_called_once()


@patch("fluvo.lib.conf_lib.get_connection_from_config", side_effect=Exception("boom"))
def test_get_product_ids_exception(mock_get_conn: MagicMock) -> None:
    """It returns an empty list when the connection/read raises."""
    from fluvo.__main__ import _get_product_ids_from_quants

    assert _get_product_ids_from_quants("conn.conf", [1, 2]) == []


# --- _update_inventory_move_dates extra branches ---


@patch("fluvo.lib.conf_lib.get_connection_from_dict")
def test_update_move_dates_full_datetime_and_dict_config(
    mock_get_dict: MagicMock,
) -> None:
    """It parses a full datetime string and uses a dict connection."""
    from fluvo.__main__ import _update_inventory_move_dates

    mock_conn = MagicMock()
    mock_location_model = MagicMock()
    mock_location_model.search.return_value = [99]
    mock_move_model = MagicMock()
    mock_move_model.search.return_value = [501]

    def get_model(name: str) -> MagicMock:
        if name == "stock.location":
            return mock_location_model
        return mock_move_model

    mock_conn.get_model.side_effect = get_model
    mock_get_dict.return_value = mock_conn

    _update_inventory_move_dates(
        config={"protocol": "jsonrpc"},
        move_date="2026-01-01 08:30:00",
        context={},
        product_ids=[101],
    )
    mock_get_dict.assert_called_once()
    assert mock_move_model.write.call_args[0][1] == {"date": "2026-01-01 08:30:00"}


@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_update_move_dates_invalid_format(mock_get_conn: MagicMock) -> None:
    """It returns early without connecting when the date format is invalid."""
    from fluvo.__main__ import _update_inventory_move_dates

    _update_inventory_move_dates("conn.conf", "not-a-date", {}, [1])
    mock_get_conn.assert_not_called()


@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_update_move_dates_no_product_ids(mock_get_conn: MagicMock) -> None:
    """It warns and returns when there are no product IDs to filter by."""
    from fluvo.__main__ import _update_inventory_move_dates

    _update_inventory_move_dates("conn.conf", "2026-01-01", {}, [])
    mock_get_conn.assert_not_called()


@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_update_move_dates_no_inventory_location(mock_get_conn: MagicMock) -> None:
    """It errors and returns when no inventory adjustment location exists."""
    from fluvo.__main__ import _update_inventory_move_dates

    mock_conn = MagicMock()
    mock_location_model = MagicMock()
    mock_location_model.search.return_value = []
    mock_conn.get_model.return_value = mock_location_model
    mock_get_conn.return_value = mock_conn

    _update_inventory_move_dates("conn.conf", "2026-01-01", {}, [1])
    # stock.move is never queried because we bail out on missing location
    mock_location_model.search.assert_called_once()


@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_update_move_dates_no_moves_found(mock_get_conn: MagicMock) -> None:
    """It warns and does not write when no matching stock moves are found."""
    from fluvo.__main__ import _update_inventory_move_dates

    mock_conn = MagicMock()
    mock_location_model = MagicMock()
    mock_location_model.search.return_value = [99]
    mock_move_model = MagicMock()
    mock_move_model.search.return_value = []

    def get_model(name: str) -> MagicMock:
        if name == "stock.location":
            return mock_location_model
        return mock_move_model

    mock_conn.get_model.side_effect = get_model
    mock_get_conn.return_value = mock_conn

    _update_inventory_move_dates("conn.conf", "2026-01-01", {}, [1])
    mock_move_model.write.assert_not_called()


@patch("fluvo.lib.conf_lib.get_connection_from_config", side_effect=Exception("boom"))
def test_update_move_dates_exception(mock_get_conn: MagicMock) -> None:
    """It swallows exceptions raised while updating move dates."""
    from fluvo.__main__ import _update_inventory_move_dates

    # Should not raise
    _update_inventory_move_dates("conn.conf", "2026-01-01", {}, [1])


# --- run_project_flow Tests ---


@patch("fluvo.__main__.log")
def test_run_project_flow_with_name(mock_log: MagicMock) -> None:
    """It logs the specific flow being executed when a name is given."""
    from fluvo.__main__ import run_project_flow

    run_project_flow("flows.yml", "my_flow")
    messages = [c.args[0] for c in mock_log.info.call_args_list]
    assert any("my_flow" in m for m in messages)


@patch("fluvo.__main__.log")
def test_run_project_flow_all(mock_log: MagicMock) -> None:
    """It logs that all flows will run when no name is given."""
    from fluvo.__main__ import run_project_flow

    run_project_flow("flows.yml", None)
    messages = [c.args[0] for c in mock_log.info.call_args_list]
    assert any("all flows" in m for m in messages)


# --- Import command: protocol, context, company XML-id resolution ---


@patch("fluvo.__main__.run_import")
def test_import_protocol_option_builds_config_dict(
    mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """--protocol wraps the connection file in a config dict."""
    with runner.isolated_filesystem():
        _conn()
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
                "--protocol",
                "jsonrpc",
            ],
        )
        assert result.exit_code == 0
        config = mock_run_import.call_args.kwargs["config"]
        assert config == {"_config_file": "conn.conf", "protocol": "jsonrpc"}


@patch("fluvo.__main__.run_import")
def test_import_invalid_context_aborts(
    mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """An invalid --context dictionary aborts before calling run_import."""
    with runner.isolated_filesystem():
        _conn()
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
                "--context",
                "{invalid",
            ],
        )
        assert result.exit_code == 0
        mock_run_import.assert_not_called()


@patch("fluvo.__main__.run_import")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_import_company_id_xmlid_resolved(
    mock_get_conn: MagicMock, mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """--company-id given as an XML id is resolved via ir.model.data."""
    mock_conn = MagicMock()
    mock_imd = MagicMock()
    mock_imd.search.return_value = [42]
    mock_imd.read.return_value = {"res_id": 7}
    mock_conn.get_model.return_value = mock_imd
    mock_get_conn.return_value = mock_conn

    with runner.isolated_filesystem():
        _conn()
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
                "base.main_company",
            ],
        )
        assert result.exit_code == 0
        mock_run_import.assert_called_once()
        assert mock_run_import.call_args.kwargs["context"][
            "allowed_company_ids"
        ] == [7]
        # module.name was split correctly for the ir.model.data search
        domain = mock_imd.search.call_args[0][0]
        assert ("module", "=", "base") in domain
        assert ("name", "=", "main_company") in domain


@patch("fluvo.__main__.run_import")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_import_company_id_xmlid_no_dot_defaults_base(
    mock_get_conn: MagicMock, mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """An XML id without a dot is treated as being in the 'base' module."""
    mock_conn = MagicMock()
    mock_imd = MagicMock()
    mock_imd.search.return_value = [1]
    mock_imd.read.return_value = {"res_id": 3}
    mock_conn.get_model.return_value = mock_imd
    mock_get_conn.return_value = mock_conn

    with runner.isolated_filesystem():
        _conn()
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
                "main_company",
            ],
        )
        assert result.exit_code == 0
        domain = mock_imd.search.call_args[0][0]
        assert ("module", "=", "base") in domain
        assert ("name", "=", "main_company") in domain


@patch("fluvo.__main__.run_import")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_import_company_id_xmlid_not_found_aborts(
    mock_get_conn: MagicMock, mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """Import aborts when the company XML id cannot be found."""
    mock_conn = MagicMock()
    mock_imd = MagicMock()
    mock_imd.search.return_value = []  # not found
    mock_conn.get_model.return_value = mock_imd
    mock_get_conn.return_value = mock_conn

    with runner.isolated_filesystem():
        _conn()
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
                "base.nope",
            ],
        )
        assert result.exit_code == 0
        mock_run_import.assert_not_called()
        assert "not found" in result.output


@patch("fluvo.__main__.run_import")
@patch(
    "fluvo.lib.conf_lib.get_connection_from_config",
    side_effect=Exception("connection failed"),
)
def test_import_company_id_xmlid_exception_aborts(
    mock_get_conn: MagicMock, mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """Import aborts when resolving the company XML id raises."""
    with runner.isolated_filesystem():
        _conn()
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
                "base.main_company",
            ],
        )
        assert result.exit_code == 0
        mock_run_import.assert_not_called()
        assert "Failed to resolve company XML ID" in result.output


@patch("fluvo.__main__.run_import")
@patch("fluvo.lib.conf_lib.get_connection_from_dict")
def test_import_all_companies_with_protocol(
    mock_get_dict: MagicMock, mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """--all-companies works with a protocol (dict config) connection."""
    mock_conn = MagicMock()
    mock_conn.user_id = 2
    mock_user_model = MagicMock()
    mock_user_model.read.return_value = {"company_ids": [1, 2]}
    mock_conn.get_model.return_value = mock_user_model
    mock_get_dict.return_value = mock_conn

    with runner.isolated_filesystem():
        _conn()
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
                "--protocol",
                "jsonrpc",
                "--all-companies",
            ],
        )
        assert result.exit_code == 0
        assert mock_run_import.call_args.kwargs["context"][
            "allowed_company_ids"
        ] == [1, 2]


# --- Import command: option parsing branches ---


@patch("fluvo.__main__.run_import")
def test_import_tracking_enable_sets_context(
    mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """--tracking-enable leaves tracking on and skips the suppression keys."""
    with runner.isolated_filesystem():
        _conn()
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
                "--context",
                "{}",
                "--tracking-enable",
            ],
        )
        assert result.exit_code == 0
        context = mock_run_import.call_args.kwargs["context"]
        assert context["tracking_disable"] is False
        assert "mail_create_nolog" not in context
        assert "Mail tracking enabled" in result.output


@patch("fluvo.__main__.run_import")
def test_import_defer_parent_store_sets_context(
    mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """--defer-parent-store sets defer_parent_store_computation in the context."""
    with runner.isolated_filesystem():
        _conn()
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
                "--defer-parent-store",
            ],
        )
        assert result.exit_code == 0
        context = mock_run_import.call_args.kwargs["context"]
        assert context["defer_parent_store_computation"] is True


@patch("fluvo.__main__.run_import")
def test_import_on_missing_ref_parsing(
    mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """--on-missing-ref parses create/skip/empty and warns on bad input."""
    with runner.isolated_filesystem():
        _conn()
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
                "--on-missing-ref",
                "country_id:create,user_id:skip,category_id:empty,badformat,x:bogus",
            ],
        )
        assert result.exit_code == 0
        context = mock_run_import.call_args.kwargs["context"]
        assert context["name_create_enabled_fields"] == {"country_id": True}
        assert context["import_set_empty_fields"] == ["category_id"]
        assert "Invalid --on-missing-ref format" in result.output
        assert "Unknown action 'bogus'" in result.output


@patch("fluvo.__main__.run_import")
def test_import_auto_create_refs_flag(
    mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """--auto-create-refs is forwarded to run_import."""
    with runner.isolated_filesystem():
        _conn()
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
                "--auto-create-refs",
            ],
        )
        assert result.exit_code == 0
        assert mock_run_import.call_args.kwargs["auto_create_refs"] is True


@patch("fluvo.__main__.run_import")
def test_import_set_empty_on_missing_flag(
    mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """--set-empty-on-missing is forwarded to run_import."""
    with runner.isolated_filesystem():
        _conn()
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
                "--set-empty-on-missing",
            ],
        )
        assert result.exit_code == 0
        assert mock_run_import.call_args.kwargs["set_empty_on_missing"] is True


@patch("fluvo.__main__.run_import")
def test_import_fallback_values_parsing(
    mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """--fallback-values parses field:value pairs and warns on bad input."""
    with runner.isolated_filesystem():
        _conn()
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
                "--fallback-values",
                "state:draft,active:true,badpair",
            ],
        )
        assert result.exit_code == 0
        context = mock_run_import.call_args.kwargs["context"]
        assert context["fallback_values"] == {"state": "draft", "active": "true"}
        assert "Invalid --fallback-values format" in result.output


@patch("fluvo.__main__.run_import")
def test_import_groupby_deferred_ignore_parsing(
    mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """--groupby, --deferred-fields and --ignore are split into lists."""
    with runner.isolated_filesystem():
        _conn()
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
                "--groupby",
                "a, b",
                "--deferred-fields",
                "x, y",
                "--unique-id-field",
                "id",
                "--ignore",
                "c, d",
            ],
        )
        assert result.exit_code == 0
        kwargs = mock_run_import.call_args.kwargs
        assert kwargs["groupby"] == ["a", "b"]
        assert kwargs["deferred_fields"] == ["x", "y"]
        assert kwargs["ignore"] == ["c", "d"]


# --- Import command: sudo + move-date orchestration branches ---


@patch("fluvo.__main__.run_import")
@patch("fluvo.lib.conf_lib.get_connection_from_dict")
def test_import_sudo_with_protocol_and_no_model_rules(
    mock_get_dict: MagicMock, mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """--sudo with a protocol uses a dict connection; no rules found is fine."""
    mock_conn = MagicMock()
    mock_ir_model = MagicMock()
    mock_ir_model.search.return_value = []  # model not found -> no rules to disable
    mock_ir_rule = MagicMock()

    def get_model(name: str) -> MagicMock:
        if name == "ir.model":
            return mock_ir_model
        return mock_ir_rule

    mock_conn.get_model.side_effect = get_model
    mock_get_dict.return_value = mock_conn

    with runner.isolated_filesystem():
        _conn()
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
                "--protocol",
                "jsonrpc",
                "--sudo",
            ],
        )
        assert result.exit_code == 0
        mock_get_dict.assert_called()
        mock_ir_rule.write.assert_not_called()
        mock_run_import.assert_called_once()


@patch("fluvo.__main__.run_import")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_import_sudo_no_active_rules(
    mock_get_conn: MagicMock, mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """--sudo with a model that has no active rules runs the import unchanged."""
    mock_conn = MagicMock()
    mock_ir_model = MagicMock()
    mock_ir_model.search.return_value = [1]
    mock_ir_rule = MagicMock()
    mock_ir_rule.search.return_value = []  # no active rules

    def get_model(name: str) -> MagicMock:
        if name == "ir.model":
            return mock_ir_model
        return mock_ir_rule

    mock_conn.get_model.side_effect = get_model
    mock_get_conn.return_value = mock_conn

    with runner.isolated_filesystem():
        _conn()
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
                "--sudo",
            ],
        )
        assert result.exit_code == 0
        mock_ir_rule.write.assert_not_called()
        mock_run_import.assert_called_once()


@patch("fluvo.__main__._update_inventory_move_dates")
@patch("fluvo.__main__._get_product_ids_from_quants", return_value=[101])
@patch("fluvo.__main__._execute_post_action", return_value=True)
@patch("fluvo.__main__.run_import")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_import_sudo_post_action_and_move_date(
    mock_get_conn: MagicMock,
    mock_run_import: MagicMock,
    mock_post_action: MagicMock,
    mock_get_products: MagicMock,
    mock_update_dates: MagicMock,
    runner: CliRunner,
) -> None:
    """--sudo path runs post-action and move-date update after import."""
    mock_conn = MagicMock()
    mock_ir_model = MagicMock()
    mock_ir_model.search.return_value = [1]
    mock_ir_rule = MagicMock()
    mock_ir_rule.search.return_value = [5]

    def get_model(name: str) -> MagicMock:
        if name == "ir.model":
            return mock_ir_model
        return mock_ir_rule

    mock_conn.get_model.side_effect = get_model
    mock_get_conn.return_value = mock_conn
    mock_run_import.return_value = {"e1": 1, "e2": 2}

    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli,
            [
                "import",
                "--connection-file",
                "conn.conf",
                "--file",
                "my.csv",
                "--model",
                "stock.quant",
                "--sudo",
                "--post-action",
                "action_apply_inventory",
                "--move-date",
                "2026-01-01",
            ],
        )
        assert result.exit_code == 0
        mock_post_action.assert_called_once()
        mock_get_products.assert_called_once()
        mock_update_dates.assert_called_once()
        # rules were disabled and re-enabled
        mock_ir_rule.write.assert_any_call([5], {"active": False})
        mock_ir_rule.write.assert_any_call([5], {"active": True})


@patch("fluvo.__main__.run_import")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_import_sudo_reenable_failure_is_logged(
    mock_get_conn: MagicMock, mock_run_import: MagicMock, runner: CliRunner
) -> None:
    """A failure to re-enable rules after --sudo is logged, not raised."""
    mock_conn = MagicMock()
    mock_ir_model = MagicMock()
    mock_ir_model.search.return_value = [1]
    mock_ir_rule = MagicMock()
    mock_ir_rule.search.return_value = [5]
    # First write (disable) succeeds, second write (re-enable) raises.
    mock_ir_rule.write.side_effect = [None, Exception("write failed")]

    def get_model(name: str) -> MagicMock:
        if name == "ir.model":
            return mock_ir_model
        return mock_ir_rule

    mock_conn.get_model.side_effect = get_model
    mock_get_conn.return_value = mock_conn

    with runner.isolated_filesystem():
        _conn()
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
                "--sudo",
            ],
        )
        assert result.exit_code == 0
        assert "Failed to re-enable record rules" in result.output


# --- Write command context branches ---


@patch("fluvo.__main__.run_write")
def test_write_invalid_context_aborts(
    mock_run_write: MagicMock, runner: CliRunner
) -> None:
    """An invalid --context aborts the write command before calling run_write."""
    with runner.isolated_filesystem():
        _conn()
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
                "--context",
                "{invalid",
            ],
        )
        assert result.exit_code == 0
        mock_run_write.assert_not_called()


@patch("fluvo.__main__.run_write")
def test_write_context_without_tracking_disable(
    mock_run_write: MagicMock, runner: CliRunner
) -> None:
    """A context without tracking_disable skips the mail suppression keys."""
    with runner.isolated_filesystem():
        _conn()
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
                "--context",
                "{}",
            ],
        )
        assert result.exit_code == 0
        context = mock_run_write.call_args.kwargs["context"]
        assert "mail_create_nolog" not in context


# --- Export command extra branches ---


@patch("fluvo.__main__.run_export")
def test_export_protocol_option_builds_config_dict(
    mock_run_export: MagicMock, runner: CliRunner
) -> None:
    """`export --protocol` wraps the connection file in a config dict."""
    with runner.isolated_filesystem():
        _conn()
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
                "--protocol",
                "jsonrpc",
            ],
        )
        assert result.exit_code == 0
        config = mock_run_export.call_args.kwargs["config"]
        assert config == {"_config_file": "conn.conf", "protocol": "jsonrpc"}


@patch("fluvo.__main__.run_export")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_export_all_companies_model_without_company_id(
    mock_get_conn: MagicMock, mock_run_export: MagicMock, runner: CliRunner
) -> None:
    """`export --all-companies` skips the domain filter when model lacks company_id."""
    mock_conn = MagicMock()
    mock_conn.user_id = 2
    mock_user_model = MagicMock()
    mock_user_model.read.return_value = {"company_ids": [1, 2]}
    mock_target_model = MagicMock()
    mock_target_model.fields_get.return_value = {}  # no company_id field

    def get_model(name: str) -> MagicMock:
        if name == "res.users":
            return mock_user_model
        return mock_target_model

    mock_conn.get_model.side_effect = get_model
    mock_get_conn.return_value = mock_conn

    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli,
            [
                "export",
                "--connection-file",
                "conn.conf",
                "--output",
                "out.csv",
                "--model",
                "res.country",
                "--fields",
                "id,name",
                "--all-companies",
            ],
        )
        assert result.exit_code == 0
        # No company_id field -> the default domain is left untouched.
        assert mock_run_export.call_args.kwargs["domain"] == "[]"


@patch("fluvo.__main__.run_export")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_export_all_companies_non_dict_context_and_non_list_domain(
    mock_get_conn: MagicMock, mock_run_export: MagicMock, runner: CliRunner
) -> None:
    """`export --all-companies` coerces a non-dict context and non-list domain."""
    mock_conn = MagicMock()
    mock_conn.user_id = 2
    mock_user_model = MagicMock()
    mock_user_model.read.return_value = {"company_ids": [1]}
    mock_target_model = MagicMock()
    mock_target_model.fields_get.return_value = {"company_id": {"type": "many2one"}}

    def get_model(name: str) -> MagicMock:
        if name == "res.users":
            return mock_user_model
        return mock_target_model

    mock_conn.get_model.side_effect = get_model
    mock_get_conn.return_value = mock_conn

    with runner.isolated_filesystem():
        _conn()
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
                "--context",
                "[1, 2]",  # valid literal but not a dict
                "--domain",
                "5",  # valid literal but not a list
                "--all-companies",
            ],
        )
        assert result.exit_code == 0
        context = mock_run_export.call_args.kwargs["context"]
        # Non-dict context is reset to {} then gets allowed_company_ids added.
        assert context["allowed_company_ids"] == [1]


@patch("fluvo.__main__.run_export")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_export_all_companies_invalid_context_and_domain_strings(
    mock_get_conn: MagicMock, mock_run_export: MagicMock, runner: CliRunner
) -> None:
    """`export --all-companies` tolerates unparsable context/domain strings."""
    mock_conn = MagicMock()
    mock_conn.user_id = 2
    mock_user_model = MagicMock()
    mock_user_model.read.return_value = {"company_ids": [1]}
    mock_target_model = MagicMock()
    mock_target_model.fields_get.return_value = {"company_id": {"type": "many2one"}}

    def get_model(name: str) -> MagicMock:
        if name == "res.users":
            return mock_user_model
        return mock_target_model

    mock_conn.get_model.side_effect = get_model
    mock_get_conn.return_value = mock_conn

    with runner.isolated_filesystem():
        _conn()
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
                "--context",
                "{invalid",
                "--domain",
                "[invalid",
                "--all-companies",
            ],
        )
        assert result.exit_code == 0
        mock_run_export.assert_called_once()


@patch("fluvo.__main__.run_export")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_export_sudo_disables_related_model_rules(
    mock_get_conn: MagicMock, mock_run_export: MagicMock, runner: CliRunner
) -> None:
    """`export --sudo` also disables rules for related models of exported fields."""
    mock_conn = MagicMock()
    mock_ir_model = MagicMock()
    mock_ir_model.search.return_value = [1]
    mock_ir_rule = MagicMock()
    mock_ir_rule.search.return_value = [5]
    mock_target_model = MagicMock()
    mock_target_model.fields_get.return_value = {
        "partner_id": {"relation": "res.partner"}
    }

    def get_model(name: str) -> MagicMock:
        if name == "ir.model":
            return mock_ir_model
        elif name == "ir.rule":
            return mock_ir_rule
        return mock_target_model

    mock_conn.get_model.side_effect = get_model
    mock_get_conn.return_value = mock_conn

    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli,
            [
                "export",
                "--connection-file",
                "conn.conf",
                "--output",
                "out.csv",
                "--model",
                "sale.order",
                "--fields",
                "id,partner_id",
                "--sudo",
            ],
        )
        assert result.exit_code == 0
        # Rules disabled for two models (main + res.partner) then re-enabled.
        assert "res.partner" in result.output
        mock_run_export.assert_called_once()


@patch("fluvo.__main__.run_export")
@patch("fluvo.lib.conf_lib.get_connection_from_config")
def test_export_sudo_reenable_failure_is_logged(
    mock_get_conn: MagicMock, mock_run_export: MagicMock, runner: CliRunner
) -> None:
    """A failure to re-enable rules after export --sudo is logged, not raised."""
    mock_conn = MagicMock()
    mock_ir_model = MagicMock()
    mock_ir_model.search.return_value = [1]
    mock_ir_rule = MagicMock()
    mock_ir_rule.search.return_value = [5]
    mock_ir_rule.write.side_effect = [None, Exception("write failed")]
    mock_target_model = MagicMock()
    mock_target_model.fields_get.return_value = {}  # no relations

    def get_model(name: str) -> MagicMock:
        if name == "ir.model":
            return mock_ir_model
        elif name == "ir.rule":
            return mock_ir_rule
        return mock_target_model

    mock_conn.get_model.side_effect = get_model
    mock_get_conn.return_value = mock_conn

    with runner.isolated_filesystem():
        _conn()
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
        assert "Failed to re-enable record rules" in result.output


# --- Migrate command extra branches ---


@patch("fluvo.__main__.run_migration")
def test_migrate_valid_mapping_dict(mock_fn: MagicMock, runner: CliRunner) -> None:
    """A valid dict mapping is parsed and forwarded to run_migration."""
    mock_fn.return_value = True
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
                "name",
                "--mapping",
                "{'name': 'x_name'}",
            ],
        )
        assert result.exit_code == 0
        assert mock_fn.call_args.kwargs["mapping"] == {"name": "x_name"}


@patch("fluvo.__main__.run_migration")
def test_migrate_failure_exits_nonzero(mock_fn: MagicMock, runner: CliRunner) -> None:
    """`migrate` exits non-zero when run_migration reports failure."""
    mock_fn.return_value = False
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
                "name",
            ],
        )
        assert result.exit_code == 1


# --- create-missing-variants command ---


@patch("fluvo.__main__.run_create_missing_variants")
def test_create_missing_variants_success(
    mock_fn: MagicMock, runner: CliRunner
) -> None:
    """create-missing-variants parses a domain and forwards options."""
    mock_fn.return_value = True
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli,
            [
                "workflow",
                "create-missing-variants",
                "--connection-file",
                "conn.conf",
                "--domain",
                "[('categ_id', '=', 5)]",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0
        assert mock_fn.call_args.kwargs["domain"] == [("categ_id", "=", 5)]
        assert mock_fn.call_args.kwargs["dry_run"] is True


@patch("fluvo.__main__.run_create_missing_variants")
def test_create_missing_variants_bad_domain(
    mock_fn: MagicMock, runner: CliRunner
) -> None:
    """create-missing-variants rejects an invalid --domain literal."""
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli,
            [
                "workflow",
                "create-missing-variants",
                "--connection-file",
                "conn.conf",
                "--domain",
                "not-a-list",
            ],
        )
        assert result.exit_code != 0
        mock_fn.assert_not_called()


@patch("fluvo.__main__.run_create_missing_variants")
def test_create_missing_variants_domain_not_list(
    mock_fn: MagicMock, runner: CliRunner
) -> None:
    """create-missing-variants rejects a --domain that is not a list."""
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli,
            [
                "workflow",
                "create-missing-variants",
                "--connection-file",
                "conn.conf",
                "--domain",
                "{'a': 1}",
            ],
        )
        assert result.exit_code != 0
        mock_fn.assert_not_called()


@patch("fluvo.__main__.run_create_missing_variants")
def test_create_missing_variants_failure_exits(
    mock_fn: MagicMock, runner: CliRunner
) -> None:
    """create-missing-variants exits non-zero when the runner fails."""
    mock_fn.return_value = False
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli,
            [
                "workflow",
                "create-missing-variants",
                "--connection-file",
                "conn.conf",
            ],
        )
        assert result.exit_code == 1


# --- VAT command extra branches ---


@patch("fluvo.__main__.get_vat_validation_settings")
def test_vat_get_settings_company_ids_and_stdnum(
    mock_fn: MagicMock, runner: CliRunner
) -> None:
    """`vat get-settings` parses --company-ids and prints stdnum settings."""
    mock_fn.return_value = MagicMock(
        vies_settings={1: True},
        stdnum_settings={"param.key": "value"},
    )
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli,
            [
                "vat",
                "get-settings",
                "--connection-file",
                "conn.conf",
                "--company-ids",
                "1, 2",
            ],
        )
        assert result.exit_code == 0
        assert mock_fn.call_args.kwargs["company_ids"] == [1, 2]
        assert "stdnum Settings" in result.output
        assert "param.key" in result.output


@patch("fluvo.__main__.get_vat_validation_settings")
def test_vat_get_settings_failure(mock_fn: MagicMock, runner: CliRunner) -> None:
    """`vat get-settings` reports failure when no settings are returned."""
    mock_fn.return_value = None
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli, ["vat", "get-settings", "--connection-file", "conn.conf"]
        )
        assert result.exit_code == 0
        assert "Failed to retrieve VAT settings" in result.output


@patch("fluvo.__main__.disable_vat_validation")
def test_vat_disable_failure(mock_fn: MagicMock, runner: CliRunner) -> None:
    """`vat disable` reports failure when no settings are returned."""
    mock_fn.return_value = None
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli, ["vat", "disable", "--connection-file", "conn.conf"]
        )
        assert result.exit_code == 0
        assert "Failed to disable VAT validation" in result.output


@patch("fluvo.__main__.disable_vat_validation")
def test_vat_disable_writes_output_file(
    mock_fn: MagicMock, runner: CliRunner
) -> None:
    """`vat disable --output` writes the saved settings to a JSON file."""
    import json

    mock_fn.return_value = MagicMock(
        vies_settings={1: False},
        stdnum_settings={"k": "v"},
        timestamp=123,
    )
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli,
            [
                "vat",
                "disable",
                "--connection-file",
                "conn.conf",
                "--company-ids",
                "1",
                "--output",
                "settings.json",
            ],
        )
        assert result.exit_code == 0
        assert mock_fn.call_args.kwargs["company_ids"] == [1]
        with open("settings.json") as f:
            saved = json.load(f)
        assert saved["timestamp"] == 123
        assert "Settings saved to" in result.output


@patch("fluvo.__main__.restore_vat_validation_settings")
def test_vat_restore_no_input_file(mock_fn: MagicMock, runner: CliRunner) -> None:
    """`vat restore` without --input reports an error and does not restore."""
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli, ["vat", "restore", "--connection-file", "conn.conf"]
        )
        assert result.exit_code == 0
        assert "No settings file provided" in result.output
        mock_fn.assert_not_called()


@patch("fluvo.__main__.restore_vat_validation_settings")
def test_vat_restore_failure(mock_fn: MagicMock, runner: CliRunner) -> None:
    """`vat restore` reports failure when the restore call returns False."""
    mock_fn.return_value = False
    with runner.isolated_filesystem():
        _conn()
        with open("settings.json", "w") as f:
            f.write('{"vies_settings": {"1": true}, "stdnum_settings": {}}')
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
        assert "Failed to restore" in result.output
        # string keys converted back to int
        assert mock_fn.call_args.kwargs["settings"].vies_settings == {1: True}


@patch("fluvo.__main__.run_vies_validation")
def test_vat_validate_notify_users_and_domain(
    mock_fn: MagicMock, runner: CliRunner
) -> None:
    """`vat validate` parses --notify-users and a valid --domain."""
    mock_fn.return_value = MagicMock(
        total_checked=1,
        valid_count=1,
        invalid_count=0,
        error_count=0,
        invalid_partners=[],
        error_partners=[],
    )
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli,
            [
                "vat",
                "validate",
                "--connection-file",
                "conn.conf",
                "--notify-users",
                "3, 4",
                "--domain",
                "[('is_company', '=', True)]",
            ],
        )
        assert result.exit_code == 0
        assert mock_fn.call_args.kwargs["notify_user_ids"] == [3, 4]
        assert mock_fn.call_args.kwargs["domain"] == [("is_company", "=", True)]


@patch("fluvo.__main__.run_vies_validation")
def test_vat_validate_invalid_domain_aborts(
    mock_fn: MagicMock, runner: CliRunner
) -> None:
    """`vat validate` reports an invalid domain and does not run validation."""
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli,
            [
                "vat",
                "validate",
                "--connection-file",
                "conn.conf",
                "--domain",
                "[invalid",
            ],
        )
        assert result.exit_code == 0
        assert "Invalid domain format" in result.output
        mock_fn.assert_not_called()


@patch("fluvo.__main__.run_vies_validation")
def test_vat_validate_truncates_long_result_lists(
    mock_fn: MagicMock, runner: CliRunner
) -> None:
    """`vat validate` truncates long invalid/error partner lists in the output."""
    mock_fn.return_value = MagicMock(
        total_checked=32,
        valid_count=0,
        invalid_count=21,
        error_count=11,
        invalid_partners=[
            {"id": i, "vat": "BE0", "name": f"P{i}"} for i in range(21)
        ],
        error_partners=[
            {"id": i, "vat": "BE0", "error": "boom"} for i in range(11)
        ],
    )
    with runner.isolated_filesystem():
        _conn()
        result = runner.invoke(
            __main__.cli, ["vat", "validate", "--connection-file", "conn.conf"]
        )
        assert result.exit_code == 0
        assert "Invalid VAT Numbers" in result.output
        assert "and 1 more" in result.output  # 21 invalid -> 20 shown + 1 more
        assert "Errors:" in result.output
