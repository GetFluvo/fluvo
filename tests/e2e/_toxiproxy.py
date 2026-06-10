"""Minimal Toxiproxy control-API client for the chaos e2e tests.

Toxiproxy (https://github.com/Shopify/toxiproxy) is a TCP fault-injection proxy.
The chaos tests put it in front of Odoo and add "toxics" (reset_peer, latency, ...)
to deterministically reproduce the kind of transport failures real imports hit.
"""

from __future__ import annotations

import time
from typing import Any

import httpx


class Toxiproxy:
    """Talks to a Toxiproxy server's HTTP control API (default ``:8474``)."""

    def __init__(self, api_url: str) -> None:
        self._api = api_url.rstrip("/")
        self._client = httpx.Client(timeout=10.0)

    def wait_ready(self, timeout: float = 60.0) -> None:
        """Block until the control API responds, or raise after ``timeout`` s."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self._client.get(f"{self._api}/version").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1.0)
        raise RuntimeError("Toxiproxy control API did not become ready")

    def create_proxy(self, name: str, listen: str, upstream: str) -> None:
        """Create (replacing any existing) a proxy ``listen`` -> ``upstream``."""
        self._client.delete(f"{self._api}/proxies/{name}")
        resp = self._client.post(
            f"{self._api}/proxies",
            json={
                "name": name,
                "listen": listen,
                "upstream": upstream,
                "enabled": True,
            },
        )
        resp.raise_for_status()

    def add_toxic(
        self,
        proxy: str,
        name: str,
        type_: str,
        *,
        toxicity: float = 1.0,
        stream: str = "downstream",
        **attributes: Any,
    ) -> None:
        """Attach a toxic (e.g. ``reset_peer``, ``latency``) to ``proxy``."""
        resp = self._client.post(
            f"{self._api}/proxies/{proxy}/toxics",
            json={
                "name": name,
                "type": type_,
                "stream": stream,
                "toxicity": toxicity,
                "attributes": attributes,
            },
        )
        resp.raise_for_status()

    def remove_toxic(self, proxy: str, name: str) -> None:
        """Remove a single toxic from ``proxy``."""
        self._client.delete(f"{self._api}/proxies/{proxy}/toxics/{name}")

    def reset(self) -> None:
        """Remove all toxics from, and re-enable, every proxy."""
        self._client.post(f"{self._api}/reset")

    def delete_proxy(self, name: str) -> None:
        """Delete a proxy entirely."""
        self._client.delete(f"{self._api}/proxies/{name}")

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()
