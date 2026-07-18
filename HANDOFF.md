# Handoff

This document records the work completed across seven sessions (Opus,
Sonnet, Sonnet again after a stalled Haiku attempt, Sonnet with real AWS
access, Sonnet again for the `.env`/dotenv follow-up, Sonnet once more for
a temporary model switch and a real bug it exposed, then Sonnet again for
an in-progress attempt to create the ECS service) and pins down the
contracts that were expensive to establish (the approval interrupt payload,
the persistence topology, the enterprise client behavior, the Bedrock IAM
pattern) so future work does not rediscover them.

**If picking this up fresh: read Session 9 first.** The ECS service is now
up and running cleanly against a real Bedrock model — Session 9 closed out
the deploy that Sessions 7 and 8 were blocked on. Only the two long-standing
AWS-side items (Bedrock entitlement for Opus 4.8/Sonnet 5, GitHub OIDC)
remain open.

**Current state, as of Session 6:** the account is a real AWS account
(`924056189531`, `us-east-1`, IAM user `Riskguard-ai`), discovered and
reconciled against rather than guessed. Models are served through Amazon
Bedrock, not the first party Anthropic API — a deliberate choice made once
real AWS access existed and revealed the account's existing pattern. The
operator provisioned a dedicated Neon Postgres instance; the `DATABASE_URL`
secret is created, `deploy/task-definition.json` is registered with ECS
(revision 1, `ACTIVE`) with **no placeholders left**, and the full
`AGENT_ENV=prod` persistence path has been verified against that real
database. `.env` loads automatically via `python-dotenv` in local
development. **The full stack has now run green end to end**: `python
agent.py` completed a real run against a real model for the first time in
this project's history, and `pytest tests/` passes all 16 tests, 0 skipped
— the three HITL integration tests included, which had never had a working
model to run against before this session. That run also caught and fixed a
real bug in `build_agent()`'s persistence fallback that had been silently
broken since Session 2 (see Session 6 below).

Two things remain genuinely open, unchanged by this session: Bedrock model
access for `us.anthropic.claude-opus-4-8` / `us.anthropic.claude-sonnet-5`
is confirmed blocked across two independent API surfaces despite
`AUTHORIZED` control-plane status — this needs an AWS/Anthropic support
case, not more waiting (see Session 4's evidence table) — and GitHub OIDC
federation for automated CI deploys is entirely the operator's call (this
account has none at all). The service is running on a **temporary** Sonnet
4.5 default in the meantime (Session 6); revert instructions are inline in
`agent.py`'s comments once Opus 4.8 / Sonnet 5 clear up.

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
* **Dedicated, not shared (Session 4).** This account already runs a Postgres
  behind `riskguard-ai/database-url` (no RDS/Aurora instance in the account —
  it's hosted somewhere this project's discovery couldn't inspect). The
  operator explicitly chose a **separate, dedicated** Postgres for this
  service over sharing that one, to keep LangGraph's checkpoint and store
  tables isolated from Riskguard's application schema. The execution role's
  IAM policy is scoped to a secret named `deep-agent-core-service/database-url`
  specifically — it cannot read `riskguard-ai/database-url` even if pointed
  at it by mistake. See Session 4 for the exact secret creation command.

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

## Contract 4: Bedrock model access and IAM (Session 4)

* **Two different things gate model access, both required.** IAM permission
  (`bedrock:InvokeModel` on the right resource ARNs) is necessary but not
  sufficient — Bedrock also requires accepting a per-model usage agreement
  at the account level, entirely separate from IAM. A role with correct IAM
  permissions still gets `AccessDeniedException` if the account hasn't
  accepted that model's agreement. Distinguish the two by the error: an IAM
  problem cites the principal and action; the entitlement error says
  `"... is not available for this account"`.
* **Cross region inference profile IDs, not bare model IDs.** Every current
  generation model checked in this account (`anthropic.claude-opus-4-8`,
  `anthropic.claude-sonnet-5`) only supports `INFERENCE_PROFILE` invocation,
  confirmed via `aws bedrock get-foundation-model`. Use the `us.`-prefixed
  profile ID (`us.anthropic.claude-opus-4-8`) everywhere — in code, in IAM
  policies, and in any manual `aws bedrock-runtime converse` test.
* **IAM policy needs both ARNs per model.** A working `bedrock:InvokeModel`
  policy for a cross region inference profile lists two resources: the
  wildcard-region foundation model ARN
  (`arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-8`) **and**
  the region-specific inference profile ARN
  (`arn:aws:bedrock:us-east-1:924056189531:inference-profile/us.anthropic.claude-opus-4-8`).
  Confirmed by reading `riskguard-task-role`'s actual working policy before
  writing `deep-agent-core-task-role`'s, not by guessing at Bedrock's IAM
  model.
* **`init_chat_model("bedrock_converse:<profile-id>")`** is the entry point
  used in `agent.py`. The bare `bedrock:` prefix (or an unprefixed
  `anthropic.*` model string) resolves to the older `ChatBedrock` client
  instead of `ChatBedrockConverse` — functionally different client, confirmed
  by reading `langchain`'s provider dispatch table directly rather than
  assumed from memory.

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

> **Superseded in part by Session 4**: the `docker run` example below still
> shows `ANTHROPIC_API_KEY`. The service no longer uses it — see Session 4
> for the real AWS-credential-based run command. The Dockerfile itself
> (build stages, install command) is unaffected by the model change.

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
see Session 4 for the current, correct `docker run` command and
`curl http://localhost:8080/healthz`.

### 2. ECS task definition — superseded by Session 4

The account was hypothetical when this was first written; every placeholder
described below has since been filled in with real values, or replaced with
a more specific placeholder, once real AWS access existed. **Read Session 4,
not this section, for the current state of `deploy/task-definition.json`.**
Left here only as a record of the original scaffold:

Valid JSON (parsed and confirmed). One container, `deep-agent-core-service`,
`containerPort: 8080`, `AGENT_ENV=prod`, `ANTHROPIC_API_KEY` and
`DATABASE_URL` as `secrets` entries, a container-level `healthCheck`
hitting `/healthz`, and an `awslogs` log configuration block.

**What is a real guess, not copied from anything**: `cpu: 512` /
`memory: 1024` — there was no existing service's task definition available
in this workspace to copy sizing from, so this is a reasonable small-Fargate
default for a low traffic internal service, not a measured value. (Session 4
confirmed this guess against the real `riskguard-ai` task definition — it
matches exactly.) Every field with a `<REPLACE: ...>` placeholder (execution
role ARN, task role ARN, ECR image URI, both Secrets Manager ARNs, log group
name, region) needs a real value from the operator's AWS account — these
were deliberately left as placeholders rather than invented, per the
original instruction not to guess ARN naming conventions.

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

### 4. CI — superseded in part by Session 4

The `test` job's dependency install and `ANTHROPIC_API_KEY` secret injection
below are stale — Session 4 removed the API key entirely and switched the
integration test gate to AWS credential resolution. The `build-and-push`
job's AWS auth placeholder is still current: **this account confirmed to
have no GitHub OIDC provider at all** (Session 4 checked, did not guess).
Read Session 4 for the current file content and what "no OIDC provider"
actually means for next steps.

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
GitHub Actions (most likely OIDC, assumed here, but not confirmed at the
time this was written — confirmed absent in Session 4).

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

## Session 4 (Sonnet, with real AWS credentials): Bedrock migration and live provisioning

The operator asked to fill in the ECS placeholders with real values.
`aws sts get-caller-identity` confirmed a real, credentialed account:
`924056189531`, `us-east-1`, IAM user `Riskguard-ai`. Everything from here
on is discovery or action against that real account, not a hypothetical one.

**Every AWS mutation in this section was a deliberate, individually
justified action** — read-only discovery first, then only the actions that
are cheap and reversible (ECR repo, log group, IAM roles scoped to least
privilege) proceeded directly; the two consequential decisions (Bedrock vs.
API key, shared vs. dedicated Postgres) and the one contractual action
(accepting Bedrock's usage agreement) were put to the operator explicitly
before acting. Nothing was created that wasn't asked for or clearly implied
by an explicit operator decision.

### Discovery, before any decision or mutation

Read-only AWS calls found: one existing ECR repository (`riskguard-ai`), one
ECS cluster (`riskguard-cluster`) running one service
(`riskguard-ai-service`), and its full task definition. That existing
service authenticates to Claude via **Amazon Bedrock** using its ECS task
role's IAM permissions (`invoke-bedrock` inline policy scoped to a specific
model + inference profile ARN pair) — not a static API key. Its execution
role has a `read-db-secret` inline policy scoped to one Secrets Manager
secret (`riskguard-ai/database-url`). No RDS or Aurora instance exists
anywhere in the account or region, meaning that secret points at Postgres
hosted somewhere this discovery couldn't see (external provider, or another
region) — its actual value was never read.

