import logging
from typing import Any, Callable, Dict, List, Optional

import asyncpg

from .models import PostgresClient

logger = logging.getLogger(__name__)


class AsyncPostgresService:
    def __init__(self, client: PostgresClient) -> None:
        self.client = client

        self._pool: Optional[asyncpg.Pool] = None
        self._listener_conn: Optional[asyncpg.Connection] = None
        self._listen_channel: Optional[str] = None

    # ---------- Connection Pool ----------

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None or self._pool._closed:  # type: ignore[attr-defined]
            dsn = self.client.get_dsn()
            ssl_mode = "require" if self.client.ssl else None

            self._pool = await asyncpg.create_pool(
                dsn,
                min_size=self.client.min_pool_size,
                max_size=self.client.max_pool_size,
                ssl=ssl_mode,
            )
            logger.info(
                "PostgreSQL connection pool created (min=%d, max=%d)",
                self.client.min_pool_size,
                self.client.max_pool_size,
            )

        return self._pool

    # ---------- Query ----------

    async def fetch(
        self,
        query: str,
        params: Optional[List[Any]] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Execute a SELECT query and return rows as list of dicts."""
        pool = await self._get_pool()

        effective_query = query
        if limit is not None and "LIMIT" not in query.upper():
            effective_query = f"{query} LIMIT {limit}"

        async with pool.acquire() as conn:
            rows = await conn.fetch(effective_query, *(params or []))

        return [dict(row) for row in rows]

    async def execute(self, query: str, params: Optional[List[Any]] = None) -> str:
        """Execute a single statement (INSERT, UPDATE, DELETE). Returns status string."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(query, *(params or []))
        return result

    async def execute_many(self, query: str, params_list: List[List[Any]]) -> None:
        """Execute a statement for each set of params in the list."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.executemany(query, params_list)

    # ---------- LISTEN / NOTIFY ----------

    async def listen(self, channel: str, callback: Callable[..., Any]) -> None:
        """
        Open a dedicated connection and subscribe to a PG NOTIFY channel.

        The callback receives (connection, pid, channel, payload).
        """
        if self._listener_conn is not None:
            logger.warning("Listener already active on channel=%s, closing before re-listen", self._listen_channel)
            await self.unlisten()

        dsn = self.client.get_dsn()
        ssl_mode = "require" if self.client.ssl else None
        conn = await asyncpg.connect(dsn, ssl=ssl_mode)
        self._listener_conn = conn
        self._listen_channel = channel

        await conn.add_listener(channel, callback)
        logger.info("Listening on PG channel=%s", channel)

    async def unlisten(self) -> None:
        """Remove listener and close the dedicated listener connection."""
        if self._listener_conn is not None:
            if self._listen_channel:
                try:
                    await self._listener_conn.remove_listener(self._listen_channel, lambda *a: None)
                except Exception:
                    pass
            await self._listener_conn.close()
            self._listener_conn = None
            self._listen_channel = None
            logger.info("PostgreSQL listener connection closed")

    # ---------- Lifecycle ----------

    async def close(self) -> None:
        """Close pool and listener connection."""
        await self.unlisten()

        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("PostgreSQL connection pool closed")
