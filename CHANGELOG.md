# Changelog

All notable changes to `fluvo` are recorded here. This project follows
[Keep a Changelog](https://keepachangelog.com/) and semantic-ish versioning while
in `0.0.x` beta. The published notes for each tagged release also appear on the
[GitHub releases page](https://github.com/getfluvo/fluvo/releases).

## [Unreleased] — targeting 0.0.4

### Breaking changes

- **Importing a company-specific model on a multi-company database now aborts by
  default** unless you choose a company. If the target model is company-specific
  (has `company_id` or company-dependent fields) and the database has more than
  one company, an import that does not pass `--company-id`, `--all-companies`, or
  `--allow-default-company` stops with a clear error instead of proceeding.

  **Why this is deliberate protection, not a regression.** Landing records under
  the wrong company is a *silent* failure: the import succeeds, reconciliation
  passes, and the data is simply attached to the wrong company in a way nobody
  notices for months. This has now been observed **multiple times in real
  migration work**, not hypothetically. Aborting by default turns a silent,
  long-latency data error into a loud, immediate one.

  **What to do:** pass `--company-id <id>` (a database id, or an XML id like
  `base.main_company`) to choose the company explicitly, `--all-companies` to load
  across all companies you can access, or `--allow-default-company` to keep the old
  behaviour (land under the connecting user's default company). Single-company
  databases are unaffected and proceed silently. If you run `fluvo import` from
  cron against a multi-company database, add one of these flags.

- **Python 3.9 is no longer supported** (`requires-python >= 3.10`). Dropping it
  was necessary to clear a set of security advisories whose fixed releases all
  require ≥ 3.10. `fluvo`'s RPC mode is otherwise decoupled from the Python
  version — only the interpreter floor moved.

### Added

- **Multi-language import** — `field@lang` columns load translations of
  translatable fields (one write pass per language). ([#254])
- **Multi-language export** — `field@lang` columns / `--languages`, so
  export → import round-trips. ([#282])
- **Per-company import** — `field@company` columns write company-dependent field
  values per company. ([#255])
- **Flow runner** — `fluvo --flow-file flows.yml` runs declarative, versioned
  flows (named flows, `on_error`, `--run`, `--var`, `--dry-run`). ([#251])
- **`fluvo assess`** — a source-migration report: entity inventory, volumes,
  field counts, and risk flags, as a table or JSON/Markdown handout.
- **Weekly-delta exports** — `export --since <timestamp>` sugar over `--domain`,
  for re-runnable delta pipelines paired with `import --skip-unchanged`.
- **Public Polars DataFrame API** — `fluvo.load_dataframe` / `fluvo.export_dataframe`
  for moving data to/from Odoo without the CLI. ([#288])
- **Parquet intermediate format** — `import --file x.parquet` reads (and coerces) a
  Parquet source, and `export --output x.parquet` writes Parquet — a typed handoff
  to/from a Polars-native transform step.

[#251]: https://github.com/GetFluvo/fluvo/issues/251
[#254]: https://github.com/GetFluvo/fluvo/issues/254
[#255]: https://github.com/GetFluvo/fluvo/issues/255
[#282]: https://github.com/GetFluvo/fluvo/issues/282
[#288]: https://github.com/GetFluvo/fluvo/issues/288