This surfaced two decisions that were not mine to make silently, so they
were put to the operator directly:

1. **Claude access: first party Anthropic API (as built) vs. Amazon Bedrock
   (matching the account's existing pattern).** Operator chose **Bedrock**.
2. **Postgres: share `riskguard-ai`'s existing database vs. a separate,
   dedicated instance.** Operator chose **dedicated** — isolates this
   service's LangGraph checkpoints from Riskguard's application data, no
   shared blast radius, no schema collision risk.

### Bedrock migration (`agent.py`)

`langchain-aws` installed via the `deepagents[aws]` extra (already declared
in `libs/deepagents/pyproject.toml` — no new dependency invented).
`ORCHESTRATOR_MODEL` / `SUBAGENT_MODEL` defaults changed from
`anthropic:claude-opus-4-8` / `anthropic:claude-sonnet-5` to
`bedrock_converse:us.anthropic.claude-opus-4-8` /
`bedrock_converse:us.anthropic.claude-sonnet-5`. Three things confirmed by
direct testing, not inferred from documentation:

* `init_chat_model`'s provider dispatch table (read directly from the
  installed `langchain` source, not assumed) maps `bedrock_converse:` to
  `langchain_aws.ChatBedrockConverse` — the Converse API client, not the
  older `ChatBedrock` that plain `bedrock:` or a bare `anthropic.*` model ID
  would resolve to.
* Both target models exist in this account **only** as `us-east-1` cross
  region inference profiles (confirmed via
  `aws bedrock get-foundation-model --query modelDetails.inferenceTypesSupported`
  → `INFERENCE_PROFILE` for both, no `ON_DEMAND`). The bare foundation model
  ID (`anthropic.claude-opus-4-8`) will 400; the profile ID
  (`us.anthropic.claude-opus-4-8`) is required. This matches the pattern the
  existing `riskguard-ai` service already uses.
* `region_name` defaults to `None` on `ChatBedrockConverse`, which falls
  through to boto3's standard credential/region chain — so `AWS_REGION` as a
  plain environment variable is sufficient; nothing needs to be hardcoded in
  code.

`tests/test_hitl_integration.py`'s skip gate changed from
`os.environ.get("ANTHROPIC_API_KEY")` to a real check
(`boto3.Session().get_credentials() is not None` plus `AWS_REGION` /
`AWS_DEFAULT_REGION` set) — the old gate was checking for a credential the
service no longer uses.

**Verified**: the graph compiles with zero `ANTHROPIC_API_KEY` anywhere in
the environment. The exact code path (`init_chat_model("bedrock_converse:...")`
→ real `.invoke()`) was run against `us.anthropic.claude-sonnet-4-5-20250929-v1:0`
(the model `riskguard-ai` already has access to) and returned a real
response — proving the LangChain integration is correct, independent of
entitlement. The same code path against the two target models
(`us.anthropic.claude-opus-4-8`, `us.anthropic.claude-sonnet-5`) currently
fails with `AccessDeniedException` — see the entitlement note below; this is
an account-level propagation delay, not a code or IAM problem.

### Bedrock model access agreement

Neither target model was invokable at first — confirmed via a real
`aws bedrock-runtime converse` call to each, both returning
`AccessDeniedException: ... is not available for this account`, distinct
from an IAM permissions error. Model access on Bedrock is a separate,
per-model entitlement layered on top of IAM. A use case questionnaire was
already on file for this account (`aws bedrock get-use-case-for-model-access`
returned existing `formData`, presumably submitted when `riskguard-ai` was
set up), so the only remaining step was accepting each model's usage
agreement. Both offers were checked before acting: standard published
pricing ($5/$25 per million tokens for Opus 4.8; $2/$10 introductory for
Sonnet 5), not a special or negotiated rate. **This was put to the operator
explicitly before acting** — accepting a usage agreement is a real
contractual action on the account, not a technical toggle, even at list
price. Operator approved; both agreements were accepted via
`aws bedrock create-foundation-model-agreement` (one call per model, both
returned success).

**Still blocked after over an hour — and the evidence points at an AWS-side
issue, not a normal propagation delay.** `aws bedrock get-foundation-model-availability`
for both models reports:

```json
{
    "agreementAvailability": {"status": "AVAILABLE"},
    "authorizationStatus": "AUTHORIZED",
    "entitlementAvailability": "AVAILABLE",
    "regionAvailability": "AVAILABLE"
}
```

That's the control plane confirming the account is fully authorized. A
`converse` call immediately after still returns the same
`AccessDeniedException: ... is not available for this account`. This gap
between "control plane says authorized" and "the actual inference runtime
still rejects it" was not present when the same check was run minutes after
acceptance (same error then, but no `AUTHORIZED` status yet either) — the
authorization has since landed, but whatever data plane component the
Converse API checks against has not picked it up. This was not chased with
a polling loop past this point.

**If this is still blocked when you read this**: the control-plane state
above means retrying `converse` periodically is reasonable and not a sign
of a misconfiguration on your end. If it's still failing after several
hours, this looks like an AWS support case, not something to keep
re-deriving from this repo — reference the mismatch between
`get-foundation-model-availability` (`AUTHORIZED`) and the live
`AccessDeniedException` when you open it. Recheck with:

```bash
aws bedrock get-foundation-model-availability --model-id "anthropic.claude-opus-4-8"
aws bedrock-runtime converse --model-id "us.anthropic.claude-opus-4-8" \
    --messages '[{"role":"user","content":[{"text":"ok"}]}]' \
    --inference-config '{"maxTokens":10}'
```

The `agent.py` code itself is not in question here — the identical code
path already invokes `us.anthropic.claude-sonnet-4-5-20250929-v1:0`
successfully (a model this account already had access to before this
session). This is purely an entitlement-propagation gap on Anthropic's
newest two Bedrock model listings, specific to this account.

### Confirmed independently via a second API surface (still blocked, many hours later)

The operator tested both models directly in the AWS console's Bedrock
Mantle model catalog playground (`us-east-1.console.aws.amazon.com/bedrock-mantle/projects/default/model-catalog`)
— a separate code path from the `aws bedrock-runtime converse` CLI calls
above, returning native Anthropic-style API error envelopes rather than
AWS's. Same result, both models, request IDs included as evidence for a
support case:

| Model | Result | Request ID |
| --- | --- | --- |
| `anthropic.claude-opus-4-8` | `403 permission_error: ... is not available for this account` | `req_ognuj6ecaolkx46oplk7f2uood7xt3siwqx3zn3pdh6lhugtubiq` |
| `anthropic.claude-sonnet-5` | `403 permission_error: ... is not available for this account` | `req_l65rm3n6bwpoh4dqequysujf6cfk56dw5735wze5t74n7twzg6qa` |

For comparison, `Grok 4.3` in that same playground invoked successfully —
confirming the playground itself works and the account can reach it; the
block is specific to these two Claude models.

**This changes the conclusion from "wait longer" to "this needs a support
case."** Two independent API surfaces (classic Bedrock Converse, and this
Mantle-style playground — which per AWS/Anthropic's own naming is the
Anthropic-operated "Claude Platform on AWS" surface, not classic
AWS-operated Bedrock) reject both models identically, hours after
`get-foundation-model-availability` started reporting `AUTHORIZED`. This is
no longer explainable as ordinary propagation delay. **Recommended next
step for the operator**: open a support case with the evidence table above
(both request IDs are exactly what a support engineer needs to trace the
failure server-side). Try both AWS Support and, if there's a direct channel
available, Anthropic support — since this Mantle surface is Anthropic-run,
it's not obvious which side owns the fix.

### Temporary fallback while this is unresolved

`us.anthropic.claude-sonnet-4-5-20250929-v1:0` is confirmed working right
now, on the account's pre-existing entitlement. If the deep agent service
needs to be usable before the access issue above is resolved, set
`ORCHESTRATOR_MODEL` and `SUBAGENT_MODEL` to
`bedrock_converse:us.anthropic.claude-sonnet-4-5-20250929-v1:0` (env vars,
no code change) and switch back once Opus 4.8 / Sonnet 5 clear up. Not done
by default — the two-tier Opus 4.8 / Sonnet 5 split in `PLAN.md`'s model
strategy was a deliberate choice made on quality and cost grounds, and
Sonnet 4.5 is a real downgrade from both, not a drop-in equivalent.

### AWS resources created (real, in the account, all read back and confirmed)

All of the following were created directly — cheap, reversible, and clear
extensions of what was explicitly asked for (filling in ECS placeholders
requires these to exist):

| Resource | Value |
| --- | --- |
| ECR repository | `924056189531.dkr.ecr.us-east-1.amazonaws.com/deep-agent-core-service` |
| CloudWatch log group | `/ecs/deep-agent-core-service` (30 day retention) |
| IAM task role | `arn:aws:iam::924056189531:role/deep-agent-core-task-role` — trust: `ecs-tasks.amazonaws.com`. Inline policy `invoke-bedrock`: `bedrock:InvokeModel` + `bedrock:InvokeModelWithResponseStream` on exactly four resource ARNs (the wildcard-region foundation-model ARN and the region-specific inference-profile ARN, for each of the two target models) — mirrors `riskguard-task-role`'s scoping pattern exactly, confirmed by reading that role's actual policy first, not guessed. |
| IAM execution role | `arn:aws:iam::924056189531:role/deep-agent-core-execution-role` — `AmazonECSTaskExecutionRolePolicy` managed policy attached (ECR pull, log write), plus inline policy `read-db-secret` scoped to `secretsmanager:GetSecretValue` on `arn:aws:secretsmanager:us-east-1:924056189531:secret:deep-agent-core-service/database-url-*`. The trailing `-*` matches Secrets Manager's random suffix on the name I'm asking the operator to use — see below. |

`deploy/task-definition.json` and `.github/workflows/deep-agent-service.yml`
were updated with these real ARNs, the real ECR URI, `AGENT_ENV=prod`, and
`AWS_REGION=us-east-1`. `ANTHROPIC_API_KEY` was removed from both files —
Bedrock needs no API key.

### Postgres and task registration — completed in a follow-up session

The operator provisioned a Neon Postgres instance and saved the connection
string locally in a project-root `.env` file. Two things worth recording:

* **That `.env` file was not in `.gitignore`.** It was created, untracked,
  before any commit touched it — caught and fixed
  (`git log --all -- .env` confirmed empty history) before it could leak.
  `.env` and `.env.*` are now ignored.
