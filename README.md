<p align="center">
  <img src="https://raw.githubusercontent.com/getfluvo/fluvo/master/docs/_static/icon.png" width="200">
</p>

# Fluvo

[![PyPI](https://img.shields.io/pypi/v/fluvo.svg)][pypi status]
[![Status](https://img.shields.io/pypi/status/fluvo.svg)][pypi status]
[![Python Version](https://img.shields.io/pypi/pyversions/fluvo)][pypi status]
[![License](https://img.shields.io/pypi/l/fluvo)][license]

[![Read the documentation at https://fluvo.readthedocs.io/](https://img.shields.io/readthedocs/fluvo/latest.svg?label=Read%20the%20Docs)][read the docs]
[![Tests](https://github.com/getfluvo/fluvo/workflows/Tests/badge.svg)][tests]
[![Codecov](https://codecov.io/gh/getfluvo/fluvo/branch/master/graph/badge.svg)][codecov]

[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)][pre-commit]
[![Ruff codestyle][ruff badge]][ruff project]

[pypi status]: https://pypi.org/project/fluvo/
[read the docs]: https://fluvo.readthedocs.io/en/latest/
[tests]: https://github.com/getfluvo/fluvo/actions?workflow=Tests
[codecov]: https://app.codecov.io/gh/getfluvo/fluvo
[pre-commit]: https://github.com/pre-commit/pre-commit
[ruff badge]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json
[ruff project]: https://github.com/astral-sh/ruff

**Fluvo is the data-migration engine for Odoo implementation partners.** When you're moving a client onto Odoo — from a legacy ERP, a spreadsheet estate, or an older Odoo — Fluvo turns a migration from a one-shot manual load into a repeatable run you can rehearse on UAT, drive from scripts, and trust in production.

It is built around one guarantee that matters on go-live weekend: **every source row is accounted for.** Nothing is silently dropped, every re-run is safe, and a failed step tells your automation it failed.

---

## Why partners use it for migrations

- 🧾 **Reconciliation, not hope** — every run proves `created + failed + unaccounted == total`. You leave each import knowing exactly what landed, what didn't, and why — not guessing from a scrolled-past log.
- 🛟 **Bad rows don't sink the batch** — a `load` → `create` fallback rescues the good records and writes the rest to a fail file with the exact Odoo error. Fix those rows and re-run with `--fail` to retry only them.
- 🔁 **Re-runnable by design** — records upsert on their external id, so re-running an import converges to the right state instead of duplicating. Interrupted by a dropped VPN or a server restart? Run it again; checkpoints resume where it stopped.
- 🚦 **Exit codes your pipeline can trust** — a fatal abort (bad credentials, wrong model, unreachable host) exits non-zero. Your `set -e` wrappers stop instead of marching on top of a database that received nothing.
- 🔗 **Relations and hierarchies, ordered for you** — parent/child and relational data are detected and imported in a two-pass strategy, so you don't hand-sequence files or untangle "record not found" errors.
- 🌐 **Per-environment by default** — point each run at a `connection.conf` (xmlrpc / jsonrpc / json2). Rehearse against UAT, then swap the connection file for production. Same scripts, same data, different target.
- 🔀 **Odoo-to-Odoo in one step** — `fluvo migrate` exports, transforms, and re-imports between two live Odoo instances in memory, for version upgrades and consolidations.
- 🧩 **Config-as-code** — mappings are plain Python with a rich `mapper` library. They live in the client's migration repo: reviewable, diffable, and re-runnable a year later when the next batch arrives.

## Installation

Install into your migration toolbox with `uv` (or `pip`) from [PyPI]:

```console
$ uv pip install fluvo
```

Fluvo talks to Odoo over RPC, so it runs from your laptop or a CI runner — it does **not** need to be installed on the client's Odoo server, and it can target Odoo versions independent of your own Python version.

## A migration run, end to end

A migration is a series of these two steps — one per model — chained in the order Odoo needs (companies → partners → products → …).

**1. Map the client's export to Odoo, as code.**
Each source file gets a `transform.py` that declares how its columns become Odoo fields. External ids (the `id` column) are what make every later re-run idempotent — give every record a stable one.

```python
# transform.py
from fluvo.lib.transform import Processor
from fluvo.lib import mapper

partner_mapping = {
    'id': mapper.m2o('client_partner', 'CustomerCode'),  # stable external id
    'name': mapper.val('CustomerName'),
    'parent_id/id': mapper.m2o('client_partner', 'ParentCode'),  # resolved in pass 2
    'country_id/id': mapper.map_val({'NL': 'base.nl', 'BE': 'base.be'}, 'CountryISO'),
}

processor = Processor(partner_mapping, source_filename='origin/customers.csv', separator=',')
processor.process('data/partners_clean.csv', {'model': 'res.partner'})
processor.write_to_file('load_partners.sh')  # emits the exact CLI command
```

```console
$ python transform.py
```

**2. Load it — first at UAT, then production.**
The transform emits a ready-to-run `fluvo import` command. Drive it from a shell wrapper so the whole migration is one auditable script per environment:

```bash
# load_partners.sh (generated)
fluvo import \
  --connection-file conf/uat_connection.conf \
  --file data/partners_clean.csv \
  --model res.partner \
  --worker 4 --size 500

echo "exit: $?"   # non-zero here stops a `set -e` migration cold — as it should
```

Import detected `parent_id` as self-referential, so it ran two passes automatically: create every partner, then link parents. Some rows failed on a bad country code? They're in `res_partner_fail.csv` with the reason. Fix them and retry just those:

```console
$ fluvo import --connection-file conf/uat_connection.conf \
    --file data/partners_clean.csv --model res.partner --fail
```

When UAT reconciles clean, change one thing — `conf/uat_connection.conf` → `conf/prod_connection.conf` — and run the same scripts against production.

> **Tip for real migrations:** don't trust the exit code alone — read the imported values back out of Odoo and diff them against the source. Fluvo's reconciliation and fail files tell you what *it* did; a read-back tells you what the *database* holds.

## Documentation

The **[full documentation on Read the Docs][read the docs]** covers the reconciliation contract, two-pass relational imports, the `mapper` library, server-to-server migration, and per-environment configuration.
See the [Command-line Reference] for every command and flag.

## Contributing

Contributions are very welcome.
To learn more, see the [Contributor Guide].

## License

Distributed under the terms of the [LGPL 3.0 license][license],
_Fluvo_ is free and open source software.
It began as, and remains a derivative of, `odoo_csv_import` by Thibault Francois — see [NOTICE](NOTICE) for attribution.

## Issues

If you hit a problem mid-migration,
please [file an issue] with a detailed description — the exact command, the Odoo version, and what reconciliation reported all help.

## Credits

Development of this project is financially supported by [stefcy.com].

[stefcy.com]: https://stefcy.com
[@bosd]: https://github.com/bosd
[pypi]: https://pypi.org/
[file an issue]: https://github.com/getfluvo/fluvo/issues
[pip]: https://pip.pypa.io/

<!-- github-only -->

[license]: https://github.com/getfluvo/fluvo/blob/master/LICENSE
[contributor guide]: https://github.com/getfluvo/fluvo/blob/master/CONTRIBUTING.md
[command-line reference]: https://fluvo.readthedocs.io/en/latest/usage.html
