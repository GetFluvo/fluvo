"""Additional tests for importer.py to cover the remaining major missed areas."""

import os
import tempfile
from typing import Any
from unittest.mock import patch

from odoo_data_flow.importer import run_import


def test_importer_main_process_with_relational_strategies() -> None:
    """Test the main process flow with relational strategies triggered."""
    # Create a temporary CSV file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        tmp.write('id,name,tags\n1,Alice,"tag1,tag2"\n2,Bob,"tag3,tag4"\n')
        csv_path = tmp.name

    try:
        with patch("odoo_data_flow.importer._count_lines", return_value=3):
            with patch(
                "odoo_data_flow.importer._run_preflight_checks"
            ) as mock_preflight:

                def preflight_side_effect(*args: Any, **kwargs: Any) -> bool:
                    # Set up strategies that will be executed in the main flow
                    kwargs["import_plan"]["strategies"] = {
                        "tags": {"strategy": "direct_relational_import"}
                    }
                    kwargs["import_plan"]["unique_id_field"] = "id"
                    return True

                mock_preflight.side_effect = preflight_side_effect

                with patch(
                    "odoo_data_flow.importer.import_threaded.import_data"
                ) as mock_import:
                    # First call (main import) - returns success and id_map
                    mock_import.return_value = (True, {"id_map": {"1": 101, "2": 102}})

                    with patch(
                        "odoo_data_flow.importer.relational_import_strategies.direct.run_direct_relational_import"
                    ) as mock_rel_import:
                        # Return None to skip additional import call
                        mock_rel_import.return_value = None

                        with patch(
                            "odoo_data_flow.importer.pl.read_csv"
                        ) as mock_read_csv:
                            import polars as pl

                            # Create a mock dataframe
                            mock_df = pl.DataFrame(
                                {
                                    "id": ["1", "2"],
                                    "name": ["Alice", "Bob"],
                                    "tags": ["tag1,tag2", "tag3,tag4"],
                                }
                            )
                            mock_read_csv.return_value = mock_df

                            # Call with config as dict to trigger different code path
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
                                no_preflight_checks=False,  # Use preflight to trigger strategy processing
                                headless=True,
                                worker=1,
                                batch_size=100,
                                skip=0,
                                fail=False,
                                separator=",",
                                ignore=None,
                                context={},
                                encoding="utf-8",
                                o2m=False,
                                groupby=None,
                            )
    finally:
        os.unlink(csv_path)


def test_importer_with_write_tuple_strategy() -> None:
    """Test run_import with write tuple strategy."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        tmp.write("id,name,parent_id\n1,Alice,101\n2,Bob,102\n")
        csv_path = tmp.name

    try:
        with patch("odoo_data_flow.importer._count_lines", return_value=3):
            with patch(
                "odoo_data_flow.importer._run_preflight_checks"
            ) as mock_preflight:

                def preflight_side_effect(*args: Any, **kwargs: Any) -> bool:
                    kwargs["import_plan"]["strategies"] = {
                        "parent_id": {"strategy": "write_tuple"}
                    }
                    kwargs["import_plan"]["unique_id_field"] = "id"
                    return True

                mock_preflight.side_effect = preflight_side_effect

                with patch(
                    "odoo_data_flow.importer.import_threaded.import_data"
                ) as mock_import:
                    mock_import.return_value = (True, {"id_map": {"1": 101, "2": 102}})

                    with patch(
                        "odoo_data_flow.importer.relational_import_strategies.write_tuple.run_write_tuple_import"
                    ) as mock_write_tuple:
                        mock_write_tuple.return_value = True  # Success

                        import polars as pl

                        with patch(
                            "odoo_data_flow.importer.pl.read_csv"
                        ) as mock_read_csv:
                            mock_df = pl.DataFrame(
                                {
                                    "id": ["1", "2"],
                                    "name": ["Alice", "Bob"],
                                    "parent_id": [101, 102],
                                }
                            )
                            mock_read_csv.return_value = mock_df

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
                                ignore=None,
                                context={},
                                encoding="utf-8",
                                o2m=False,
                                groupby=None,
                            )
    finally:
        os.unlink(csv_path)


def test_importer_with_write_o2m_tuple_strategy() -> None:
    """Test run_import with write O2M tuple strategy."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        tmp.write('id,name,child_ids\n1,Alice,"101,102"\n2,Bob,"103,104"\n')
        csv_path = tmp.name

    try:
        with patch("odoo_data_flow.importer._count_lines", return_value=3):
            with patch(
                "odoo_data_flow.importer._run_preflight_checks"
            ) as mock_preflight:

                def preflight_side_effect(*args: Any, **kwargs: Any) -> bool:
                    kwargs["import_plan"]["strategies"] = {
                        "child_ids": {"strategy": "write_o2m_tuple"}
                    }
                    kwargs["import_plan"]["unique_id_field"] = "id"
                    return True

                mock_preflight.side_effect = preflight_side_effect

                with patch(
                    "odoo_data_flow.importer.import_threaded.import_data"
                ) as mock_import:
                    mock_import.return_value = (True, {"id_map": {"1": 101, "2": 102}})

                    with patch(
                        "odoo_data_flow.importer.relational_import_strategies.write_o2m_tuple.run_write_o2m_tuple_import"
                    ) as mock_write_o2m:
                        mock_write_o2m.return_value = True  # Success

                        import polars as pl

                        with patch(
                            "odoo_data_flow.importer.pl.read_csv"
                        ) as mock_read_csv:
                            mock_df = pl.DataFrame(
                                {
                                    "id": ["1", "2"],
                                    "name": ["Alice", "Bob"],
                                    "child_ids": ["101,102", "103,104"],
                                }
                            )
                            mock_read_csv.return_value = mock_df

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
                                ignore=None,
                                context={},
                                encoding="utf-8",
                                o2m=False,
                                groupby=None,
                            )
    finally:
        os.unlink(csv_path)


