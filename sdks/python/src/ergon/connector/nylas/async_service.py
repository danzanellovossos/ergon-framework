import asyncio
import logging
from typing import Any, Dict, List, Optional

from .models import MessageFields, MessageQueryFilter, NylasClient, SendMessagePayload
from .service import NylasService

logger = logging.getLogger(__name__)


class AsyncNylasService:
    def __init__(self, client: NylasClient) -> None:
        self._sync = NylasService(client)

    @property
    def grant_id(self) -> str:
        return self._sync.grant_id

    async def reset_pagination(self) -> None:
        self._sync.reset_pagination()

    async def close(self) -> None:
        await asyncio.to_thread(self._sync.close)

    async def list_messages(
        self,
        query: MessageQueryFilter,
        limit: int,
        page_token: Optional[str] = None,
        use_internal_pagination: bool = True,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self._sync.list_messages,
            query,
            limit,
            page_token,
            use_internal_pagination,
        )

    async def list_threads(
        self,
        query: MessageQueryFilter,
        limit: int,
        page_token: Optional[str] = None,
        use_internal_pagination: bool = True,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self._sync.list_threads,
            query,
            limit,
            page_token,
            use_internal_pagination,
        )

    async def find_message(
        self,
        message_id: str,
        fields: Optional[MessageFields] = None,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.find_message, message_id, fields)

    async def get_messages_count(self, query: MessageQueryFilter, max_pages: Optional[int] = None) -> int:
        return await asyncio.to_thread(self._sync.get_messages_count, query, max_pages)

    async def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        return await asyncio.to_thread(self._sync.download_attachment, message_id, attachment_id)

    async def get_attachment_metadata(self, message_id: str, attachment_id: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.get_attachment_metadata, message_id, attachment_id)

    async def send_message(self, payload: SendMessagePayload, default_from: Optional[Any] = None) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.send_message, payload, default_from)

    async def create_draft(self, payload: SendMessagePayload, default_from: Optional[Any] = None) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.create_draft, payload, default_from)

    async def send_draft(self, draft_id: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.send_draft, draft_id)

    async def update_message(self, message_id: str, request_body: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.update_message, message_id, request_body)

    async def list_folders(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._sync.list_folders)

    async def create_folder(self, name: str, parent: Optional[str] = None) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.create_folder, name, parent)

    async def update_folder(self, folder_id: str, request_body: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.update_folder, folder_id, request_body)
