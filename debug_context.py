"""
Odoo Context Diagnostic Tool (execute_kw)

This script helps diagnose issues with Odoo context handling by using the
low-level `execute_kw` method.

It's designed to be a definitive test to confirm if the `vat_check_vies`
context flag is being respected by your Odoo server, bypassing any potential
issues with the `odoolib` wrappers.

Usage:
    python debug_context.py --config /path/to/your/odoo.conf
"""

import argparse
import sys

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


def run_context_test(config_file: str) -> None:
    """Connects to Odoo and runs a `create` test with a specific context."""
    console = Console()
    console.print(
        Panel(
            f"Config: [cyan]{config_file}[/cyan]\n"
            "Model: [cyan]res.partner[/cyan]\n"
            "Method: [cyan]create[/cyan]\n"
            "Context: [cyan]{'vat_check_vies': False}[/cyan]",
            title="[bold green]Odoo Context Diagnostic[/bold green]",
        )
    )

    try:
        console.print("\n[bold]Step 1: Connecting to Odoo...[/bold]")
        connection = conf_lib.get_connection_from_config(config_file)
        model = connection.get_model("res.partner")
        console.print("[green]  -> Connection successful![/green]")
    except Exception as e:
        console.print(f"[bold red]Error: Connection failed.[/bold red]\nDetails: {e}")
        sys.exit(1)

    # Prepare a dummy record with a VAT number that would normally trigger a VIES check
    dummy_partner = {
        "name": "VIES Diagnostic Test Partner",
        "vat": "BE0477472701",  # A valid Belgian VAT number
    }
    context = {"vat_check_vies": False}

    try:
        console.print(
            "\n[bold]Step 2: Executing `create` via `execute_kw` with context...[/bold]"
        )
        # This is the core of the test: use the low-level execute_kw
        record_id = model.execute_kw(
            "create", [dummy_partner], {"context": context}
        )
        console.print(
            f"[bold green]  -> `execute_kw` call succeeded![/bold green]\n"
            f"     - Created record ID: {record_id}"
        )
        console.print(
            Panel(
                "The `execute_kw` method with context appears to be working correctly. "
                "If no VIES check was logged on your server, the fix is working.",
                title="[bold green]Diagnosis: OK[/bold green]",
            )
        )
    except Exception as e:
        console.print(f"[bold red]Error: The `execute_kw` call failed.[/bold red]")
        console.print(Syntax(str(e), "python", theme="solarized-dark"))
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Odoo Context Diagnostic Tool.")
    parser.add_argument(
        "-c", "--config", required=True, help="Path to the Odoo configuration file."
    )
    args = parser.parse_args()
    run_context_test(args.config)
