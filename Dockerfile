# syntax=docker/dockerfile:1

# Build context is the project root (deep-agent-ai/). The deepagents
# library is installed editable from the vendored clone at
# deep-agent-core/libs/deepagents, so that directory must be copied in
# before the install step.

FROM python:3.13-slim AS builder

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY deep-agent-core/libs/deepagents /app/deep-agent-core/libs/deepagents
COPY pyproject.toml /app/pyproject.toml
COPY agent.py /app/agent.py
COPY service/ /app/service/

RUN uv venv /opt/venv && \
    uv pip install -p /opt/venv \
        -e /app/deep-agent-core/libs/deepagents \
        langgraph \
        langgraph-checkpoint-postgres \
        "psycopg[binary,pool]" \
        fastapi \
        "uvicorn[standard]" \
        httpx \
        python-dotenv

FROM python:3.13-slim AS runtime

COPY --from=builder /opt/venv /opt/venv
COPY agent.py /app/agent.py
COPY service/ /app/service/

WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH"

EXPOSE 8080

CMD ["uvicorn", "service.app:app", "--host", "0.0.0.0", "--port", "8080"]