def test_importer_process_with_no_strategies() -> None:
    """Test the main process when there are strategies defined but none match the expected types."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        tmp.write("id,name\n1,Alice\n2,Bob\n")
        csv_path = tmp.name

    try:
        with patch("odoo_data_flow.importer._count_lines", return_value=3):
            with patch(
                "odoo_data_flow.importer._run_preflight_checks"
            ) as mock_preflight:

                def preflight_side_effect(*args: Any, **kwargs: Any) -> bool:
                    # Set up a strategy with an unknown type to test the else branch
                    kwargs["import_plan"]["strategies"] = {
                        "unknown_field": {"strategy": "unknown_strategy_type"}
                    }
                    kwargs["import_plan"]["unique_id_field"] = "id"
                    return True

                mock_preflight.side_effect = preflight_side_effect

                with patch(
                    "odoo_data_flow.importer.import_threaded.import_data"
                ) as mock_import:
                    mock_import.return_value = (True, {"id_map": {"1": 101, "2": 102}})

                    import polars as pl

                    with patch("odoo_data_flow.importer.pl.read_csv") as mock_read_csv:
                        mock_df = pl.DataFrame(
                            {"id": ["1", "2"], "name": ["Alice", "Bob"]}
                        )
                        mock_read_csv.return_value = mock_df

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
                            ignore=None,
                            context={},
                            encoding="utf-8",
                            o2m=False,
                            groupby=None,
                        )
    finally:
        os.unlink(csv_path)


def test_importer_with_write_tuple_failure() -> None:
    """Test run_import with write tuple strategy that fails."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        tmp.write("id,name,parent_id\n1,Alice,101\n2,Bob,102\n")
        csv_path = tmp.name

    try:
        with patch("odoo_data_flow.importer._count_lines", return_value=3):
            with patch(
                "odoo_data_flow.importer._run_preflight_checks"
            ) as mock_preflight:

                def preflight_side_effect(*args: Any, **kwargs: Any) -> bool:
                    kwargs["import_plan"]["strategies"] = {
                        "parent_id": {"strategy": "write_tuple"}
                    }
                    kwargs["import_plan"]["unique_id_field"] = "id"
                    return True

                mock_preflight.side_effect = preflight_side_effect

                with patch(
                    "odoo_data_flow.importer.import_threaded.import_data"
                ) as mock_import:
                    mock_import.return_value = (True, {"id_map": {"1": 101, "2": 102}})

                    with patch(
                        "odoo_data_flow.importer.relational_import_strategies.write_tuple.run_write_tuple_import"
                    ) as mock_write_tuple:
                        mock_write_tuple.return_value = False  # Failure case

                        import polars as pl

                        with patch(
                            "odoo_data_flow.importer.pl.read_csv"
                        ) as mock_read_csv:
                            mock_df = pl.DataFrame(
                                {
                                    "id": ["1", "2"],
                                    "name": ["Alice", "Bob"],
                                    "parent_id": [101, 102],
                                }
                            )
                            mock_read_csv.return_value = mock_df

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
                                ignore=None,
                                context={},
                                encoding="utf-8",
                                o2m=False,
                                groupby=None,
                            )
    finally:
        os.unlink(csv_path)
