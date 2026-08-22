"""Load a Polars DataFrame into Odoo with Fluvo — the "Fluvo as a sink" recipe.

This is the copy-paste body for a **Flowfile** *Polars code* / *Python* node (or
any Polars-native script): it takes the transformed DataFrame that reaches the
node and loads it into Odoo through Fluvo's supported DataFrame API, which gives
you the real connector — two-pass relational load, reconciliation, and a fail
file — plus automatic type coercion (real booleans/dates/numbers become
import-ready values). Nothing here imports Flowfile: it takes a plain
``polars.DataFrame``, so it works unchanged in a Flowfile node, a notebook, or a
plain script.

See ``README.md`` for the file-handoff mode and the wider integration
(PLAN 4.5 / issue #288).
"""

from __future__ import annotations

import polars as pl

from fluvo import load_dataframe

if __name__ == "__main__":
    # Minimal standalone demo (point config at a real connection file to run):
    demo = pl.DataFrame(
        {
            "id": ["flowfile_partner_1", "flowfile_partner_2"],
            "name": ["Acme (via Flowfile)", "Globex (via Flowfile)"],
            # Real Polars types — load_dataframe coerces them for Odoo.
            "is_company": [True, True],
        }
    )
    ok, stats = load_dataframe(demo, config="connection.conf", model="res.partner")
    print(f"loaded: success={ok} stats={stats}")
