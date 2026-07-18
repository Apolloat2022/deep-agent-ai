"""End-to-end demo: drive the real deep-agent service against a mock backend.

Runs the actual production code path -- ``service.app`` over HTTP via
``httpx.ASGITransport`` (the same transport the integration tests use) -- with
``ENTERPRISE_API_BASE_URL`` pointed at ``examples/mock_enterprise_api.py``. It
sends one realistic request, then walks the human-in-the-loop approval loop:
whenever the agent pauses on a gated tool, the pending action is printed and
approved, until the run finishes.

This exercises the whole stack for real: the agent decides to call a tool ->
makes a real HTTP request to the mock -> the model (Amazon Bedrock) reasons
over the returned data -> the run PAUSES for human approval before the gated
``submit_change_request`` tool -> on approval it resumes and submits.

Prerequisites (same as running the service itself, see the project README):
  * The deepagents venv is set up (``deep-agent-core/.venv``).
  * Resolvable AWS credentials with Bedrock access, and a region -- this makes
    real (small) Bedrock model calls.

Run it (one command; it starts and stops the mock for you):

    deep-agent-core/.venv/Scripts/python.exe examples/run_demo.py      # Windows
    deep-agent-core/.venv/bin/python examples/run_demo.py              # macOS/Linux

Set ``MOCK_ALREADY_RUNNING=1`` to skip launching the mock (if you started it
yourself on ``$MOCK_PORT``, default 8099).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

# --- Paths and environment MUST be set before importing service/agent
# --- modules, because several read os.environ at import time (STATE_DIR,
# --- model ids, the enterprise base URL).
EXAMPLES_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EXAMPLES_DIR.parent

MOCK_PORT = int(os.environ.get("MOCK_PORT", "8099"))
MOCK_BASE_URL = f"http://127.0.0.1:{MOCK_PORT}"

os.environ["ENTERPRISE_API_BASE_URL"] = MOCK_BASE_URL
os.environ["AGENT_ENV"] = "local"  # async SQLite; no Postgres needed
os.environ["AGENT_STATE_DIR"] = str(EXAMPLES_DIR / ".demo_state")
os.environ["AGENT_WORKSPACE"] = str(EXAMPLES_DIR / ".demo_workspace")
os.environ.setdefault("AWS_REGION", "us-east-1")
# Keep output ASCII-safe across consoles (Windows cp1252, etc.).
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
from service.app import app, _shutdown, _startup  # noqa: E402


def parse_sse(raw: str) -> list[tuple[str, dict]]:
    """Parse the SSE frames ``service.app`` emits into (event, data) tuples."""
    events = []
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        try:
            event_line = next(l for l in lines if l.startswith("event: "))
            data_line = next(l for l in lines if l.startswith("data: "))
        except StopIteration:
            continue
        events.append(
            (
                event_line.removeprefix("event: "),
                json.loads(data_line.removeprefix("data: ")),
            )
        )
    return events


def summarize(events: list[tuple[str, dict]]) -> None:
    """Print a compact view: streamed model text, any interrupts, the result."""
    tokens = "".join(d.get("text", "") for e, d in events if e == "token")
    if tokens.strip():
        trimmed = tokens.strip()
        if len(trimmed) > 600:
            trimmed = trimmed[:600] + " ..."
        print(f"    model text: {trimmed}")
    for e, d in events:
        if e == "interrupt":
            for ar in d.get("action_requests", []):
                print(f"    [PAUSED for approval] -> tool: {ar['name']}")
                print(f"       args: {json.dumps(ar.get('args', {}), default=str)[:400]}")
        elif e == "done":
            content = d.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
            print(f"    [DONE]: {str(content).strip()[:600]}")


def start_mock() -> subprocess.Popen | None:
    """Launch the mock backend and wait for it to answer /healthz."""
    if os.environ.get("MOCK_ALREADY_RUNNING") == "1":
        print(f"[demo] using already-running mock at {MOCK_BASE_URL}")
        return None
    print(f"[demo] starting mock backend on {MOCK_BASE_URL} ...")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "mock_enterprise_api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(MOCK_PORT),
            "--log-level",
            "warning",
        ],
        cwd=str(EXAMPLES_DIR),
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(f"{MOCK_BASE_URL}/healthz", timeout=1).status_code == 200:
                print("[demo] mock is up.")
                return proc
        except Exception:
            time.sleep(0.4)
    proc.terminate()
    raise RuntimeError("mock backend did not become healthy in time")


async def main() -> None:
    mock = start_mock()
    await _startup()
    transport = httpx.ASGITransport(app=app)
    thread_id = str(uuid.uuid4())
    print(f"\n=== Deep agent demo - thread {thread_id} ===\n")

    prompt = (
        "Look up entity 42 using the fetch_entity_record tool. Based on its "
        "current risk flags, submit a single change request (via "
        "submit_change_request) to escalate the entity for enhanced due "
        "diligence review. Put the flag codes in the summary."
    )

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://svc") as c:
            print(f">>> USER: {prompt}\n")
            r = await c.post(
                f"/threads/{thread_id}/messages",
                json={"content": prompt},
                timeout=120,
            )
            events = parse_sse(r.text)
            print(f"[turn 1] HTTP {r.status_code}, events: {[e for e, _ in events]}")
            summarize(events)

            # Approval loop: keep approving pending gated actions until the run
            # finishes (a 'done' event, no more interrupts).
            for turn in range(2, 8):
                interrupts = [d for e, d in events if e == "interrupt"]
                if not interrupts:
                    break
                n_actions = len(interrupts[0].get("action_requests", []))
                decisions = [{"type": "approve"} for _ in range(n_actions)]
                print(f"\n>>> REVIEWER: approve ({n_actions} pending action(s))\n")
                r = await c.post(
                    f"/threads/{thread_id}/resume",
                    json={"decisions": decisions},
                    timeout=120,
                )
                events = parse_sse(r.text)
                print(f"[turn {turn}] HTTP {r.status_code}, events: {[e for e, _ in events]}")
                summarize(events)

            state = await c.get(f"/threads/{thread_id}/state")
            print(
                f"\n=== final thread state: awaiting_approval="
                f"{state.json().get('awaiting_approval')} ==="
            )
    finally:
        await _shutdown()
        if mock is not None:
            mock.terminate()
            try:
                mock.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mock.kill()
            print("[demo] mock backend stopped.")


if __name__ == "__main__":
    asyncio.run(main())
