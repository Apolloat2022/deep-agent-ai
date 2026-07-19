# syntax=docker/dockerfile:1

# Build context is the project root (deep-agent-ai/). deepagents is installed
# from PyPI (pinned to the version the vendored deep-agent-core clone tracks),
# not from a local editable path, so the source tree does not need to be in
# the build context or the image -- CI checkouts don't include it (it's a
# gitignored embedded repo). Bump this pin when the vendored clone is updated.

FROM python:3.13-slim AS builder

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
COPY agent.py /app/agent.py
COPY service/ /app/service/

RUN uv venv /opt/venv && \
    uv pip install -p /opt/venv \
        "deepagents[aws]==0.6.12" \
        langgraph \
        langgraph-checkpoint-postgres \
        "psycopg[binary,pool]" \
        fastapi \
        "uvicorn[standard]" \
        httpx \
        python-dotenv

FROM python:3.13-slim AS runtime

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY agent.py /app/agent.py
COPY service/ /app/service/

WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH"

EXPOSE 8080

CMD ["uvicorn", "service.app:app", "--host", "0.0.0.0", "--port", "8080"]
