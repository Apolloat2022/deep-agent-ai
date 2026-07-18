# Examples

## End-to-end demo (`run_demo.py`)

Runs the **real** deep-agent FastAPI service against a **mock** enterprise
backend, and walks the full tool-call + human-in-the-loop approval loop, so you
can watch the whole system work without wiring up a production API.

It exercises the entire stack for real:

1. The agent decides to call a tool and makes a **real HTTP request** to the
   mock (`GET /v1/entities/42`).
2. The model (Amazon Bedrock) **reasons over the returned record** — a fake
   high-risk counterparty with two compliance flags.
3. The run **pauses for human approval** before the gated
   `submit_change_request` tool — nothing irreversible happens automatically.
4. On approval it **resumes and submits** (`POST /v1/change-requests`), and the
   mock issues a ticket.

The mock logs every call it receives with a `[MOCK]` prefix, so you can confirm
the calls are real and not fabricated by the model.

### Prerequisites

Same as running the service itself (see the top-level [README](../README.md)):

- The deepagents venv set up at `deep-agent-core/.venv`.
- Resolvable AWS credentials with Bedrock access, and `AWS_REGION` set. **This
  makes real (small) Bedrock model calls.**

### Run it

One command — it starts and stops the mock for you:

```bash
# Windows (PowerShell / Git Bash)
deep-agent-core/.venv/Scripts/python.exe examples/run_demo.py

# macOS / Linux
deep-agent-core/.venv/bin/python examples/run_demo.py
```

Expected output (abridged):

```
[demo] mock is up.
>>> USER: Look up entity 42 ... submit a single change request ...
[turn 1] HTTP 200, events: ['token', ..., 'interrupt']
    [PAUSED for approval] -> tool: submit_change_request
       args: {"summary": "Escalate entity 42 (Northwind Trading Co.) ...
>>> REVIEWER: approve (1 pending action(s))
[turn 2] HTTP 200, events: ['token', ..., 'done']
    [DONE]: Change request submitted successfully ... CR-XXXXXXXX (status: pending_review)
=== final thread state: awaiting_approval=False ===
```

### Files

| File | Purpose |
| --- | --- |
| `run_demo.py` | Driver: starts the mock, drives the real service, walks the approval loop, tears down. |
| `mock_enterprise_api.py` | Mock backend implementing the two endpoint shapes `service/clients.py` expects. Fake risk/compliance data. |

`run_demo.py` uses local async SQLite persistence (`AGENT_ENV=local`) and writes
its scratch state under `examples/.demo_state/` and `examples/.demo_workspace/`
(both gitignored). It never touches Postgres or any AWS resource other than
Bedrock for the model calls.

### Pointing at a real backend instead

The mock exists only to stand in for a real service. To run the agent against a
real backend, set `ENTERPRISE_API_BASE_URL` (and `ENTERPRISE_API_TOKEN` if it
needs auth) to that service instead — see
[Configuration](../README.md#configuration). The only requirement is that the
backend implements `GET /v1/entities/{id}` and `POST /v1/change-requests`, or
that `service/clients.py` is adjusted to match its actual shape.
```
