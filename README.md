# Deep Agent Core Service

A supervisor style deep agent built on the [deepagents](https://github.com/langchain-ai/deepagents)
harness (LangGraph runtime), wrapped in a FastAPI service with streaming and
human in the loop approval. Intended to integrate with an existing React,
TypeScript, and Python enterprise stack and deploy as a container on ECS.
Models are served through Amazon Bedrock: authentication is IAM based, via
the ECS task role, not a static API key.

For the architecture and design decisions behind this project, see
[PLAN.md](PLAN.md). For exact API contracts, what has been verified in which
environment, and what is still placeholder work for the operator, see
[HANDOFF.md](HANDOFF.md).

## Prerequisites

* Python 3.11 or newer
* [uv](https://docs.astral.sh/uv/)
* AWS credentials with Bedrock access to `us.anthropic.claude-opus-4-8` and
  `us.anthropic.claude-sonnet-5` in `us-east-1` (a local AWS profile for
  development, the ECS task role in production), plus `AWS_REGION` set.
  Bedrock model access is a separate, per model entitlement from IAM
  permissions -- see [HANDOFF.md](HANDOFF.md) for how it was granted.
* Docker, only if building the container image or running the local Postgres
  compose file

## Setup

Clone the upstream deepagents library and build a virtual environment. This
step is not vendored into this repository; `deep-agent-core/` is
gitignored and reproduced fresh by the commands below. Install the `aws`
extra so `langchain-aws` is available for the Bedrock client.

```bash
git clone https://github.com/langchain-ai/deepagents deep-agent-core
cd deep-agent-core
uv venv .venv
uv pip install -p .venv \
    -e "./libs/deepagents[aws]" \
    langgraph \
    langgraph-checkpoint-sqlite \
    langgraph-checkpoint-postgres \
    "psycopg[binary,pool]" \
    fastapi \
    "uvicorn[standard]" \
    httpx \
    pytest \
    pytest-httpx \
    pytest-asyncio \
    ruff
cd ..
```

Validate the install by compiling a real agent graph:

```bash
deep-agent-core/.venv/bin/python -c "from deepagents import create_deep_agent; a = create_deep_agent(model='bedrock_converse:us.anthropic.claude-opus-4-8', tools=[], system_prompt='validation'); print(type(a).__name__)"
```

Expected output: `CompiledStateGraph`.

On Windows PowerShell, replace `deep-agent-core/.venv/bin/python` with
`deep-agent-core\.venv\Scripts\python.exe` throughout this document.

## Configuration

All configuration is read from environment variables.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AWS_REGION` | none, required | Region for Bedrock and, if used, other AWS calls. Both target models are only available as `us-east-1` cross region inference profiles in this account. |
| `ORCHESTRATOR_MODEL` | `bedrock_converse:us.anthropic.claude-opus-4-8` | Model for the top level agent. The `bedrock_converse:` prefix selects `langchain_aws.ChatBedrockConverse` via `init_chat_model`; the `us.` prefix on the model ID is a cross region inference profile, not the bare foundation model ID -- neither target model supports on demand invocation in this account. |
| `SUBAGENT_MODEL` | `bedrock_converse:us.anthropic.claude-sonnet-5` | Model for the research subagent |
| `AGENT_WORKSPACE` | `./workspace` | Filesystem root the agent's file tools operate in |
| `AGENT_STATE_DIR` | `./state` | Local SQLite checkpoint and store files, used when persistence is not injected |
| `AGENT_ENV` | `local` | `local` selects async SQLite, `prod` selects async Postgres |
| `DATABASE_URL` | none, required when `AGENT_ENV=prod` | Postgres connection string |
| `ENTERPRISE_API_BASE_URL` | unset | Base URL of the enterprise data and workflow API. With no base URL, tool calls return a labeled "not configured" result instead of failing, so the agent runs standalone |
| `ENTERPRISE_API_TOKEN` | unset | Bearer token sent to the enterprise API |
| `ENTERPRISE_API_TIMEOUT_SECONDS` | `10` | Per request timeout for the enterprise client |

## Usage

### Run the standalone smoke test

Exercises the full graph once, end to end, using local SQLite persistence:

```bash
export AWS_REGION=us-east-1
deep-agent-core/.venv/bin/python agent.py
```

### Run the FastAPI service

```bash
export AWS_REGION=us-east-1
deep-agent-core/.venv/bin/python -m uvicorn service.app:app --reload
```

The service builds the graph once at startup and exposes it over three
endpoints. Every conversation is a LangGraph thread, addressed by
`thread_id` in the URL path.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/threads/{thread_id}/messages` | Send a user message; streams the run as Server Sent Events |
| `POST` | `/threads/{thread_id}/resume` | Resume a run paused on a gated tool call, with one decision per pending action |
| `GET` | `/threads/{thread_id}/state` | Recover a pending approval request after a client reconnect |

The stream emits `token` events while the model is generating, then a single
terminal `interrupt` event (a gated tool call is awaiting approval) or `done`
event (the run finished normally). See Contract 1 in
[HANDOFF.md](HANDOFF.md) for the exact interrupt payload and decision
shapes, and [web/deep-agent-client](web/deep-agent-client) for a React hook
that implements this contract.

Example, sending a message and reading the stream:

```bash
curl -N -X POST http://localhost:8000/threads/demo/messages \
    -H "Content-Type: application/json" \
    -d '{"content": "Fetch entity 42 and summarize it."}'
```

If the run pauses on the gated `submit_change_request` tool, resume it with:

```bash
curl -N -X POST http://localhost:8000/threads/demo/resume \
    -H "Content-Type: application/json" \
    -d '{"decisions": [{"type": "approve"}]}'
```

### Human in the loop approval

Three tools are gated behind approval by default in `agent.py`:
`submit_change_request`, `write_file`, and `execute`. A gated call pauses the
graph rather than executing; the client must send back one decision per
pending action before the run continues. Each decision is one of `approve`,
`edit`, `reject`, or `respond`. See Contract 1 in
[HANDOFF.md](HANDOFF.md) for the full shape.

## Testing

```bash
deep-agent-core/.venv/bin/python -m pytest tests/ -v
```

`tests/test_clients.py` and `tests/test_service_sse.py` need no credentials and
no network access; they mock HTTP with `pytest-httpx`. `tests/test_hitl_integration.py`
drives a real model through the full approve, reject, and busy thread flows
and is skipped automatically unless AWS credentials and `AWS_REGION` resolve
(checked via `boto3.Session().get_credentials()`).

## Docker

Build and run the container. Outside ECS there is no task role, so pass AWS
credentials in explicitly -- either mount `~/.aws` or export the standard
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` variables:

```bash
docker build -t deep-agent-core-service .
docker run --rm -p 8080:8080 \
    -e AWS_REGION=us-east-1 \
    -e AWS_ACCESS_KEY_ID \
    -e AWS_SECRET_ACCESS_KEY \
    -e AWS_SESSION_TOKEN \
    deep-agent-core-service
```

For a local Postgres instance to test the `AGENT_ENV=prod` persistence path:

```bash
docker compose -f docker-compose.dev.yml up -d
export AGENT_ENV=prod
export DATABASE_URL=postgresql://deepagent:deepagent@localhost:5432/deepagent
```

## Project layout

```
agent.py                        Deep agent graph: prompt, tools, subagent, persistence
service/
  app.py                        FastAPI service: streaming, approval, thread endpoints
  persistence.py                Async SQLite or Postgres checkpointer and store, by AGENT_ENV
  clients.py                    Async client for the enterprise data and workflow API
tests/                          Unit tests (no API key needed) and HITL integration tests
web/deep-agent-client/          React hook and example component for the SSE contract
deploy/task-definition.json     ECS task definition, with placeholders for account specific values
Dockerfile                      Multi stage build for the service container
docker-compose.dev.yml          Local Postgres for testing the prod persistence path
.github/workflows/              CI: lint, test, build and push on merge to main
PLAN.md                         Architecture and design decisions
HANDOFF.md                      API contracts and per session verification status
```

## Deploying to ECS

`deploy/task-definition.json` is filled in with real values from this AWS
account (account `924056189531`, region `us-east-1`) and has **no
placeholders left**: the ECR repository, the dedicated
`deep-agent-core-task-role` and `deep-agent-core-execution-role` IAM roles,
the CloudWatch log group, and the `DATABASE_URL` secret (a dedicated Neon
Postgres instance, kept separate from the account's other service's
database) are all real and registered with ECS (revision 1, `ACTIVE`). Two
things remain:

1. **Bedrock model access is still propagating.** Both target models are
   authorized at the control plane level (confirmed via
   `aws bedrock get-foundation-model-availability`) but the live Converse
   API still rejects them. This is an AWS-side timing issue, not something
   to fix in this repo -- see [HANDOFF.md](HANDOFF.md) for the exact
   recheck commands and what to do if it doesn't clear.
2. **CI deploy automation.** This AWS account has no GitHub Actions OIDC
   provider at all yet (confirmed, not assumed). The `build-and-push` job in
   `.github/workflows/deep-agent-service.yml` has a placeholder
   `role-to-assume` until that federation exists. Until then, build and push
   the image manually, or set up OIDC first -- see [HANDOFF.md](HANDOFF.md)
   for what that involves.

See [HANDOFF.md](HANDOFF.md) for the full account discovery, the Bedrock
model access grant, and exactly what was and was not verified live.
