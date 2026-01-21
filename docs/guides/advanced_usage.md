# Guide: Advanced Usage

This guide covers more complex scenarios and advanced features of the library that can help you solve specific data transformation challenges.


## Processing XML Files

While CSV is common, you may have source data in XML format. The `Processor` can handle XML files with a couple of extra configuration arguments.

- **`xml_root_tag` (str)**: The name of the root tag in your XML document that contains the collection of records.
- **`xml_record_tag` (str)**: The name of the tag that represents a single record.

### Example XML Input (`origin/clients.xml`)

Here is an example of an XML file that the `Processor` can parse. Note the `<ClientList>` container tag and the repeating `<Client>` tags for each record. The processor can also handle nested tags like `<Contact>`.

```{code-block} xml
:caption: origin/clients.xml
<?xml version="1.0" encoding="UTF-8"?>
<ClientList>
    <Client>
        <ClientID>C1001</ClientID>
        <Name>The World Company</Name>
        <Contact>
            <Email>contact@worldco.com</Email>
            <Phone>111-222-3333</Phone>
        </Contact>
    </Client>
    <Client>
        <ClientID>C1002</ClientID>
        <Name>The Famous Company</Name>
        <Contact>
            <Email>info@famous.com</Email>
            <Phone>444-555-6666</Phone>
        </Contact>
    </Client>
</ClientList>
```

### Example Transformation Code
To process this XML, you provide an XPath expression to the xml_root_tag argument. This tells the Processor which nodes in the XML tree represent the individual records (rows) you want to process. The tags inside each record are then treated as columns.

xml_root_tag: An XPath expression to select the list of records. For the example above, './Client' tells the processor to find every <Client> tag within the document.

The Processor automatically flattens the nested structure, so you can access tags like <Email> and <Phone> directly in your mapping.


```python
from odoo_data_flow.lib.transform import Processor
from odoo_data_flow.lib import mapper

# Access nested XML tags using dot notation.
res_partner_mapping = {
    'id': mapper.m2o_map('xml_client_', 'ClientID'),
    'name': mapper.val('Name'),
    'email': mapper.val('Contact.Email'),
    'phone': mapper.val('Contact.Phone'),
}

# Initialize the Processor with XML-specific arguments
processor = Processor(
    'origin/clients.xml',
    xml_root_tag='ClientList',
    xml_record_tag='Client'
)
# ... rest of the process
```

---

## Importing Data for Multiple Companies

When working in a multi-company Odoo environment, you need a clear strategy to ensure records are created in the correct company. There are two primary methods to achieve this.

### Method 1: The Procedural Approach (Recommended)

This is the safest and most common approach. The core idea is to separate your data by company and run a distinct import process for each one.

1.  **Separate your source files:** Create one set of data files for Company A and a completely separate set for Company B.
2.  **Set the User's Company:** In Odoo, log in as the user defined in your `connection.conf`. In the user preferences, set their default company to **Company A**.
3.  **Run the Import for Company A:** Execute your transformation and load scripts for Company A's data. All records created will be assigned to Company A by default.
4.  **Change the User's Company:** Go back to Odoo and change the same user's default company to **Company B**.
5.  **Run the Import for Company B:** Execute the import process for Company B's data. These new records will now be correctly assigned to Company B.

This method is robust because it relies on Odoo's standard multi-company behavior and prevents accidental data mixing.

### Method 2: The Programmatic Approach (`company_id`)

This method is useful when your source file contains data for multiple companies mixed together. You can explicitly tell Odoo which company a record belongs to by mapping a value to the `company_id/id` field.

**Example: A source file with mixed-company products**

```text
SKU,ProductName,CompanyCode
P100,Product A,COMPANY_US
P101,Product B,COMPANY_EU
```

**Transformation Script**
Your mapping dictionary can use the `CompanyCode` to link to the correct company record in Odoo using its external ID.

