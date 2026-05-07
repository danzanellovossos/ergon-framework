"""Tests for AsyncRabbitMQConnector — fetch/dispatch mapping to Transaction, ack/nack."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ergon.connector.rabbitmq.async_connector import AsyncRabbitMQConnector
from ergon.connector.rabbitmq.models import (
    AsyncRabbitmqClient,
    AsyncRabbitmqConsumerConfig,
    AsyncRabbitmqProducerConfig,
)
from ergon.connector.transaction import Transaction

pytestmark = pytest.mark.asyncio(loop_scope="function")


def _make_client() -> AsyncRabbitmqClient:
    return AsyncRabbitmqClient(username="guest", password="guest")


def _make_connector(
    consumer_config=None,
    producer_config=None,
) -> AsyncRabbitMQConnector:
    return AsyncRabbitMQConnector(
        client=_make_client(),
        consumer_config=consumer_config,
        producer_config=producer_config,
    )


def _raw_message():
    msg = MagicMock()
    msg.ack = AsyncMock()
    msg.nack = AsyncMock()
    return msg


class TestFetchTransactions:
    async def test_fetch_maps_to_transactions(self):
        raw_msg = _raw_message()
        service_messages = [
            {
                "body": {"event_type": "user.created"},
                "routing_key": "user.created",
                "delivery_tag": 42,
                "headers": {"x-source": "api"},
                "content_type": "application/json",
                "message_id": "msg-123",
                "correlation_id": "corr-456",
                "_message": raw_msg,
            }
        ]

        config = AsyncRabbitmqConsumerConfig(queue_name="test-queue", exchange_name="events")
        connector = _make_connector(consumer_config=config)

        with patch.object(connector.service, "consume", new_callable=AsyncMock, return_value=service_messages):
            txns = await connector.fetch_transactions_async(batch_size=5)

        assert len(txns) == 1
        tx = txns[0]
        assert tx.id == "42"
        assert tx.payload == {"event_type": "user.created"}
        assert tx.metadata["routing_key"] == "user.created"
        assert tx.metadata["delivery_tag"] == 42
        assert tx.metadata["headers"] == {"x-source": "api"}
        assert tx.metadata["_message"] is raw_msg

    async def test_fetch_empty_returns_empty_list(self):
        config = AsyncRabbitmqConsumerConfig(queue_name="test-queue")
        connector = _make_connector(consumer_config=config)

        with patch.object(connector.service, "consume", new_callable=AsyncMock, return_value=[]):
            txns = await connector.fetch_transactions_async(batch_size=10)

        assert txns == []

    async def test_fetch_requires_consumer_config(self):
        connector = _make_connector()
        with pytest.raises(ValueError, match="consumer_config"):
            await connector.fetch_transactions_async(batch_size=1)

    async def test_fetch_with_overrides(self):
        config = AsyncRabbitmqConsumerConfig(queue_name="original-queue", exchange_name="original-exchange")
        connector = _make_connector(consumer_config=config)

        with patch.object(connector.service, "consume", new_callable=AsyncMock, return_value=[]) as mock_consume:
            await connector.fetch_transactions_async(
                batch_size=1,
                queue_name="override-queue",
                exchange_name="override-exchange",
            )

        called_config = mock_consume.call_args[0][0]
        assert called_config.queue_name == "override-queue"
        assert called_config.exchange_name == "override-exchange"


class TestDispatchTransactions:
    async def test_dispatch_publishes_each_transaction(self):
        config = AsyncRabbitmqProducerConfig(exchange_name="events", routing_key="default.key")
        connector = _make_connector(producer_config=config)

        txns = [
            Transaction(id="1", payload={"action": "create"}, metadata={"routing_key": "user.created"}),
            Transaction(id="2", payload={"action": "delete"}, metadata={"routing_key": "user.deleted"}),
        ]

        with patch.object(connector.service, "publish", new_callable=AsyncMock) as mock_publish:
            await connector.dispatch_transactions_async(txns)

        assert mock_publish.await_count == 2

        first_call = mock_publish.call_args_list[0]
        assert first_call.kwargs["routing_key"] == "user.created"

        second_call = mock_publish.call_args_list[1]
        assert second_call.kwargs["routing_key"] == "user.deleted"

    async def test_dispatch_uses_default_routing_key(self):
        config = AsyncRabbitmqProducerConfig(exchange_name="events", routing_key="fallback.key")
        connector = _make_connector(producer_config=config)

        txns = [Transaction(id="1", payload={"x": 1}, metadata={})]

        with patch.object(connector.service, "publish", new_callable=AsyncMock) as mock_publish:
            await connector.dispatch_transactions_async(txns)

        assert mock_publish.call_args.kwargs["routing_key"] == "fallback.key"

    async def test_dispatch_json_encodes_dict_payload(self):
        config = AsyncRabbitmqProducerConfig()
        connector = _make_connector(producer_config=config)

        txns = [Transaction(id="1", payload={"key": "value"})]

        with patch.object(connector.service, "publish", new_callable=AsyncMock) as mock_publish:
            await connector.dispatch_transactions_async(txns)

        body = mock_publish.call_args.kwargs["body"]
        assert isinstance(body, bytes)
        assert json.loads(body) == {"key": "value"}


class TestAckNack:
    async def test_ack_delegates_to_service(self):
        raw_msg = _raw_message()
        connector = _make_connector()

        tx = Transaction(id="1", payload={}, metadata={"_message": raw_msg})

        with patch.object(connector.service, "ack", new_callable=AsyncMock) as mock_ack:
            await connector.ack_transaction(tx)

        mock_ack.assert_awaited_once_with(raw_msg)

    async def test_nack_delegates_to_service(self):
        raw_msg = _raw_message()
        connector = _make_connector()

        tx = Transaction(id="1", payload={}, metadata={"_message": raw_msg})

        with patch.object(connector.service, "nack", new_callable=AsyncMock) as mock_nack:
            await connector.nack_transaction(tx, requeue=False)

        mock_nack.assert_awaited_once_with(raw_msg, requeue=False)

    async def test_ack_raises_without_raw_message(self):
        connector = _make_connector()
        tx = Transaction(id="1", payload={}, metadata={})

        with pytest.raises(ValueError, match="no raw message"):
            await connector.ack_transaction(tx)


class TestClose:
    async def test_close_delegates_to_service(self):
        connector = _make_connector()

        with patch.object(connector.service, "close", new_callable=AsyncMock) as mock_close:
            await connector.close()

        mock_close.assert_awaited_once()
