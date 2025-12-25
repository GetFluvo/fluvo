# API Reference

This section provides an auto-generated API reference for the core components of the `odoo-data-flow` library.

## Command-Line Interface (`__main__`)

This module contains the main `click`-based command-line interface.

```{eval-rst}
.. click:: odoo_data_flow.__main__:cli
  :prog: odoo-data-flow
  :nested: full
```

## Transformation Processor (`lib.transform`)

This module contains the main `Processor` class used for data transformation.

```{eval-rst}
.. automodule:: odoo_data_flow.lib.transform
   :members: Processor
   :member-order: bysource
```

## Mapper Functions (`lib.mapper`)

This module contains all the built-in `mapper` functions for data transformation.

```{eval-rst}
.. automodule:: odoo_data_flow.lib.mapper
   :members:
   :undoc-members:
```

## High-Level Runners

These modules contain the high-level functions that are called by the CLI commands.

### Importer (`importer`)

```{eval-rst}
.. automodule:: odoo_data_flow.importer
   :members: run_import
```

### Exporter (`exporter`)

```{eval-rst}
.. automodule:: odoo_data_flow.exporter
   :members: run_export
```

### Migrator (`migrator`)

```{eval-rst}
.. automodule:: odoo_data_flow.migrator
   :members: run_migration
```

## Actions

These modules contain action functions for managing Odoo server state.

### VIES/VAT Manager (`lib.actions.vies_manager`)

This module provides functions for managing VAT validation settings during imports.

```{eval-rst}
.. automodule:: odoo_data_flow.lib.actions.vies_manager
   :members: get_vat_validation_settings, disable_vat_validation, restore_vat_validation_settings, run_vies_validation, run_import_with_vat_validation_disabled, validate_vat_format, validate_vat_local, set_custom_vat_validator
   :member-order: bysource
```

### Module Manager (`lib.actions.module_manager`)

This module provides functions for managing Odoo modules.

```{eval-rst}
.. automodule:: odoo_data_flow.lib.actions.module_manager
   :members: run_module_installation, run_module_uninstallation, run_update_module_list
   :member-order: bysource
```
