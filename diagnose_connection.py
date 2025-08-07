"""
Odoo Connection and Load Test Diagnostic Tool

This script helps diagnose connection and performance issues with an Odoo server,
specifically testing the `load` method which is critical for odoo-data-flow.

It's designed to identify problems like:
- Incorrect connection parameters.
- Proxy/firewall issues (e.g., timeouts, request size limits).
- Performance bottlenecks on the Odoo server.

Usage:
    python diagnose_connection.py --config /path/to/your/odoo.conf --records 1000
"""

import argparse
import sys
from typing import Any

# Add the 'src' directory to the Python path to import odoo_data_flow modules
sys.path.insert(0, "src")

try:
    from odoo_data_flow.lib import conf_lib
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
except ImportError as e:
    print(
        f"Error: Failed to import necessary modules. Make sure you have the required "
        f"dependencies installed. You might need to run 'pip install rich'.\n({e})"
    )
    sys.exit(1)


def run_diagnostic(config_file: str, num_records: int) -> None:
    """Connects to Odoo and runs a load test."""
    console = Console()

    console.print(
        Panel(
            f"Starting Odoo Connection Diagnostic\n"
            f"Config File: [bold cyan]{config_file}[/bold cyan]\n"
            f"Records to Test: [bold cyan]{num_records}[/bold cyan]",
            title="[bold green]Odoo Diagnostic Tool[/bold green]",
            expand=False,
        )
    )

    # 1. Test Connection
    try:
        console.print("\n[bold]Step 1: Attempting to connect to Odoo...[/bold]")
        connection = conf_lib.get_connection_from_config(config_file)
        model = connection.get_model("res.partner")
        version = connection.version()
        console.print(
            f"[green]  -> Connection successful![/green]\n"
            f"     - Odoo Version: {version.get('server_serie', 'N/A')}\n"
            f"     - Server Version: {version.get('server_version', 'N/A')}"
        )
    except Exception as e:
        console.print(
            f"[bold red]Error: Connection failed.[/bold red]\n"
            f"Please check your configuration file: [cyan]{config_file}[/cyan]\n"
            f"Details: {e}"
        )
        sys.exit(1)

    # 2. Prepare Dummy Data
    console.print(f"\n[bold]Step 2: Preparing {num_records} dummy records...[/bold]")
    header = ["name", "is_company"]
    data = [[f"Diagnostic Test Partner {i}", False] for i in range(num_records)]
    console.print("[green]  -> Dummy data prepared.[/green]")

    # 3. Run Load Test
    console.print(
        f"\n[bold]Step 3: Executing `load` method on `res.partner`...[/bold]"
    )
    try:
        result = model.load(header, data)
        messages = result.get("messages", [])
        if messages:
            console.print(
                "[bold yellow]Warning: Odoo returned messages during load.[/bold yellow]"
            )
            for msg in messages:
                console.print(f"  - {msg.get('message', 'Unknown message')}")
        else:
            console.print(
                f"[bold green]  -> `load` method executed successfully![/bold green]\n"
                f"     - Created/updated {len(result.get('ids', []))} records."
            )
            console.print(
                Panel(
                    "Your connection and server appear to be configured correctly for bulk loading.",
                    title="[bold green]Diagnosis: OK[/bold green]",
                )
            )

    except Exception as e:
        console.print(f"[bold red]Error: The `load` method failed.[/bold red]")
        error_str = str(e)

        if "Expecting value" in error_str:
            console.print(
                Panel(
                    "The server returned a non-JSON response. This is the most common cause of import failures with remote servers.\n\n"
                    "[bold]Likely Causes:[/bold]\n"
                    "1. [bold]Proxy Timeout:[/bold] The request took too long and a proxy server (like Nginx) cut it off.\n"
                    "2. [bold]Request Size Limit:[/bold] The request was too large and was blocked by a proxy (e.g., `client_max_body_size` in Nginx).\n"
                    "3. [bold]Odoo Server Error:[/bold] Odoo itself crashed and returned an HTML error page.\n\n"
                    "[bold]Next Steps:[/bold]\n"
                    "- Ask your server administrator to check the Odoo and proxy logs for errors at the time of this test.\n"
                    "- Try running this script with a smaller number of records (e.g., `--records 100`) to see if it succeeds.",
                    title="[bold red]Diagnosis: Probable Proxy/Network Issue[/bold red]",
                )
            )
        else:
            console.print(
                "An unexpected error occurred. Here is the full error details:"
            )
            console.print(Syntax(error_str, "python", theme="solarized-dark"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Odoo Connection Diagnostic Tool.")
    parser.add_argument(
        "-c",
        "--config",
        required=True,
        help="Path to the Odoo configuration file.",
    )
    parser.add_argument(
        "-r",
        "--records",
        type=int,
        default=1000,
        help="Number of dummy records to use for the load test.",
    )
    args = parser.parse_args()
    run_diagnostic(args.config, args.records)
