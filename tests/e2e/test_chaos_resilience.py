"""E2E chaos: the Toxiproxy fault-injection harness.

Routes fluvo's connection through Toxiproxy and verifies that an injected transport
fault actually reaches fluvo's *real* pooled client — the foundation the
resilience scenarios in issue #197 build on. Opt-in (``-m chaos``), local only.

A notable finding while building this: fluvo's import path proved remarkably
resilient to these faults. Idempotent xmlid upsert + the binary-search ``load``
fallback + a reconnecting pooled httpx client absorb resets/truncation (often with
server-side success despite a client-side reset), so the import keeps its
no-silent-loss guarantee without surfacing the faults. Asserting *import-level*
loss/recovery under fault therefore needs a carefully-constructed scenario and is
tracked in #197; this test confirms the harness can inject a fault into fluvo's
transport at all (the precondition for those scenarios).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from fluvo.lib import conf_lib


@pytest.mark.chaos
def test_harness_injects_transport_fault_into_fluvo(
    conn_config_flaky: dict[str, Any], toxiproxy: Any
) -> None:
    """A reset toxic on the proxy disrupts a real fluvo RPC routed through it."""
    # Baseline: a clean RPC through the proxy works.
    conn = conf_lib.get_connection_from_dict(dict(conn_config_flaky))
    assert conn.get_model("res.partner").search_count([]) >= 0

    # Inject: reset every connection. A fresh fluvo RPC must now fail at the
    # transport layer — proving the harness reaches fluvo's pooled client.
    toxiproxy.add_toxic("odoo", "rst", "reset_peer", toxicity=1.0, timeout=0)
    conn2 = conf_lib.get_connection_from_dict(dict(conn_config_flaky))
    with pytest.raises((httpx.HTTPError, ConnectionError, OSError)):
        conn2.get_model("res.partner").search_count([])

    # Remove the fault; the connection recovers.
    toxiproxy.reset()
    conn3 = conf_lib.get_connection_from_dict(dict(conn_config_flaky))
    assert conn3.get_model("res.partner").search_count([]) >= 0
