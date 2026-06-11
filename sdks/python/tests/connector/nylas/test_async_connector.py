"""Tests for AsyncNylasConnector — fetch/dispatch mapping to Transaction, ack."""

from unittest.mock import AsyncMock, patch

import pytest

from ergon.connector.nylas.async_connector import AsyncNylasConnector
from ergon.connector.nylas.models import (
    AckActionConfig,
    NylasClient,
    NylasConsumerConfig,
    NylasProducerConfig,
)
from ergon.connector.transaction import Transaction

pytestmark = pytest.mark.asyncio(loop_scope="function")


def _make_client() -> NylasClient:
    return NylasClient(api_key="test-key", grant_id="grant-123")


def _make_connector(consumer_config=None, producer_config=None) -> AsyncNylasConnector:
    with patch("ergon.connector.nylas.service._get_nylas_client"):
        return AsyncNylasConnector(
            client=_make_client(),
            consumer_config=consumer_config,
            producer_config=producer_config,
        )


class TestFetchTransactions:
    async def test_fetch_delegates_to_service(self):
        config = NylasConsumerConfig(subject="Invoice", has_attachment=True, batch_size=5)
        connector = _make_connector(consumer_config=config)

        expected_tx = Transaction(
            id="msg-1",
            payload={"id": "msg-1", "subject": "Invoice March"},
            metadata={"grant_id": "grant-123", "has_attachment": True},
        )

        with patch.object(
            connector.service, "fetch_items", new_callable=AsyncMock, return_value=[expected_tx]
        ) as mock_fetch:
            txns = await connector.fetch_transactions_async(batch_size=5)

        assert txns == [expected_tx]
        mock_fetch.assert_awaited_once()
        call_kwargs = mock_fetch.call_args.kwargs
        assert call_kwargs["limit"] == 5
        assert call_kwargs["query"].subject == "Invoice"

    async def test_fetch_empty_returns_empty_list(self):
        config = NylasConsumerConfig()
        connector = _make_connector(consumer_config=config)

        with patch.object(connector.service, "fetch_items", new_callable=AsyncMock, return_value=[]):
            txns = await connector.fetch_transactions_async(batch_size=10)

        assert txns == []

    async def test_fetch_requires_consumer_config(self):
        connector = _make_connector()
        with pytest.raises(ValueError, match="consumer_config"):
            await connector.fetch_transactions_async()


class TestDispatchTransactions:
    async def test_dispatch_send_mode(self):
        consumer = NylasConsumerConfig()
        producer = NylasProducerConfig(send_mode="send")
        connector = _make_connector(consumer_config=consumer, producer_config=producer)

        payload = {
            "to": [{"email": "user@example.com"}],
            "subject": "Hello",
            "body": "World",
        }
        tx = Transaction(id="out-1", payload=payload)

        with patch.object(
            connector.service,
            "send_message",
            new_callable=AsyncMock,
            return_value={"id": "sent-99"},
        ) as mock_send:
            sent_ids = await connector.dispatch_transactions_async([tx])

        assert sent_ids == ["sent-99"]
        mock_send.assert_awaited_once_with(payload, default_from=None)


class TestAckTransaction:
    async def test_ack_marks_read(self):
        ack = AckActionConfig(mark_as_read=True, add_star=True)
        config = NylasConsumerConfig(ack_config=ack)
        connector = _make_connector(consumer_config=config)
        tx = Transaction(id="msg-1", payload={"id": "msg-1"})

        with patch.object(
            connector.service,
            "update_message",
            new_callable=AsyncMock,
            return_value={"id": "msg-1"},
        ) as mock_update:
            await connector.ack_transaction(tx)

        mock_update.assert_awaited_once_with("msg-1", {"unread": False, "starred": True})

    async def test_nack_is_noop(self):
        config = NylasConsumerConfig()
        connector = _make_connector(consumer_config=config)
        tx = Transaction(id="msg-1", payload={})
        await connector.nack_transaction(tx)
