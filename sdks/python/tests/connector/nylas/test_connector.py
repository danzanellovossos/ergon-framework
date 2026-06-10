"""Tests for NylasConnector — fetch/dispatch mapping to Transaction, ack."""

from unittest.mock import MagicMock, patch

import pytest

from ergon.connector.nylas.connector import NylasConnector
from ergon.connector.nylas.models import (
    AckActionConfig,
    NylasClient,
    NylasConsumerConfig,
    NylasProducerConfig,
)
from ergon.connector.transaction import Transaction


def _make_client() -> NylasClient:
    return NylasClient(api_key="test-key", grant_id="grant-123")


def _make_connector(consumer_config=None, producer_config=None) -> NylasConnector:
    with patch("ergon.connector.nylas.service._get_nylas_client"):
        return NylasConnector(
            client=_make_client(),
            consumer_config=consumer_config,
            producer_config=producer_config,
        )


class TestFetchTransactions:
    def test_fetch_maps_to_transactions(self):
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

        with patch.object(connector.service, "list_messages", return_value=service_result) as mock_list:
            txns = connector.fetch_transactions(batch_size=5)

        assert len(txns) == 1
        tx = txns[0]
        assert tx.id == "msg-1"
        assert tx.payload["subject"] == "Invoice March"
        assert tx.metadata["grant_id"] == "grant-123"
        assert tx.metadata["thread_id"] == "thread-1"
        assert tx.metadata["has_attachment"] is True
        assert tx.metadata["unread"] is True

        call_args = mock_list.call_args
        query = call_args.args[0]
        assert query.subject == "Invoice"
        assert query.has_attachment is True

    def test_fetch_empty_returns_empty_list(self):
        config = NylasConsumerConfig()
        connector = _make_connector(consumer_config=config)

        with patch.object(connector.service, "list_messages", return_value={"data": [], "next_cursor": None}):
            txns = connector.fetch_transactions(batch_size=10)

        assert txns == []

    def test_fetch_requires_consumer_config(self):
        connector = _make_connector()
        with pytest.raises(ValueError, match="consumer_config"):
            connector.fetch_transactions()


class TestDispatchTransactions:
    def test_dispatch_send_mode(self):
        consumer = NylasConsumerConfig()
        producer = NylasProducerConfig(send_mode="send")
        connector = _make_connector(consumer_config=consumer, producer_config=producer)

        payload = {
            "to": [{"email": "user@example.com"}],
            "subject": "Hello",
            "body": "World",
        }
        tx = Transaction(id="out-1", payload=payload)

        with patch.object(connector.service, "send_message", return_value={"id": "sent-99"}) as mock_send:
            sent_ids = connector.dispatch_transactions([tx])

        assert sent_ids == ["sent-99"]
        mock_send.assert_called_once_with(payload, default_from=None)

    def test_dispatch_draft_mode(self):
        consumer = NylasConsumerConfig()
        producer = NylasProducerConfig(send_mode="draft")
        connector = _make_connector(consumer_config=consumer, producer_config=producer)

        payload = {"to": [{"email": "user@example.com"}], "subject": "Draft", "body": "Body"}
        tx = Transaction(id="out-1", payload=payload)

        with (
            patch.object(connector.service, "create_draft", return_value={"id": "draft-1"}) as mock_create,
            patch.object(connector.service, "send_draft", return_value={"id": "sent-draft-1"}) as mock_send_draft,
        ):
            sent_ids = connector.dispatch_transactions([tx])

        assert sent_ids == ["sent-draft-1"]
        mock_create.assert_called_once()
        mock_send_draft.assert_called_once_with("draft-1")


class TestAckTransaction:
    def test_ack_marks_read_and_moves_folder(self):
        ack = AckActionConfig(mark_as_read=True, move_to_folder_id="processed-folder")
        config = NylasConsumerConfig(ack_config=ack)
        connector = _make_connector(consumer_config=config)

        tx = Transaction(id="msg-1", payload={"id": "msg-1"})

        with patch.object(connector.service, "update_message", return_value={"id": "msg-1"}) as mock_update:
            connector.ack_transaction(tx)

        mock_update.assert_called_once_with(
            "msg-1",
            {"unread": False, "folders": ["processed-folder"]},
        )

    def test_ack_noop_without_config(self):
        config = NylasConsumerConfig()
        connector = _make_connector(consumer_config=config)
        tx = Transaction(id="msg-1", payload={})

        with patch.object(connector.service, "update_message") as mock_update:
            connector.ack_transaction(tx)

        mock_update.assert_not_called()


class TestFetchById:
    def test_fetch_transaction_by_id(self):
        config = NylasConsumerConfig()
        connector = _make_connector(consumer_config=config)

        with patch.object(
            connector.service,
            "find_message",
            return_value={"id": "msg-42", "subject": "Test"},
        ):
            tx = connector.fetch_transaction_by_id("msg-42")

        assert tx.id == "msg-42"
        assert tx.payload["subject"] == "Test"
