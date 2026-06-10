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

**Fluvo gets data into Odoo, fast.** A high-performance, Polars-backed ETL toolkit that replaces fragile, manual data prep with declarative, repeatable, configuration-as-code workflows.

---

## Why Fluvo?

- ⚡ **Fast at scale** — a Polars-backed, multi-threaded engine for importing and exporting large Odoo datasets.
- 🛟 **Imports that don't lose data** — a `load` → `create` fallback rescues the good records from a failing batch and writes the rest to a fail file with the exact error, so a few bad rows never sink the whole import.
- 🔗 **No import-order headaches** — automatically detects relational and parent/child data and runs a two-pass import, so you don't have to hand-order your files.
- 🧩 **Configuration-as-code** — define transforms as plain Python with a rich `mapper` library: readable, repeatable, and version-controlled.
- 🔄 **Server-to-server migration** — export, transform, and import between two Odoo instances in a single in-memory step.
- ✅ **Clean data before it lands** — built-in cleaning and validation catch bad values before they ever reach Odoo.


## Installation

You can install _Fluvo_ via `uv` or `pip` from [PyPI]:

```console
$ uv pip install fluvo
```

## Quick Usage Example

The core workflow involves two simple steps:

**1. Transform your source data with a Python script.**
Create a `transform.py` file to define the mapping from your source file to Odoo's format.

```python
# transform.py
from fluvo.lib.transform import Processor
from fluvo.lib import mapper

my_mapping = {
    'id': mapper.m2o('product', 'SKU'),  # external id, e.g. product.A1
    'name': mapper.val('ProductName'),
    'list_price': mapper.num('Price'),
}

processor = Processor(my_mapping, source_filename='origin/products.csv', separator=',')
processor.process('data/products_clean.csv', {'model': 'product.product'})
processor.write_to_file("load.sh")
```
...
```console
$ python transform.py
```
**2. Load the clean data into Odoo using the CLI.**
The `transform.py` script generates a `load.sh` file containing the correct CLI command.

```bash
# Contents of the generated load.sh
fluvo import --connection-file conf/connection.conf --file data/products_clean.csv --model product.product ...
```

Then execute the script.
```console
$ bash load.sh
```

When the import command runs, it automatically detects the data structure. If it finds relational data like parent_id fields, it will automatically switch to a robust two-pass strategy to ensure the import succeeds.

## Documentation

For a complete user guide, tutorials, and API reference, please see the **[full documentation on Read the Docs][read the docs]**.
Please see the [Command-line Reference] for details.

## Contributing

Contributions are very welcome.
To learn more, see the [Contributor Guide].

## License

Distributed under the terms of the [LGPL 3.0 license][license],
_Fluvo_ is free and open source software.

## Issues

If you encounter any problems,
please [file an issue] along with a detailed description.

## Credits

This development of project is financially supported by [stefcy.com].

[stefcy.com]: https://stefcy.com
[@bosd]: https://github.com/bosd
[pypi]: https://pypi.org/
[file an issue]: https://github.com/getfluvo/fluvo/issues
[pip]: https://pip.pypa.io/

<!-- github-only -->

[license]: https://github.com/getfluvo/fluvo/blob/master/LICENSE
[contributor guide]: https://github.com/getfluvo/fluvo/blob/master/CONTRIBUTING.md
[command-line reference]: https://fluvo.readthedocs.io/en/latest/usage.html
