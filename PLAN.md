# Deep Agent Platform Plan

## 1. Objective

Build a production grade agentic service on the deepagents harness (LangGraph runtime) and integrate it with the existing React, TypeScript, and Python enterprise stack. The service exposes a supervisor style deep agent through FastAPI and is deployable as a container on ECS.

## 2. Environment

The repository is cloned at `deep-agent-core` and the environment lives in `deep-agent-core\.venv`. Commands to reproduce or refresh the setup:

```powershell
git clone https://github.com/langchain-ai/deepagents deep-agent-core
cd deep-agent-core
python -m uv venv .venv
python -m uv pip install -p .venv -e .\libs\deepagents langgraph langgraph-checkpoint-sqlite fastapi "uvicorn[standard]"
```

Validation command (compiles a real agent graph, which is the meaningful health check for the core library; the `doctor` command belongs to the separate CLI package in `libs\code` and is not part of the harness):

```powershell
.\.venv\Scripts\python.exe -c "from deepagents import create_deep_agent; a = create_deep_agent(model='anthropic:claude-opus-4-8', tools=[], system_prompt='validation'); print(type(a).__name__)"
```

Expected output: `CompiledStateGraph`.

## 3. Architecture of the harness

The deepagents library (version 0.6.12) is a thin, opinionated assembly layer over LangGraph:

1. `create_deep_agent` returns a `CompiledStateGraph`, the same object type any LangGraph application produces. Everything that works with LangGraph (checkpointers, stores, streaming, LangGraph Server) works with a deep agent unchanged.
2. Built in middleware provides planning (`write_todos`), a filesystem tool surface (`ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`), shell execution (`execute` when the backend supports sandboxing), and subagent delegation (`task`).
3. Backends decide where agent files live: `StateBackend` (ephemeral graph state), `FilesystemBackend` (real disk, optionally virtualized to a root directory), `StoreBackend` (a LangGraph `BaseStore`, durable across threads), and `CompositeBackend` (routes path prefixes to different backends).
4. Human in the loop approval is declarative: `interrupt_on` maps tool names to interrupt policies. The graph pauses at a LangGraph interrupt, the client inspects the pending tool call, and resumes with an approve, edit, or reject decision. A checkpointer is required for this to function.
5. Persistence is standard LangGraph: a checkpointer for conversation state per `thread_id` and a store for cross thread memory.

## 4. Template selection from examples

Best fit for a multi agent system with filesystem interaction and human in the loop approval: `examples\nvidia_deep_agent`. It demonstrates a frontier orchestrator model, multiple subagents with distinct models and prompts, a backend factory with `CompositeBackend` routing, memory files, and the `interrupt_on` hook for approval of code execution.

Secondary references:

1. `examples\deep_research` for parallel subagent research patterns and prompt structure.
2. `examples\async-subagent-server` for exposing an agent behind a self hosted Agent Protocol server, the closest analog to the FastAPI integration target and a working example of interrupt handling over HTTP.
3. `libs\code` for the richest human in the loop approval implementation in the repository (it powers the terminal coding agent).

## 5. FastAPI plus LangGraph integration requirements

1. Construct the compiled graph once at process startup (FastAPI lifespan handler), not per request.
2. Use async: `await agent.ainvoke(...)` or `agent.astream(...)` inside endpoints, with `AsyncSqliteSaver` locally and `AsyncPostgresSaver` in production.
3. Map each conversation to a LangGraph `thread_id` passed through `config={"configurable": {"thread_id": ...}}`. Tie thread ids to your existing auth identity.
4. Stream tokens and tool events to the React client over Server Sent Events or WebSocket using `astream(stream_mode=["updates", "messages"])`.
5. Surface interrupts as a first class API state: when a run returns an interrupt, return HTTP 202 with the pending tool call payload; resume via a dedicated endpoint that invokes the graph with a `Command(resume=...)` value.
6. Swap SQLite for Postgres (`langgraph-checkpoint-postgres`) when moving to ECS, since container filesystems are ephemeral.

## 6. Model strategy

Recommendation: a two tier split rather than a single model.

1. Orchestrator: Claude Opus 4.8 (`claude-opus-4-8`). Planning, delegation, and approval sensitive tool calls benefit most from the strongest reasoning, and the orchestrator emits comparatively few tokens.
2. Subagents: Claude Sonnet 5 (`claude-sonnet-5`). Subagents do the bulk of token consumption (research, drafting, extraction). Sonnet 5 delivers near Opus quality on agentic work at 3 dollars input and 15 dollars output per million tokens versus 5 and 25 for Opus, with introductory pricing of 2 and 10 through August 31, 2026.

If a single model must be chosen, choose Opus 4.8 and control spend with prompt caching and tight subagent prompts. Token savings should come from architecture (delegation, caching, context isolation per subagent) before model downgrades.

## 7. Milestones

1. Done: clone, environment, install, validation.
2. Done: boilerplate `agent.py` with custom prompt, placeholder tools, subagent, filesystem backend, SQLite persistence, and approval interrupts.
3. Done (Opus): FastAPI service in `service/app.py` with token streaming over Server Sent Events and a first class interrupt resume contract, plus async persistence selection in `service/persistence.py` (async SQLite locally, async Postgres in prod). `agent.py` refactored to accept injected persistence. See `HANDOFF.md` for the exact interrupt payload and the ECS/Postgres topology.
4. Done (Sonnet): real enterprise client in `service/clients.py` wired into `agent.py`'s tools; Postgres dependencies installed and import-verified (live connection verification blocked on a local Docker issue — `docker-compose.dev.yml` provided for when Docker is healthy); React SSE client in `web/deep-agent-client/`, type-checked clean; `tests/` with 13 passing unit tests plus 3 written-but-key-gated integration tests. Full detail and verification status in `HANDOFF.md`.
5. Done (Sonnet, after a stalled Haiku session): Dockerfile and `.dockerignore`, ECS task definition with placeholder ARNs/URIs for the operator to fill in, `/healthz` endpoint (verified 200 in-process), a CI workflow, and a ruff formatting pass — all confirmed not to break the existing test suite (13 passed, 3 skipped throughout). Remaining work is entirely operator-side: fill in the AWS placeholders, run the service against a real Postgres and a real Anthropic key, and exercise the React client in the real app. Full detail and verification status in `HANDOFF.md`.
