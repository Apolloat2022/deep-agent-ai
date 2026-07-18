"""Async HTTP client for the enterprise data and workflow services.

Wraps the calls the deep agent tools make to backend services behind a
single, testable client. Configuration comes from environment variables so
the same code runs unmodified against a local mock, staging, or production,
matching how the platform's other Python services are wired.

When ``ENTERPRISE_API_BASE_URL`` is unset, every call returns a clearly
labeled "not configured" string instead of raising. This keeps
``python agent.py`` and local development working without a live backend,
and keeps the failure mode visible to whoever is reading the agent's output
rather than a stack trace at import time.

Env vars:
    ENTERPRISE_API_BASE_URL: Base URL of the enterprise API.
    ENTERPRISE_API_TOKEN: Bearer token sent as the ``Authorization`` header.
    ENTERPRISE_API_TIMEOUT_SECONDS: Per request timeout. Defaults to 10.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv

# Idempotent and a no-op when .env is absent (always true in production).
# Called here too so this module reads correct config even if imported
# before agent.py in some other entry point (e.g. a standalone script).
load_dotenv()

_ERROR_BODY_PREVIEW_CHARS = 500


class EnterpriseClientError(Exception):
    """Raised when the enterprise API returns an error or is unreachable.

    Tool implementations catch this and return the message as the tool
    result so the model can see what went wrong and decide how to proceed,
    rather than the exception propagating out of the graph run.
    """


@dataclass(frozen=True)
class EnterpriseClientConfig:
    """Connection settings for the enterprise API, read once from env."""

    base_url: str | None
    token: str | None
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> EnterpriseClientConfig:
        return cls(
            base_url=os.environ.get("ENTERPRISE_API_BASE_URL"),
            token=os.environ.get("ENTERPRISE_API_TOKEN"),
            timeout_seconds=float(
                os.environ.get("ENTERPRISE_API_TIMEOUT_SECONDS", "10")
            ),
        )


class EnterpriseClient:
    """Thin async wrapper over the enterprise REST API.

    One instance is shared for the process lifetime via
    ``get_enterprise_client``. Pass an explicit ``http_client`` (for example
    one built with ``httpx.MockTransport`` or under ``pytest-httpx``) to
    test call sites without a live backend.
    """

    def __init__(
        self,
        config: EnterpriseClientConfig | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config or EnterpriseClientConfig.from_env()
        # Only close the client on aclose() if we constructed it ourselves;
        # a caller-supplied client (tests, a shared pool) manages its own
        # lifecycle.
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            base_url=self._config.base_url or "http://unconfigured.invalid",
            timeout=self._config.timeout_seconds,
            headers=self._auth_headers(),
        )

    def _auth_headers(self) -> dict[str, str]:
        if self._config.token:
            return {"Authorization": f"Bearer {self._config.token}"}
        return {}

    @property
    def configured(self) -> bool:
        """Whether a base URL was provided; gates every network call."""
        return bool(self._config.base_url)

    async def fetch_entity(self, entity_id: str) -> str:
        """Fetch a normalized entity record.

        Args:
            entity_id: Stable identifier of the entity to fetch.

        Returns:
            The response body as text on success, or a "not configured"
            notice when no base URL is set.

        Raises:
            EnterpriseClientError: On a non-2xx response or a network
                failure.
        """
        if not self.configured:
            return (
                f"ENTERPRISE_API_BASE_URL is not set; no live record "
                f"for entity {entity_id}."
            )
        try:
            response = await self._http.get(f"/v1/entities/{entity_id}")
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:_ERROR_BODY_PREVIEW_CHARS]
            msg = (
                f"entity service returned {exc.response.status_code} "
                f"for entity {entity_id}: {body}"
            )
            raise EnterpriseClientError(msg) from exc
        except httpx.RequestError as exc:
            msg = f"entity service unreachable: {exc}"
            raise EnterpriseClientError(msg) from exc
        return response.text

    async def submit_change_request(self, summary: str, payload: str) -> str:
        """Submit a change request to the downstream workflow system.

        Args:
            summary: One paragraph description a reviewer can evaluate.
            payload: Serialized change payload for the workflow system.

        Returns:
            The response body as text on success, or a "not configured"
            notice when no base URL is set.

        Raises:
            EnterpriseClientError: On a non-2xx response or a network
                failure.
        """
        if not self.configured:
            return (
                "ENTERPRISE_API_BASE_URL is not set; change request was not submitted."
            )
        try:
            response = await self._http.post(
                "/v1/change-requests",
                json={"summary": summary, "payload": payload},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:_ERROR_BODY_PREVIEW_CHARS]
            msg = f"workflow service returned {exc.response.status_code}: {body}"
            raise EnterpriseClientError(msg) from exc
        except httpx.RequestError as exc:
            msg = f"workflow service unreachable: {exc}"
            raise EnterpriseClientError(msg) from exc
        return response.text

    async def aclose(self) -> None:
        """Close the underlying HTTP client, if this instance owns it."""
        if self._owns_client:
            await self._http.aclose()


_client: EnterpriseClient | None = None


def get_enterprise_client() -> EnterpriseClient:
    """Return the process wide enterprise client, creating it on first use."""
    global _client
    if _client is None:
        _client = EnterpriseClient()
    return _client


async def aclose_enterprise_client() -> None:
    """Close the process wide client. Call from the service shutdown hook."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
