"""Constants for odoo-data-flow."""

# Default context for disabling tracking/chatter in Odoo
# Includes keys for various Odoo versions and ecosystem modules
DEFAULT_TRACKING_CONTEXT = {
    "tracking_disable": True,  # Community standard
    "mail_create_nolog": True,  # Odoo standard (creation)
    "mail_notrack": True,  # Odoo standard (tracking)
    "import_file": True,  # Standard import context
}