```python
from odoo_data_flow.lib import mapper

product_mapping = {
    'id': mapper.m2o_map('prod_', 'SKU'),
    'name': mapper.val('ProductName'),
    # This line explicitly sets the company for each row.
    # Assumes your res.company records have external IDs like 'main_COMPANY_US'.
    'company_id/id': mapper.m2o_map('main_', 'CompanyCode'),
}
```

**Warning:** While powerful, this method requires that you have stable and correct external IDs for your `res.company` records. The procedural approach is often simpler and less error-prone.

---

## Importing Company-Dependent Fields (Cost Prices)

Some fields in Odoo are **company-dependent**, meaning the same record can have different values for different companies. The most common example is `standard_price` (cost price) on `product.product`.

### Automatic Detection

The importer automatically detects company-dependent fields and shows a warning:

```
╭───────────────────── Company-Dependent Fields Detected ──────────────────────╮
│ The following fields are company-dependent:                                  │
│   - 'standard_price' (float)                                                 │
│                                                                              │
│ Important: These fields store separate values per company.                   │
│ Without --company-id, values will only be set for the first company          │
│ in allowed_company_ids (usually company 1).                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

This warning helps you identify when you need the special multi-company workflow described below.

### Understanding Company-Dependent Fields

In Odoo, company-dependent fields store separate values per company. For example:
- Product "Widget A" can have cost price €10 in Company 1
- The same product can have cost price €15 in Company 2
- This is essential for intercompany scenarios where products are sourced internally

**Key fields that are company-dependent:**
- `product.product.standard_price` - Product cost price
- `product.template.property_account_income_id` - Income account
- `product.template.property_account_expense_id` - Expense account
- Various accounting properties

### Why `--sudo --all-companies` Doesn't Work for Cost Prices

When you import products with `--sudo --all-companies`, the cost price is only set for **Company 1** (the first company in `allowed_company_ids`). This is because Odoo stores company-dependent field values based on the first company in the context.

```bash
# This creates products across all companies, BUT...
# standard_price is only set for Company 1!
odoo-data-flow import \
    --file data/products.csv \
    --model product.product \
    --sudo --all-companies
```

### Recommended Workflow (Two-Step Import)

**Step 1: Import products WITHOUT cost prices**

Either exclude `standard_price` from your CSV, or use `--ignore`:

```bash
# Option A: CSV without standard_price
odoo-data-flow import \
    --file data/products.csv \
    --model product.product \
    --sudo --all-companies

# Option B: Ignore the cost price field
odoo-data-flow import \
    --file data/products_with_costs.csv \
    --model product.product \
    --sudo --all-companies \
    --ignore standard_price
```

**Step 2: Import cost prices per company**

Create separate cost price files (just `id` and `standard_price`):

```csv
# costs_company_1.csv
id;standard_price
PRODUCT.SKU001;100.50
PRODUCT.SKU002;75.00
```

Import for each company:

```bash
# Import costs for Company 1 (using database ID)
odoo-data-flow import \
    --file data/costs_company_1.csv \
    --model product.product \
    --company-id 1

# Import costs for Company 2 (using XML ID)
odoo-data-flow import \
    --file data/costs_company_2.csv \
    --model product.product \
    --company-id my_module.company_germany
```

!!! tip "XML IDs for Companies"
    The `--company-id` flag accepts both database IDs (e.g., `1`, `2`) and XML IDs
    (e.g., `base.main_company`, `my_module.company_germany`). Using XML IDs makes
    your import scripts more portable across environments.

### Transformation Script for Multi-Company Costs

Update your transformation scripts to generate company-specific cost files:

```python
from odoo_data_flow.lib.transform import Processor
from odoo_data_flow.lib import mapper

# Main product mapping (without standard_price)
product_mapping = {
    'id': mapper.concat('PRODUCT.', 'SKU'),
    'name': mapper.val('ProductName'),
    'default_code': mapper.val('SKU'),
    'type': mapper.const('consu'),
    # NO standard_price here!
}

# Process main product file
processor = Processor('origin/products.csv')
processor.process(
    mapping=product_mapping,
    filename_out='data/products.csv',
    params={
        'model': 'product.product',
        # Use sudo/all-companies for product creation
    }
)

