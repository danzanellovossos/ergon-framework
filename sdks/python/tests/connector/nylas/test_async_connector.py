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
    async def test_fetch_maps_to_transactions(self):
        config = NylasConsumerConfig(subject="Invoice", has_attachment=True, batch_size=5)
        connector = _make_connector(consumer_config=config)

        service_result = {
            "data": [
                {
                    "id": "msg-1",
                    "subject": "Invoice March",
                    "thread_id": "thread-1",
                    "folders": ["inbox-id"],
                    "unread": True,
                    "attachments": [{"id": "att-1", "filename": "invoice.pdf"}],
                }
            ],
            "next_cursor": "cursor-abc",
        }

        with patch.object(connector.service, "list_messages", new_callable=AsyncMock, return_value=service_result):
            txns = await connector.fetch_transactions_async(batch_size=5)

        assert len(txns) == 1
        tx = txns[0]
        assert tx.id == "msg-1"
        assert tx.metadata["grant_id"] == "grant-123"
        assert tx.metadata["has_attachment"] is True

    async def test_fetch_empty_returns_empty_list(self):
        config = NylasConsumerConfig()
        connector = _make_connector(consumer_config=config)

        with patch.object(
            connector.service,
            "list_messages",
            new_callable=AsyncMock,
            return_value={"data": [], "next_cursor": None},
        ):
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
