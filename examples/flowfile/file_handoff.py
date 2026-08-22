"""File-handoff mode: the Flowfile transform step between two Fluvo commands.

The full pipeline is three steps; this script is the middle (visual/Polars)
transform, kept as plain code so it is runnable and reviewable:

1. Extract from the source Odoo (Fluvo CLI)::

     fluvo export --connection-file src.conf --model res.partner \
       --fields "id,name,email" --output partners_raw.csv

2. Transform (this script, or the same nodes on the Flowfile canvas)::

     python file_handoff.py partners_raw.csv partners_clean.csv

3. Load into the destination Odoo, idempotently (Fluvo CLI)::

     fluvo import --connection-file dst.conf --model res.partner \
       --file partners_clean.csv --skip-unchanged

Flowfile is an optional, separate install (``pip install Flowfile``) — it is not a
dependency of Fluvo. Both sides are Polars-native, so the CSV (or, with PLAN 4.8,
a Parquet) handoff is lossless. See ``README.md`` and issue #288.
"""

from __future__ import annotations

import sys

import flowfile as ff


def transform(src_csv: str, out_csv: str) -> str:
    """Read a Fluvo export, apply a Flowfile transform, and write it back.

    The transform here is illustrative — drop rows with no email, upper-case the
    name — standing in for whatever you build on the Flowfile canvas. The output
    keeps Fluvo's import column conventions (``id`` external id, ``field/id``
    lookups) so it re-imports cleanly.

    Args:
        src_csv: Path to the CSV produced by ``fluvo export``.
        out_csv: Path to write the transformed CSV for ``fluvo import``.

    Returns:
        str: ``out_csv``, for convenience.
    """
    frame = (
        ff.read_csv(src_csv)
        .filter(ff.col("email").is_not_null())
        .with_columns(ff.col("name").str.to_uppercase().alias("name"))
    )
    frame.write_csv(out_csv)
    return out_csv


if __name__ == "__main__":
    source = sys.argv[1] if len(sys.argv) > 1 else "partners_raw.csv"
    dest = sys.argv[2] if len(sys.argv) > 2 else "partners_clean.csv"
    print(f"transformed {source} -> {transform(source, dest)}")
