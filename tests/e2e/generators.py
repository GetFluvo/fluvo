"""Deterministic, scalable source-data generators for the e2e suite.

Datasets are generated on the fly and never committed: only these builders and the
scenario specs live in git, so the suite stays small and movable. Everything is
seeded/deterministic (no randomness) so failures reproduce exactly.

Each scenario namespaces its records with a unique ``prefix`` (woven into both the
external id and the ``name``) so independent tests can share one session database
without colliding: assertions query by that prefix.
"""

from __future__ import annotations

import csv
from typing import Any


def write_csv(
    path: str,
    header: list[str],
    rows: list[dict[str, Any]],
    separator: str = ",",
) -> str:
    """Write ``rows`` to ``path`` with the given ``header`` order.

    Returns:
        The path written, for convenience.
    """
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=header, delimiter=separator, extrasaction="ignore"
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in header})
    return path


def partners(
    n: int, prefix: str, *, start: int = 0, is_company: bool = True
) -> list[dict[str, str]]:
    """Generate ``n`` res.partner rows namespaced by ``prefix``.

    Each row has ``id`` (external id), ``name`` (carrying the prefix marker so the
    DB can be queried by it), and ``email``.
    """
    rows = []
    for i in range(start, start + n):
        rows.append(
            {
                "id": f"{prefix}_p{i}",
                "name": f"{prefix} Partner {i}",
                "email": f"{prefix}.{i}@example.com",
                "is_company": "1" if is_company else "0",
            }
        )
    return rows


def name_domain(prefix: str) -> list[Any]:
    """A search domain matching every record this prefix created (by name)."""
    return [["name", "like", f"{prefix} %"]]


def hierarchy(
    n: int, prefix: str, *, fanout: int = 10
) -> list[dict[str, str]]:
    """Generate a self-referencing partner hierarchy (``parent_id`` external ids).

    Children reference parents that appear *later* in the file, forcing correct
    two-pass / sort handling rather than relying on row order.
    """
    rows = partners(n, prefix)
    for i, row in enumerate(rows):
        if i % fanout != 0:
            # Point at the block's root, which is generated after this row.
            root_index = (i // fanout) * fanout
            row["parent_id"] = rows[root_index]["id"]
        else:
            row["parent_id"] = ""
    # Reverse so parents come after children in file order.
    return list(reversed(rows))


def with_country(
    n: int, prefix: str, country_xmlid: str = "base.be"
) -> list[dict[str, str]]:
    """Partner rows with a cross-model ``country_id`` reference (an XML id)."""
    rows = partners(n, prefix)
    for row in rows:
        row["country_id"] = country_xmlid
    return rows


def states(
    n: int, prefix: str, country_xmlid: str = "base.us"
) -> list[dict[str, str]]:
    """Generate res.country.state rows (``country_id`` is a *required* m2o).

    Used to prove the engine never defers a required relational field - doing so
    would make the Pass-1 create fail with 'Missing required value'.
    """
    # (country_id, code) is unique in Odoo; derive a per-prefix discriminator so
    # independent tests don't collide on a shared database.
    disc = prefix.upper().replace("_", "")[:3]
    rows = []
    for i in range(n):
        rows.append(
            {
                "id": f"{prefix}_st{i}",
                "name": f"{prefix} State {i}",
                "code": f"{disc}{i:02d}",
                # /id => resolved as an external id by Odoo's load() in Pass 1.
                "country_id/id": country_xmlid,
            }
        )
    return rows


def inject_duplicates(
    rows: list[dict[str, str]], dupe_count: int, id_field: str = "id"
) -> tuple[list[dict[str, str]], list[str]]:
    """Append ``dupe_count`` rows reusing earlier external ids.

    Returns:
        ``(rows_with_dupes, duplicated_ids)``.
    """
    duped_ids = []
    extra = []
    for i in range(dupe_count):
        src = rows[i]
        clone = dict(src)
        clone["name"] = src["name"] + " (dupe)"
        extra.append(clone)
        duped_ids.append(src[id_field])
    return rows + extra, duped_ids


def inject_malformed(
    rows: list[dict[str, str]], bad_count: int
) -> tuple[list[dict[str, str]], list[str]]:
    """Mark ``bad_count`` rows as malformed (empty required ``name``).

    Returns:
        ``(rows, bad_ids)`` where bad_ids are expected in the fail file.
    """
    bad_ids = []
    for i in range(bad_count):
        rows[i]["name"] = ""  # name is required on res.partner
        bad_ids.append(rows[i]["id"])
    return rows, bad_ids


def mixed_type_column(
    n: int, prefix: str, column: str = "ref"
) -> list[dict[str, str]]:
    """Partner rows whose ``column`` mixes integer-looking and text values.

    Reproduces the Polars type-inference crash class (commit a1eeca5): a column
    must not be coerced to a single numeric type and blow up on text rows.
    """
    rows = partners(n, prefix)
    for i, row in enumerate(rows):
        row[column] = str(i) if i % 2 == 0 else f"REF-{i:04d}"
    return rows
