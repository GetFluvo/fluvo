"""Multi-language import scenarios (``field@lang``) against a real Odoo (#254).

These prove the translation passes end-to-end: the base value and each
``field@lang`` value land in the correct per-language storage (verified by reading
the record back under each language's context), an uninstalled language aborts the
import *before* any write, and the multi-language path does not blow up wall-clock
versus a single-language import (the benchmark acceptance criterion).

``res.partner.category`` is used because its ``name`` field is translatable and the
model ships in ``base`` (no extra module install needed).
"""

from __future__ import annotations

import time
from typing import Any

from . import assertions as A
from . import generators as G

MODEL = "res.partner.category"


def _rows(
    n: int, prefix: str, langs: list[str]
) -> tuple[list[dict[str, str]], list[str]]:
    """Build category rows with a base name plus one column per language.

    Each language's value is a distinct, recognisable string so a read-back under
    that language proves the right translation landed (and not the base value).

    Args:
        n: Number of category rows to generate.
        prefix: Namespacing marker carried in every value (for querying/cleanup).
        langs: Language codes to emit a ``name@<lang>`` column for.

    Returns:
        tuple[list[dict[str, str]], list[str]]: The rows and the CSV header
        (``id, name, name@<lang>...``).
    """
    rows = []
    for i in range(n):
        row = {"id": f"{prefix}_c{i}", "name": f"{prefix} EN {i}"}
        for lang in langs:
            row[f"name@{lang}"] = f"{prefix} {lang} {i}"
        rows.append(row)
    header = ["id", "name", *[f"name@{lang}" for lang in langs]]
    return rows, header


def _name_in(rpc: Any, db_id: int, lang: str) -> str:
    """Read a single record's ``name`` under a given language context."""
    rec = rpc.get_model(MODEL).read([db_id], ["name"], context={"lang": lang})
    rec = rec[0] if isinstance(rec, list) else rec
    return str(rec["name"])


def test_translation_columns_write_per_language(
    conn_config: dict[str, Any],
    rpc: Any,
    tmp_path: Any,
    translated_languages: list[str],
) -> None:
    """Base + field@lang values each land in their own language's storage."""
    langs = translated_languages
    prefix = "tr_write"
    n = 5
    rows, header = _rows(n, prefix, langs)
    csv_path = G.write_csv(str(tmp_path / "tr.csv"), header, rows)

    id_map = A.run_full_import(conn_config, MODEL, csv_path)

    assert id_map is not None, "translation import returned None (aborted)"
    assert len(id_map) == n, f"expected {n} categories, got {len(id_map)}"

    for i in range(n):
        db_id = id_map[f"{prefix}_c{i}"]
        # Base language keeps the untranslated value.
        assert _name_in(rpc, db_id, "en_US") == f"{prefix} EN {i}"
        # Every translated column landed under its own language.
        for lang in langs:
            assert _name_in(rpc, db_id, lang) == f"{prefix} {lang} {i}", (
                f"translation for {lang} did not land on record {i}"
            )


def test_uninstalled_language_aborts_before_writing(
    conn_config: dict[str, Any],
    rpc: Any,
    tmp_path: Any,
) -> None:
    """A field@lang for a language that isn't installed aborts with no writes."""
    prefix = "tr_badlang"
    rows = [
        {"id": f"{prefix}_c0", "name": f"{prefix} EN 0", "name@zu_ZA": "ignored"},
    ]
    header = ["id", "name", "name@zu_ZA"]
    csv_path = G.write_csv(str(tmp_path / "badlang.csv"), header, rows)

    before = A.count(rpc, MODEL, G.name_domain(prefix))
    id_map = A.run_full_import(conn_config, MODEL, csv_path)
    after = A.count(rpc, MODEL, G.name_domain(prefix))

    assert id_map is None, "import should abort on an uninstalled language"
    assert after == before, "no records may be written when the import aborts"


