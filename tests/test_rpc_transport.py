"""Tests for the pooled JSON-RPC transport patch."""

from unittest.mock import MagicMock, patch

import odoolib.rpc
import odoolib.tools
import pytest

from odoo_data_flow.lib import rpc_transport


def test_install_patches_both_namespaces() -> None:
    """install() replaces json_rpc in both tools and rpc namespaces."""
    rpc_transport.install()
    assert odoolib.tools.json_rpc is rpc_transport.pooled_json_rpc
    # rpc.py did `from .tools import json_rpc`, so it holds its own reference.
    assert odoolib.rpc.json_rpc is rpc_transport.pooled_json_rpc


def test_install_patches_json2_httpx() -> None:
    """install() swaps odoolib.json2's module-level httpx for the pooled shim."""
    import odoolib.json2

    rpc_transport.install()
    assert isinstance(odoolib.json2.httpx, rpc_transport._PooledHttpx)


def test_json2_shim_routes_through_pooled_client_and_drops_timeout() -> None:
    """The json2 shim sends via the pooled client and ignores per-call timeout."""
    fake_client = MagicMock()
    real_httpx = MagicMock()
    shim = rpc_transport._PooledHttpx(real_httpx)

    with patch.object(rpc_transport, "_get_client", return_value=fake_client):
        shim.post("http://x/json/2/m/read", json={"ids": [1]}, timeout=60)
        shim.get("http://x/doc.json", headers={"A": "B"})

    # timeout was dropped; pooled client used (not the per-call httpx.post).
    _, post_kwargs = fake_client.post.call_args
    assert "timeout" not in post_kwargs
    fake_client.get.assert_called_once()


def test_json2_shim_delegates_unknown_attrs_to_real_httpx() -> None:
    """Non post/get attributes fall through to the real httpx module."""
    real_httpx = MagicMock()
    real_httpx.Timeout = "sentinel"
    shim = rpc_transport._PooledHttpx(real_httpx)
    assert shim.Timeout == "sentinel"


def test_install_is_idempotent() -> None:
    """Calling install() twice is safe and stays installed."""
    assert rpc_transport.install() is True
    assert rpc_transport.install() is True


def test_pooled_json_rpc_returns_result() -> None:
    """A successful response returns its 'result' payload via the shared client."""
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"result": {"ok": 1}}
    fake_client = MagicMock()
    fake_client.post.return_value = fake_resp

    with patch.object(rpc_transport, "_get_client", return_value=fake_client):
        out = rpc_transport.pooled_json_rpc(
            "http://x/jsonrpc", "call", {"service": "object"}
        )

    assert out == {"ok": 1}
    # Single pooled client used (not a per-call httpx.post).
    fake_client.post.assert_called_once()


def test_pooled_json_rpc_raises_on_error() -> None:
    """An Odoo-level error is surfaced as JsonRPCException."""
    from odoolib.tools import JsonRPCException

    fake_resp = MagicMock()
    fake_resp.json.return_value = {"error": {"message": "boom"}}
    fake_client = MagicMock()
    fake_client.post.return_value = fake_resp

    with patch.object(rpc_transport, "_get_client", return_value=fake_client):
        with pytest.raises(JsonRPCException):
            rpc_transport.pooled_json_rpc("http://x/jsonrpc", "call", {})


def test_get_client_is_singleton() -> None:
    """The pooled client is created once and reused."""
    rpc_transport._client = None
    try:
        c1 = rpc_transport._get_client()
        c2 = rpc_transport._get_client()
        assert c1 is c2
    finally:
        if rpc_transport._client is not None:
            rpc_transport._client.close()
            rpc_transport._client = None
