"""FastAPI service exposing the deep agent with streaming and approval gates.

The compiled graph is built once in the lifespan handler and reused across
requests. Each conversation maps to a LangGraph ``thread_id``; conversation
state and cross thread memory persist through the injected checkpointer and
store (see ``persistence.open_persistence``).

Human approval is a first class part of the API. When the agent calls a
gated tool it pauses at a LangGraph interrupt. The message endpoint streams
the run and, if it ends paused, emits an ``interrupt`` event carrying the
pending tool calls and the decisions the reviewer may make. The client then
calls the resume endpoint with one decision per pending tool call.

Endpoints:
    POST /threads/{thread_id}/messages  Send a user message; stream the run.
    POST /threads/{thread_id}/resume    Resume a paused run with decisions.
    GET  /threads/{thread_id}/state     Inspect a thread's pending interrupt.

Run from the project root:
    uvicorn service.app:app --reload
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessageChunk
from langgraph.types import Command
from pydantic import BaseModel

# Allow `from agent import build_agent` when launched from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import build_agent  # noqa: E402
from service.clients import aclose_enterprise_client  # noqa: E402
from service.persistence import open_persistence  # noqa: E402


class MessageRequest(BaseModel):
    """A user message to append to a thread."""

    content: str


class ResumeRequest(BaseModel):
    """A batch of approval decisions, one per pending tool call, in order.

    Each decision is one of:
        {"type": "approve"}
        {"type": "edit", "edited_action": {"name": str, "args": dict}}
        {"type": "reject", "message": str}
        {"type": "respond", "message": str}
    """

    decisions: list[dict[str, Any]]


app = FastAPI(title="Deep Agent Core Service")


@app.on_event("startup")
async def _startup() -> None:
    # Enter the persistence context for the process lifetime and build the
    # graph once. Stored on app.state so every request reuses one graph.
    app.state._persistence_cm = open_persistence()
    checkpointer, store = await app.state._persistence_cm.__aenter__()
    app.state.agent = build_agent(checkpointer=checkpointer, store=store)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await app.state._persistence_cm.__aexit__(None, None, None)
    await aclose_enterprise_client()


def _sse(event: str, data: Any) -> str:
    """Format one Server Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _chunk_text(chunk: AIMessageChunk) -> str:
    """Extract streamed text from a message chunk across content shapes."""
    content = chunk.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "".join(parts)
    return ""


async def _run(agent: Any, graph_input: Any, config: dict) -> AsyncIterator[str]:
    """Stream a run as SSE frames, ending with an interrupt or done event.

    ``graph_input`` is either a state dict (new user turn) or a
    ``Command`` (resume). The generator emits ``token`` events during the
    run, then a single terminal ``interrupt`` or ``done`` event.
    """
    async for mode, payload in agent.astream(
        graph_input, config=config, stream_mode=["updates", "messages"]
    ):
        if mode == "messages":
            message, _metadata = payload
            if isinstance(message, AIMessageChunk):
                text = _chunk_text(message)
                if text:
                    yield _sse("token", {"text": text})

    # The run has settled. If it paused on a gated tool, surface the pending
    # approval request; otherwise report the final assistant message.
    state = await agent.aget_state(config)
    if state.interrupts:
        yield _sse("interrupt", state.interrupts[0].value)
    else:
        final = state.values.get("messages", [])
        content = final[-1].content if final else ""
        yield _sse("done", {"content": content})


def _agent(app: FastAPI) -> Any:
    agent = getattr(app.state, "agent", None)
    if agent is None:
        raise HTTPException(status_code=503, detail="agent not ready")
    return agent


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness check for the ECS/ALB health check. No dependencies checked."""
    return {"status": "ok"}


@app.post("/threads/{thread_id}/messages")
async def send_message(thread_id: str, body: MessageRequest) -> StreamingResponse:
    """Append a user message and stream the resulting run."""
    agent = _agent(app)
    config = {"configurable": {"thread_id": thread_id}}

    # A thread already waiting on approval must be resumed, not appended to.
    state = await agent.aget_state(config)
    if state.interrupts:
        raise HTTPException(
            status_code=409,
            detail="thread is awaiting approval; call the resume endpoint",
        )

    graph_input = {"messages": [{"role": "user", "content": body.content}]}
    return StreamingResponse(
        _run(agent, graph_input, config), media_type="text/event-stream"
    )


@app.post("/threads/{thread_id}/resume")
async def resume(thread_id: str, body: ResumeRequest) -> StreamingResponse:
    """Resume a paused run by supplying one decision per pending tool call."""
    agent = _agent(app)
    config = {"configurable": {"thread_id": thread_id}}

    state = await agent.aget_state(config)
    if not state.interrupts:
        raise HTTPException(status_code=409, detail="thread has no pending approval")

    command = Command(resume={"decisions": body.decisions})
    return StreamingResponse(
        _run(agent, command, config), media_type="text/event-stream"
    )


@app.get("/threads/{thread_id}/state")
async def get_state(thread_id: str) -> dict[str, Any]:
    """Return a thread's pending approval request, if any.

    Lets a client that reconnected recover the interrupt payload without
    replaying the stream.
    """
    agent = _agent(app)
    config = {"configurable": {"thread_id": thread_id}}
    state = await agent.aget_state(config)
    return {
        "awaiting_approval": bool(state.interrupts),
        "interrupt": state.interrupts[0].value if state.interrupts else None,
    }