def test_translation_benchmark_reports_ratio(
    conn_config: dict[str, Any],
    rpc: Any,
    tmp_path: Any,
    scale: int,
    translated_languages: list[str],
) -> None:
    """Benchmark AC: a 3-language import must not blow up wall-clock vs 1-language.

    Imports the same record count once with all languages and once with none, and
    reports the ratio. The soft guard only catches a pathological blow-up; the
    printed ratio is the datum for the direct-SQL-vs-multi-pass decision (#254).
    """
    langs = translated_languages
    n = scale

    multi_rows, multi_header = _rows(n, "tr_multi", langs)
    multi_csv = G.write_csv(str(tmp_path / "multi.csv"), multi_header, multi_rows)
    t0 = time.monotonic()
    multi_map = A.run_full_import(conn_config, MODEL, multi_csv)
    multi_secs = time.monotonic() - t0

    base_rows, base_header = _rows(n, "tr_base", [])
    base_csv = G.write_csv(str(tmp_path / "base.csv"), base_header, base_rows)
    t0 = time.monotonic()
    base_map = A.run_full_import(conn_config, MODEL, base_csv)
    base_secs = time.monotonic() - t0

    assert multi_map is not None and base_map is not None
    assert len(multi_map) == n and len(base_map) == n

    ratio = multi_secs / base_secs if base_secs else float("inf")
    n_langs = len(langs) + 1  # base + translated languages
    print(
        f"\n[translation timing] {n_langs}-lang={multi_secs:.2f}s "
        f"1-lang={base_secs:.2f}s ratio={ratio:.2f}x (n={n}, langs={langs})"
    )
    # Spot-check that at least one translation actually landed (not a no-op run).
    sample = multi_map["tr_multi_c0"]
    assert _name_in(rpc, sample, langs[0]) == f"tr_multi {langs[0]} 0"
    # Soft guard: multi-language must stay within a generous bound of single-pass;
    # a real regression (e.g. per-row instead of per-language passes) would blow past.
    assert multi_secs <= base_secs * (n_langs + 1.0) + 5.0


def test_export_emits_field_at_lang_and_round_trips(
    conn_config: dict[str, Any],
    rpc: Any,
    tmp_path: Any,
    translated_languages: list[str],
) -> None:
    """Export produces field@lang columns that re-import into the right languages.

    Seeds translated categories, exports them with --languages, checks the file's
    per-language columns match the DB truth, then re-imports the exported file into
    fresh records and confirms each language landed — a full export->import loop on
    the same column convention (#282).
    """
    import polars as pl

    from fluvo import exporter

    langs = translated_languages
    prefix = "rt_seed"
    n = 4
    rows, header = _rows(n, prefix, langs)
    seed_csv = G.write_csv(str(tmp_path / "seed.csv"), header, rows)
    seed_map = A.run_full_import(conn_config, MODEL, seed_csv)
    assert seed_map is not None and len(seed_map) == n

    # Export just the seeded records, requesting all languages via --languages.
    out = str(tmp_path / "exported.csv")
    exporter.run_export(
        config=conn_config,
        model=MODEL,
        fields="id,name",
        output=out,
        domain=str([["name", "like", f"{prefix} EN %"]]),
        languages=",".join(langs),
        separator=",",
        context={},
    )

    df = pl.read_csv(out, separator=",")
    for lang in langs:
        assert f"name@{lang}" in df.columns, f"export missing name@{lang}"
    assert df.height == n
    # File values match what each record actually holds per language.
    by_xmlid = {r["id"]: r for r in df.to_dicts()}
    for i in range(n):
        db_id = seed_map[f"{prefix}_c{i}"]
        # exported `id` is the external id (module-qualified); match on suffix.
        rec = next(r for xid, r in by_xmlid.items() if xid.endswith(f"{prefix}_c{i}"))
        assert rec["name"] == _name_in(rpc, db_id, "en_US")
        for lang in langs:
            assert rec[f"name@{lang}"] == f"{prefix} {lang} {i}"

    # Re-import the exported file under fresh ids -> translations must survive.
    df2 = df.with_columns(
        pl.col("id").str.replace_all(r".*rt_seed_c", "rt_rr_c").alias("id"),
        pl.col("name").str.replace_all("rt_seed", "rt_rr").alias("name"),
    )
    for lang in langs:
        col = f"name@{lang}"
        df2 = df2.with_columns(
            pl.col(col).str.replace_all("rt_seed", "rt_rr").alias(col)
        )
    rr_csv = str(tmp_path / "roundtrip.csv")
    df2.write_csv(rr_csv, separator=",")

    rr_map = A.run_full_import(conn_config, MODEL, rr_csv)
    assert rr_map is not None and len(rr_map) == n
    for i in range(n):
        db_id = rr_map[f"rt_rr_c{i}"]
        assert _name_in(rpc, db_id, "en_US") == f"rt_rr EN {i}"
        for lang in langs:
            assert _name_in(rpc, db_id, lang) == f"rt_rr {lang} {i}"
