"""Tests for AsyncPostgresService — pool lifecycle, fetch, execute, listen/unlisten."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ergon.connector.postgres.async_service import AsyncPostgresService
from ergon.connector.postgres.models import PostgresClient

pytestmark = pytest.mark.asyncio(loop_scope="function")


def _make_client(**overrides) -> PostgresClient:
    defaults = {"user": "test", "password": "test", "database": "testdb"}
    defaults.update(overrides)
    return PostgresClient(**defaults)


def _make_pool_with_conn(mock_conn):
    """Build a mock pool whose acquire() returns a proper async context manager."""
    mock_pool = MagicMock()
    mock_pool._closed = False
    mock_pool.close = AsyncMock()

    @asynccontextmanager
    async def _acquire():
        yield mock_conn

    mock_pool.acquire = _acquire
    return mock_pool


class TestClientDsn:
    def test_dsn_from_individual_params(self):
        client = _make_client()
        assert client.get_dsn() == "postgresql://test:test@localhost:5432/testdb"

    def test_explicit_dsn_takes_precedence(self):
        client = _make_client(dsn="postgresql://other:other@db:5433/otherdb")
        assert client.get_dsn() == "postgresql://other:other@db:5433/otherdb"


class TestPool:
    @patch("ergon.connector.postgres.async_service.asyncpg.create_pool", new_callable=AsyncMock)
    async def test_lazy_pool_creation(self, mock_create_pool):
        mock_pool = AsyncMock()
        mock_pool._closed = False
        mock_create_pool.return_value = mock_pool

        service = AsyncPostgresService(_make_client())
        assert service._pool is None

        pool = await service._get_pool()
        assert pool is mock_pool
        mock_create_pool.assert_awaited_once()

    @patch("ergon.connector.postgres.async_service.asyncpg.create_pool", new_callable=AsyncMock)
    async def test_pool_reuse(self, mock_create_pool):
        mock_pool = AsyncMock()
        mock_pool._closed = False
        mock_create_pool.return_value = mock_pool

        service = AsyncPostgresService(_make_client())
        await service._get_pool()
        await service._get_pool()
        mock_create_pool.assert_awaited_once()


class TestFetch:
    @patch("ergon.connector.postgres.async_service.asyncpg.create_pool", new_callable=AsyncMock)
    async def test_fetch_returns_dicts(self, mock_create_pool):
        row1 = {"id": "1", "name": "alice"}
        row2 = {"id": "2", "name": "bob"}

        class FakeRecord(dict):
            pass

        records = [FakeRecord(row1), FakeRecord(row2)]

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=records)
        mock_create_pool.return_value = _make_pool_with_conn(mock_conn)

        service = AsyncPostgresService(_make_client())
        result = await service.fetch("SELECT * FROM users", [])

        assert len(result) == 2
        assert result[0] == row1
        assert result[1] == row2

    @patch("ergon.connector.postgres.async_service.asyncpg.create_pool", new_callable=AsyncMock)
    async def test_fetch_appends_limit(self, mock_create_pool):
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_create_pool.return_value = _make_pool_with_conn(mock_conn)

        service = AsyncPostgresService(_make_client())
        await service.fetch("SELECT * FROM users", [], limit=50)

        called_query = mock_conn.fetch.call_args[0][0]
        assert "LIMIT 50" in called_query

    @patch("ergon.connector.postgres.async_service.asyncpg.create_pool", new_callable=AsyncMock)
    async def test_fetch_does_not_double_limit(self, mock_create_pool):
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_create_pool.return_value = _make_pool_with_conn(mock_conn)

        service = AsyncPostgresService(_make_client())
        await service.fetch("SELECT * FROM users LIMIT 10", [], limit=50)

        called_query = mock_conn.fetch.call_args[0][0]
        assert called_query.count("LIMIT") == 1


class TestExecute:
    @patch("ergon.connector.postgres.async_service.asyncpg.create_pool", new_callable=AsyncMock)
    async def test_execute_runs_statement(self, mock_create_pool):
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")
        mock_create_pool.return_value = _make_pool_with_conn(mock_conn)

        service = AsyncPostgresService(_make_client())
        result = await service.execute("UPDATE users SET active = $1 WHERE id = $2", [True, "1"])

        assert result == "UPDATE 1"
        mock_conn.execute.assert_awaited_once()


class TestListen:
    @patch("ergon.connector.postgres.async_service.asyncpg.connect", new_callable=AsyncMock)
    async def test_listen_opens_dedicated_connection(self, mock_connect):
        mock_listener_conn = AsyncMock()
        mock_connect.return_value = mock_listener_conn

        service = AsyncPostgresService(_make_client())
        callback = MagicMock()

        await service.listen("outbox", callback)

        mock_connect.assert_awaited_once()
        mock_listener_conn.add_listener.assert_awaited_once_with("outbox", callback)
        assert service._listen_channel == "outbox"

    @patch("ergon.connector.postgres.async_service.asyncpg.connect", new_callable=AsyncMock)
    async def test_unlisten_closes_connection(self, mock_connect):
        mock_listener_conn = AsyncMock()
        mock_connect.return_value = mock_listener_conn

        service = AsyncPostgresService(_make_client())
        await service.listen("outbox", MagicMock())
        await service.unlisten()

        mock_listener_conn.close.assert_awaited_once()
        assert service._listener_conn is None
        assert service._listen_channel is None


class TestClose:
    @patch("ergon.connector.postgres.async_service.asyncpg.create_pool", new_callable=AsyncMock)
    async def test_close_closes_pool(self, mock_create_pool):
        mock_pool = AsyncMock()
        mock_pool._closed = False
        mock_pool.close = AsyncMock()
        mock_create_pool.return_value = mock_pool

        service = AsyncPostgresService(_make_client())
        await service._get_pool()
        await service.close()

        mock_pool.close.assert_awaited_once()
        assert service._pool is None
