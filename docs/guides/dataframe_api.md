# Guide: The Polars DataFrame API

Fluvo is a library as well as a CLI. When your data already lives in (or is headed for) a [Polars](https://pola.rs) DataFrame — in a notebook, a script, or a visual tool like [Flowfile](https://github.com/edwardvaneechoud/Flowfile) — you can move it to and from Odoo directly, without writing a CSV or shelling out.

Two supported functions, importable from the top-level package:

```python
from fluvo import load_dataframe, export_dataframe, FluvoError
```

## `load_dataframe` — DataFrame → Odoo

```python
import polars as pl
from fluvo import load_dataframe

df = pl.DataFrame(
    {
        "id": ["partner_acme", "partner_globex"],   # external id
        "name": ["Acme", "Globex"],
        "is_company": [True, True],                  # real bool — coerced for you
    }
)

success, stats = load_dataframe(df, config="dest.conf", model="res.partner")
```

- **Column names are the import header** — use the same conventions as [`fluvo import`](importing_data.md): `id` for the external id, `field/id` for a relational lookup by external id, [`field@lang`](importing_data.md) for a translation, [`field@company`](importing_data.md) for a per-company value.
- **Types are coerced** from their Polars types to Odoo-import-ready values, so you can pass a natural frame:
  - booleans → `1` / `0`,
  - `Date` → `YYYY-MM-DD`, `Datetime` → `YYYY-MM-DD HH:MM:SS`, `Time` → `HH:MM:SS`,
  - everything else → its string form, and nulls → empty string.
  - Pass `coerce=False` if your frame already holds import-ready strings.
- **Returns `(success, stats)`** — the same reconciliation stats as an import, so you can branch on a partial failure rather than assume success. Pass `fail_file="…"` to capture rows that fail (never silently dropped). An empty frame is a no-op success.

It runs the full import engine: two-pass relational load, reconciliation, and the fail file — the same guarantees as the CLI.

## `export_dataframe` — Odoo → DataFrame

The mirror image, so `export_dataframe` → transform → `load_dataframe` is a Polars-native round-trip:

```python
from fluvo import export_dataframe

df = export_dataframe(
    "source.conf",
    "res.partner",
    ["id", "name", "email", "country_id/id"],
    domain=[["is_company", "=", True]],
    context={"lang": "nl_NL"},
)
```

Fields use the same specifiers as [`fluvo export --fields`](exporting_data.md) (including `.id` and `field/.id`). Values come back as strings. On failure it raises `FluvoError` rather than returning a partial or empty frame.

## When to use which

| You have… | Use |
| --- | --- |
| A file on disk | `fluvo import` / `fluvo export` (CLI) or a [`flows.yml`](importing_data.md) |
| A Polars DataFrame in memory | `load_dataframe` / `export_dataframe` |

The DataFrame API is what makes Fluvo composable inside any Polars-native pipeline — see the [Flowfile integration](https://github.com/GetFluvo/fluvo/issues/288).