# Cost price mapping (just id + standard_price)
cost_mapping = {
    'id': mapper.concat('PRODUCT.', 'SKU'),
    'standard_price': mapper.val('Cost'),
}

# Generate cost files per company
# If costs are the same, use the same source file
# If costs differ, you need source data per company
companies = [1, 2, 3, 5]

for company_id in companies:
    processor = Processor('origin/products.csv')  # Or company-specific source
    processor.process(
        mapping=cost_mapping,
        filename_out=f'data/costs_company_{company_id}.csv',
        params={
            'model': 'product.product',
        }
    )
```

### Shell Script for Multi-Company Cost Import

```bash
#!/bin/bash
# import_products_with_costs.sh

CONFIG="conf/connection.conf"

# Step 1: Import products (without costs)
echo "Step 1: Importing products..."
odoo-data-flow import \
    --connection-file "$CONFIG" \
    --file data/products.csv \
    --model product.product \
    --sudo --all-companies

# Step 2: Import cost prices per company
COMPANIES=(1 2 3 5)  # Your company IDs

for COMPANY_ID in "${COMPANIES[@]}"; do
    COST_FILE="data/costs_company_${COMPANY_ID}.csv"
    if [ -f "$COST_FILE" ]; then
        echo "Step 2: Importing costs for company $COMPANY_ID..."
        odoo-data-flow import \
            --connection-file "$CONFIG" \
            --file "$COST_FILE" \
            --model product.product \
            --company-id "$COMPANY_ID"
    else
        echo "Warning: $COST_FILE not found, skipping company $COMPANY_ID"
    fi
done

echo "Done!"
```

### Verifying Cost Prices

After import, verify that cost prices are correctly set for each company:

```python
from odoo_data_flow.lib.conf_lib import get_connection_from_config

conn = get_connection_from_config("conf/connection.conf")
product = conn.get_model('product.product')

# Find product by external ID or code
prod_id = product.search([('default_code', '=', 'SKU001')])[0]

# Read cost price for different companies
for company_id in [1, 2, 3]:
    data = product.read(
        prod_id,
        ['standard_price'],
        context={'allowed_company_ids': [company_id]}
    )
    print(f"Company {company_id}: {data['standard_price']}")
```

### Common Issues and Solutions

| Issue | Cause | Solution |
|-------|-------|----------|
| Same cost for all companies | Missing `--company-id` flag | Add `--company-id X` to each import |
| Access error during import | User lacks company access | Ensure import user has access to target company |
| Cost not updating | Existing record not being found | Verify external ID matches existing product |
| Wrong company context | Context not properly set | Use `--company-id` instead of manual context |

### Best Practices

1. **Use external IDs**: Always reference products by external ID (`id` column) rather than database ID
2. **One file per company**: Cleaner and easier to debug than mixed files
3. **Verify after import**: Always check a few products to confirm costs are correct
4. **Document your process**: Keep notes on which files go to which companies
5. **Use `--skip-existing` for re-runs**: Safe to run multiple times without errors

---

## Multi-Environment Imports

When working with multiple Odoo environments (e.g., test, UAT, production), the importer automatically organizes fail files into environment-specific subfolders based on your connection file name.

### How It Works

The environment name is extracted from your connection file:

| Connection File | Environment | Fail File Location |
|----------------|-------------|-------------------|
| `test_connection.conf` | `test` | `data/test/res_partner_fail.csv` |
| `uat_connection.conf` | `uat` | `data/uat/res_partner_fail.csv` |
| `prod_connection.conf` | `prod` | `data/prod/res_partner_fail.csv` |
| `uat.conf` | `uat` | `data/uat/res_partner_fail.csv` |

The `_connection` suffix is automatically stripped to determine the environment name.

### Example: Importing to Multiple Environments

**Directory Structure:**
```
project/
├── data/
│   └── res_partner.csv
├── test_connection.conf
├── uat_connection.conf
└── prod_connection.conf
```

**Import to UAT:**
```bash
odoo-data-flow import \
    --connection-file uat_connection.conf \
    --file data/res_partner.csv \
    --model res.partner