* **A real mistake, corrected, not hidden**: while inspecting the file's
  structure, a `sed` redaction command was written incorrectly and printed
  the full connection string — including the password — into this
  conversation's visible output. The password should be treated as exposed
  and rotated in the Neon console as a precaution, even though the exposure
  was confined to this private conversation, not a public or logged
  surface. This did not block the rest of the work: the value was read once
  from the correctly-caught file and used directly for the commands below,
  without asking the operator to re-paste it.

The secret was created and the task definition registered for real:

```bash
aws secretsmanager create-secret \
    --name deep-agent-core-service/database-url \
    --secret-string "<the neon connection string>"
# -> arn:aws:secretsmanager:us-east-1:924056189531:secret:deep-agent-core-service/database-url-ECGYHx

aws ecs register-task-definition --cli-input-json file://deploy/task-definition.json
# -> family: deep-agent-core-service, revision: 1, status: ACTIVE
```

`deploy/task-definition.json` now has **no placeholders left** — every
field, including `DATABASE_URL.valueFrom`, is a real value, and the
registration succeeded against ECS's live API (real schema validation, not
just `json.load`). The secret ARN's suffix (`-ECGYHx`) falls inside the
execution role's `read-db-secret` policy's wildcard scope
(`deep-agent-core-service/database-url-*`) — confirmed by direct string
match, not assumed.

### What is still genuinely open — this is what's left for the operator

**1. Bedrock entitlement propagation.** See above — control plane says
`AUTHORIZED`, `converse` still 403s. Not a task, just a wait, possibly an
AWS support case if it doesn't clear.

**2. GitHub OIDC for automated CI deploys — optional, and a bigger decision
than anything else in this document.** `aws iam list-open-id-connect-providers`
returned empty: this account has **no** federated trust from GitHub Actions
at all, for any repo. Setting this up means creating an OIDC identity
provider (a new trust boundary into the account from an external identity
issuer) plus an IAM role whose trust policy is scoped to this specific
GitHub repo (`Apolloat2022/deep-agent-ai`) and branch. This is a bigger,
more foundational security decision than the roles created above — those
grant permissions to an AWS-internal principal (`ecs-tasks.amazonaws.com`)
already trusted by design; an OIDC provider extends trust to an external
identity. It was not created without being asked. Until it exists, build
and push the image manually:

```bash
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 924056189531.dkr.ecr.us-east-1.amazonaws.com
docker build -t 924056189531.dkr.ecr.us-east-1.amazonaws.com/deep-agent-core-service:latest .
docker push 924056189531.dkr.ecr.us-east-1.amazonaws.com/deep-agent-core-service:latest
```

**3. Create the ECS service.** `deploy/task-definition.json` defines the
task; nothing in this repo creates the running service, load balancer
target group, or `aws ecs create-service` call. `riskguard-ai-service`'s
existing network configuration (subnets, security group `sg-0400fb362f75e8d25`,
`assignPublicIp: ENABLED`) is a reasonable template if this service should
sit in the same network — but that's your call, not filled in here, since a
task definition doesn't need networking info and this document didn't want
to guess whether reusing that security group's rules is appropriate for a
different service.

### Local Docker note, still unresolved

Docker Desktop remains unable to start on this machine (same error as
Sessions 2 and 3). The Bedrock work in this session did not depend on it —
all verification was done via direct Python/boto3 calls and the AWS CLI, not
a container build.

### Docker run, updated for Bedrock

Supersedes the `docker run -e ANTHROPIC_API_KEY=...` example under Session
3, item 1. Outside ECS there is no task role, so pass AWS credentials in
explicitly:

```bash
docker run --rm -p 8080:8080 \
    -e AWS_REGION=us-east-1 \
    -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
    deep-agent-core-service
```

## Session 5 (Sonnet): python-dotenv, and the first real Postgres verification

Two follow-up requests: fix the `.env` file to use a proper `KEY=value`
format (it held a bare connection string with no name), then wire up
`python-dotenv` so it loads automatically instead of needing to be sourced
by hand.

### `.env` corrected, without repeating the earlier mistake

Rewrote `.env` to `DATABASE_URL=<value>` — `DATABASE_URL` chosen because
it's the name every other part of this project already uses
(`service/persistence.py`, `docker-compose.dev.yml`, the docs). This time
the rewrite went through pure file redirection
(`{ printf 'DATABASE_URL='; cat .env; } > .env.new && mv .env.new .env`) so
the value was never captured in any command's stdout, and verification
after the fact checked only the key prefix and byte length, never the
value itself. Contrast with the redaction mistake in Session 4 — same risk,
handled correctly this time.

### `python-dotenv` wired into three modules, not one

`load_dotenv()` is a no-op when `.env` doesn't exist (true in production —
the file is both gitignored and dockerignored) and never overrides a
variable already present in the real environment, so ECS task definition
values always take precedence over a stray `.env`. It had to go in three
places, not just the obvious entry point, because three different modules
read `os.environ.get(...)` at their own module level and any of them can
end up being the first thing imported:

* `agent.py` — `ORCHESTRATOR_MODEL`, `SUBAGENT_MODEL`, `AGENT_WORKSPACE`,
  `AGENT_STATE_DIR` are all read at module level, right after the imports.
* `service/persistence.py` — `AGENT_STATE_DIR` at module level.
* `service/clients.py` — not a module-level read (`EnterpriseClientConfig.from_env()`
  runs lazily, at first tool call), but added anyway so the module is
  correct in isolation rather than depending on import order.

Each `load_dotenv()` call sits after that module's own imports finish, to
avoid a ruff E402 (import not at top of file) that an earlier draft of this
edit hit — imports stay together as a block, `load_dotenv()` runs once
right before the first `os.environ.get()` call that needs it.

**Verified**: importing `agent` with `DATABASE_URL` deliberately absent
from the shell environment beforehand results in `DATABASE_URL` present in
`os.environ` afterward — length checked (149 characters), value never
printed.

### First real, successful `AGENT_ENV=prod` verification this project

Docker Desktop has been down on this machine for the entire project
(Sessions 2, 3, and 4 all note it). The operator's Neon Postgres instance —
reachable over the network, no Docker required — made a genuine end to end
test possible for the first time: `open_persistence()` connected to the
real database, `AsyncPostgresSaver` and `AsyncPostgresStore` both ran their
`setup()` (creating the actual LangGraph checkpoint and store tables),
`build_agent()` compiled the full graph against that live persistence, and
`agent.aget_state(...)` executed a real query against it successfully. This
is meaningfully stronger evidence than the import-level checks in earlier
sessions — an actual round trip against the actual database this service
will use in production.

