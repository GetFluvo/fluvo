"""Pooled JSON-RPC transport for odoo-client-lib.

odoo-client-lib's ``json_rpc`` (odoolib/tools.py) calls the module-level
``httpx.post()`` for every request. That builds a brand-new ``httpx.Client`` -
and therefore a fresh SSL context via ``ssl.create_default_context`` (which
reloads the system CA bundle from disk) plus a new TCP connection - on *every*
RPC call, with httpx's default 5s timeout.

On large imports this is catastrophic:

- The per-call SSL-context construction dominates CPU (no reuse).
- The 5s default timeout is shorter than a big batch ``load`` takes, so batches
  spuriously "fail" and trigger the binary-search fallback, multiplying the call
  count - which makes everything slower still.

This module replaces ``json_rpc`` with a version backed by a single shared,
connection-pooled ``httpx.Client`` with a generous timeout. ``httpx.Client`` is
safe for concurrent use across the import's worker threads.

The Odoo 19 ``json2`` connector (odoolib/json2.py) has the same problem but calls
``httpx.post``/``httpx.get`` inline, so there is no function to replace. For it we
swap the module-level ``httpx`` reference for a thin shim that routes ``post``/
``get`` through the same pooled client (and delegates everything else to real
httpx).

Call :func:`install` once (conf_lib does this on import). It is idempotent and a
no-op if odoo-client-lib's internals change shape.
"""

from __future__ import annotations

import os
import random
import threading
from typing import Any

import httpx
from odoolib.tools import JsonRPCException

from ..logging_config import log

# Generous default; a single large batch load can take well over httpx's 5s
# default. Override with FLUVO_RPC_TIMEOUT (seconds).
_DEFAULT_TIMEOUT = float(os.environ.get("FLUVO_RPC_TIMEOUT", "600"))

# Cloudflare (and similar WAF bot protection) challenges httpx's default
# ``python-httpx/x.y`` User-Agent, which breaks the json2 endpoint against
# hosted / Cloudflare-fronted Odoo even with a valid API key (#193). Default to a
# browser-like UA so json2 works out of the box; override with FLUVO_USER_AGENT
# or the ``user_agent`` key in the connection file's ``[Connection]`` section.
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Default UA, sent on every pooled request as the client's default header.
_default_user_agent: str = os.environ.get("FLUVO_USER_AGENT") or _DEFAULT_USER_AGENT
# Per-host UA overrides (hostname -> User-Agent), injected per-request. Keeping
# these per-request rather than mutating the shared client means a connection's
# custom UA never closes/rebuilds the pool or races with in-flight requests from
# other threads or connections.
_user_agents: dict[str, str] = {}

_client: httpx.Client | None = None
_client_lock = threading.Lock()
_installed = False


def set_user_agent(hostname: str, user_agent: str) -> None:
    """Register a per-host User-Agent override for pooled requests.

    The override is injected per-request based on the request URL's host, so it
    never closes or rebuilds the shared pooled client. Used by ``conf_lib`` when a
    connection file sets ``user_agent`` (e.g. to pass a WAF such as Cloudflare that
    challenges the default UA on the json2 endpoint).

    Args:
        hostname: The Odoo host the override applies to (from ``[Connection]``).
        user_agent: The User-Agent header value to send to that host.
    """
    # Normalize: the lookup key (httpx.URL(...).host) is lowercased, so store the
    # registration key the same way and trim accidental whitespace.
    host = (hostname or "").strip().lower()
    if host and user_agent:
        _user_agents[host] = user_agent


def _user_agent_for(url: str) -> str | None:
    """Return the registered per-host User-Agent override for ``url``, if any."""
    if not _user_agents:
        return None
    try:
        host = httpx.URL(url).host
    except Exception:  # pragma: no cover - malformed url
        return None
    return _user_agents.get(host)


