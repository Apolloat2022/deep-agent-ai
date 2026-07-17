"""Unit tests for the enterprise API client.

No network access and no model calls: HTTP is mocked with ``pytest-httpx``.
These run in any environment, including one with no ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import pytest

from service.clients import (
    EnterpriseClient,
    EnterpriseClientConfig,
    EnterpriseClientError,
)


def _client(httpx_mock, *, token: str | None = None) -> EnterpriseClient:
    config = EnterpriseClientConfig(
        base_url="https://enterprise.example.internal",
        token=token,
        timeout_seconds=5.0,
    )
    return EnterpriseClient(config=config)


@pytest.mark.asyncio
async def test_fetch_entity_not_configured() -> None:
    """With no base URL, the call returns a notice instead of erroring."""
    client = EnterpriseClient(
        config=EnterpriseClientConfig(base_url=None, token=None, timeout_seconds=5.0)
    )
    result = await client.fetch_entity("42")
    assert "not set" in result
    assert "42" in result
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_entity_success(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://enterprise.example.internal/v1/entities/42",
        text='{"id": "42", "name": "Acme"}',
        status_code=200,
    )
    client = _client(httpx_mock)
    result = await client.fetch_entity("42")
    assert "Acme" in result
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_entity_sends_bearer_token(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://enterprise.example.internal/v1/entities/7",
        text="ok",
        status_code=200,
        match_headers={"Authorization": "Bearer secret-token"},
    )
    client = _client(httpx_mock, token="secret-token")
    result = await client.fetch_entity("7")
    assert result == "ok"
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_entity_http_error_raises_client_error(httpx_mock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://enterprise.example.internal/v1/entities/missing",
        text="not found",
        status_code=404,
    )
    client = _client(httpx_mock)
    with pytest.raises(EnterpriseClientError, match="404"):
        await client.fetch_entity("missing")
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_entity_network_error_raises_client_error(httpx_mock) -> None:
    import httpx

    def _raise(request):
        raise httpx.ConnectError("connection refused", request=request)

    httpx_mock.add_callback(_raise)
    client = _client(httpx_mock)
    with pytest.raises(EnterpriseClientError, match="unreachable"):
        await client.fetch_entity("42")
    await client.aclose()


@pytest.mark.asyncio
async def test_submit_change_request_success(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://enterprise.example.internal/v1/change-requests",
        text='{"status": "queued"}',
        status_code=202,
    )
    client = _client(httpx_mock)
    result = await client.submit_change_request("summary", "payload")
    assert "queued" in result
    await client.aclose()


@pytest.mark.asyncio
async def test_submit_change_request_not_configured() -> None:
    client = EnterpriseClient(
        config=EnterpriseClientConfig(base_url=None, token=None, timeout_seconds=5.0)
    )
    result = await client.submit_change_request("summary", "payload")
    assert "not" in result and "submitted" in result
    await client.aclose()


@pytest.mark.asyncio
async def test_configured_property() -> None:
    configured = EnterpriseClient(
        config=EnterpriseClientConfig(
            base_url="https://x.example", token=None, timeout_seconds=5.0
        )
    )
    unconfigured = EnterpriseClient(
        config=EnterpriseClientConfig(base_url=None, token=None, timeout_seconds=5.0)
    )
    assert configured.configured is True
    assert unconfigured.configured is False
    await configured.aclose()
    await unconfigured.aclose()
