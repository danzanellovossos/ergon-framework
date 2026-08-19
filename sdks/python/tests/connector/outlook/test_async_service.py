from unittest.mock import MagicMock

from ergon.connector.outlook.async_service import AsyncOutlookGraphService
from ergon.connector.outlook.models import (
    OutlookEmailAddress,
    OutlookFlagStatus,
    OutlookGraphClient,
    OutlookWellKnownFolder,
)


def _service() -> AsyncOutlookGraphService:
    return AsyncOutlookGraphService(
        OutlookGraphClient(
            tenant_id="tenant",
            client_id="client",
            client_secret="secret",
            user_email="mailbox@example.com",
        )
    )


async def test_async_message_actions_delegate_to_sync_service() -> None:
    service = _service()
    service._sync.reply = MagicMock()
    service._sync.reply_all = MagicMock()
    service._sync.forward = MagicMock()

    recipient = OutlookEmailAddress(email="recipient@example.com")
    await service.reply("m1", comment="Reply")
    await service.reply_all("m1", comment="Reply all")
    await service.forward("m1", [recipient], comment="Forward")

    service._sync.reply.assert_called_once_with("m1", comment="Reply")
    service._sync.reply_all.assert_called_once_with("m1", comment="Reply all")
    service._sync.forward.assert_called_once_with("m1", [recipient], comment="Forward")


async def test_async_mail_read_write_actions_delegate() -> None:
    service = _service()
    service._sync.mark_as_read = MagicMock(return_value={"isRead": True})
    service._sync.mark_as_unread = MagicMock(return_value={"isRead": False})
    service._sync.set_flag = MagicMock(return_value={})
    service._sync.set_categories = MagicMock(return_value={})
    service._sync.move_message = MagicMock(return_value={"id": "moved"})
    service._sync.delete_message = MagicMock()

    assert await service.mark_as_read("m1") == {"isRead": True}
    assert await service.mark_as_unread("m1") == {"isRead": False}
    await service.set_flag("m1", OutlookFlagStatus.FLAGGED)
    await service.set_categories("m1", ["Finance"])
    assert await service.move_message("m1", OutlookWellKnownFolder.DELETED_ITEMS) == {"id": "moved"}
    await service.delete_message("m1")

    service._sync.set_flag.assert_called_once_with("m1", OutlookFlagStatus.FLAGGED)
    service._sync.set_categories.assert_called_once_with("m1", ["Finance"])
    service._sync.move_message.assert_called_once_with("m1", OutlookWellKnownFolder.DELETED_ITEMS)
    service._sync.delete_message.assert_called_once_with("m1")


async def test_async_folder_helpers_delegate() -> None:
    service = _service()
    service._sync.get_mail_folder = MagicMock(return_value={"id": "sent"})
    service._sync.list_mail_folders = MagicMock(return_value=[{"id": "inbox"}])

    assert await service.get_mail_folder(OutlookWellKnownFolder.SENT_ITEMS) == {"id": "sent"}
    assert await service.list_mail_folders() == [{"id": "inbox"}]

    service._sync.get_mail_folder.assert_called_once_with(OutlookWellKnownFolder.SENT_ITEMS)
    service._sync.list_mail_folders.assert_called_once_with(include_hidden=False)
