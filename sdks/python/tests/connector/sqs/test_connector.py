"""Tests for ergon.connector.sqs.connector — SQSConnector."""

import json
from unittest.mock import MagicMock, patch

import pytest

from ergon.connector import Transaction
from ergon.connector.sqs.connector import SQSConnector
from ergon.connector.sqs.models import SQSClient


def _make_client(**overrides) -> SQSClient:
    defaults = {
        "region_name": "us-east-1",
        "queue_url": "https://sqs.us-east-1.amazonaws.com/123456789/test-queue",
    }
    defaults.update(overrides)
    return SQSClient(**defaults)


@pytest.fixture
def mock_service():
    with patch("ergon.connector.sqs.connector.SQSService") as cls:
        yield cls.return_value


# =====================================================================
#   fetch_transactions
# =====================================================================


class TestFetchTransactions:
    def test_returns_empty_when_no_messages(self, mock_service):
        mock_service.receive.return_value = []

        conn = SQSConnector(client=_make_client())
        result = conn.fetch_transactions(batch_size=5)

        assert result == []
        mock_service.receive.assert_called_once_with(
            queue_url=None,
            max_messages=5,
        )

    def test_returns_transactions_from_messages(self, mock_service):
        mock_service.receive.return_value = [
            {
                "data": {"foo": "bar"},
                "receipt_handle": "rh-1",
                "message_id": "msg-1",
                "attributes": {},
                "message_attributes": {},
            },
            {
                "data": "plain",
                "receipt_handle": "rh-2",
                "message_id": "msg-2",
                "attributes": {},
                "message_attributes": {},
            },
        ]

        conn = SQSConnector(client=_make_client())
        txs = conn.fetch_transactions(batch_size=10)

        assert len(txs) == 2
        assert txs[0].id == "msg-1"
        assert txs[0].payload["data"] == {"foo": "bar"}
        assert txs[0].payload["receipt_handle"] == "rh-1"
        assert txs[1].id == "msg-2"

    def test_passes_custom_queue_url(self, mock_service):
        mock_service.receive.return_value = []

        conn = SQSConnector(client=_make_client())
        conn.fetch_transactions(queue_url="https://other-queue")

        call_kwargs = mock_service.receive.call_args[1]
        assert call_kwargs["queue_url"] == "https://other-queue"

    def test_attaches_metadata(self, mock_service):
        mock_service.receive.return_value = [
            {
                "data": {},
                "receipt_handle": "rh-1",
                "message_id": "msg-1",
                "attributes": {},
                "message_attributes": {},
            },
        ]

        conn = SQSConnector(client=_make_client())
        txs = conn.fetch_transactions(metadata={"source": "test"})

        assert txs[0].metadata == {"source": "test"}


# =====================================================================
#   dispatch_transactions
# =====================================================================


class _FakeWrapper:
    """Simulates the RabbitMQMessageWrapper-style payload with .payload attr."""

    def __init__(self, id, payload):
        self.id = id
        self.payload = payload


class TestDispatchTransactions:
    def test_sends_from_wrapper_payload(self, mock_service):
        wrapper = _FakeWrapper(
            id="tx-1",
            payload={
                "queue_name": "my-queue",
                "body": {"msg": "hello"},
            },
        )
        tx = Transaction(id="tx-1", payload=wrapper)

        conn = SQSConnector(client=_make_client())
        conn.dispatch_transactions([tx])

        mock_service.send.assert_called_once()
        call_kwargs = mock_service.send.call_args[1]
        assert call_kwargs["queue_url"] == "my-queue"
        assert json.loads(call_kwargs["body"]) == {"msg": "hello"}

    def test_sends_from_plain_dict_payload(self, mock_service):
        tx = Transaction(
            id="tx-2",
            payload={
                "queue_url": "https://q",
                "body": "raw-string",
            },
        )

        conn = SQSConnector(client=_make_client())
        conn.dispatch_transactions([tx])

        call_kwargs = mock_service.send.call_args[1]
        assert call_kwargs["queue_url"] == "https://q"
        assert call_kwargs["body"] == "raw-string"

    def test_sends_fifo_params(self, mock_service):
        wrapper = _FakeWrapper(
            id="tx-3",
            payload={
                "body": "fifo-msg",
                "message_group_id": "grp",
                "message_deduplication_id": "dup",
            },
        )
        tx = Transaction(id="tx-3", payload=wrapper)

        conn = SQSConnector(client=_make_client())
        conn.dispatch_transactions([tx])

        call_kwargs = mock_service.send.call_args[1]
        assert call_kwargs["message_group_id"] == "grp"
        assert call_kwargs["message_deduplication_id"] == "dup"

    def test_sends_multiple_transactions(self, mock_service):
        txs = [
            Transaction(id=f"tx-{i}", payload={"body": f"msg-{i}"})
            for i in range(3)
        ]

        conn = SQSConnector(client=_make_client())
        conn.dispatch_transactions(txs)

        assert mock_service.send.call_count == 3

    def test_handles_string_payload(self, mock_service):
        tx = Transaction(id="tx-str", payload="just a string")

        conn = SQSConnector(client=_make_client())
        conn.dispatch_transactions([tx])

        call_kwargs = mock_service.send.call_args[1]
        assert call_kwargs["body"] == "just a string"


# =====================================================================
#   delete_message
# =====================================================================


class TestDeleteMessage:
    def test_delegates_to_service(self, mock_service):
        conn = SQSConnector(client=_make_client())
        conn.delete_message(receipt_handle="rh-42")

        mock_service.delete.assert_called_once_with(
            receipt_handle="rh-42",
            queue_url=None,
        )

    def test_passes_custom_queue_url(self, mock_service):
        conn = SQSConnector(client=_make_client())
        conn.delete_message(receipt_handle="rh-1", queue_url="https://q")

        mock_service.delete.assert_called_once_with(
            receipt_handle="rh-1",
            queue_url="https://q",
        )