```

If any records fail, they are written to `data/uat/res_partner_fail.csv`.

**Retry Failed Records:**
```bash
odoo-data-flow import \
    --connection-file uat_connection.conf \
    --file data/res_partner.csv \
    --model res.partner \
    --fail
```

The `--fail` flag automatically looks for the fail file in the correct environment folder (`data/uat/res_partner_fail.csv`).

### Benefits

- **Isolated environments**: Fail files from different environments don't mix
- **Easy retry**: The `--fail` flag finds the correct fail file automatically
- **Clean organization**: Each environment has its own subfolder for tracking failures
- **Automatic folder creation**: Environment folders are created automatically when needed

---

## Importing Translations

The most efficient way to import translations is to perform a standard import with a special `lang` key in the context. This lets Odoo's ORM handle the translation creation process correctly.

The process involves two steps:

1.  **Import the base terms:** First, import your records with their default language values (e.g., English).
2.  **Import the translated terms:** Then, import a second file containing only the external IDs and the translated values, while setting the target language in the context.

### Example: Translating Product Names to French

**Step 1: Import the base product data in English**

**Source File (`product_template.csv`):**

```text
id;name;price
my_module.product_wallet;Wallet;10.0
my_module.product_bicyle;Bicycle;400.0
```

You would import this file normally. The `id` column provides the stable external ID for each product.

**Step 2: Import the French translations**

**Source File (`product_template_FR.csv`):**
This file only needs to contain the external ID and the fields that are being translated.

```text
id;name
my_module.product_wallet;Portefeuille
my_module.product_bicyle;Bicyclette
```

**Transformation and Load**
While you can use a `transform.py` script to generate the load script, for a simple translation update, you can also run the command directly.

**Command-line Example:**

```bash
odoo-data-flow import \
    --config conf/connection.conf \
    --file product_template_FR.csv \
    --model product.template \
    --context "{'lang': 'fr_FR'}"
```

This does not overwrite the English name; instead, it correctly creates or updates the French translation for the `name` field on the specified products.

---

## Importing Account Move Lines

Importing journal entries (`account.move`) with their debit/credit lines (`account.move.line`) is a classic advanced use case that requires creating related records using `mapper.record` and stateful processing.

### Performance Tip: Skipping Validation

For a significant performance boost when importing large, pre-validated accounting entries, you can tell Odoo to skip its balancing check (debits == credits) during the import. This is done by passing a special context key.

### Example: Importing an Invoice

**Source File: `invoices.csv`**

```text
Journal,Reference,Date,Account,Label,Debit,Credit
INV,INV2023/12/001,2023-12-31,,,
,,"Customer Invoices",600,"Customer Debtor",250.00,
,,"Customer Invoices",400100,"Product Sales",,200.00
,,"Customer Invoices",451000,"VAT Collected",,50.00
```

**Transformation Script**

```python
from odoo_data_flow.lib.transform import Processor
from odoo_data_flow.lib import mapper

# ... (see Data Transformations guide for full stateful processing example)

# Define parameters, including the crucial context key
params = {
    'model': 'account.move',
    # WARNING: Only use check_move_validity: False if you are certain
    # your source data is balanced.
    'context': "{'check_move_validity': False, 'tracking_disable': True}"
}