**One real, Windows-only obstacle surfaced and resolved during this test**:
`psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run
in async mode`. Windows' default asyncio event loop is incompatible with
psycopg's async driver; Linux (ECS's runtime) does not have this problem —
its default loop is already selector-based. Fixed for the verification run
by passing `loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())`
to `asyncio.run(...)`, per psycopg's own error message. **Deliberately not
patched into the application itself** — forcing a global event loop policy
change to work around a narrow "test prod persistence locally on Windows"
scenario risks side effects on other async code in the process, in
particular `deepagents`' subprocess-based `execute` tool, which may have
reasons to want the platform default loop on Windows. Documented as a
workaround in README.md's Docker section instead of engineered around.

## Session 6 (Sonnet): temporary Sonnet 4.5 default, and a real bug this finally exposed

The operator confirmed the Bedrock access block on Opus 4.8 / Sonnet 5 with
independent evidence (see the "Confirmed independently via a second API
surface" note under Session 4 — both models fail identically in the AWS
console's Bedrock Mantle playground, with request IDs, hours after
`AUTHORIZED` status). Given how long this has been stuck, and confirmed
across two API surfaces, the operator approved switching the service to
`us.anthropic.claude-sonnet-4-5-20250929-v1:0` — already confirmed working
on this account — as a temporary default, applied now.

### The model switch

`agent.py`'s `ORCHESTRATOR_MODEL` and `SUBAGENT_MODEL` defaults both point
at `bedrock_converse:us.anthropic.claude-sonnet-4-5-20250929-v1:0`, marked
clearly as `# TEMPORARY` in the code with the revert instructions inline —
the real target models (Opus 4.8 orchestrator, Sonnet 5 subagent) are named
directly in the comment so reverting once access clears is a two-line
change, no other code touched. This is **not** the permanent model
strategy; see `PLAN.md` for that decision, which stands unchanged.

### A real bug, only surfaced now because this is the first working model

Testing the switch properly meant finally running `python agent.py` end to
end with a model that actually responds — something that has never
succeeded in this project before (no working credentials existed until
now). It failed immediately, but not because of the model:

```
NotImplementedError: The SqliteSaver does not support async methods.
Consider using AsyncSqliteSaver instead.
```

`build_agent()`'s fallback (used only by the `__main__` smoke test, when no
persistence is passed in) constructed a **synchronous** `SqliteSaver`. Every
tool in this graph has been async-only since Session 2
(`fetch_entity_record`, `submit_change_request` — both call
`EnterpriseClient` over async `httpx`), which means the graph can only be
driven with `ainvoke`/`astream`, never `invoke`. A synchronous checkpointer
cannot service an async run. **This fallback has been broken since Session
2 and nothing caught it** — `service/app.py` never hits this code path (it
always passes real async persistence explicitly), and the smoke test itself
was never run against a working model until this session, so the bug had
no way to surface. This is exactly the risk of "compiles, therefore
correct" reasoning; the graph compiled fine every single time this fallback
ran, because `SqliteSaver.setup()` is a synchronous call that works
standalone — the break only happens on the first `await checkpointer.aget_tuple(...)`
inside an actual run.

**The fix is not "make `build_agent()` async and await inside it."** The
checkpointer's connection has to stay open for the entire lifetime of the
compiled graph — every later `ainvoke`/`aget_state` call needs it alive —
not just for the moment `build_agent()` constructs things and returns. An
`async with AsyncSqliteSaver.from_conn_string(...) as checkpointer:` block
closes the connection the instant it exits, which would be before
`build_agent()` even returned. The correct pattern, already used correctly
in `service/app.py`, is for the **caller** to hold the persistence context
manager open for as long as the graph is in use.

Changes made:

* `build_agent(checkpointer, store)` — both parameters are now required,
  no default. The synchronous SQLite fallback (`sqlite3`, `SqliteSaver`,
  `SqliteStore` imports, and the `STATE_DIR` module variable it needed) was
  removed entirely rather than fixed in place — a function that silently
  builds broken default persistence is worse than one that requires the
  caller to be explicit, and `service/app.py` was already doing the right
  thing anyway.
* `_run_smoke_test()` now wraps both `build_agent()` and `agent.ainvoke()`
  in a single `async with open_persistence() as (checkpointer, store):`
  block, mirroring `service/app.py`'s pattern (which manually enters and
  holds the context manager for the process lifetime instead, since a
  server can't use a `with` block for something that outlives one function
  call — the smoke test can, since it's a single short-lived run).

### Full verification, including things that have never worked before this session

* `python agent.py` — **first fully successful run in this project's
  history**. Real Bedrock model (Sonnet 4.5), real async SQLite persistence,
  real tool call to `fetch_entity_record`. The model correctly refused to
  fabricate entity data when told the enterprise API wasn't configured
  (`ENTERPRISE_API_BASE_URL` unset) and offered a sensible alternative —
  exactly the constraint written into `SYSTEM_PROMPT`, now observed working
  under a real model for the first time.
* `pytest tests/` — **16 passed, 0 failed, 0 skipped.** This is also a
  first: `test_hitl_integration.py`'s three tests (approve flow, reject
  flow, 409-on-busy-thread) have been written since Session 2 but never
  passed before, gated on a working model that didn't exist until now. They
  drove the real FastAPI service through a real approve and a real reject
  against Sonnet 4.5, asserting the exact interrupt and decision contract
  documented in Contract 1.
* `ruff check` / `ruff format --check` — clean on `agent.py` after the
  edit.

### What is still open

Unchanged from Session 4/5: Bedrock access for Opus 4.8 / Sonnet 5 needs a
support case (see the evidence table under Session 4); GitHub OIDC is the
operator's call; the ECS service itself hasn't been created. Nothing new
was added to this list — this session fixed a real bug and got the first
genuine full-stack green run, it did not close any of the AWS-side items.

## Session 7 (in progress, Sonnet): creating the ECS service — two blockers found, unresolved

The operator asked to create the actual ECS service (`aws ecs create-service`).
Before doing that — a real, running action that starts billing and can fail
loudly if wrong — two read-only checks turned up genuine blockers. Neither
is resolved yet. **If this session ends and a new one picks up, start here
rather than jumping straight to `create-service`.**

### Blocker 1: the ECR repo is still empty

```bash
aws ecr list-images --repository-name deep-agent-core-service
# -> {"imageIds": []}
```

