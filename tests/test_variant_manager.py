"""Tests for the create-missing-variants workflow (#188)."""

from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from fluvo.__main__ import cli
from fluvo.lib.actions.variant_manager import (
    check_missing_variants_after_import,
    run_create_missing_variants,
)

CFG = "fluvo.lib.actions.variant_manager.conf_lib.get_connection_from_config"
CFG_DICT = "fluvo.lib.actions.variant_manager.conf_lib.get_connection_from_dict"


def _mock_conn(
    orphan_ids: list[int], create_side_effect: Optional[Exception] = None
) -> tuple[MagicMock, MagicMock, MagicMock]:
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
def test_no_orphans_returns_true_and_creates_nothing(mock_get: MagicMock) -> None:
    """No orphan templates -> returns True and creates nothing."""
    conn, _template, product = _mock_conn([])
    mock_get.return_value = conn
    assert run_create_missing_variants("conn.conf") is True
    product.create.assert_not_called()


@patch(CFG)
def test_creates_a_variant_per_orphan_template(mock_get: MagicMock) -> None:
    """Creates one product.product per orphan template, in one batch."""
    conn, _template, product = _mock_conn([1, 2, 3])
    mock_get.return_value = conn
    assert run_create_missing_variants("conn.conf") is True
    product.create.assert_called_once_with(
        [{"product_tmpl_id": 1}, {"product_tmpl_id": 2}, {"product_tmpl_id": 3}]
    )


@patch(CFG)
def test_extra_domain_is_combined_with_the_no_variant_filter(
    mock_get: MagicMock,
) -> None:
    """The extra domain is ANDed with the no-variant filter."""
    conn, template, _product = _mock_conn([1])
    mock_get.return_value = conn
    run_create_missing_variants("conn.conf", domain=[("categ_id", "=", 5)])
    template.search.assert_called_once_with(
        [("categ_id", "=", 5), ("product_variant_count", "=", 0)]
    )


@patch(CFG)
def test_dry_run_reports_without_creating(mock_get: MagicMock) -> None:
    """Dry run reports the count without creating any variant."""
    conn, _template, product = _mock_conn([1, 2])
    mock_get.return_value = conn
    assert run_create_missing_variants("conn.conf", dry_run=True) is True
    product.create.assert_not_called()


@patch(CFG)
def test_creates_are_batched(mock_get: MagicMock) -> None:
    """Creates are split into batches of batch_size."""
    conn, _template, product = _mock_conn([1, 2, 3, 4, 5])
    mock_get.return_value = conn
    run_create_missing_variants("conn.conf", batch_size=2)
    assert product.create.call_count == 3  # 2 + 2 + 1


@patch(CFG)
def test_connection_error_returns_false(mock_get: MagicMock) -> None:
    """A connection failure returns False."""
    mock_get.side_effect = Exception("boom")
    assert run_create_missing_variants("conn.conf") is False


@patch(CFG)
def test_create_failure_returns_false(mock_get: MagicMock) -> None:
    """A failing create batch returns False."""
    conn, _template, _product = _mock_conn([1, 2], create_side_effect=Exception("nope"))
    mock_get.return_value = conn
    assert run_create_missing_variants("conn.conf") is False


@patch(CFG)
def test_falls_back_to_individual_create_on_batch_failure(
    mock_get: MagicMock,
) -> None:
    """If batch create fails (Odoo < 14), fall back to one create() per template."""
    conn = MagicMock()
    template = MagicMock()
    product = MagicMock()
    template.search.return_value = [1, 2]

    def _create(vals: object) -> int:
        if isinstance(vals, list):
            raise Exception("Odoo < 14 does not support batch create")
        return 1

    product.create.side_effect = _create
    conn.get_model.side_effect = lambda m: (
        template if m == "product.template" else product
    )
    mock_get.return_value = conn

    assert run_create_missing_variants("conn.conf") is True
    # 1 failed batch attempt + 2 successful individual creates
    assert product.create.call_count == 3


@patch(CFG_DICT)
def test_accepts_a_dict_config(mock_get: MagicMock) -> None:
    """A dict config routes through get_connection_from_dict."""
    conn, _template, _product = _mock_conn([1])
    mock_get.return_value = conn
    assert run_create_missing_variants({"hostname": "h"}) is True
    mock_get.assert_called_once()


# --- CLI ---
def _conf(tmp_path: Path) -> str:
    p = tmp_path / "c.conf"
    p.write_text("[Connection]\nhostname=h\n")
    return str(p)


def test_cli_invokes_action_and_passes_options(tmp_path: Path) -> None:
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


def test_cli_exits_nonzero_when_action_fails(tmp_path: Path) -> None:
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


def test_cli_rejects_an_invalid_domain(tmp_path: Path) -> None:
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


# --- post-import guardrail (#188) ---
@patch(CFG)
def test_guardrail_skips_non_product_template(mock_get: MagicMock) -> None:
    """The guardrail is a no-op (no connection) for other models."""
    assert check_missing_variants_after_import("c.conf", "res.partner", {"x": 1}) == 0
    mock_get.assert_not_called()


def test_guardrail_skips_empty_id_map() -> None:
    """The guardrail is a no-op when nothing was imported."""
    assert check_missing_variants_after_import("c.conf", "product.template", {}) == 0


@patch(CFG)
def test_guardrail_returns_zero_when_no_orphans(mock_get: MagicMock) -> None:
    """No imported template lacks a variant -> returns 0, creates nothing."""
    conn, _t, product = _mock_conn([])
    mock_get.return_value = conn
    n = check_missing_variants_after_import("c.conf", "product.template", {"a": 1})
    assert n == 0
    product.create.assert_not_called()


@patch(CFG)
def test_guardrail_warns_only_by_default(mock_get: MagicMock) -> None:
    """With fix=False, orphans are reported but NOT created."""
    conn, _t, product = _mock_conn([10, 11])
    mock_get.return_value = conn
    n = check_missing_variants_after_import(
        "c.conf", "product.template", {"a": 10, "b": 11}
    )
    assert n == 2
    product.create.assert_not_called()


@patch(CFG)
def test_guardrail_fixes_when_requested(mock_get: MagicMock) -> None:
    """With fix=True, the missing variants are created."""
    conn, _t, product = _mock_conn([10, 11])
    mock_get.return_value = conn
    n = check_missing_variants_after_import(
        "c.conf", "product.template", {"a": 10, "b": 11}, fix=True
    )
    assert n == 2
    product.create.assert_called()


@patch(CFG)
def test_guardrail_swallows_connection_errors(mock_get: MagicMock) -> None:
    """A connection failure during the check returns 0 (never breaks the import)."""
    mock_get.side_effect = Exception("boom")
    assert (
        check_missing_variants_after_import("c.conf", "product.template", {"a": 1}) == 0
    )