processor = Processor('origin/invoices.csv')
# ... rest of process
```

---

## Importing One-to-Many Relationships (`--o2m` flag)

The `--o2m` flag enables a special import mode for handling source files where child records (the "many" side) are listed directly under their parent record (the "one" side).

### Use Case and File Structure

This mode is designed for files structured like this, where a master record has lines for two different one-to-many fields (`child1_ids` and `child2_ids`):

**Source File (`master_with_children.csv`)**

```text
MasterID,MasterName,Child1_SKU,Child2_Ref
M01,Master Record 1,field_value1_of_child1,field_value1_of_child2
, , , field_value2_of_child1,field_value2_of_child2
, , , ,field_value3_of_child2
```

With the `--o2m` option, the processor understands that the lines with empty master fields belong to the last master record encountered. It will import "Master Record 1" with two `child1` records and three `child2` records simultaneously.

!!! info "When to use --o2m vs. Automatic Two-Pass"
The `--o2m` flag is specifically for the file format shown above, where child records do not have their own unique ID and are identified only by being on the lines below their parent.
For standard relational fields (like `parent_id`) where **every record in the file has its own unique ID**, you do not need this flag. The importer will automatically detect the relationship and use the two-pass strategy.



### Transformation and Load

Your mapping would use `mapper.record` and `mapper.cond` to process the child lines, similar to the `account.move.line` example. The key difference is enabling the `o2m` flag in your `params` dictionary.

```python
# In your transform.py
params = {
    'model': 'master.model',
    'o2m': True # Enable the special o2m handling
}
```

The generated `load.sh` script will then include the `--o2m` flag in the `odoo-data-flow import` command.

### Important Limitations

This method is convenient but has significant consequences because **it is impossible to set XML_IDs on the child records**. As a result:

- You **cannot run the import again to update** the child records. Any re-import will create new child records.
- The child records **cannot be referenced** by their external ID in any other import file.

This method is best suited for simple, one-off imports of transactional data where the child lines do not need to be updated or referenced later.

---

## Advanced Product Imports: Creating Variants

When you import `product.template` records along with their attributes and values, Odoo does not create the final `product.product` variants by default. You must explicitly tell Odoo to do so using a context key.

### The `create_product_product` Context Key

By setting `create_product_product: True` in the context of your `product.template` import, you trigger the Odoo mechanism that generates all possible product variants based on the attribute lines you have imported for that template.

This is typically done as the final step _after_ you have already imported the product attributes, attribute values, and linked them to the templates via attribute lines.

### Example: Triggering Variant Creation

Assume you have already run separate imports for `product.attribute`, `product.attribute.value`, and `product.attribute.line`. Now, you want to trigger the variant creation.

The easiest way is to re-import your `product.template.csv` file with the special context key.

**Transformation and Load**
In the `params` dictionary of your `product.template` transformation script, add the key:

```python
# In your transform.py for product templates

params = {
    'model': 'product.template',
    # This context key tells Odoo to generate the variants
    'context': "{'create_product_product': True, 'tracking_disable': True}"
}

# The mapping would be the same as your initial template import
template_mapping = {
    'id': mapper.m2o_map('prod_tmpl_', 'Ref'),
    'name': mapper.val('Name'),
    # ... other template fields
}
```

When you run the generated `load.sh` script for this process, Odoo will find each product template, look at its attribute lines, and create all the necessary `product.product` variants (e.g., a T-Shirt in sizes S, M, L and colors Red, Blue).

---

## Merging Data from Multiple Files (`join_file`)

Sometimes, the data you need for a single import is spread across multiple source files. The `.join_file()` method allows you to enrich your main dataset by merging columns from a second file, similar to a VLOOKUP in a spreadsheet.

### The `.join_file()` Method

You first initialize a `Processor` with your primary file. Then, you call `.join_file()` to merge data from a secondary file based on a common key.

- **`filename` (str)**: The path to the secondary file to merge in.
- **`key1` (str)**: The name of the key column in the **primary** file.
- **`key2` (str)**: The name of the key column in the **secondary** file.

### Example: Merging Customer Details into an Order File

**Transformation Script (`transform_merge.py`)**

```{code-block} python
:caption: transform_merge.py
from odoo_data_flow.lib.transform import Processor
from odoo_data_flow.lib import mapper

# 1. Initialize a processor with the primary file (orders)
processor = Processor('origin/orders.csv')

# 2. Join the customer details file.
print("Joining customer details into orders data...")
processor.join_file('origin/customer_details.csv', 'CustomerCode', 'Code')

