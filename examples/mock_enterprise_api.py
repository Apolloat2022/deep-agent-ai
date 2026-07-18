"""A runnable mock of the enterprise API the deep agent's tools call.

Implements exactly the two endpoint shapes ``service/clients.py`` expects, so
you can exercise the agent's full tool + human-approval loop end to end without
a real backend:

    GET  /v1/entities/{entity_id}   -> a normalized entity record (JSON)
    POST /v1/change-requests        -> accepts {summary, payload}, returns a ticket

The data is fake but shaped like the risk / compliance domain this platform
lives in, so the model has something concrete to reason about. Every call is
logged to stdout with a ``[MOCK]`` prefix so you can confirm the agent is
actually reaching this service over the network, not fabricating results.

Run directly:

    uvicorn examples.mock_enterprise_api:app --host 127.0.0.1 --port 8099

or let ``examples/run_demo.py`` start and stop it for you.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Mock Enterprise API")

# A tiny in-memory "database" of entity records.
_ENTITIES = {
    "42": {
        "entity_id": "42",
        "legal_name": "Northwind Trading Co.",
        "type": "counterparty",
        "jurisdiction": "US-DE",
        "risk_score": 0.82,
        "risk_band": "HIGH",
        "status": "under_review",
        "open_flags": [
            {"code": "SANCTIONS_NAME_MATCH", "severity": "high", "opened": "2026-07-10"},
            {"code": "ADVERSE_MEDIA", "severity": "medium", "opened": "2026-07-12"},
        ],
        "last_reviewed": "2026-06-01",
        "assigned_analyst": "team-kyc-3",
    },
    "77": {
        "entity_id": "77",
        "legal_name": "Acme Logistics LLC",
        "type": "vendor",
        "jurisdiction": "US-CA",
        "risk_score": 0.35,
        "risk_band": "LOW",
        "status": "active",
        "open_flags": [],
        "last_reviewed": "2026-07-01",
        "assigned_analyst": "team-vendor-1",
    },
}

_CHANGE_REQUESTS: list[dict] = []


class ChangeRequest(BaseModel):
    summary: str
    payload: str


@app.get("/v1/entities/{entity_id}")
async def get_entity(entity_id: str):
    record = _ENTITIES.get(entity_id)
    if record is None:
        print(f"[MOCK] GET /v1/entities/{entity_id} -> 404 not found", flush=True)
        return {"error": "not_found", "entity_id": entity_id}
    print(
        f"[MOCK] GET /v1/entities/{entity_id} -> 200 "
        f"({record['legal_name']}, risk_band={record['risk_band']})",
        flush=True,
    )
    return record


@app.post("/v1/change-requests")
async def create_change_request(body: ChangeRequest):
    ticket_id = f"CR-{uuid.uuid4().hex[:8].upper()}"
    stored = {
        "change_request_id": ticket_id,
        "status": "pending_review",
        "summary": body.summary,
        "payload": body.payload,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    _CHANGE_REQUESTS.append(stored)
    print(
        f"[MOCK] POST /v1/change-requests -> 201 {ticket_id} "
        f"(summary={body.summary[:60]!r})",
        flush=True,
    )
    return stored


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "entities": len(_ENTITIES),
        "change_requests": len(_CHANGE_REQUESTS),
    }
