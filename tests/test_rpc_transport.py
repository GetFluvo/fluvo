"""Tests for the pooled JSON-RPC transport patch."""

from typing import Any
from unittest.mock import MagicMock, patch

import odoolib.rpc
import odoolib.tools
import pytest

from fluvo.lib import rpc_transport


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
        # Annotate as Any: mypy can't see that _get_client() repopulated the
        # module global, so it would otherwise mark this cleanup unreachable.
        client: Any = rpc_transport._client
        if client is not None:
            client.close()
            rpc_transport._client = None


def test_pooled_client_uses_browser_user_agent() -> None:
    """The pooled client defaults to a browser-like UA so WAFs don't challenge it."""
    orig_client = rpc_transport._client
    rpc_transport._client = None
    created = None
    try:
        created = rpc_transport._get_client()
        ua = created.headers.get("User-Agent") or ""
        assert "Mozilla/5.0" in ua
        assert "python-httpx" not in ua
    finally:
        rpc_transport._client = orig_client
        if created is not None and created is not orig_client:
            created.close()


def test_set_user_agent_registers_per_host_override() -> None:
    """set_user_agent records a per-host override; other hosts are unaffected (#193)."""
    rpc_transport.set_user_agent("odoo.example.com", "Custom-Agent/1.0")
    try:
        assert (
            rpc_transport._user_agent_for(
                "https://odoo.example.com/doc-bearer/res.users.json"
            )
            == "Custom-Agent/1.0"
        )
        assert rpc_transport._user_agent_for("https://other.example.com/x") is None
    finally:
        rpc_transport._user_agents.pop("odoo.example.com", None)


def test_set_user_agent_ignores_empty() -> None:
    """set_user_agent ignores an empty hostname or value."""
    before = dict(rpc_transport._user_agents)
    rpc_transport.set_user_agent("", "X")
    rpc_transport.set_user_agent("host", "")
    assert rpc_transport._user_agents == before


def test_json2_shim_injects_per_host_user_agent() -> None:
    """The json2 shim injects the per-host UA override into request headers (#193)."""
    rpc_transport.set_user_agent("waf.example.com", "Mozilla/5.0 waf")
    fake_client = MagicMock()
    try:
        with patch.object(rpc_transport, "_get_client", return_value=fake_client):
            shim = rpc_transport._PooledHttpx(MagicMock())
            shim.post("https://waf.example.com/doc-bearer/res.users.json", json={})
            shim.get("https://waf.example.com/doc-bearer/res.users.json")
        assert fake_client.post.call_args.kwargs["headers"]["User-Agent"] == (
            "Mozilla/5.0 waf"
        )
        assert fake_client.get.call_args.kwargs["headers"]["User-Agent"] == (
            "Mozilla/5.0 waf"
        )
    finally:
        rpc_transport._user_agents.pop("waf.example.com", None)


def test_pooled_json_rpc_injects_per_host_user_agent() -> None:
    """pooled_json_rpc injects the per-host UA override into the request (#193)."""
    rpc_transport.set_user_agent("rpc.example.com", "Mozilla/5.0 rpc")
    fake_client = MagicMock()
    fake_client.post.return_value.json.return_value = {"result": 42}
    try:
        with patch.object(rpc_transport, "_get_client", return_value=fake_client):
            out = rpc_transport.pooled_json_rpc(
                "https://rpc.example.com/jsonrpc", "call", {}
            )
        assert out == 42
        assert fake_client.post.call_args.kwargs["headers"]["User-Agent"] == (
            "Mozilla/5.0 rpc"
        )
    finally:
        rpc_transport._user_agents.pop("rpc.example.com", None)


def test_set_user_agent_normalizes_hostname() -> None:
    """Hostnames are stored case-insensitively and stripped (#193 review)."""
    rpc_transport.set_user_agent("  Odoo.Example.COM  ", "UA/1")
    try:
        # httpx.URL(...).host is lowercased; the registry key must match it.
        assert rpc_transport._user_agent_for("https://odoo.example.com/x") == "UA/1"
    finally:
        rpc_transport._user_agents.pop("odoo.example.com", None)


def test_apply_user_agent_overrides_existing_header() -> None:
    """An explicit per-host UA overrides a pre-set User-Agent header (#193 review)."""
    rpc_transport.set_user_agent("h.example.com", "Override/1")
    try:
        kwargs: dict[str, object] = {"headers": {"User-Agent": "old", "X": "y"}}
        rpc_transport._apply_user_agent("https://h.example.com/x", kwargs)
        assert kwargs["headers"] == {"User-Agent": "Override/1", "X": "y"}
    finally:
        rpc_transport._user_agents.pop("h.example.com", None)