# 3. Define a mapping that uses columns from BOTH files
order_mapping = {
    'id': mapper.m2o_map('import_so_', 'OrderID'),
    'name': mapper.val('OrderID'),
    'date_order': mapper.val('OrderDate'),
    # 'ContactPerson' comes from the joined file
    'x_studio_contact_person': mapper.val('ContactPerson'),
}

# The processor now contains the merged data and can be processed as usual
processor.process(
    mapping=order_mapping,
    filename_out='data/orders_with_details.csv',
    params={'model': 'sale.order'}
)
```

---

## Splitting Large Datasets for Import

When dealing with extremely large source files, processing everything in a single step can be memory-intensive and unwieldy. The library provides a `.split()` method on the `Processor` to break down a large dataset into smaller, more manageable chunks.

### The `.split()` Method

The `.split()` method divides the processor's in-memory dataset into a specified number of parts. It does not write any files itself; instead, it returns a dictionary where each key is an index and each value is a new, smaller `Processor` object containing a slice of the original data.

You can then iterate over this dictionary to process each chunk independently.

### Example: Splitting a Large File into 4 Parts

**Transformation Script (`transform_split.py`)**

```{code-block} python
:caption: transform_split.py
from odoo_data_flow.lib.transform import Processor
from odoo_data_flow.lib import mapper

# 1. Define your mapping as usual
product_mapping = {
    'id': mapper.concat('large_prod_', 'SKU'),
    'name': mapper.val('ProductName'),
}

# 2. Initialize a single processor with the large source file
processor = Processor('origin/large_products.csv')

# 3. Split the processor into 4 smaller, independent processors
split_processors = processor.split(mapper.split_file_number(4))

# 4. Loop through the dictionary of new processors
for index, chunk_processor in split_processors.items():
    output_filename = f"data/products_chunk_{index}.csv"
    chunk_processor.process(
        mapping=product_mapping,
        filename_out=output_filename,
        params={'model': 'product.product'}
    )
```

---

## VAT Validation Settings Recovery

When importing contact data, the importer can temporarily disable VAT validation (VIES and stdnum checks) to prevent timeouts and performance issues. If the restoration of these settings fails (e.g., due to a 503 error or connection timeout), the settings may remain in a "disabled" state.

To prevent settings from being permanently lost, the importer uses a **file-based backup system** that preserves the original settings across import runs.

### How the Backup System Works

1. **Before disabling**: Original VAT settings are saved to a JSON backup file
2. **After import**: Settings are restored with automatic retry on transient errors
3. **On successful restore**: The backup file is deleted
4. **On failed restore**: The backup file is preserved for the next run

**Backup location:** `~/.odoo-data-flow/vat_settings_backup/`

Each database has its own backup file named: `vat_settings_{host}_{database}.json`

### Automatic Recovery

If a backup file exists when starting a new import, the importer recognizes that a previous restoration failed. It will:

1. Use the backed-up settings (the correct original values) instead of polling the database
2. Attempt to restore these settings after the import completes
3. Retry up to 5 times with exponential backoff (2s, 4s, 8s, 16s, 32s) for transient errors

### Manual Recovery

If you notice that VAT validation is stuck in a "disabled" state, you can manually check and restore settings:

**Check if a backup exists:**

```python
from odoo_data_flow.lib.actions.vies_manager import check_vat_settings_backup_status

status = check_vat_settings_backup_status("conf/connection.conf")

if status["exists"]:
    print(f"Backup found at: {status['path']}")
    print(f"Age: {status['age_hours']:.1f} hours")
    print(f"Companies with VIES settings: {status['vies_company_count']}")
    print(f"Stdnum parameters: {status['stdnum_param_count']}")
else:
    print("No backup file found - settings were restored successfully")
```

**Restore settings from backup:**

```python
from odoo_data_flow.lib.actions.vies_manager import restore_vat_settings_from_backup

success = restore_vat_settings_from_backup("conf/connection.conf")

if success:
    print("VAT validation settings restored successfully")
else:
    print("Restoration failed - check logs for details")