def _apply_user_agent(url: str, kwargs: dict[str, Any]) -> None:
    """Inject the per-host User-Agent override into a request's headers, if set."""
    override = _user_agent_for(url)
    if override:
        headers = dict(kwargs.get("headers") or {})
        # An explicit per-host UA is set to pass a WAF, so it must win over any
        # pre-existing User-Agent header (matches pooled_json_rpc's behaviour).
        headers["User-Agent"] = override
        kwargs["headers"] = headers


def _get_client() -> httpx.Client:
    """Return the process-wide pooled client, creating it once."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = httpx.Client(
                    timeout=httpx.Timeout(_DEFAULT_TIMEOUT, connect=30.0),
                    limits=httpx.Limits(
                        max_keepalive_connections=32, max_connections=64
                    ),
                    headers={"User-Agent": _default_user_agent},
                )
    return _client


class _PooledHttpx:
    """Shim for odoolib.json2's module-level ``httpx``.

    Routes ``post``/``get`` through the shared pooled client (dropping any
    per-call ``timeout`` so the client's generous default applies) and delegates
    every other attribute to the real ``httpx`` module.
    """

    def __init__(self, real: Any) -> None:
        self._real = real

    def post(self, url: str, **kwargs: Any) -> Any:
        """POST via the pooled client."""
        kwargs.pop("timeout", None)
        _apply_user_agent(url, kwargs)
        return _get_client().post(url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> Any:
        """GET via the pooled client."""
        kwargs.pop("timeout", None)
        _apply_user_agent(url, kwargs)
        return _get_client().get(url, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Delegate anything else (Response, exceptions, ...) to real httpx."""
        return getattr(self._real, name)


def pooled_json_rpc(url: str, fct_name: str, params: dict[str, Any]) -> Any:
    """Drop-in replacement for ``odoolib.tools.json_rpc`` using a pooled client.

    Mirrors the original contract: returns the ``result`` payload and raises
    ``odoolib.tools.JsonRPCException`` on an Odoo-level error.

    Args:
        url: The JSON-RPC endpoint URL.
        fct_name: The RPC method name (e.g. "call").
        params: The JSON-RPC params payload.

    Returns:
        The ``result`` field of the response (or False if absent).
    """
    data = {
        "jsonrpc": "2.0",
        "method": fct_name,
        "params": params,
        "id": random.randint(0, 1000000000),  # noqa: S311 (id, not crypto)
    }
    headers = {"Content-Type": "application/json"}
    override = _user_agent_for(url)
    if override:
        headers["User-Agent"] = override
    response = _get_client().post(url, json=data, headers=headers)
    result = response.json()
    if result.get("error", None):
        raise JsonRPCException(result["error"])
    return result.get("result", False)


def install() -> bool:
    """Patch odoo-client-lib's ``json_rpc`` to use the pooled transport.

    Idempotent. Patches every namespace that holds a reference to the original
    function (``odoolib.tools`` defines it; ``odoolib.rpc`` imports it by name,
    so both must be replaced). Returns True if the patch is in effect.
    """
    global _installed
    if _installed:
        return True
    try:
        import odoolib.tools as _tools

        _tools.json_rpc = pooled_json_rpc
        try:
            import odoolib.rpc as _rpc

            # rpc.py did `from .tools import json_rpc`, so it has its own ref.
            _rpc.json_rpc = pooled_json_rpc
        except Exception as exc:  # pragma: no cover - older/newer layouts
            log.debug(f"odoolib.rpc json_rpc not patched: {exc}")
        try:
            # Odoo 19 json2 connector: swap its module-level httpx for the shim.
            import odoolib.json2 as _json2

            _json2.httpx = _PooledHttpx(_json2.httpx)
        except Exception as exc:  # pragma: no cover - older/newer layouts
            log.debug(f"odoolib.json2 httpx not patched: {exc}")
        _installed = True
        log.debug(
            "Pooled JSON-RPC transport installed "
            f"(timeout={_DEFAULT_TIMEOUT}s, keep-alive pooling)."
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(f"Could not install pooled JSON-RPC transport: {exc}")
    return _installed
