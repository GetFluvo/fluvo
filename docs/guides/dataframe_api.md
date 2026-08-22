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

- **Column names are the import header** — use the same conventions as [`fluvo import`](importing_data.md): `id` for the external id (required — or `.id` for the database id), `field/id` for a relational lookup by external id, `field/.id` by database id.
- **Types are coerced** from their Polars types to Odoo-import-ready values, so you can pass a natural frame:
  - booleans → `1` / `0`;
  - `Date` → `YYYY-MM-DD`, `Datetime` → `YYYY-MM-DD HH:MM:SS` (a **tz-aware** datetime is converted to UTC first, since Odoo stores naive UTC), `Time` → `HH:MM:SS`;
  - floats keep their form, but `NaN`/`inf` become empty — and note a float column feeding an **integer** field will fail to parse (`"1.0"`); use an integer column for integer fields;
  - everything else → its string form; **nulls → empty string** (on an *update* this clears the field rather than leaving it unchanged).
  - Columns with list/struct/binary/duration dtypes are rejected with a clear error; pass `coerce=False` if your frame already holds import-ready strings.
- **Returns `(success, stats)`** — `success` is True only when the load completed **and no rows failed**, so `if success:` is safe. Pass `fail_file="…"` to capture failed rows; without it, failures are counted but not recoverable (a warning is logged). An empty frame is a no-op success.

```{note}
`load_dataframe` is a **direct load** — it does *not* run the CLI's pre-flight checks, auto-deferral, or the per-language / per-company passes. `field@lang` and `field@company` columns are rejected (use the [`fluvo import` CLI](importing_data.md) for those). For the two-pass relational load it resolves `field/id` / `field/.id` references, but it does not auto-detect and defer self-referencing relations the way the CLI's pre-flight does — order such rows yourself, or use the CLI.
```

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

Fields use the same specifiers as [`fluvo export --fields`](exporting_data.md) (including `.id` and `field/.id`). Values come back **typed** (booleans as `Boolean`, integers as `Int64`, and so on — not strings). On failure it raises `FluvoError` rather than returning a partial or empty frame.

## When to use which

| You have… | Use |
| --- | --- |
| A file on disk | `fluvo import` / `fluvo export` (CLI) or a [`flows.yml`](importing_data.md) |
| A Polars DataFrame in memory | `load_dataframe` / `export_dataframe` |

The DataFrame API is what makes Fluvo composable inside any Polars-native pipeline — see the [Flowfile integration](https://github.com/GetFluvo/fluvo/issues/288).
