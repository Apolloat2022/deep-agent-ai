# Handoff

This document records the work completed across three sessions (Opus,
Sonnet, then Sonnet again after a stalled Haiku attempt) and pins down the
contracts that were expensive to establish (the approval interrupt payload,
the persistence topology, the enterprise client behavior) so future work
does not rediscover them. As of Session 3, every item originally planned in
this document is either done and verified, or done with explicit
`<REPLACE: ...>` placeholders where a real value can only come from the
operator's AWS account. Nothing is left unassigned.

## Session 1 (Opus): service scaffold

1. `agent.py` accepts an injected `checkpointer` and `store`. When both
   are omitted it falls back to local synchronous SQLite so `python agent.py`
   still runs as a smoke test. The tool surface, prompt, subagent, filesystem
   backend, and approval gates are unchanged.
2. `service/persistence.py` selects async persistence from `AGENT_ENV`:
   async SQLite for `local`, async Postgres for `prod`. Postgres imports are
   lazy so a local install needs no Postgres driver.
3. `service/app.py` is a FastAPI service that builds the graph once at
   startup, streams runs as Server Sent Events, and exposes human approval as
   a first class API state.

Verified without spending model tokens: the graph compiles with injected
async SQLite persistence, the service module imports and registers its
routes, and `aget_state` returns a correct empty interrupt snapshot on a
fresh thread.

## Session 2 (Sonnet): real client, Postgres wiring, React client, tests

1. **Enterprise client** — `service/clients.py`. Async `httpx` wrapper around
   two calls: `GET /v1/entities/{id}` and `POST /v1/change-requests`, both
   assumed REST/JSON endpoint shapes (no real API spec was available; the
   paths and payload shape are the one thing the operator must confirm or
   correct against the actual enterprise service). Configured via
   `ENTERPRISE_API_BASE_URL`, `ENTERPRISE_API_TOKEN`,
   `ENTERPRISE_API_TIMEOUT_SECONDS`. With no base URL set, calls return a
   labeled "not configured" string instead of raising, so `python agent.py`
   and local dev keep working with zero backend. `agent.py`'s two tools now
   call this client and catch `EnterpriseClientError`, returning the message
   as the tool result rather than letting the exception crash the run.
2. **A behavior change this required**: the tools became `async def`. A
   LangChain `StructuredTool` built from a coroutine only has no sync `func`,
   so `agent.invoke(...)` raises `NotImplementedError` — confirmed directly,
   not assumed (see Verified section). The `agent.py` `__main__` smoke test
   was switched to `asyncio.run(agent.ainvoke(...))` accordingly. Anything
   that still calls `agent.invoke()` synchronously on this graph will break;
   use `ainvoke`/`astream` everywhere downstream.
3. **Postgres dependencies** — `langgraph-checkpoint-postgres` and
   `psycopg[binary,pool]` installed and import-verified against
   `service/persistence.py`'s `_open_postgres` branch (`AsyncPostgresSaver`,
   `AsyncPostgresStore`, both `from_conn_string` async context managers,
   confirmed present with matching signatures).
   **Not verified**: a live connection. Docker Desktop is installed on this
   machine but failed to start ("Docker Desktop is unable to start") when
   attempted this session — an existing local Docker issue, not something
   introduced here. `docker-compose.dev.yml` is provided at the project root
   for the operator (or Haiku, once Docker is healthy) to bring up a real
   Postgres and run the prod branch against it:
   ```
   docker compose -f docker-compose.dev.yml up -d
   $env:AGENT_ENV = "prod"
   $env:DATABASE_URL = "postgresql://deepagent:deepagent@localhost:5432/deepagent"
   python -c "import asyncio; from service.persistence import open_persistence
   async def main():
       async with open_persistence() as (cp, store):
           print(type(cp).__name__, type(store).__name__)
   asyncio.run(main())"
   ```
