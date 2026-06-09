# API Reference

This section provides an auto-generated API reference for the core components of the `fluvo` library.

## Command-Line Interface (`__main__`)

This module contains the main `click`-based command-line interface.

```{eval-rst}
.. click:: fluvo.__main__:cli
  :prog: fluvo
  :nested: full
```

## Transformation Processor (`lib.transform`)

This module contains the main `Processor` class used for data transformation.

```{eval-rst}
.. automodule:: fluvo.lib.transform
   :members: Processor
   :member-order: bysource
```

## Mapper Functions (`lib.mapper`)

This module contains all the built-in `mapper` functions for data transformation.
These are row-by-row functions that work with Python dictionaries.

```{eval-rst}
.. automodule:: fluvo.lib.mapper
   :members:
   :undoc-members:
```

## Expression-Based Mappers (`lib.expr`)

This module provides high-performance Polars expression-based mappers.
These return `pl.Expr` objects that leverage Polars' vectorized execution engine
for 10-100x speedups compared to the row-by-row `mapper` functions.

```{eval-rst}
.. automodule:: fluvo.lib.expr
   :members:
   :undoc-members:
```

## High-Level Runners

These modules contain the high-level functions that are called by the CLI commands.

### Importer (`importer`)

```{eval-rst}
.. automodule:: fluvo.importer
   :members: run_import
```

### Exporter (`exporter`)

```{eval-rst}
.. automodule:: fluvo.exporter
   :members: run_export
```

### Migrator (`migrator`)

```{eval-rst}
.. automodule:: fluvo.migrator
   :members: run_migration
```

## Actions

These modules contain action functions for managing Odoo server state.

### VIES/VAT Manager (`lib.actions.vies_manager`)

This module provides functions for managing VAT validation settings during imports.

```{eval-rst}
.. automodule:: fluvo.lib.actions.vies_manager
   :members: get_vat_validation_settings, disable_vat_validation, restore_vat_validation_settings, run_vies_validation, run_import_with_vat_validation_disabled, validate_vat_format, validate_vat_local, set_custom_vat_validator
   :member-order: bysource
```

### Module Manager (`lib.actions.module_manager`)

This module provides functions for managing Odoo modules.

```{eval-rst}
.. automodule:: fluvo.lib.actions.module_manager
   :members: run_module_installation, run_module_uninstallation, run_update_module_list
   :member-order: bysource
```
