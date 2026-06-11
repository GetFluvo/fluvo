# Guide: Automation & Workflows

The `fluvo` library includes a powerful system for running automated actions directly on your Odoo server. These actions are split into two main categories:

* **Module Management (`module` command):** For administrative tasks like installing, uninstalling, or updating the list of available modules and languages. These are typically run to prepare an Odoo environment.
* **Data Workflows (`workflow` command):** For running multi-step processes on data that already exists in the database, such as validating a batch of imported invoices.

---

## Module Management

You can automate the installation, upgrade, and uninstallation of modules and languages directly from the command line. This is particularly useful for setting up a new database or ensuring your environments are consistent.

### Step 1: Updating the Apps List

Before you can install a new module, you must first ensure that Odoo is aware of it. The `module update-list` command scans your Odoo instance's addons path and updates the list of available modules.

This is a crucial first step to run after you have added new custom modules to your server. The command will wait for the server to complete the scan before finishing, so you can safely chain it with an installation command.

#### Usage
```bash
fluvo module update-list
```

#### Command-Line Options

| Option | Description |
| :--- | :--- |
| `-c`, `--config` | **(Optional)** Path to your `connection.conf` file. Defaults to `conf/connection.conf`. |

### Step 2: Installing Languages

Before installing modules that have their own translations, you may need to install the required languages in your database.

#### Usage
```bash
fluvo module install-languages --languages nl_BE,fr_FR
```

#### Command-Line Options

| Option | Description |
| :--- | :--- |
| `-c`, `--config` | **(Optional)** Path to your `connection.conf` file. Defaults to `conf/connection.conf`. |
| `-l`, `--languages`| **(Required)** A comma-separated string of language codes to install (e.g., 'nl_BE,fr_FR'). |


### Step 3: Installing or Upgrading Modules


The `module install` command will install new modules or upgrade them if they are already installed.

#### Usage
```bash
fluvo module install --modules sale_management,mrp
```

#### Command-Line Options

| Option | Description |
| :--- | :--- |
| `-c`, `--config` | **(Optional)** Path to your `connection.conf` file. Defaults to `conf/connection.conf`. |
| `-m`, `--modules`| **(Required)** A comma-separated string of technical module names to install or upgrade. |

### Uninstalling Modules

The `module uninstall` command will uninstall modules that are currently installed.

#### Usage
```bash
fluvo module uninstall --modules stock,account
```

#### Command-Line Options

| Option | Description |
| :--- | :--- |
| `-c`, `--config` | **(Optional)** Path to your `connection.conf` file. Defaults to `conf/connection.conf`. |
| `-m`, `--modules`| **(Required)** A comma-separated string of technical module names to uninstall. |

---

## VAT Validation Management

When importing large numbers of contacts into Odoo, VAT validation can cause significant issues:

* **VIES (VAT Information Exchange System):** Online EU VAT validation that causes API timeouts with many contacts
* **stdnum validation:** Python-based local format checking that adds CPU overhead

The `vat` command group allows you to temporarily disable these validations during imports and restore them afterwards.

### Checking Current Settings

Before making changes, you can check the current VAT validation settings for all companies:

```bash
fluvo vat get-settings --connection-file conf/connection.conf
```

This displays a table showing which companies have VIES checking enabled.

### Disabling VAT Validation for Import

To disable VAT validation before a large contact import:

```bash
# Disable and save settings to a file for later restoration
fluvo vat disable --connection-file conf/connection.conf --output vat_settings.json

# Disable only VIES (keep stdnum validation)
fluvo vat disable --connection-file conf/connection.conf --no-stdnum --output vat_settings.json

# Disable for specific companies only
fluvo vat disable --connection-file conf/connection.conf --company-ids 1,2,3 --output vat_settings.json
```

#### Command-Line Options

| Option | Description |
| :--- | :--- |
| `--connection-file` | **(Required)** Path to your connection config file. |
| `--company-ids` | Comma-separated list of company IDs. If omitted, applies to all companies. |
| `--vies/--no-vies` | Enable/disable VIES online check. Default: disable. |
| `--stdnum/--no-stdnum` | Enable/disable stdnum format validation. Default: disable. |
| `--output` | Save original settings to a JSON file for later restoration. |

### Restoring VAT Validation Settings

After your import is complete, restore the original settings:

```bash
fluvo vat restore --connection-file conf/connection.conf --input vat_settings.json
```

### Batch VAT Validation

After importing contacts with validation disabled, you can validate VAT numbers in controlled batches to avoid API timeouts:

```bash
# Validate all partners with VAT numbers
fluvo vat validate --connection-file conf/connection.conf --batch-size 50 --delay 1.0

# Validate with user notifications on failures
fluvo vat validate --connection-file conf/connection.conf --notify-users 1,2

# Validate only companies
fluvo vat validate --connection-file conf/connection.conf \
    --domain "[('is_company', '=', True)]" \
    --max-records 1000
```

#### Command-Line Options

| Option | Description |
| :--- | :--- |
| `--connection-file` | **(Required)** Path to your connection config file. |
| `--batch-size` | Number of records per batch. Default: 50. |
| `--delay` | Seconds to wait between batches. Default: 1.0. |
| `--notify-users` | Comma-separated user IDs to notify on failures. |
| `--domain` | Odoo domain filter (e.g., `"[('is_company', '=', True)]"`). |
| `--max-records` | Maximum number of records to validate. |

### Complete Import Workflow Example

Here's a typical workflow for importing contacts with VAT validation management:

```bash
# 1. Save current settings and disable validation
fluvo vat disable --connection-file conf/connection.conf --output vat_settings.json

# 2. Run the contact import
fluvo import --connection-file conf/connection.conf \
    --file contacts.csv \
    --model res.partner \
    --worker 4 \
    --size 500

# 3. Restore VAT validation settings
fluvo vat restore --connection-file conf/connection.conf --input vat_settings.json

# 4. Validate VAT numbers in batches with notifications
fluvo vat validate --connection-file conf/connection.conf \
    --batch-size 50 \
    --delay 2.0 \
    --notify-users 1
```

### Programmatic Usage

You can also use these functions programmatically in Python:

```python
from fluvo.lib.actions.vies_manager import (
    disable_vat_validation,
    restore_vat_validation_settings,
    run_import_with_vat_validation_disabled,
)

# Option 1: Manual control
settings = disable_vat_validation("conf/connection.conf")
# ... run your import ...
restore_vat_validation_settings("conf/connection.conf", settings)

# Option 2: Context manager style
from fluvo.importer import run_import

result = run_import_with_vat_validation_disabled(
    config="conf/connection.conf",
    import_func=run_import,
    import_kwargs={
        "config": "conf/connection.conf",
        "filename": "contacts.csv",
        "model": "res.partner",
        # ... other import options
    },
)
```

### Custom VAT Validators

For high-performance VAT validation, you can replace the default Python validator with a custom implementation (e.g., Rust-based via PyO3):

```python
from fluvo.lib.actions.vies_manager import (
    set_custom_vat_validator,
    validate_vat_local,
)

# Define a custom validator function
def my_rust_validator(vat: str) -> tuple[bool, str | None]:
    # Call your Rust library here
    from my_rust_vat_lib import validate
    result = validate(vat)
    return result.is_valid, result.error_message

# Register it
set_custom_vat_validator(my_rust_validator)

# Now validate_vat_local() will use your custom validator
is_valid, error = validate_vat_local("BE0123456789")
```

---

## Product Variant Management

When Odoo creates a `product.template` through the ORM, it automatically creates a
default variant (`product.product`). The `load()` API used for imports does **not**
do this, so templates imported (or migrated) without attribute lines can end up
with **no variants** — making them unusable in sales/purchase orders, BoMs, etc.

The `workflow create-missing-variants` command finds every `product.template` with
`product_variant_count == 0` and creates the missing default variant for it.

```bash
# Dry run first: just report how many templates lack a variant.
fluvo workflow create-missing-variants --connection-file conf/connection.conf --dry-run

# Create the missing variants.
fluvo workflow create-missing-variants --connection-file conf/connection.conf

# Scope to a subset with an Odoo domain (Python list literal).
fluvo workflow create-missing-variants --connection-file conf/connection.conf \
    --domain "[('categ_id', '=', 5)]"
```

| Argument | Description |
|---|---|
| `--connection-file` | **(Required)** Path to the Odoo connection file. |
| `--domain` | Optional Odoo domain (Python list literal) to scope which templates are checked. Combined with the no-variant filter. |
| `--batch-size` | Number of variants to create per RPC call (default: 200). |
| `--dry-run` | Only report how many templates lack a variant; create nothing. |

---

## Data Processing Workflows

This command group is for running multi-step processes on records that are already in the database.

### The `invoice-v9` Workflow (Legacy Example)

The library also includes a built-in workflow specifically for processing customer invoices (`account.invoice`) in **Odoo version 9**.

**Warning:** This workflow uses legacy Odoo v9 API calls and will **not** work on modern Odoo versions (10.0+). It is provided as a reference and an example of how a post-import process can be structured.

The workflow allows you to perform the following actions on your imported invoices:

- **`tax`**: Computes taxes for imported draft invoices.
- **`validate`**: Validates draft invoices, moving them to the 'Open' state.
- **`pay`**: Registers a payment against an open invoice, moving it to the 'Paid' state.
- **`proforma`**: Converts draft invoices to pro-forma invoices.
- **`rename`**: A utility to move a value from a custom field to the official `number` field.

#### Usage

You run the workflow from the command line, specifying which action(s) you want to perform.

```bash
fluvo workflow invoice-v9 [OPTIONS]
```

#### Command-Line Options

| Option              | Description                                                                                                                                                      |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `-c`, `--config`      | Path to your `connection.conf` file. Defaults to `conf/connection.conf`.                                                                                         |
| `--action`          | The workflow action to run (`tax`, `validate`, `pay`, `proforma`, `rename`). This option can be used multiple times. If omitted, all actions are run.              |
| `--field`           | **Required**. The name of the field in `account.invoice` that holds the legacy status from your source system. The workflow uses this to find the right invoices. |
| `--status-map`      | **Required**. A dictionary string that maps Odoo states to your legacy statuses. For example: `"{'open': ['OP', 'Validated'], 'paid': ['PD']}"`                   |
| `--paid-date-field` | **Required**. The name of the field containing the payment date, used by the `pay` action.                                                                         |
| `--payment-journal` | **Required**. The database ID (integer) of the `account.journal` to be used for payments.                                                                          |
| `--max-connection`  | The number of parallel threads to use for processing. Defaults to `4`.                                                                                           |

### Example Command

Imagine you have imported thousands of invoices. Now, you want to find all the invoices with a legacy status of "Validated" and move them to the "Open" state in Odoo.

You would run the following command:

```bash
fluvo workflow invoice-v9 \
    --config conf/connection.conf \
    --action validate \
    --field x_studio_legacy_status \
    --status-map "{'open': ['Validated']}" \
    --paid-date-field x_studio_payment_date \
    --payment-journal 5
```

This command will:

1. Connect to Odoo.
2. Search for all `account.invoice` records where `x_studio_legacy_status` is 'Validated'.
3. Run the `validate_invoice` function on those records, triggering the workflow to open them.