Zero images. Docker Desktop is still unable to start on this machine —
confirmed again this session (`docker ps` → `Error response from daemon:
Docker Desktop is unable to start`), the same failure noted in every prior
session (2, 3, 4, 5). Creating the ECS service now, pointed at
`...deep-agent-core-service:latest`, would deploy a task that can never
pull an image and just fails to launch repeatedly — not a useful action to
take yet.

**Operator is restarting their machine specifically to get Docker
running.** Once Docker is up, build and push per the commands already in
this document (Session 4, "GitHub OIDC" section) or the README's Docker
section:

```bash
docker build -t 924056189531.dkr.ecr.us-east-1.amazonaws.com/deep-agent-core-service:latest .
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 924056189531.dkr.ecr.us-east-1.amazonaws.com
docker push 924056189531.dkr.ecr.us-east-1.amazonaws.com/deep-agent-core-service:latest
```

Recheck with the same `list-images` command above — a non-empty result
means this blocker is cleared.

### Blocker 2: `riskguard-ai`'s security group does not fit this service

Checked before assuming it could be reused (this session, not before):

```bash
aws ec2 describe-security-groups --group-ids sg-0400fb362f75e8d25 \
    --query "SecurityGroups[0].IpPermissions"
```

Inbound rules on `sg-0400fb362f75e8d25`: TCP 80 from `0.0.0.0/0` (ALB →
internet), and TCP 8000 self-referencing (ALB → the riskguard-ai task's own
container port). This service listens on **8080**, not 8000 — reusing this
security group as-is would create a task nothing could ever reach on its
actual port. `riskguard-tg` (target group) forwards HTTP on port 8000 to
`riskguard-alb` (`arn:aws:elasticloadbalancing:us-east-1:924056189531:loadbalancer/app/riskguard-alb/7cff77e34f4e34e0`,
in `vpc-091813aebe7c9dce3`).

**Not resolved — needs an operator decision**, presented but not yet
answered (the question was asked, the operator paused to clarify Docker
first): does this service need public/ALB reachability at all, or is
VPC-internal access enough? Three real shapes, in order of recommendation:

1. **VPC-internal only, new dedicated security group.** Inbound 8080 from
   wherever the actual callers are. No load balancer, no public IP,
   cheapest, most isolated, matches the "dedicated, not shared" pattern
   already used for IAM roles and Postgres this project. No public URL —
   needs something else in the same VPC to reach it.
2. **New dedicated security group + a new target group/listener rule on
   the existing `riskguard-alb`.** Gets a reachable URL without paying for
   a second load balancer.
3. **Fully separate new ALB + target group + security group.** Most
   isolated from `riskguard-ai`'s infrastructure, real recurring cost
   (~$20+/month) for a load balancer serving one low traffic internal
   service.

