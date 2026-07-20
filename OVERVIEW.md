# Overview

A high-level description of what this application is and does. For build,
deploy, and operational history, see `HANDOFF.md`.

## What it is

**An enterprise operations "deep agent"** — an AI orchestrator that takes a
natural-language request (e.g. *"look into entity 42 and draft a change
request"*), breaks it into a plan, gathers the needed data, drafts supporting
analysis, and submits a change to a downstream enterprise system — **with a
human approving anything consequential before it happens.**

It is built on the **`deepagents`** framework (a supervisor-style agent
harness) which compiles to a standard **LangGraph** state machine, and it is
served as a web API.

## What it does — the workflow

1. **Plans** — breaks an incoming request into steps using the todo tools
   before acting.
2. **Delegates research** — hands focused data-gathering to a dedicated
   **research subagent**, so raw records never flood the orchestrator's
   context window. The subagent fetches records, verifies consistency, and
   returns a compact synthesis with citations to the record identifiers it
   used.
3. **Drafts in a workspace** — uses filesystem tools to write and review
   working documents under a sandboxed workspace root (real disk access is
   confined to that directory).
4. **Submits a change request** — only after the analysis is written and it is
   confident, it calls `submit_change_request` against the downstream workflow
   system.

## The two capabilities that define it

- **Human-in-the-loop approval gates.** Sensitive actions —
  `submit_change_request`, `write_file`, and `execute` — **pause the agent
  mid-run** at a LangGraph interrupt. The client sees the pending action and
  must **approve, edit, or reject** it before the agent continues. Nothing
  irreversible happens without a human decision.
- **Grounded, not fabricated.** The system prompt forbids inventing entity
  data — the agent must fetch real records through `fetch_entity_record` (an
  async HTTP client to an enterprise data service). If that backend is not
  configured, the tools return a clearly labeled "not configured" result
  instead of failing, so the module runs standalone.

## Tool surface

| Tool | Purpose | Gated by approval? |
| --- | --- | --- |
| `fetch_entity_record(entity_id)` | Fetch a normalized entity record from the enterprise data service | No |
| `submit_change_request(summary, payload)` | Submit an (irreversible) change request to the downstream workflow system | **Yes** |
| todo / filesystem tools | Planning and drafting in the workspace | `write_file` / `execute`: **yes** |

## How it is wired together

| Layer | What |
| --- | --- |
| **Agent** | `agent.py` — orchestrator + research subagent, the tools above, and the approval gates |
| **API** | `service/app.py` — FastAPI service that builds the graph once at startup, streams runs as Server-Sent Events, and exposes human approval as a first-class API state (returns HTTP 409 if you message a thread that is awaiting approval) |
| **Models** | Amazon **Bedrock** (`ChatBedrockConverse`), authenticated by the ECS task role's IAM permissions — no static API key. *Currently on a temporary Sonnet 4.5 default* pending resolution of the Opus 4.8 / Sonnet 5 Bedrock entitlement issue (see `HANDOFF.md` and `deploy/bedrock-support-case.md`) |
| **State** | Per-conversation, keyed by LangGraph `thread_id` — async **SQLite** locally, async **Postgres** (Neon) in production — so conversation checkpoints and cross-thread memory survive restarts and are shared across replicas (required for the resume-after-approval flow) |
| **Runtime** | Docker image → Amazon ECR → **ECS Fargate**, kept healthy behind a `/healthz` container health check |
| **CI/CD** | GitHub Actions: push to `main` → test → build → push (via GitHub **OIDC** federation, no long-lived keys) → **auto-deploy** to ECS. Docs-only (`*.md`) changes skip the pipeline |

## Client integration

The service streams over Server-Sent Events (SSE) via POST. A React hook and
example component live under `web/deep-agent-client/`. The load-bearing
contract is the **approval interrupt**: when the agent hits a gated tool, the
run pauses and surfaces the pending action as an SSE `interrupt` event; the
client resumes it on `POST /threads/{thread_id}/resume` with one decision per
pending action (`approve` / `edit` / `reject` / `respond`). See `HANDOFF.md`
"Contract 1" for the exact payload shape.

## In one sentence

> A durable, API-served AI agent that plans enterprise operations tasks,
> researches the real data behind them, and proposes changes — but hands every
> irreversible action to a human for approval first.
