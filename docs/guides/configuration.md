# Configuration Guide

This guide provides a detailed reference for the `connection.conf` file, which is essential for connecting the `fluvo` tool to your Odoo instance.

## The Connection File

All commands that need to communicate with an Odoo server (e.g., `import`, `export`, `migrate`) require connection details. These are stored in a standard INI-formatted configuration file.

By default, the tool looks for this file at `conf/connection.conf`, but you can specify a different path using the `--config` command-line option.

### File Format and Example

The configuration file must contain a `[Connection]` section with the necessary key-value pairs.


```{code-block} ini
:caption: conf/connection.conf
[Connection]
hostname = localhost
port = 8069
database = my_odoo_db
login = admin
password = my_admin_password
uid = 2
protocol = jsonrpc
```

### Configuration Keys

#### `hostname`
* **Required**: Yes
* **Description**: The IP address or domain name of your Odoo server.
* **Example**: `hostname = odoo.mycompany.com`

#### `port`
* **Required**: Yes
* **Description**: The port your Odoo server is running on. This is typically `8069` for standard Odoo instances.
* **Example**: `port = 8069`

#### `database`
* **Required**: Yes
* **Description**: The name of the Odoo database you want to connect to.
* **Example**: `database = my_production_db`

#### `login`
* **Required**: Yes
* **Description**: The username (login email) of the Odoo user that the tool will use to connect.
* **Example**: `login = admin`

#### `password`
* **Required**: Yes
* **Description**: The password for the specified Odoo user.
* **Example**: `password = my_secret_password`

#### `uid`
* **Required**: Yes
* **Description**: The database ID of the Odoo user identified by the `login` parameter. This is required for making RPC calls.
* **Well-known IDs**:
  * `1`: The default administrator user in Odoo versions prior to 12.0.
  * `2`: The default administrator user in Odoo versions 12.0 and newer.
* **Example**: `uid = 2`

#### `protocol`
* **Required**: No
* **Description**: The RPC protocol to use for communication with Odoo. The choice of protocol can significantly impact performance.
* **Default**: `xmlrpc`
* **Available Options**:
  * `xmlrpc` - XML-RPC over HTTP (default, compatible with all Odoo versions)
  * `xmlrpcs` - XML-RPC over HTTPS (secure)
  * `jsonrpc` - JSON-RPC over HTTP (**recommended for Odoo 10+**, ~30% faster)
  * `jsonrpcs` - JSON-RPC over HTTPS (secure, recommended for production)
  * `json2` - JSON-2 API over HTTP (Odoo 19+ only, requires API key)
  * `json2s` - JSON-2 API over HTTPS (Odoo 19+ only, requires API key)

* **Performance Note**: JSON-RPC is approximately 30% faster than XML-RPC due to more efficient parsing and smaller payload sizes. For Odoo 10 and newer, using `jsonrpc` or `jsonrpcs` is recommended.

* **Odoo 19+ Note**: Odoo 19 introduces the new JSON-2 API which will replace XML-RPC and JSON-RPC in Odoo 20. JSON-2 requires an API key instead of a password. Generate an API key from your Odoo user preferences (Account Security section) and use it in the `password` field.

* **Example**: `protocol = jsonrpcs`

#### Overriding Protocol via CLI

You can override the protocol setting from your config file using the `--protocol` CLI option:

```bash
# Use JSON-RPC for better performance
fluvo import --protocol jsonrpc --connection-file conf/connection.conf ...

# Use JSON-2 for Odoo 19+
fluvo import --protocol json2 --connection-file conf/connection.conf ...
```

#### `user_agent`
* **Required**: No
* **Description**: The `User-Agent` header sent on RPC requests. Mainly needed when your Odoo server sits behind a **Web Application Firewall** (e.g. Cloudflare) that challenges non-browser clients — see the caveat below.
* **Default**: a browser-like User-Agent (so the `json2` connector works out of the box behind Cloudflare).
* **Example**: `user_agent = Mozilla/5.0 (X11; Linux x86_64) ... Chrome/124 Safari/537.36`
* You can also set it without editing the config via the `FLUVO_USER_AGENT` environment variable. Precedence: config key → env var → built-in browser-like default.

```{admonition} Cloudflare / WAF caveat for json2
:class: warning

If your Odoo is fronted by Cloudflare (or similar bot protection), the `json2`
endpoint can return an opaque **HTTP 403** ("Just a moment…") even with a valid
API key, because the firewall challenges the HTTP client's User-Agent. Fluvo
defaults to a browser-like User-Agent to avoid this; if your edge still blocks it,
set a `user_agent` your WAF allows (XML-RPC and JSON-RPC are unaffected).
```

```{admonition} Unknown keys are ignored
:class: note

Any key in `[Connection]` that isn't a recognised connection parameter (e.g. an
inline note, or an old credential kept for reference) is **ignored** rather than
causing an error, so your config file can safely hold extra fields.
```

---


```{admonition} Tip
:class: note

On premise, it's advised to use a dedicated API user with the minimal access rights required for the models related to the import, rather than using the main administrator account.
```

### Real world Example

Below is a real world example of connection to a cloud hosted odoo instance on [opaas](https://www.opaas.cloud/).

```{code-block} ini
:caption: conf/connection.conf
[Connection]
hostname = test.yourinstance.opa.as
database = bvnem-test
login = admin
password = secret_password
protocol = jsonrpcs
port = 443
uid = 2
```

---

## Managing Multiple Environments

Most projects target more than one Odoo instance — a local sandbox, a test/UAT
server, and production. The recommended convention is **one connection file per
environment**, named with the environment as a prefix:

```text
conf/
├── local_connection.conf
├── test_connection.conf
└── prod_connection.conf
```

You select the environment simply by pointing `--connection-file` at the matching
file:

```bash
# Import against the test server
fluvo import --connection-file conf/test_connection.conf --file data/products.csv --model product.product

# Same data, against production
fluvo import --connection-file conf/prod_connection.conf --file data/products.csv --model product.product
```

### What the prefix does: isolated recovery files

Fluvo derives the **environment name** from the connection filename — it strips a
trailing `_connection` or `_conn` suffix, so `prod_connection.conf` → `prod`,
`test_connection.conf` → `test`, and a bare `uat.conf` → `uat`.

That environment name is then used to **keep each environment's fail/recovery files
separate**. When an import produces a [fail file](importing_data.md), it is written
into an environment subfolder next to your data rather than alongside it:

```text
data/
├── products.csv
├── test/
│   └── product_product_fail.csv   # from test_connection.conf
└── prod/
    └── product_product_fail.csv   # from prod_connection.conf
```

This means a `--fail` recovery run for production never picks up rows that failed
against test (or vice-versa) — each environment retries only its own failures. The
subfolder is created automatically; if the connection file has no recognisable
prefix, fail files stay in the data directory as before.

```{admonition} Tip
:class: note

Keep credentials out of version control: commit a `conf/*.conf.example` template and
add the real `conf/*_connection.conf` files to your `.gitignore`.
```
