"""Boilerplate deep agent for the deep agent ai platform.

Builds a supervisor style deep agent on the deepagents harness, which
compiles to a standard LangGraph ``CompiledStateGraph``. The module
demonstrates the three integration points required by the platform:

1. A custom system prompt for the orchestrator.
2. A defined tool surface backed by ``service.clients.EnterpriseClient``,
   an async HTTP client configured via ``ENTERPRISE_API_BASE_URL``. With
   no base URL set, the tools return a clearly labeled "not configured"
   result instead of failing, so this module runs standalone.
3. Durable persistence through a local SQLite checkpointer and store.

Models are served through Amazon Bedrock (``langchain_aws.ChatBedrockConverse``,
selected via the ``bedrock_converse:`` prefix on ``init_chat_model``), matching
the authentication pattern already used by this AWS account's other ECS
services: the ECS task role's IAM permissions grant access, not a static API
key. Both model IDs below are Bedrock cross region inference profile IDs, not
the bare Anthropic model IDs, since neither Opus 4.8 nor Sonnet 5 supports
on demand invocation in this account and region. Bedrock model access is a
separate, per model entitlement from IAM permissions; see HANDOFF.md for how
that was granted for this account.

The compiled graph is exposed through the ``build_agent`` factory so a
FastAPI service constructs it once at startup and reuses it across
requests. Conversation state is isolated per thread via the LangGraph
``thread_id`` configurable. Sensitive tool calls pause at a LangGraph
interrupt and wait for an explicit approval decision from the client.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.chat_models import init_chat_model
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.sqlite import SqliteStore

from service.clients import EnterpriseClientError, get_enterprise_client

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.store.base import BaseStore

ORCHESTRATOR_MODEL = os.environ.get(
    "ORCHESTRATOR_MODEL", "bedrock_converse:us.anthropic.claude-opus-4-8"
)
SUBAGENT_MODEL = os.environ.get(
    "SUBAGENT_MODEL", "bedrock_converse:us.anthropic.claude-sonnet-5"
)
WORKSPACE_DIR = Path(os.environ.get("AGENT_WORKSPACE", "./workspace")).resolve()
STATE_DIR = Path(os.environ.get("AGENT_STATE_DIR", "./state")).resolve()

SYSTEM_PROMPT = """\
You are the orchestrator for an enterprise operations platform.

Your responsibilities:
1. Break incoming requests into a plan with the todo tools before acting.
2. Delegate focused research and data gathering to the research subagent \
through the task tool so raw context never floods your own window.
3. Use the filesystem tools to draft, review, and persist working \
documents under the workspace root.
4. Call submit_change_request only after the supporting analysis is \
written to the workspace and you are confident in the payload. This tool \
is gated by human approval, so include a clear summary the reviewer can \
evaluate quickly.

Constraints:
1. Never fabricate entity data; always fetch records through the tools.
2. Keep final responses concise and reference workspace files by path.
"""

RESEARCH_SUBAGENT_PROMPT = """\
You are a research specialist. You receive one focused question per task.
Gather the relevant records with your tools, verify consistency, and \
return a compact synthesis with citations to the record identifiers you \
used. Do not speculate beyond the retrieved data.
"""


@tool
async def fetch_entity_record(entity_id: str) -> str:
    """Fetch a normalized entity record from the enterprise data service.

    Args:
        entity_id: Stable identifier of the entity to fetch.
    """
    try:
        return await get_enterprise_client().fetch_entity(entity_id)
    except EnterpriseClientError as exc:
        # Returned, not raised: the model sees this as the tool result and
        # can retry, ask for clarification, or report the failure upward
        # instead of the graph run crashing.
        return f"error fetching entity {entity_id}: {exc}"


@tool
async def submit_change_request(summary: str, payload: str) -> str:
    """Submit a change request to the downstream workflow system.

    This action is irreversible once accepted, so it is gated behind a
    human approval interrupt in the agent configuration.

    Args:
        summary: One paragraph description a reviewer can evaluate.
        payload: Serialized change payload for the workflow system.
    """
    try:
        return await get_enterprise_client().submit_change_request(summary, payload)
    except EnterpriseClientError as exc:
        return f"error submitting change request: {exc}"


def build_agent(
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
) -> CompiledStateGraph:
    """Construct and compile the deep agent graph.

    Persistence is injected so the caller controls durability. The FastAPI
    service passes async Postgres (or async SQLite) implementations; when
    both are omitted the function falls back to a synchronous local SQLite
    checkpointer and store, which is enough for the ``__main__`` smoke test.

    Args:
        checkpointer: Conversation state store keyed by ``thread_id``. When
            ``None``, a local synchronous SQLite checkpointer is created.
        store: Cross thread memory store. When ``None``, a local
            synchronous SQLite store is created.

    Returns:
        The compiled LangGraph state graph, ready for ``invoke``,
        ``ainvoke``, or ``astream`` with a per thread configurable.
    """
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if checkpointer is None:
        checkpoint_conn = sqlite3.connect(
            STATE_DIR / "checkpoints.sqlite", check_same_thread=False
        )
        checkpointer = SqliteSaver(checkpoint_conn)
        checkpointer.setup()
    if store is None:
        store_conn = sqlite3.connect(
            STATE_DIR / "store.sqlite", check_same_thread=False
        )
        store = SqliteStore(store_conn)
        store.setup()

    research_subagent = {
        "name": "research_subagent",
        "description": (
            "Delegate focused research and data gathering to this agent. "
            "Give it one narrowly scoped question at a time; it returns a "
            "compact synthesis instead of raw records."
        ),
        "system_prompt": RESEARCH_SUBAGENT_PROMPT,
        "tools": [fetch_entity_record],
        "model": init_chat_model(SUBAGENT_MODEL),
    }

    return create_deep_agent(
        model=init_chat_model(ORCHESTRATOR_MODEL),
        tools=[fetch_entity_record, submit_change_request],
        system_prompt=SYSTEM_PROMPT,
        subagents=[research_subagent],
        # Real disk access confined to the workspace root. virtual_mode
        # makes the workspace behave as the filesystem root for the agent.
        backend=FilesystemBackend(root_dir=WORKSPACE_DIR, virtual_mode=True),
        # Human approval gates. Each listed tool pauses the graph at an
        # interrupt; the client resumes with approve, edit, or reject.
        interrupt_on={
            "submit_change_request": True,
            "write_file": True,
            "execute": True,
        },
        checkpointer=checkpointer,
        store=store,
        name="deep_agent_core_service",
    )


async def _run_smoke_test() -> None:
    """Invoke the agent once end to end. Requires AWS credentials with
    Bedrock access (a configured profile, or the default credential chain)
    and the ``AWS_REGION`` environment variable set.

    Async because ``fetch_entity_record`` and ``submit_change_request``
    are coroutine-only tools (they call ``EnterpriseClient`` over async
    httpx): a ``StructuredTool`` built from an ``async def`` has no sync
    ``func``, so ``agent.invoke(...)`` raises ``NotImplementedError``.
    ``ainvoke`` is required, not a style preference.
    """
    agent = build_agent()
    config = {"configurable": {"thread_id": "local_smoke_test"}}
    result = await agent.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Fetch entity 42, summarize it, and draft a change request.",
                }
            ]
        },
        config=config,
    )
    print(result["messages"][-1].content)


if __name__ == "__main__":
    import asyncio

    asyncio.run(_run_smoke_test())
