"""Unit tests for the SSE framing helpers in ``service.app``.

Pure functions, no model calls, no network. Verifies the exact wire format
the React client's SSE parser must match (see ``web/deep-agent-client``).
"""

from __future__ import annotations

from langchain_core.messages import AIMessageChunk

from service.app import _chunk_text, _sse


def test_sse_frame_format() -> None:
    frame = _sse("token", {"text": "hello"})
    assert frame == 'event: token\ndata: {"text": "hello"}\n\n'


def test_sse_frame_serializes_arbitrary_json() -> None:
    frame = _sse(
        "interrupt",
        {"action_requests": [{"name": "submit_change_request", "args": {}}]},
    )
    assert frame.startswith("event: interrupt\ndata: ")
    assert frame.endswith("\n\n")
    assert "submit_change_request" in frame


def test_chunk_text_from_plain_string_content() -> None:
    chunk = AIMessageChunk(content="hello world")
    assert _chunk_text(chunk) == "hello world"


def test_chunk_text_from_content_block_list() -> None:
    chunk = AIMessageChunk(
        content=[
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
            {"type": "tool_use", "name": "ignored", "input": {}},
        ]
    )
    assert _chunk_text(chunk) == "hello world"


def test_chunk_text_empty_content_list_returns_empty_string() -> None:
    chunk = AIMessageChunk(content=[])
    assert _chunk_text(chunk) == ""
