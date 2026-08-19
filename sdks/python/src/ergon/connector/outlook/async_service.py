import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..transaction import Transaction
from .models import (
    OutlookEmailAddress,
    OutlookFlagStatus,
    OutlookGraphClient,
    OutlookMessageQuery,
    OutlookProducerConfig,
    OutlookSendMessagePayload,
    OutlookWellKnownFolder,
)
from .service import OutlookGraphService


class AsyncOutlookGraphService:
    """Async facade over the blocking MSAL and requests implementations."""

    def __init__(self, client: OutlookGraphClient) -> None:
        self._sync = OutlookGraphService(client)

    @property
    def user_email(self) -> str:
        return self._sync.user_email

    async def close(self) -> None:
        await asyncio.to_thread(self._sync.close)

    async def reset_pagination(self) -> None:
        self._sync.reset_pagination()

    async def list_messages(
        self,
        query: OutlookMessageQuery,
        limit: int,
        next_link: Optional[str] = None,
        use_internal_pagination: bool = True,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self._sync.list_messages,
            query,
            limit,
            next_link=next_link,
            use_internal_pagination=use_internal_pagination,
        )

    async def find_message(self, message_id: str, *, select: Optional[List[str]] = None) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.find_message, message_id, select=select)

    async def list_attachments(
        self,
        message_id: str,
        download_content: bool = False,
    ) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(
            self._sync.list_attachments,
            message_id,
            download_content=download_content,
        )

    async def save_attachment(
        self,
        attachment: Dict[str, Any],
        destination: str | Path,
        overwrite: bool = False,
    ) -> Path:
        return await asyncio.to_thread(
            self._sync.save_attachment,
            attachment,
            destination,
            overwrite=overwrite,
        )

    async def fetch_items(
        self,
        query: OutlookMessageQuery,
        limit: int,
        download_attachments: bool = False,
    ) -> List[Transaction]:
        return await asyncio.to_thread(
            self._sync.fetch_items,
            query,
            limit,
            download_attachments=download_attachments,
        )

    async def find_message_transaction(
        self,
        message_id: str,
        select: Optional[List[str]] = None,
        download_attachments: bool = False,
    ) -> Transaction:
        return await asyncio.to_thread(
            self._sync.find_message_transaction,
            message_id,
            select=select,
            download_attachments=download_attachments,
        )

    async def get_messages_count(
        self,
        query: OutlookMessageQuery,
        max_pages: Optional[int] = None,
    ) -> int:
        return await asyncio.to_thread(self._sync.get_messages_count, query, max_pages=max_pages)

    async def send_message(
        self,
        payload: OutlookSendMessagePayload,
        producer_config: Optional[OutlookProducerConfig] = None,
    ) -> None:
        await asyncio.to_thread(
            self._sync.send_message,
            payload,
            producer_config=producer_config,
        )

    async def update_message(self, message_id: str, request_body: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.update_message, message_id, request_body)

    async def mark_as_read(self, message_id: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.mark_as_read, message_id)

    async def mark_as_unread(self, message_id: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.mark_as_unread, message_id)

    async def set_flag(
        self,
        message_id: str,
        status: OutlookFlagStatus | str = OutlookFlagStatus.FLAGGED,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.set_flag, message_id, status)

    async def set_categories(self, message_id: str, categories: List[str]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.set_categories, message_id, categories)

    async def move_message(
        self,
        message_id: str,
        folder_id: OutlookWellKnownFolder | str,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.move_message, message_id, folder_id)

    async def delete_message(self, message_id: str) -> None:
        await asyncio.to_thread(self._sync.delete_message, message_id)

    async def reply(self, message_id: str, *, comment: str = "") -> None:
        await asyncio.to_thread(self._sync.reply, message_id, comment=comment)

    async def reply_all(self, message_id: str, *, comment: str = "") -> None:
        await asyncio.to_thread(self._sync.reply_all, message_id, comment=comment)

    async def forward(
        self,
        message_id: str,
        to: List[OutlookEmailAddress],
        *,
        comment: str = "",
    ) -> None:
        await asyncio.to_thread(self._sync.forward, message_id, to, comment=comment)

    async def list_mail_folders(self, *, include_hidden: bool = False) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._sync.list_mail_folders, include_hidden=include_hidden)

    async def get_mail_folder(self, folder_id: OutlookWellKnownFolder | str) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.get_mail_folder, folder_id)
