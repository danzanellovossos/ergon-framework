from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ergon.connector.outlook.async_connector import AsyncOutlookGraphConnector
from ergon.connector.outlook.connector import OutlookGraphConnector
from ergon.connector.outlook.models import (
    OutlookAckActionConfig,
    OutlookConsumerConfig,
    OutlookEmailAddress,
    OutlookGraphClient,
    OutlookNackActionConfig,
    OutlookSendMessageInput,
)
from ergon.connector.transaction import Transaction


def _client() -> OutlookGraphClient:
    return OutlookGraphClient(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
        user_email="mailbox@example.com",
    )


def test_fetch_transactions_delegates_query_and_attachment_config() -> None:
    config = OutlookConsumerConfig(search="invoice", batch_size=5, download_attachments=True)
    connector = OutlookGraphConnector(_client(), consumer_config=config)
    expected = Transaction(id="m1", payload={"id": "m1"})

    with patch.object(connector.service, "fetch_items", return_value=[expected]) as fetch:
        transactions = connector.fetch_transactions()

    assert transactions == [expected]
    assert fetch.call_args.args[1] == 5
    assert fetch.call_args.args[0].search == "invoice"
    assert fetch.call_args.kwargs["download_attachments"] is True


def test_ack_does_not_mutate_mailbox() -> None:
    connector = OutlookGraphConnector(_client(), consumer_config=OutlookConsumerConfig())
    transaction = Transaction(id="m1", payload={})

    with patch.object(connector.service, "reset_pagination") as reset:
        connector.ack_transaction(transaction)

    reset.assert_not_called()


def test_ack_applies_read_move_or_delete_actions() -> None:
    config = OutlookConsumerConfig(ack_config=OutlookAckActionConfig(mark_as_read=True, move_to_folder_id="processed"))
    connector = OutlookGraphConnector(_client(), consumer_config=config)
    transaction = Transaction(id="m1", payload={})

    with (
        patch.object(connector.service, "update_message") as update,
        patch.object(connector.service, "move_message") as move,
        patch.object(connector.service, "delete_message") as delete,
    ):
        connector.ack_transaction(transaction)
        connector.ack_transaction(transaction, OutlookAckActionConfig(delete=True))

    update.assert_called_once_with("m1", {"isRead": True})
    move.assert_called_once_with("m1", "processed")
    delete.assert_called_once_with("m1")


def test_fetch_requires_consumer_config() -> None:
    connector = OutlookGraphConnector(_client())

    with pytest.raises(ValueError, match="consumer_config"):
        connector.fetch_transactions()


def test_connector_exposes_read_and_send_operations() -> None:
    connector = OutlookGraphConnector(_client())
    connector.service.send_message = MagicMock()
    connector.service.reply = MagicMock()
    connector.service.forward = MagicMock()
    connector.service.mark_as_read = MagicMock(return_value={"isRead": True})
    connector.service.move_message = MagicMock(return_value={"id": "moved"})
    connector.service.delete_message = MagicMock()
    connector.service.list_mail_folders = MagicMock(return_value=[{"id": "inbox"}])
    payload = OutlookSendMessageInput(
        to=[OutlookEmailAddress(email="recipient@example.com")],
        subject="Hello",
        body="World",
    )

    connector.send_message(payload)
    connector.reply("m1", comment="Reply")
    connector.forward("m1", payload.to, comment="Forward")
    assert connector.mark_as_read("m1") == {"isRead": True}
    assert connector.move_message("m1", "processed") == {"id": "moved"}
    connector.delete_message("m1")

    assert connector.list_mail_folders() == [{"id": "inbox"}]
    connector.service.send_message.assert_called_once_with(payload, producer_config=connector._producer_config)
    connector.service.reply.assert_called_once_with("m1", comment="Reply")
    connector.service.forward.assert_called_once_with("m1", payload.to, comment="Forward")
    connector.service.mark_as_read.assert_called_once_with("m1")
    connector.service.move_message.assert_called_once_with("m1", "processed")
    connector.service.delete_message.assert_called_once_with("m1")


def test_nack_requeue_marks_unread_and_resets_pagination() -> None:
    connector = OutlookGraphConnector(_client())
    transaction = Transaction(id="m1", payload={})

    with (
        patch.object(connector.service, "update_message") as update,
        patch.object(connector.service, "reset_pagination") as reset,
    ):
        connector.nack_transaction(transaction)

    update.assert_called_once_with("m1", {"isRead": False})
    reset.assert_called_once_with()


def test_nack_without_requeue_applies_failure_actions() -> None:
    config = OutlookConsumerConfig(
        nack_config=OutlookNackActionConfig(
            mark_as_unread=False,
            move_to_folder_id="failed",
            categories=["Processing failed"],
        )
    )
    connector = OutlookGraphConnector(_client(), consumer_config=config)

    with (
        patch.object(connector.service, "update_message") as update,
        patch.object(connector.service, "move_message") as move,
        patch.object(connector.service, "reset_pagination") as reset,
    ):
        connector.nack_transaction(Transaction(id="m1", payload={}), requeue=False)

    update.assert_called_once_with("m1", {"categories": ["Processing failed"]})
    move.assert_called_once_with("m1", "failed")
    reset.assert_not_called()


def test_nack_without_requeue_requires_failure_action() -> None:
    connector = OutlookGraphConnector(_client())

    with pytest.raises(ValueError, match="nack_config"):
        connector.nack_transaction(Transaction(id="m1", payload={}), requeue=False)


async def test_async_connector_delegates_to_async_service() -> None:
    config = OutlookConsumerConfig(batch_size=3)
    connector = AsyncOutlookGraphConnector(_client(), consumer_config=config)
    expected = Transaction(id="m1", payload={"id": "m1"})
    connector.service.fetch_items = AsyncMock(return_value=[expected])

    transactions = await connector.fetch_transactions_async()

    assert transactions == [expected]
    connector.service.fetch_items.assert_awaited_once()


async def test_async_ack_delete_skips_other_actions() -> None:
    connector = AsyncOutlookGraphConnector(_client())
    connector.service.update_message = AsyncMock()
    connector.service.delete_message = AsyncMock()

    await connector.ack_transaction(
        Transaction(id="m1", payload={}),
        OutlookAckActionConfig(delete=True),
    )

    connector.service.update_message.assert_not_awaited()
    connector.service.delete_message.assert_awaited_once_with("m1")


async def test_async_connector_exposes_facade_and_nack() -> None:
    connector = AsyncOutlookGraphConnector(_client())
    connector.service.reply_all = AsyncMock()
    connector.service.update_message = AsyncMock()
    connector.service.reset_pagination = AsyncMock()

    await connector.reply_all("m1", comment="Reply")
    await connector.ack_transaction(Transaction(id="m1", payload={}))
    await connector.nack_transaction(Transaction(id="m1", payload={}))

    connector.service.reply_all.assert_awaited_once_with("m1", comment="Reply")
    connector.service.update_message.assert_awaited_once_with("m1", {"isRead": False})
    connector.service.reset_pagination.assert_awaited_once_with()
