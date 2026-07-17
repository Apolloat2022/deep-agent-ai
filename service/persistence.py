"""Async persistence selection for the deep agent service.

Chooses the checkpointer and store implementation from the ``AGENT_ENV``
environment variable:

* ``local`` (default): async SQLite on the local filesystem. Suitable for
  development and the single container smoke test.
* ``prod``: async Postgres. This is the correct choice on ECS, where task
  filesystems are ephemeral and multiple replicas must share conversation
  state and cross thread memory.

The Postgres dependencies are imported lazily so a local install does not
need ``langgraph-checkpoint-postgres`` or ``psycopg`` present.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.store.base import BaseStore

STATE_DIR = Path(os.environ.get("AGENT_STATE_DIR", "./state")).resolve()


@asynccontextmanager
async def open_persistence() -> AsyncIterator[tuple[BaseCheckpointSaver, BaseStore]]:
    """Yield an async ``(checkpointer, store)`` pair for the current env.

    Both objects are set up (tables created) before being yielded and are
    cleaned up when the surrounding context exits. Use this from the
    FastAPI lifespan handler so the connections live for the process.
    """
    env = os.environ.get("AGENT_ENV", "local").lower()
    if env == "prod":
        async with _open_postgres() as pair:
            yield pair
    else:
        async with _open_sqlite() as pair:
            yield pair


@asynccontextmanager
async def _open_sqlite() -> AsyncIterator[tuple[BaseCheckpointSaver, BaseStore]]:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.store.sqlite.aio import AsyncSqliteStore

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint_path = str(STATE_DIR / "checkpoints.sqlite")
    store_path = str(STATE_DIR / "store.sqlite")

    async with (
        AsyncSqliteSaver.from_conn_string(checkpoint_path) as checkpointer,
        AsyncSqliteStore.from_conn_string(store_path) as store,
    ):
        await checkpointer.setup()
        await store.setup()
        yield checkpointer, store


@asynccontextmanager
async def _open_postgres() -> AsyncIterator[tuple[BaseCheckpointSaver, BaseStore]]:
    # Lazy imports: only required when AGENT_ENV=prod.
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore

    db_url = os.environ["DATABASE_URL"]  # injected from Secrets Manager on ECS.

    async with (
        AsyncPostgresSaver.from_conn_string(db_url) as checkpointer,
        AsyncPostgresStore.from_conn_string(db_url) as store,
    ):
        # setup runs idempotent migrations; safe to call on every boot.
        await checkpointer.setup()
        await store.setup()
        yield checkpointer, store
