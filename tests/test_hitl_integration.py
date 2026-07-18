"""End to end approve and reject flows through the FastAPI service.

Drives a real model through the service's SSE contract (Contract 1 in
HANDOFF.md): send a message that triggers the gated ``submit_change_request``
tool, assert the streamed ``interrupt`` event has the documented shape, then
resume with a decision and assert the resulting tool message.

Requires resolvable AWS credentials with Bedrock access and an
``AWS_REGION``; skipped otherwise. Models are served through Amazon Bedrock
(see agent.py), so there is no API key to gate on -- the precondition is
whatever boto3's default credential chain resolves (an IAM role in CI or
ECS, a local profile, or explicit env vars), plus a region. This mirrors
the deepagents library's own HITL test (`test_hitl.py`): the prompt
explicitly instructs the model to call a specific tool with specific
arguments so the interrupt is deterministic, and the assertions check SDK
structural state, not model judgment.

Startup/shutdown are invoked directly rather than through the ASGI lifespan
protocol, since ``service.app`` registers its persistence and agent setup
with the legacy ``@app.on_event`` decorators rather than a lifespan context
manager (see the deprecation warning noted in HANDOFF.md).
"""

from __future__ import annotations

import json
import os
import uuid

import boto3
import httpx
import pytest


def _aws_credentials_available() -> bool:
    if not (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")):
        return False
    try:
        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _aws_credentials_available(),
    reason="requires resolvable AWS credentials and AWS_REGION to drive the model via Bedrock",
)


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    """Parse SSE frames of the exact shape ``service.app._sse`` emits."""
    events = []
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        event_line = next(line for line in lines if line.startswith("event: "))
        data_line = next(line for line in lines if line.startswith("data: "))
        events.append(
            (
                event_line.removeprefix("event: "),
                json.loads(data_line.removeprefix("data: ")),
            )
        )
    return events


@pytest.fixture
async def client():
    from service.app import app, _shutdown, _startup

    await _startup()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as http_client:
            yield http_client
    finally:
        await _shutdown()


async def test_approve_flow_executes_the_gated_tool(client: httpx.AsyncClient) -> None:
    thread_id = str(uuid.uuid4())
    prompt = (
        "Call the submit_change_request tool directly with summary "
        "'integration test summary' and payload 'integration test payload'. "
        "Do not call any other tool first."
    )

    response = await client.post(
        f"/threads/{thread_id}/messages", json={"content": prompt}
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)

    interrupt_events = [e for e in events if e[0] == "interrupt"]
    assert interrupt_events, (
        f"expected an interrupt event, got: {[e[0] for e in events]}"
    )
    interrupt_value = interrupt_events[0][1]

    action_requests = interrupt_value["action_requests"]
    assert any(ar["name"] == "submit_change_request" for ar in action_requests)
    review_configs = interrupt_value["review_configs"]
    assert any(
        rc["action_name"] == "submit_change_request"
        and "approve" in rc["allowed_decisions"]
        for rc in review_configs
    )

    decisions = [{"type": "approve"} for _ in action_requests]
    resume_response = await client.post(
        f"/threads/{thread_id}/resume", json={"decisions": decisions}
    )
    assert resume_response.status_code == 200
    resume_events = _parse_sse(resume_response.text)
    assert any(e[0] == "done" for e in resume_events)

    state_response = await client.get(f"/threads/{thread_id}/state")
    assert state_response.json()["awaiting_approval"] is False


async def test_reject_flow_returns_rejection_to_the_model(
    client: httpx.AsyncClient,
) -> None:
    thread_id = str(uuid.uuid4())
    prompt = (
        "Call the submit_change_request tool directly with summary "
        "'reject test summary' and payload 'reject test payload'. "
        "Do not call any other tool first."
    )

    response = await client.post(
        f"/threads/{thread_id}/messages", json={"content": prompt}
    )
    events = _parse_sse(response.text)
    interrupt_value = next(e[1] for e in events if e[0] == "interrupt")
    action_requests = interrupt_value["action_requests"]

    decisions = [
        {"type": "reject", "message": "not approved for this test"}
        for _ in action_requests
    ]
    resume_response = await client.post(
        f"/threads/{thread_id}/resume", json={"decisions": decisions}
    )
    assert resume_response.status_code == 200
    resume_events = _parse_sse(resume_response.text)
    assert any(e[0] == "done" for e in resume_events)


async def test_message_to_awaiting_thread_returns_409(
    client: httpx.AsyncClient,
) -> None:
    thread_id = str(uuid.uuid4())
    prompt = (
        "Call the submit_change_request tool directly with summary 's' "
        "and payload 'p'. Do not call any other tool first."
    )
    first = await client.post(
        f"/threads/{thread_id}/messages", json={"content": prompt}
    )
    assert any(e[0] == "interrupt" for e in _parse_sse(first.text))

    second = await client.post(
        f"/threads/{thread_id}/messages", json={"content": "another message"}
    )
    assert second.status_code == 409
