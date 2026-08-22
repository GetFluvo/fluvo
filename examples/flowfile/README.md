# Fluvo + Flowfile

[Flowfile](https://github.com/edwardvaneechoud/Flowfile) is an MIT-licensed,
Polars-native **visual ETL** tool (drag-and-drop canvas, a Polars-like API, a
Polars/Python code node, flows saved as YAML). Fluvo is the **Odoo extract + load**
layer. Because both are Polars-native, they compose over a Polars frame or a file
— Flowfile does the visual *transform*, Fluvo does the Odoo *move*.

This is the kick-off recipe for PLAN 4.5 (issue
[#288](https://github.com/GetFluvo/fluvo/issues/288)). Fluvo does **not** depend on
Flowfile; the two stay orchestrator-agnostic.

## Mode 1 — File handoff (works today)

```
Odoo ──fluvo export──▶ data.csv ──▶ Flowfile (visual transform) ──▶ out.csv ──fluvo import──▶ Odoo
```

```bash
# 1. Extract from the source Odoo.
fluvo export --connection-file source.conf --model res.partner \
  --fields "id,name,email" --output partners.csv

# 2. In Flowfile: read partners.csv, transform visually, write out.csv.

# 3. Load into the destination Odoo (idempotently).
fluvo import --connection-file dest.conf --model res.partner \
  --file out.csv --skip-unchanged
```

A [Parquet](https://github.com/GetFluvo/fluvo/issues) intermediate (PLAN 4.8)
would make this a typed, zero-copy handoff — both sides already use `pyarrow`.

## Mode 2 — Fluvo as an in-flow sink

Load the transformed DataFrame straight into Odoo from a Flowfile **Polars code**
or **Python** node, so a flow ends in a "load to Odoo" step. The recipe is
[`fluvo_sink.py`](fluvo_sink.py) — a single `load_dataframe(df, config, model)`
call over Fluvo's in-memory importer (real two-pass load, reconciliation, fail
file):

```python
from fluvo_sink import load_dataframe  # paste the function into the node

# `df` is the Polars frame arriving at the node; name its columns like a CSV
# for `fluvo import` (id, field/id, field@lang, field@company, …).
ok, stats = load_dataframe(df, config="dest.conf", model="res.partner")
```

Column-naming conventions (external ids, relational lookups, translations,
per-company values) are the same as [`fluvo import`](../../docs/guides/importing_data.md).

## Status

Kick-off spike. Mode 2's Fluvo side works today (it wraps the existing
`run_import_for_migration`); Mode 1 works today over CSV. Open question for the
spike: whether to promote `load_dataframe` to a first-class `fluvo` API for any
Polars-native caller — tracked in #288.