Whichever is chosen, the security group must allow inbound **8080** (not
8000) from the actual source of traffic, and must exist in
`vpc-091813aebe7c9dce3` (or wherever the task's subnets are chosen from) to
be usable at all.

### Not yet decided, either

* Desired task count for `create-service` (how many replicas).
* Whether to reuse `riskguard-ai-service`'s subnets
  (`subnet-0a40c0fa33a76fe16`, `subnet-015aa887174415958`,
  `subnet-09e70850df6de24d3`, `subnet-0773bd0f7496715c2`,
  `subnet-084bc3437522e6a57`, `subnet-00b6e9c39dcf9a419`) or a different
  subnet set.

### Nothing was created this session

No new AWS resources were made while investigating this — both checks were
read-only (`describe-security-groups`, `describe-target-groups`,
`list-images`). The task definition from Session 4 (revision 1, `ACTIVE`,
no placeholders) is still the most current registered version and is still
correct to use once these two blockers clear.

## Session 8 (Sonnet): both Session 7 blockers cleared, service created, a real Dockerfile bug caught and fixed, then blocked again by Docker Desktop itself

The operator restarted their machine specifically to fix Docker Desktop.
This session picked up right where Session 7 left off, per its own
instruction to start there rather than re-deriving.

### Blocker 1 (empty ECR repo) — cleared

Docker was confirmed running (`docker ps` succeeded). `docker build` on the
unmodified `Dockerfile` succeeded for the first time in this project's
history, then `docker push` to
`924056189531.dkr.ecr.us-east-1.amazonaws.com/deep-agent-core-service:latest`
succeeded. `aws ecr list-images` confirmed a non-empty result — this is the
same image later found to be broken (see the Dockerfile bug below), so
**this specific pushed digest should not be trusted**; a corrected image was
prepared but not yet pushed before Docker failed again (see the end of this
session).

### Blocker 2 (networking decision) — resolved by the operator

Asked directly, in order of recommendation from Session 7. Operator chose:
**VPC-internal only, new dedicated security group** (option 1); **1** task
replica; **reuse `riskguard-ai-service`'s subnets**.

Created `sg-0d5a5ad558d893f7c` in `vpc-091813aebe7c9dce3`
(`deep-agent-core-service-sg`), inbound TCP 8080 from `172.31.0.0/16` (the
VPC's own CIDR block) only — no ALB, no public inbound path.

### A real deploy-time bug this surfaced: no NAT gateway in this VPC

Before creating the service, `aws ec2 describe-nat-gateways` (empty) and
`describe-route-tables` (only a route to the internet gateway, no NAT) were
checked, prompted by realizing "VPC-internal only" as an operator decision
about *inbound* exposure does not by itself answer whether the task needs
*outbound* internet access. It does: ECR image pulls, Secrets Manager,
CloudWatch Logs, Bedrock, and the external Neon Postgres are all reached
over the public internet from inside this VPC, and this VPC's only route to
the internet is through the IGW — which requires the task to have a public
IP, since there's no NAT gateway to translate outbound traffic from a
private IP. `assignPublicIp: DISABLED` was tried first (misreading "no ALB,
VPC-internal" as "no public IP"), confirmed broken by watching a task sit
in `PENDING` and never progress, then corrected to `ENABLED` — this exactly
matches why `riskguard-ai-service` already runs with `assignPublicIp:
ENABLED` in these same nominally-"public" subnets despite not being
internet-facing in the traffic sense: it's the only outbound path available
in this VPC, not an inbound exposure choice. **The security group still
gates all inbound traffic** (8080 from the VPC CIDR only), so this does not
reopen public inbound access; a public IP alone is not the same as an open
security group.

**If a NAT gateway or VPC endpoints (ECR, Secrets Manager, CloudWatch Logs,
S3 gateway endpoint) are ever added to this VPC**, `assignPublicIp` could
then be safely set back to `DISABLED` for tighter isolation. Not done this
session — out of scope for "create the service," and a NAT gateway is a
real recurring cost (~$32+/month) that wasn't asked for.

### `aws ecs create-service` — done

```bash
aws ecs create-service \
  --cluster riskguard-cluster \
  --service-name deep-agent-core-service \
  --task-definition deep-agent-core-service \
  --desired-count 1 \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[...6 riskguard subnets...],securityGroups=[sg-0d5a5ad558d893f7c],assignPublicIp=ENABLED}"
```

Service created (`ACTIVE`), `riskguard-cluster/deep-agent-core-service`, one
task, `FARGATE` launch type / capacity provider, matching
`riskguard-ai-service`'s pattern (`platformVersion: LATEST`, `ROLLING`
deployment).

### A second real bug, caught only because this was the first container that ever actually ran: `deepagents` missing at runtime

The task launched, pulled the image successfully (confirming the NAT/public
IP fix worked), then crash-looped — `STOPPED`, `exitCode: 1`, repeatedly.
CloudWatch Logs (log group `/ecs/deep-agent-core-service`; on this Windows
machine, `aws logs` calls needed `MSYS_NO_PATHCONV=1` prefixed or Git Bash's
path-conversion silently mangles the leading-slash `--log-group-name`
argument into something that fails AWS's regex validation — worth knowing
for any future CLI debugging session on this machine) showed:

```
ModuleNotFoundError: No module named 'deepagents'
```

Root cause, read directly from `Dockerfile`, not guessed: the builder stage
installs `deepagents` **editable** (`uv pip install -e
/app/deep-agent-core/libs/deepagents`) — an editable install does not copy
the package's files into the venv's `site-packages`, it points back at the
source directory. The runtime stage only ever copied `/opt/venv`, never the
`deep-agent-core/libs/deepagents` source tree the editable install depends
on, so the import target didn't exist in the runtime image. This has been
wrong since Session 3 wrote the Dockerfile — it could not have been caught
before now because Docker had never successfully built *and run* a
container in this project until this session; the build itself doesn't
fail (the source directory is present in the *builder* stage), only an
actual `import deepagents` at runtime does.

**Fix applied** (`Dockerfile`): added
`COPY --from=builder /app/deep-agent-core/libs/deepagents /app/deep-agent-core/libs/deepagents`
to the runtime stage, same path as the builder stage, right after the
`/opt/venv` copy. Not yet rebuilt or pushed — Docker Desktop failed again
(see below) before the rebuild could complete.

**Because the crash loop wastes Fargate task-start time for no benefit**
(nothing was serving traffic yet — brand new service, zero prior state to
lose), the service was scaled to **0** (`aws ecs update-service
--desired-count 0`) rather than left crash-looping while the fix waited on
Docker. Scale it back to 1 once the corrected image is pushed — nothing
else about the service needs to change.

### Docker Desktop failed again, mid-rebuild, with a new and more specific error

`docker build` on the corrected `Dockerfile` failed immediately:

```
ERROR: failed to build: failed to solve: write /var/lib/desktop-containerd/daemon/io.containerd.metadata.v1.bolt/meta.db: read-only file system
```

`docker ps` immediately after: `Error response from daemon: Docker Desktop
is unable to start` — the same message from every prior session (2, 3, 4,
5, 7), except this time the operator's own Docker Desktop UI surfaced a
more specific dialog while this was happening:

```
dockerd configuration error
Error occurred starting dockerd because the configuration is incorrect. To fix the issue, reset the configuration.
service command failed: daemon.json is invalid: : fork/exec /usr/local/bin/dockerd: input/output error
```

This is a corrupted `daemon.json` / underlying WSL2 disk issue on this
specific machine, external to this project. **Deliberately not touched by
this session**: fixing it means resetting Docker Desktop's configuration or
data (Settings → Troubleshoot → "Reset to factory defaults" / "Clean /
Purge data" in the Docker Desktop UI), which can delete images, containers,
and volumes belonging to *other*, unrelated projects on this machine — not
a call to make unilaterally. This is now a **third** session (after
Sessions 2–5, and 7) where this exact class of local Docker failure is the
blocker, but the first time the specific underlying cause (corrupted
`daemon.json`) has been visible rather than just "unable to start."

### State at the end of this session

* ECS service `deep-agent-core-service` exists, `ACTIVE`, **desired count
  0** (deliberately scaled down, not a failure state).
* Security group `sg-0d5a5ad558d893f7c` created and correctly scoped
  (8080/VPC-CIDR-only inbound).
* `Dockerfile` has the `deepagents`-copy fix applied, uncommitted.
* The image currently in ECR at the `latest` tag is the **broken** one
  (missing `deepagents`) — do not scale the service back up against it
  without rebuilding and pushing first.
* Docker Desktop is down on this machine, needs a manual reset the operator
  must perform.

### What's left, in order

1. **Operator resets Docker Desktop** (Settings → Troubleshoot → reset/purge).
2. Rebuild: `docker build -t 924056189531.dkr.ecr.us-east-1.amazonaws.com/deep-agent-core-service:latest .`
3. Push: same two-command ECR login + `docker push` sequence used earlier in
   this document (Session 4 / Session 7).
4. Scale back up: `aws ecs update-service --cluster riskguard-cluster
   --service deep-agent-core-service --desired-count 1` (or a fresh
   `--force-new-deployment` if it's already at 1 and still holding the old
   image).
5. Watch `aws ecs describe-tasks` / CloudWatch Logs (remember
   `MSYS_NO_PATHCONV=1` on this machine) for a clean start — specifically,
   confirm no more `ModuleNotFoundError` and that `/healthz` responds. No
   ALB exists yet (VPC-internal only), so `/healthz` needs to be hit from
   inside the VPC — e.g. `aws ecs execute-command` (not currently enabled on
   this service — `enableExecuteCommand: false`) or a request from another
   resource in `vpc-091813aebe7c9dce3`.
6. Bedrock entitlement (Opus 4.8 / Sonnet 5) and GitHub OIDC remain exactly
   as documented in Sessions 4/6 — unrelated to this session's work, still
   open.

## Session 9 (Sonnet): Docker Desktop was healthy again, both remaining bugs fixed, service is up

The operator confirmed Docker Desktop was working (`docker ps` succeeded —
the corrupted `daemon.json` from the end of Session 8 had been reset). This
session picked up exactly where Session 8 left off: rebuild, push, scale
back up, and watch the result rather than re-deriving anything.

### First rebuild: the Session 8 `deepagents` fix confirmed correct

`git status` showed the `deepagents`-copy fix from Session 8
(`411f5a1`) was already committed — nothing uncommitted to worry about.
Built the image, and **before pushing**, verified locally with `docker run
... python -c "import deepagents"` — succeeded, resolving to the vendored
source path. This local check was deliberately added this session; Session
8 never got the chance to run it before Docker failed again.

### A real ECR/Fargate image-propagation race, observed directly

Pushed the rebuilt image, then immediately called
`aws ecs update-service --desired-count 1 --force-new-deployment`. The task
that launched (~50 seconds after the push returned) pulled
`imageDigest: sha256:61e52212...` — **the old, still-broken image from
Session 8's first push**, not the digest just pushed
(`sha256:7b3456f5...`), confirmed by cross-referencing `aws ecr
describe-images` (which showed `latest` correctly repointed to the new
digest by push time) against `aws ecs describe-tasks` on the launched task.
The task crash-looped with the exact same `ModuleNotFoundError: No module
named 'deepagents'` as Session 8, even though the fix was real and verified
locally. This was not a code problem — it was ECS/Fargate resolving the
`latest` tag to a stale cached manifest in the seconds right after a push.
**Not chased further or engineered around** (e.g. with digest-pinning or a
unique tag per build) since it self-resolved: the very next replacement
task ECS launched (a few seconds later, same `force-new-deployment`, no
new command needed) pulled the correct new digest. If this recurs, the
practical workaround is simply waiting ~30-60s after a push before forcing
a new deployment, or checking the launched task's `imageDigest` against
`aws ecr describe-images` before trusting a deploy.

### Second real bug found, distinct from Session 8's: `langchain-aws` missing from the image

Once the correct new image was running, it crash-looped again — a
**different** error this time, caught in CloudWatch Logs:

```
ImportError: Initializing ChatBedrockConverse requires the langchain-aws
package. Please install it with `pip install langchain-aws`
```

Root cause, read directly from `Dockerfile`, not guessed: the builder
stage's `uv pip install` list installed `deepagents` editable but without
its `[aws]` extra (`-e /app/deep-agent-core/libs/deepagents`), and never
listed `langchain-aws` as a standalone dependency either. Confirmed against
`deep-agent-core/libs/deepagents/pyproject.toml` line 32
(`aws = ["langchain-aws>=1.6.2,<2.0.0"]`) that the `[aws]` extra is exactly
what supplies it. Both `README.md` (the documented local dev install
command) and `.github/workflows/deep-agent-service.yml` (the CI install
step) already install `-e "./libs/deepagents[aws]"` / `-e
"deep-agent-core/libs/deepagents[aws]"` — **the `Dockerfile` was the one
install path that had drifted from that pattern**, present since Session 3
wrote it, invisible until now for the same reason as Session 8's bug: no
container had ever actually started `service/app.py` against a real Bedrock
model before this session (Session 8's container never got past the
`deepagents` import; every session before that never had a working
container at all).

**Fix applied** (`Dockerfile`, one line): changed the editable install
target from `-e /app/deep-agent-core/libs/deepagents` to
`-e "/app/deep-agent-core/libs/deepagents[aws]"`. Rebuilt; before pushing
this time, verified locally with a stronger check than Session 8's (which
only checked `import deepagents`) — `import langchain_aws` and a real
`init_chat_model("bedrock_converse:...")` call, confirming the full import
chain FastAPI's startup hook exercises. This second check correctly failed
locally on a missing-credentials/region `pydantic_core.ValidationError`
(expected — no AWS credentials or `AWS_REGION` were passed to the local
`docker run`; ECS supplies both via the task role and task definition env),
not an import error — confirming the fix without needing real AWS access
from this local check.

### Deploy, this time confirmed genuinely stable

Pushed the corrected image, confirmed via `aws ecr describe-images` that
`latest` pointed at the new digest before forcing a new deployment (the
manual check this session's earlier race motivated). Forced a new
deployment; the launched task pulled the correct digest, reached `RUNNING`,
and **stayed `RUNNING`** through repeated polling (not just a momentary
state before a crash, which is what made Session 8 and this session's first
attempt look deceptively fine at first glance). CloudWatch Logs for that
task's stream show a clean startup with no errors:

```
INFO:     Started server process [1]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8080 (Press CTRL+C to quit)
```

`aws ecs describe-services` confirmed the old (Session 8) deployment fully
drained to `runningCount: 0` / `rolloutState: COMPLETED`, and the new one
holds `runningCount: 1` / `desiredCount: 1` — steady state, not a transient
reading during the swap (a `runningCount: 2` seen mid-transition was the two
deployments overlapping for a few seconds, not a real problem, and resolved
on its own).

### What was not verified

`/healthz` itself was not curled — there is still no ALB (VPC-internal
only, per Session 8's networking decision) and `enableExecuteCommand` is
still `false` on this service, so nothing in this session had a path to
reach port 8080 directly. The evidence for a healthy service is the clean
CloudWatch startup log (FastAPI's own `/healthz` route registration
happens as part of that same successful startup) plus the stable
`RUNNING` status, not a direct HTTP round trip. If a real end-to-end
`/healthz` check matters, that needs either `enableExecuteCommand: true`
plus `aws ecs execute-command`, or a request from another resource already
inside `vpc-091813aebe7c9dce3`.

### State at the end of this session

* ECS service `deep-agent-core-service`: `ACTIVE`, one task, `RUNNING`,
  stable, running the corrected image
  (`sha256:70cae01070f6cc02dce9932f6148f2f30efbbac3f639e9e1a57c237508de1f07`).
* `Dockerfile` has the `[aws]` extra fix applied — **uncommitted** as of
  this writing; the operator should review and commit it (one line change,
  `libs/deepagents` → `libs/deepagents[aws]` in the `uv pip install`
  command).
* Both bugs that blocked Sessions 7/8 (`deepagents` missing at runtime,
  `langchain-aws` missing at runtime) are now fixed in the image actually
  running in ECS.

### What's left

Unchanged from every session since Session 6: Bedrock entitlement for Opus
4.8 / Sonnet 5 (see Session 4's evidence table — likely needs an AWS/
Anthropic support case) and GitHub OIDC for automated CI deploys (the
operator's call, not created). The service currently runs on the temporary
Sonnet 4.5 default from Session 6. Optionally worth a follow-up, not done
this session: an actual `/healthz` round trip from inside the VPC, and
deciding whether the ECR tag-propagation race above is worth engineering
around (digest-pinning the task definition, or a build-unique tag) if it
recurs.
