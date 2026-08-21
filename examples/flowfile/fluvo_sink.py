"""Load a Polars DataFrame into Odoo with Fluvo — the "Fluvo as a sink" recipe.

This is the copy-paste body for a **Flowfile** *Polars code* / *Python* node (or
any Polars-native script): it takes the transformed DataFrame that reaches the
node and loads it into Odoo through Fluvo's in-memory migration importer, which
gives you the real connector — two-pass relational load, reconciliation, and a
fail file — without leaving your visual flow.

Nothing here imports Flowfile: the function takes a plain ``polars.DataFrame``,
so it works unchanged inside a Flowfile node, a notebook, or a plain script.
See ``README.md`` for the file-handoff mode (export → transform → import) and the
wider integration (PLAN 4.5 / issue #288).
"""

from __future__ import annotations

from typing import Any

import polars as pl

from fluvo.importer import run_import_for_migration


def load_dataframe(
    df: pl.DataFrame,
    config: str | dict[str, Any],
    model: str,
    *,
    worker: int = 1,
    batch_size: int = 100,
    fail_file: str | None = None,
) -> tuple[bool, dict[str, int]]:
    """Load a Polars DataFrame into an Odoo model via Fluvo.

    The DataFrame's columns become the import header, so name them exactly as you
    would a CSV for ``fluvo import`` — including Fluvo's conventions: ``id`` for the
    external id, ``field/id`` for a relational lookup by external id, ``field@lang``
    for a translation, ``field@company`` for a per-company value.

    Args:
        df: The transformed data. Values should already be import-ready strings
            (e.g. ``"1"``/``"0"`` for booleans) as for a CSV import.
        config: A connection config file path, or a config dict.
        model: The target Odoo model (e.g. ``"res.partner"``).
        worker: Number of parallel connections.
        batch_size: Records per load batch.
        fail_file: Optional path for rows that fail to import (never silently
            dropped). When None, no fail file is written.

    Returns:
        tuple[bool, dict[str, int]]: ``(overall_success, stats)`` from Fluvo's
        import engine, so a downstream node can branch on partial failure.
    """
    header = df.columns
    # Fluvo's engine expects rows as lists of stringifiable values; Polars gives
    # us exactly that via iter_rows.
    data = [list(row) for row in df.iter_rows()]
    return run_import_for_migration(
        config=config,
        model=model,
        header=header,
        data=data,
        worker=worker,
        batch_size=batch_size,
        fail_file=fail_file,
    )


if __name__ == "__main__":
    # Minimal standalone demo (point CONFIG at a real connection file to run):
    demo = pl.DataFrame(
        {
            "id": ["flowfile_partner_1", "flowfile_partner_2"],
            "name": ["Acme (via Flowfile)", "Globex (via Flowfile)"],
            "is_company": ["1", "1"],
        }
    )
    ok, stats = load_dataframe(demo, config="connection.conf", model="res.partner")
    print(f"loaded: success={ok} stats={stats}")
