# Handoff

This document records the work completed across four sessions (Opus,
Sonnet, Sonnet again after a stalled Haiku attempt, then Sonnet with real
AWS access) and pins down the contracts that were expensive to establish
(the approval interrupt payload, the persistence topology, the enterprise
client behavior, the Bedrock IAM pattern) so future work does not
rediscover them.

**Current state, as of Session 4:** the account is a real AWS account
(`924056189531`, `us-east-1`, IAM user `Riskguard-ai`), discovered and
reconciled against rather than guessed. Models are served through Amazon
Bedrock, not the first party Anthropic API — a deliberate choice made once
real AWS access existed and revealed the account's existing pattern (see
Session 4 and the model strategy section in `PLAN.md`). Two things remain
genuinely open, both because they are the operator's call, not because
anything was skipped: provisioning Postgres and creating the `DATABASE_URL`
secret, and deciding whether to set up GitHub OIDC federation for automated
CI deploys (this account has none at all right now). Both are spelled out
exactly, with commands, in Session 4 below.

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

**Not yet verified as unblocked**: as of the last check in this session
(roughly 20 minutes after acceptance), both models still return
`AccessDeniedException`. AWS documents that access can take a few minutes
to propagate after acceptance; this can occasionally take longer. This was
not chased further with a polling loop. **Recheck before relying on this**:

```bash
aws bedrock-runtime converse --model-id "us.anthropic.claude-opus-4-8" \
    --messages '[{"role":"user","content":[{"text":"ok"}]}]' \
    --inference-config '{"maxTokens":10}'
```

If it still 403s after a longer wait, the acceptance may need to be redone
or checked in the Bedrock console under Model access.

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

**Not registered with ECS** (`aws ecs register-task-definition`): the file
still has one placeholder (`DATABASE_URL`'s `valueFrom`), which would just
fail schema validation on that field — a throwaway registration for that
sole purpose seemed lower value than leaving a stray revision in the
account, so it was skipped. Register it for real once the secret below
exists.

### What is still genuinely open — this is what's left for the operator

**1. Provision Postgres and create the `DATABASE_URL` secret.** Per the
operator's own "dedicated Postgres" decision — this repo doesn't create
databases. Once you have a connection string, create the secret with this
exact name (the execution role's IAM policy is scoped to this name prefix,
so a different name will not work without also updating that policy):

```bash
aws secretsmanager create-secret \
    --name deep-agent-core-service/database-url \
    --secret-string "postgresql://<user>:<password>@<host>:5432/<dbname>"
```

Then put the resulting `ARN` from that command's output into
`deploy/task-definition.json`'s `DATABASE_URL.valueFrom`, and register the
task definition:

```bash
aws ecs register-task-definition --cli-input-json file://deploy/task-definition.json
```

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