4. **React client** — `web/deep-agent-client/useDeepAgentThread.ts` and an
   example component. Implements the SSE-over-POST parsing the browser's
   `EventSource` cannot do (it's GET only), buffered correctly on `\n\n` frame
   boundaries across chunk splits. Type-checks clean under `tsc --strict`
   (verified in a scratch project with `react`/`@types/react` installed,
   zero errors). **Not verified**: never run against a live `service/app.py`
   instance or built inside the real frontend app, since no frontend project
   exists in this repository. Wire it in and drive one approve and one
   reject by hand before shipping.
5. **Tests** — `tests/`:
   - `test_clients.py` (8 tests) and `test_service_sse.py` (5 tests): fully
     executed, all pass, no API key needed. Mock HTTP with `pytest-httpx`;
     cover success, 4xx, network failure, auth header, and the unconfigured
     fallback for the client; cover exact SSE frame format and both
     `AIMessageChunk` content shapes for the streaming helper.
   - `test_hitl_integration.py` (3 tests): full approve, reject, and
     409-on-busy-thread flows against the real FastAPI app via
     `httpx.ASGITransport`, following the exact prompt-and-assert pattern the
     deepagents library uses in its own `test_hitl.py` (instruct the model to
     call a specific tool with specific args, so the interrupt is
     deterministic). **Skip gated on `ANTHROPIC_API_KEY`** — no key was
     available this session, so these are written and confirmed to collect
     and skip cleanly, and confirmed to reach the model layer (fails with a
     real `401 AuthenticationError` from a placeholder key, not an import or
     logic error) — but the actual approve/reject assertions have never
     passed. Run them with a real key before trusting them.
6. `pyproject.toml` added at the project root with pytest config
   (`asyncio_mode = "auto"`) so the async tests run without per-test
   boilerplate.

## Contract 1: the approval interrupt

This is the load bearing contract for the React client and any resume logic.
It comes directly from the library, not from guesswork
(`libs/deepagents/tests/integration_tests/test_hitl.py` and
`langchain/agents/middleware/human_in_the_loop.py`).

When the agent calls a tool listed in `interrupt_on`, the graph pauses. The
pending request is available at `state.interrupts[0].value` and has this
shape:

```json
{
  "action_requests": [
    {"name": "submit_change_request", "args": {"summary": "...", "payload": "..."}, "description": "..."}
  ],
  "review_configs": [
    {"action_name": "submit_change_request", "allowed_decisions": ["approve", "edit", "reject", "respond"]}
  ]
}
```

Resume with one decision per pending action, in the same order, via
`Command(resume={"decisions": [ ... ]})`. Each decision is one of:

```json
{"type": "approve"}
{"type": "edit", "edited_action": {"name": "submit_change_request", "args": {"summary": "...", "payload": "..."}}}
{"type": "reject", "message": "why the reviewer rejected"}
{"type": "respond", "message": "a direct answer returned to the model"}
```

The service surfaces the pending request as an SSE `interrupt` event and
accepts the decisions on `POST /threads/{thread_id}/resume`. Sending a new
message to a thread that is awaiting approval returns HTTP 409; the client
must resume first.

## Contract 2: persistence topology

* Local: async SQLite files under `AGENT_STATE_DIR`. Fine for one process.
* Prod on ECS: async Postgres, selected by `AGENT_ENV=prod` with a
  `DATABASE_URL` injected from Secrets Manager. Postgres is required because
  ECS task filesystems are ephemeral and multiple replicas must share both
  conversation checkpoints and cross thread memory. SQLite on a task local
  disk would lose state on every redeploy and would not be shared across
  replicas, which breaks the resume flow when a resume lands on a different
  task than the one that paused.
* The filesystem backend workspace (`AGENT_WORKSPACE`) is scratch space, not
  durable state. Anything that must survive belongs in the store or the
  checkpointer, both of which are Postgres in prod.

## Contract 3: enterprise client configuration

* `ENTERPRISE_API_BASE_URL` unset → both tools return a "not configured"
  string, never raise. This is intentional: it keeps `python agent.py`,
  local dev, and CI (no secret configured) working without a live backend.
  Do not treat this as an error state to fix; it is the designed fallback.
* `ENTERPRISE_API_TOKEN`, when set, is sent as `Authorization: Bearer
  <token>` on every request — verified by `test_fetch_entity_sends_bearer_token`
  in `tests/test_clients.py`.
* Assumed endpoint shapes (not confirmed against a real spec — the operator
  must verify or correct these): `GET {base_url}/v1/entities/{entity_id}` and
  `POST {base_url}/v1/change-requests` with JSON body
  `{"summary": ..., "payload": ...}`. If the real enterprise service uses
  different paths or a different payload shape, only `service/clients.py`
  needs to change — `agent.py`'s tool signatures and the interrupt gate on
  `submit_change_request` are unaffected.
* Errors from the backend (4xx/5xx or unreachable) raise
  `EnterpriseClientError` from the client; the tool functions in `agent.py`
  catch it and return the message as the tool result string, so the model
  sees a plain English failure and the graph run does not crash.

## Work done by Sonnet — see Session 2 above

All four originally assigned items are done: real enterprise client wired
into `agent.py`, Postgres dependencies installed and import-verified (live
connection blocked on a local Docker issue, not on this work), the React SSE
client, and integration tests. Everything is enumerated with its exact
verification status in Session 2 — read that before assuming anything below
is untested.

## Session 3 (Sonnet, standing in for Haiku): the five items below

Haiku's session (invoked between sessions 2 and 3) made zero changes to this
repository — no Dockerfile, no `deploy/`, no CI workflow, nothing. It's
unclear whether it stalled or its output was never persisted; several
prompts ("work on the rest of items", "continue", "check the HANDOFF.md do")
went by with no visible file change. Rather than keep escalating to Haiku,
the operator switched to Sonnet and asked to continue, so all five items
below were completed in that session instead. They remain scoped exactly as
originally written for Haiku — no scope was added.

### 1. Dockerfile — done (`Dockerfile`, `.dockerignore`)

Two stage build exactly as specified: `builder` installs into `/opt/venv`
via `uv`, `runtime` copies the venv and app code, `EXPOSE 8080`,
`CMD uvicorn service.app:app --host 0.0.0.0 --port 8080`. `.dockerignore`
excludes `tests/`, `web/`, `deploy/`, `docker-compose.dev.yml`, both `.venv`
directories, `state/`, `workspace/`, and cache directories.

**Not verified**: Docker Desktop is still down on this machine (same issue
as Session 2 — "Docker Desktop is unable to start"), so `docker build` has
never actually run. The install command inside the Dockerfile is the same
`uv pip install -p <venv> -e <path> langgraph langgraph-checkpoint-postgres
"psycopg[binary,pool]" fastapi "uvicorn[standard]" httpx` that has been run
successfully against this exact dependency set multiple times this session
outside Docker — high confidence, but not a substitute for a real build.
Run `docker build -t deep-agent-core-service .` once Docker is healthy, then
`docker run --rm -e ANTHROPIC_API_KEY=<real key> -p 8080:8080
deep-agent-core-service` and `curl http://localhost:8080/healthz`.

### 2. ECS task definition — done, with placeholders (`deploy/task-definition.json`)

Valid JSON (parsed and confirmed). One container, `deep-agent-core-service`,
`containerPort: 8080`, `AGENT_ENV=prod`, `ANTHROPIC_API_KEY` and
`DATABASE_URL` as `secrets` entries, a container-level `healthCheck`
hitting `/healthz`, and an `awslogs` log configuration block.

**What is a real guess, not copied from anything**: `cpu: 512` /
`memory: 1024` — there was no existing service's task definition available
in this workspace to copy sizing from, so this is a reasonable small-Fargate
default for a low traffic internal service, not a measured value. Every
field with a `<REPLACE: ...>` placeholder (execution role ARN, task role
ARN, ECR image URI, both Secrets Manager ARNs, log group name, region) needs
a real value from the operator's AWS account — these were deliberately left
as placeholders rather than invented, per the original instruction not to
guess ARN naming conventions.

### 3. Health check endpoint — done and verified (`service/app.py`)

Added verbatim as specified, directly above the `/threads/*` routes, nothing
else in the file touched:

```python
@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness check for the ECS/ALB health check. No dependencies checked."""
    return {"status": "ok"}
```

**Verified, not just assumed**: called in-process via
`httpx.ASGITransport` after `_startup()` — returned `200 {"status": "ok"}`.
Full test suite re-run afterward: still 13 passed, 3 skipped, same count as
before the change.

### 4. CI — done, with placeholders (`.github/workflows/deep-agent-service.yml`)

Valid YAML (parsed and confirmed; jobs `test` and `build-and-push` both
present). `test` job: checkout, install `uv`, install the same dependency
set as the Dockerfile plus `pytest pytest-httpx pytest-asyncio ruff`, run
`ruff check .`, run `pytest tests/ -v` with `ANTHROPIC_API_KEY` read from a
repo secret if present (integration tests skip cleanly if it isn't).
`build-and-push` job: gated to `main` pushes only, runs after `test` passes,
builds and pushes the Dockerfile image to ECR.

**Not copied from an existing template, and said so instead of pretending
otherwise**: the original instruction said to copy the shape of "this repo's
other Python service workflows," but no such workflow exists in this
workspace — `deep-agent-core/.github/workflows/` only has CI for the
upstream OSS `deepagents` project itself (its own libs, evals, release
process), which is not a template for this operator's ECS deployment
pattern. The AWS auth step (`role-to-assume`, ECR repository name) is
placeholder-flagged for the same reason as the task definition — reconcile
against however the account's other services authenticate to AWS from
GitHub Actions (most likely OIDC, assumed here, but not confirmed).

### 5. Docstring and formatting pass — done and verified

Ran `ruff check agent.py service/ tests/` first, before any fix: **all
checks passed** under ruff's default rule set (this project has no
`[tool.ruff]` config, unlike `deep-agent-core`'s own strict `select =
["ALL"]` setup — worth knowing if a stricter lint pass is wanted later, but
nothing to report or flag at the default level; no non-formatting findings
existed either way). Then `ruff format --check --diff` to preview, then
`ruff format .` applied: 2 files reformatted (`service/clients.py`,
`tests/test_hitl_integration.py`), both pure line-wrap changes, no logic
touched. Full test suite re-run after: still 13 passed, 3 skipped, same
count as before.
