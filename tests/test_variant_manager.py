"""Tests for the create-missing-variants workflow (#188)."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from fluvo.__main__ import cli
from fluvo.lib.actions.variant_manager import run_create_missing_variants

CFG = "fluvo.lib.actions.variant_manager.conf_lib.get_connection_from_config"
CFG_DICT = "fluvo.lib.actions.variant_manager.conf_lib.get_connection_from_dict"


def _mock_conn(orphan_ids, create_side_effect=None):
    """Build a mock connection whose product.template.search returns orphan_ids."""
    conn = MagicMock()
    template = MagicMock()
    product = MagicMock()
    template.search.return_value = orphan_ids
    if create_side_effect is not None:
        product.create.side_effect = create_side_effect
    conn.get_model.side_effect = lambda m: (
        template if m == "product.template" else product
    )
    return conn, template, product


@patch(CFG)
def test_no_orphans_returns_true_and_creates_nothing(mock_get):
    """No orphan templates -> returns True and creates nothing."""
    conn, _template, product = _mock_conn([])
    mock_get.return_value = conn
    assert run_create_missing_variants("conn.conf") is True
    product.create.assert_not_called()


@patch(CFG)
def test_creates_a_variant_per_orphan_template(mock_get):
    """Creates one product.product per orphan template, in one batch."""
    conn, _template, product = _mock_conn([1, 2, 3])
    mock_get.return_value = conn
    assert run_create_missing_variants("conn.conf") is True
    product.create.assert_called_once_with(
        [{"product_tmpl_id": 1}, {"product_tmpl_id": 2}, {"product_tmpl_id": 3}]
    )


@patch(CFG)
def test_extra_domain_is_combined_with_the_no_variant_filter(mock_get):
    """The extra domain is ANDed with the no-variant filter."""
    conn, template, _product = _mock_conn([1])
    mock_get.return_value = conn
    run_create_missing_variants("conn.conf", domain=[("categ_id", "=", 5)])
    template.search.assert_called_once_with(
        [("categ_id", "=", 5), ("product_variant_count", "=", 0)]
    )


@patch(CFG)
def test_dry_run_reports_without_creating(mock_get):
    """Dry run reports the count without creating any variant."""
    conn, _template, product = _mock_conn([1, 2])
    mock_get.return_value = conn
    assert run_create_missing_variants("conn.conf", dry_run=True) is True
    product.create.assert_not_called()


@patch(CFG)
def test_creates_are_batched(mock_get):
    """Creates are split into batches of batch_size."""
    conn, _template, product = _mock_conn([1, 2, 3, 4, 5])
    mock_get.return_value = conn
    run_create_missing_variants("conn.conf", batch_size=2)
    assert product.create.call_count == 3  # 2 + 2 + 1


@patch(CFG)
def test_connection_error_returns_false(mock_get):
    """A connection failure returns False."""
    mock_get.side_effect = Exception("boom")
    assert run_create_missing_variants("conn.conf") is False


@patch(CFG)
def test_create_failure_returns_false(mock_get):
    """A failing create batch returns False."""
    conn, _template, _product = _mock_conn([1, 2], create_side_effect=Exception("nope"))
    mock_get.return_value = conn
    assert run_create_missing_variants("conn.conf") is False


@patch(CFG_DICT)
def test_accepts_a_dict_config(mock_get):
    """A dict config routes through get_connection_from_dict."""
    conn, _template, _product = _mock_conn([1])
    mock_get.return_value = conn
    assert run_create_missing_variants({"hostname": "h"}) is True
    mock_get.assert_called_once()


# --- CLI ---
def _conf(tmp_path):
    p = tmp_path / "c.conf"
    p.write_text("[Connection]\nhostname=h\n")
    return str(p)


def test_cli_invokes_action_and_passes_options(tmp_path):
    """The CLI invokes the action with the parsed options."""
    runner = CliRunner()
    with patch(
        "fluvo.__main__.run_create_missing_variants", return_value=True
    ) as action:
        result = runner.invoke(
            cli,
            [
                "workflow",
                "create-missing-variants",
                "--connection-file",
                _conf(tmp_path),
                "--domain",
                "[('categ_id', '=', 5)]",
                "--batch-size",
                "50",
                "--dry-run",
            ],
        )
    assert result.exit_code == 0
    action.assert_called_once_with(
        config=_conf(tmp_path),
        domain=[("categ_id", "=", 5)],
        batch_size=50,
        dry_run=True,
    )


def test_cli_exits_nonzero_when_action_fails(tmp_path):
    """The CLI exits non-zero when the action returns False."""
    runner = CliRunner()
    with patch("fluvo.__main__.run_create_missing_variants", return_value=False):
        result = runner.invoke(
            cli,
            [
                "workflow",
                "create-missing-variants",
                "--connection-file",
                _conf(tmp_path),
            ],
        )
    assert result.exit_code == 1


def test_cli_rejects_an_invalid_domain(tmp_path):
    """The CLI rejects a --domain that is not a valid literal."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "workflow",
            "create-missing-variants",
            "--connection-file",
            _conf(tmp_path),
            "--domain",
            "not-a-valid-literal",
        ],
    )
    assert result.exit_code != 0
    assert "Invalid --domain" in result.output
