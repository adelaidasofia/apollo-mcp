"""SSRF mitigation tests for apollo-mcp _request (MYC-101).

API_BASE is loaded from config.yaml at module import; monkeypatch the
module-level value to exercise the SSRF check against different URLs.
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("APOLLO_API_KEY", "dummy-key")

sys.path.insert(0, str(Path(__file__).parent.parent))

import server  # noqa: E402


@pytest.fixture
def restore_api_base():
    orig = server.API_BASE
    yield
    server.API_BASE = orig


@pytest.mark.asyncio
class TestSSRFApolloRequest:
    async def test_rejects_url_with_backslash(self, restore_api_base):
        server.API_BASE = "https://api.apollo.io/\\bad"
        result = await server._request("GET", "/sequences")
        assert "error" in result
        assert "SSRF" in result["error"]

    async def test_rejects_embedded_credentials(self, restore_api_base):
        server.API_BASE = "https://u:p@api.apollo.io/api/v1"
        result = await server._request("GET", "/sequences")
        assert "error" in result
        assert "SSRF" in result["error"]

    async def test_rejects_ipv6_link_local(self, restore_api_base):
        server.API_BASE = "http://[fe80::1]/api/v1"
        result = await server._request("GET", "/sequences")
        assert "error" in result
        assert "SSRF" in result["error"]

    async def test_rejects_dns_resolving_to_private_ip(self, restore_api_base):
        server.API_BASE = "http://attacker.example.com/api/v1"
        with patch("mycelium_security.url.socket.getaddrinfo") as mock_resolver:
            mock_resolver.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0))
            ]
            result = await server._request("GET", "/sequences")
        assert "error" in result
        assert "SSRF" in result["error"]

    async def test_rejects_aws_metadata_endpoint(self, restore_api_base):
        server.API_BASE = "http://169.254.169.254/api/v1"
        result = await server._request("GET", "/latest/meta-data/iam")
        assert "error" in result
        assert "SSRF" in result["error"]

    async def test_follow_redirects_false_is_set(self):
        import httpx
        captured = {}

        class _Spy(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)
                super().__init__(*args, **kwargs)

        with patch("server.httpx.AsyncClient", _Spy):
            try:
                await server._request("GET", "/zen")
            except Exception:
                pass
        assert captured.get("follow_redirects") is False