```

### Troubleshooting

| Symptom | Cause | Solution |
|---------|-------|----------|
| Backup file keeps reappearing | Restoration fails repeatedly | Check Odoo server logs for errors; verify connection settings |
| VIES check stays disabled | Restoration failed, no backup | Manually enable VIES in Odoo Settings > General Settings |
| Old backup file (days old) | Multiple failed restorations | Use `restore_vat_settings_from_backup()` to manually restore |

!!! warning "Don't delete the backup file manually"
    The backup file contains the original VAT validation settings. If you delete it while settings are in the wrong state, the original values will be lost. Always use `restore_vat_settings_from_backup()` to properly restore and clean up.

---

## Idempotent Imports with `--skip-existing`

When you need to run the same import multiple times to ensure completeness (common for accounting purposes), the `--skip-existing` flag makes imports safely re-runnable.

### The Problem

Without `--skip-existing`, re-running an import with existing external IDs causes Odoo to attempt an **update** instead of a **create**. For certain models like `stock.quant`, updates are restricted:

```
Error: Quant's editing is restricted, you can't do this operation.
```

### The Solution

```bash
odoo-data-flow import \
    --connection-file conf/connection.conf \
    --file data/stock.quant.csv \
    --model stock.quant \
    --skip-existing
```

The `--skip-existing` flag:

1. **Queries `ir.model.data`** to find which external IDs already exist
2. **Filters out** rows with existing external IDs before import
3. **Only imports** truly new records
4. **Logs** which records were skipped

### Example Output

```
INFO: Skip-existing mode: checking for records with existing external IDs...
INFO: Skip-existing filter: 100 -> 5 records (skipped 95 with existing external IDs)
INFO: Example skipped external IDs: ['my_import.quant_001', 'my_import.quant_002'] ... and 93 more
```

### When to Use

| Scenario | Use `--skip-existing`? |
|----------|------------------------|
| First-time import | No |
| Re-running after partial failure | Yes |
| Ensuring all records are imported | Yes |
| Models with update restrictions (stock.quant) | Yes |
| Daily/recurring imports | Yes |

---

## Importing Stock Quantities (`stock.quant`)

Importing stock levels requires special handling due to Odoo's inventory management system.

### Required Context

Stock quant imports require `inventory_mode: True` in the context:

```bash
odoo-data-flow import \
    --connection-file conf/connection.conf \
    --file data/stock.quant.csv \
    --model stock.quant \
    --context "{'inventory_mode': True, 'tracking_disable': True}" \
    --sudo --all-companies \
    --skip-existing \
    --post-action action_apply_inventory
```

### Key Options Explained

| Option | Purpose |
|--------|---------|
| `--context "{'inventory_mode': True}"` | Required to enable inventory adjustments |
| `--sudo` | Bypasses record rules (needed for stock.quant) |
| `--all-companies` | Enables cross-company access |
| `--skip-existing` | Allows safe re-runs without update errors |
| `--post-action action_apply_inventory` | Applies the stock adjustment after import |

### Allowed Fields

In inventory mode, only these fields can be imported:

- `product_id`, `location_id`, `lot_id`, `package_id`, `owner_id`
- `inventory_quantity` (the quantity to set)
- `inventory_date`, `user_id`

!!! warning "Do NOT include `company_id`"
    The company is automatically derived from the location. Including `company_id` in your CSV will cause an error.

### CSV Format

```csv
id;product_id/id;location_id/id;inventory_quantity;lot_id/id
my_import.quant_001;PRODUCT.SKU001;STOCK_LOCATION.WH1;100.0;
my_import.quant_002;PRODUCT.SKU002;STOCK_LOCATION.WH1;50.0;LOT.LOT001
```

### External ID Naming Convention

For stock quants, use a naming convention that reflects the unique combination of dimensions:

**Recommended format:**
```
{module}.quant_{product}_{location}_{lot}
```

**Examples:**
```csv
# Without lot tracking
stock_import.quant_SKU001_WH1
stock_import.quant_SKU002_WH1

# With lot tracking
stock_import.quant_SKU001_WH1_LOT2024001
stock_import.quant_SKU002_WH1_LOT2024002

