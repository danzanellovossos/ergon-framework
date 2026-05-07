"""Tests for AsyncPostgresConnector — fetch/dispatch mapping to Transaction, init_async, close."""

from unittest.mock import AsyncMock, patch

import pytest

from ergon.connector.postgres.async_connector import AsyncPostgresConnector
from ergon.connector.postgres.models import PostgresClient, PostgresConsumerConfig, PostgresProducerConfig
from ergon.connector.transaction import Transaction

pytestmark = pytest.mark.asyncio(loop_scope="function")


def _make_client() -> PostgresClient:
    return PostgresClient(user="test", password="test", database="testdb")


def _make_connector(
    consumer_config=None,
    producer_config=None,
) -> AsyncPostgresConnector:
    return AsyncPostgresConnector(
        client=_make_client(),
        consumer_config=consumer_config,
        producer_config=producer_config,
    )


class TestFetchTransactions:
    async def test_fetch_maps_rows_to_transactions(self):
        config = PostgresConsumerConfig(
            fetch_query="SELECT * FROM events_outbox WHERE published_at IS NULL",
            id_column="id",
        )
        connector = _make_connector(consumer_config=config)

        rows = [
            {"id": "abc-123", "event_type": "user.created", "payload": {"name": "alice"}},
            {"id": "def-456", "event_type": "user.deleted", "payload": {"name": "bob"}},
        ]

        with patch.object(connector.service, "fetch", new_callable=AsyncMock, return_value=rows):
            txns = await connector.fetch_transactions_async(batch_size=10)

        assert len(txns) == 2
        assert txns[0].id == "abc-123"
        assert txns[0].payload == rows[0]
        assert txns[1].id == "def-456"

    async def test_fetch_empty_returns_empty_list(self):
        config = PostgresConsumerConfig(fetch_query="SELECT 1")
        connector = _make_connector(consumer_config=config)

        with patch.object(connector.service, "fetch", new_callable=AsyncMock, return_value=[]):
            txns = await connector.fetch_transactions_async(batch_size=10)

        assert txns == []

    async def test_fetch_requires_consumer_config(self):
        connector = _make_connector()
        with pytest.raises(ValueError, match="consumer_config"):
            await connector.fetch_transactions_async(batch_size=1)

    async def test_fetch_uses_custom_id_column(self):
        config = PostgresConsumerConfig(
            fetch_query="SELECT * FROM jobs",
            id_column="job_id",
        )
        connector = _make_connector(consumer_config=config)

        rows = [{"job_id": "j-001", "status": "pending"}]

        with patch.object(connector.service, "fetch", new_callable=AsyncMock, return_value=rows):
            txns = await connector.fetch_transactions_async()

        assert txns[0].id == "j-001"

    async def test_fetch_passes_batch_size(self):
        config = PostgresConsumerConfig(fetch_query="SELECT 1", batch_size=50)
        connector = _make_connector(consumer_config=config)

        with patch.object(connector.service, "fetch", new_callable=AsyncMock, return_value=[]) as mock_fetch:
            await connector.fetch_transactions_async(batch_size=25)

        assert mock_fetch.call_args.kwargs.get("limit") == 25 or mock_fetch.call_args[0][2] == 25


class TestDispatchTransactions:
    async def test_dispatch_executes_query_per_transaction(self):
        config = PostgresProducerConfig(
            dispatch_query="UPDATE events_outbox SET published_at = now() WHERE id = $1",
        )
        connector = _make_connector(producer_config=config)

        txns = [
            Transaction(id="abc-123", payload={"event_type": "user.created"}),
            Transaction(id="def-456", payload={"event_type": "user.deleted"}),
        ]

        with patch.object(connector.service, "execute", new_callable=AsyncMock) as mock_execute:
            await connector.dispatch_transactions_async(txns)

        assert mock_execute.await_count == 2
        first_params = mock_execute.call_args_list[0][0][1]
        assert "abc-123" in first_params

    async def test_dispatch_requires_producer_config(self):
        connector = _make_connector()
        txns = [Transaction(id="1", payload={})]
        with pytest.raises(ValueError, match="producer_config"):
            await connector.dispatch_transactions_async(txns)


class TestInitAsync:
    async def test_init_async_sets_up_listener(self):
        config = PostgresConsumerConfig(
            fetch_query="SELECT 1",
            listen_channel="outbox",
        )
        connector = _make_connector(consumer_config=config)

        with patch.object(connector.service, "listen", new_callable=AsyncMock) as mock_listen:
            await connector.init_async()

        mock_listen.assert_awaited_once()
        called_channel = mock_listen.call_args[0][0]
        assert called_channel == "outbox"
        assert connector._notify_event is not None

    async def test_init_async_noop_without_listen_channel(self):
        config = PostgresConsumerConfig(fetch_query="SELECT 1")
        connector = _make_connector(consumer_config=config)

        with patch.object(connector.service, "listen", new_callable=AsyncMock) as mock_listen:
            await connector.init_async()

        mock_listen.assert_not_awaited()
        assert connector._notify_event is None


class TestWaitForNotify:
    async def test_wait_returns_false_without_listener(self):
        connector = _make_connector()
        result = await connector.wait_for_notify(timeout=0.01)
        assert result is False

    async def test_wait_returns_false_on_timeout(self):
        config = PostgresConsumerConfig(fetch_query="SELECT 1", listen_channel="test")
        connector = _make_connector(consumer_config=config)

        with patch.object(connector.service, "listen", new_callable=AsyncMock):
            await connector.init_async()

        result = await connector.wait_for_notify(timeout=0.01)
        assert result is False

    async def test_wait_returns_true_when_notified(self):
        config = PostgresConsumerConfig(fetch_query="SELECT 1", listen_channel="test")
        connector = _make_connector(consumer_config=config)

        with patch.object(connector.service, "listen", new_callable=AsyncMock):
            await connector.init_async()

        connector._notify_event.set()
        result = await connector.wait_for_notify(timeout=1.0)
        assert result is True


class TestClose:
    async def test_close_delegates_to_service(self):
        connector = _make_connector()

        with patch.object(connector.service, "close", new_callable=AsyncMock) as mock_close:
            await connector.close()

        mock_close.assert_awaited_once()
