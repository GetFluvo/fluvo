"""Additional tests for final coverage push."""

import os
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch

from odoo_data_flow.import_threaded import import_data
from odoo_data_flow.importer import run_import


def test_import_data_force_create_path() -> None:
    """Test import_data with force_create=True to cover that branch."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        tmp.write("id,name\n1,Alice\n2,Bob\n")
        csv_path = tmp.name

    try:
        with patch("odoo_data_flow.import_threaded._read_data_file") as mock_read:
            mock_read.return_value = (["id", "name"], [["1", "Alice"], ["2", "Bob"]])

            with patch(
                "odoo_data_flow.lib.conf_lib.get_connection_from_dict"
            ) as mock_get_conn:
                mock_connection = MagicMock()
                mock_get_conn.return_value = mock_connection
                mock_model = MagicMock()
                mock_connection.get_model.return_value = mock_model

                with patch(
                    "odoo_data_flow.import_threaded._orchestrate_pass_1"
                ) as mock_pass_1:
                    mock_pass_1.return_value = {
                        "success": True,
                        "id_map": {"1": 101, "2": 102},
                    }

                    # Call with force_create=True to cover that path
                    success, _stats = import_data(
                        config={
                            "hostname": "localhost",
                            "database": "test",
                            "login": "admin",
                            "password": "admin",
                        },
                        model="res.partner",
                        unique_id_field="id",
                        file_csv=csv_path,
                        deferred_fields=None,
                        context={"tracking_disable": True},
                        fail_file=None,
                        encoding="utf-8",
                        separator=",",
                        ignore=[],
                        max_connection=1,
                        batch_size=5,
                        skip=0,
                        force_create=True,  # This is the key - to cover the force_create path
                        o2m=False,
                        split_by_cols=None,
                    )

                    assert success is True
    finally:
        os.unlink(csv_path)


def test_importer_with_sorted_strategy() -> None:
    """Test importer with sorted strategy to cover that path."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        tmp.write("id,name,parent_id\n1,Alice,0\n2,Bob,1\n")
        csv_path = tmp.name

    try:
        with patch("odoo_data_flow.importer._count_lines", return_value=3):
            with patch(
                "odoo_data_flow.importer._run_preflight_checks"
            ) as mock_preflight:

                def preflight_side_effect(*args: Any, **kwargs: Any) -> bool:
                    kwargs["import_plan"]["strategy"] = "sort_and_one_pass_load"
                    kwargs["import_plan"]["id_column"] = "id"
                    kwargs["import_plan"]["parent_column"] = "parent_id"
                    return True

                mock_preflight.side_effect = preflight_side_effect

                with patch(
                    "odoo_data_flow.importer.sort.sort_for_self_referencing"
                ) as mock_sort:
                    mock_sort.return_value = True  # Already sorted

                    with patch(
                        "odoo_data_flow.importer.import_threaded.import_data"
                    ) as mock_import:
                        mock_import.return_value = (
                            True,
                            {"id_map": {"1": 101, "2": 102}},
                        )

                        run_import(
                            config={
                                "hostname": "localhost",
                                "database": "test",
                                "login": "admin",
                                "password": "admin",
                            },
                            filename=csv_path,
                            model="res.partner",
                            deferred_fields=None,
                            unique_id_field="id",
                            no_preflight_checks=False,
                            headless=True,
                            worker=1,
                            batch_size=100,
                            skip=0,
                            fail=False,
                            separator=",",
                            ignore=[],
                            context={},
                            encoding="utf-8",
                            o2m=False,
                            groupby=None,
                        )
    finally:
        os.unlink(csv_path)


def test_importer_with_groupby() -> None:
    """Test importer with groupby to cover that branch."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        tmp.write("id,name,category\n1,Alice,cat1\n2,Bob,cat1\n3,Charlie,cat2\n")
        csv_path = tmp.name

    try:
        with patch("odoo_data_flow.importer._count_lines", return_value=4):
            with patch(
                "odoo_data_flow.importer._run_preflight_checks", return_value=True
            ):
                with patch(
                    "odoo_data_flow.importer.import_threaded.import_data"
                ) as mock_import:
                    mock_import.return_value = (
                        True,
                        {"id_map": {"1": 101, "2": 102, "3": 103}},
                    )

                    with patch("odoo_data_flow.importer.pl.read_csv") as mock_read_csv:
                        import polars as pl

                        mock_df = pl.DataFrame(
                            {
                                "id": ["1", "2", "3"],
                                "name": ["Alice", "Bob", "Charlie"],
                                "category": ["cat1", "cat1", "cat2"],
                            }
                        )
                        mock_read_csv.return_value = mock_df

                        # Test with groupby to cover that branch
                        run_import(
                            config={
                                "hostname": "localhost",
                                "database": "test",
                                "login": "admin",
                                "password": "admin",
                            },
                            filename=csv_path,
                            model="res.partner",
                            deferred_fields=None,
                            unique_id_field="id",
                            no_preflight_checks=True,
                            headless=True,
                            worker=2,
                            batch_size=5,
                            skip=0,
                            fail=True,  # Enable fail mode with single batch
                            separator=",",
                            ignore=[],
                            context={},
                            encoding="utf-8",
                            o2m=False,
                            groupby=["category"],  # This should cover the groupby logic
                        )
    finally:
        os.unlink(csv_path)