# With package
stock_import.quant_SKU001_WH1_PKG001

# Using source system IDs
legacy_import.quant_{legacy_quant_id}
```

**Why this matters:**
- External IDs must be unique across the entire database
- Using product+location+lot ensures uniqueness
- Makes it easy to trace back to source data
- Allows safe re-imports with `--skip-existing`

### How `action_apply_inventory` Works

1. **Import creates quants** with `inventory_quantity` set (pending adjustment)
2. **`action_apply_inventory`** is called via `--post-action`
3. **Stock moves are created** from Inventory Adjustment location
4. **`quantity` field is updated** with actual stock
5. **Quants are consolidated** if same product/location/lot/package/owner exists

### Opening Inventory with a Specific Date

When importing opening inventory (e.g., for a new Odoo implementation or fiscal year), the stock moves created by `action_apply_inventory` default to **today's date**. For proper accounting, you often need these moves dated to a specific date (e.g., the opening balance date).

**The Problem:**
- `action_apply_inventory()` creates stock moves with `date = today`
- The `accounting_date` field on stock.quant affects accounting entries but NOT the stock move date
- For accurate inventory history, you need the opening date on the actual stock moves

**The Solution: `--move-date` flag**

The `--move-date` flag updates the stock move dates after inventory adjustment:

```bash
odoo-data-flow import \
    --connection-file conf/connection.conf \
    --file data/stock.quant.csv \
    --model stock.quant \
    --context "{'inventory_mode': True, 'tracking_disable': True}" \
    --sudo --all-companies \
    --skip-existing \
    --post-action action_apply_inventory \
    --move-date 2026-01-01
```

**How it works:**
1. Import creates stock quants with pending adjustments
2. `--post-action action_apply_inventory` applies the adjustments (creates moves dated today)
3. `--move-date` finds the newly created moves and updates their date

**Format options:**
- Date only: `--move-date 2026-01-01` (sets time to 00:00:00)
- Full datetime: `--move-date "2026-01-01 08:00:00"`

### Complete Opening Inventory Workflow

**Step 1: Prepare your opening inventory CSV**

```csv
id;product_id/id;location_id/id;inventory_quantity;lot_id/id
opening.quant_SKU001_WH1;PRODUCT.SKU001;STOCK.WH1_STOCK;100.0;
opening.quant_SKU002_WH1;PRODUCT.SKU002;STOCK.WH1_STOCK;50.0;LOT.LOT001
opening.quant_SKU003_WH2;PRODUCT.SKU003;STOCK.WH2_STOCK;25.0;
```

**Step 2: Run the import with opening date**

```bash
#!/bin/bash
# import_opening_inventory.sh

CONFIG="conf/connection.conf"
OPENING_DATE="2026-01-01"

odoo-data-flow import \
    --connection-file "$CONFIG" \
    --file data/stock.quant.csv \
    --model stock.quant \
    --context "{'inventory_mode': True, 'tracking_disable': True}" \
    --sudo --all-companies \
    --skip-existing \
    --post-action action_apply_inventory \
    --move-date "$OPENING_DATE"

echo "Opening inventory imported with date: $OPENING_DATE"
```

**Step 3: Verify the results**

Check in Odoo that:
1. Stock quants have the correct quantities
2. Stock moves are dated to your opening date
3. Inventory valuation (if using) shows correct historical costs

### Troubleshooting Opening Inventory

| Issue | Cause | Solution |
|-------|-------|----------|
| Moves still show today's date | `--move-date` not used | Re-run with `--move-date` flag |
| Wrong moves updated | Multiple inventory adjustments | Use unique product codes per import |
| "No stock moves found" | Post-action didn't complete | Check logs for `action_apply_inventory` errors |
| Date format error | Invalid date format | Use `YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS` |

!!! tip "Re-running safe with `--skip-existing`"
    If you need to re-run the import (e.g., after adding more products), the `--skip-existing`
    flag ensures already-imported quants are skipped. However, `--move-date` will still update
    moves for all imported products, so be careful with multiple runs on the same day.
