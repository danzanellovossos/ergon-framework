import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..connector import AsyncConnector
from ..transaction import Transaction
from .async_service import AsyncOutlookGraphService
from .models import (
    OutlookAckActionConfig,
    OutlookConsumerConfig,
    OutlookEmailAddress,
    OutlookFlagStatus,
    OutlookGraphClient,
    OutlookMessageQuery,
    OutlookNackActionConfig,
    OutlookProducerConfig,
    OutlookSendMessagePayload,
    OutlookWellKnownFolder,
)
from .utils import build_ack_patch, build_nack_patch, merge_query

logger = logging.getLogger(__name__)


class AsyncOutlookGraphConnector(AsyncConnector):
    service: AsyncOutlookGraphService

    def __init__(
        self,
        client: OutlookGraphClient,
        consumer_config: Optional[OutlookConsumerConfig] = None,
        producer_config: Optional[OutlookProducerConfig] = None,
    ) -> None:
        self.service = AsyncOutlookGraphService(client)
        self._consumer_config = consumer_config
        self._producer_config = producer_config or OutlookProducerConfig()

    async def fetch_transactions_async(
        self,
        batch_size: Optional[int] = None,
        query_overrides: Optional[OutlookMessageQuery] = None,
        *args: Any,
        **kwargs: Any,
    ) -> List[Transaction]:
        if self._consumer_config is None:
            raise ValueError("AsyncOutlookGraphConnector requires a consumer_config to fetch transactions")
        config = self._consumer_config
        return await self.service.fetch_items(
            merge_query(config, query_overrides, **kwargs),
            batch_size or config.batch_size,
            download_attachments=config.download_attachments,
        )

    async def dispatch_transactions_async(
        self,
        transactions: List[Transaction],
        *args: Any,
        **kwargs: Any,
    ) -> List[str]:
        dispatched_ids: List[str] = []
        for transaction in transactions:
            await self.service.send_message(
                transaction.payload,
                producer_config=self._producer_config,
            )
            dispatched_ids.append(transaction.id)
        return dispatched_ids

    async def send_message(self, payload: OutlookSendMessagePayload) -> None:
        await self.service.send_message(payload, producer_config=self._producer_config)

    async def mark_as_read(self, message_id: str) -> Dict[str, Any]:
        return await self.service.mark_as_read(message_id)

    async def mark_as_unread(self, message_id: str) -> Dict[str, Any]:
        return await self.service.mark_as_unread(message_id)

    async def set_flag(
        self,
        message_id: str,
        status: OutlookFlagStatus | str = OutlookFlagStatus.FLAGGED,
    ) -> Dict[str, Any]:
        return await self.service.set_flag(message_id, status)

    async def set_categories(self, message_id: str, categories: List[str]) -> Dict[str, Any]:
        return await self.service.set_categories(message_id, categories)

    async def move_message(
        self,
        message_id: str,
        folder_id: OutlookWellKnownFolder | str,
    ) -> Dict[str, Any]:
        return await self.service.move_message(message_id, folder_id)

    async def delete_message(self, message_id: str) -> None:
        await self.service.delete_message(message_id)

    async def reply(self, message_id: str, *, comment: str = "") -> None:
        await self.service.reply(message_id, comment=comment)

    async def reply_all(self, message_id: str, *, comment: str = "") -> None:
        await self.service.reply_all(message_id, comment=comment)

    async def forward(
        self,
        message_id: str,
        to: List[OutlookEmailAddress],
        comment: str = "",
    ) -> None:
        await self.service.forward(message_id, to, comment=comment)

    async def list_attachments(
        self,
        message_id: str,
        download_content: bool = False,
    ) -> List[Dict[str, Any]]:
        return await self.service.list_attachments(message_id, download_content=download_content)

    async def save_attachment(
        self,
        attachment: Dict[str, Any],
        destination: str | Path,
        overwrite: bool = False,
    ) -> Path:
        return await self.service.save_attachment(attachment, destination, overwrite=overwrite)

    async def list_mail_folders(self, include_hidden: bool = False) -> List[Dict[str, Any]]:
        return await self.service.list_mail_folders(include_hidden=include_hidden)

    async def get_mail_folder(self, folder_id: OutlookWellKnownFolder | str) -> Dict[str, Any]:
        return await self.service.get_mail_folder(folder_id)

    async def reset_pagination(self) -> None:
        await self.service.reset_pagination()

    async def fetch_transaction_by_id_async(
        self,
        transaction_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> Transaction:
        config = self._consumer_config
        return await self.service.find_message_transaction(
            transaction_id,
            select=kwargs.get("select"),
            download_attachments=config.download_attachments if config else False,
        )

    async def get_transactions_count_async(self, *args: Any, **kwargs: Any) -> int:
        if self._consumer_config is None:
            raise ValueError("AsyncOutlookGraphConnector requires a consumer_config to count transactions")
        query_overrides = kwargs.pop("query_overrides", None)
        max_pages = kwargs.pop("max_pages", None)
        query = merge_query(self._consumer_config, query_overrides, **kwargs)
        return await self.service.get_messages_count(query, max_pages=max_pages)

    async def ack_transaction(
        self,
        transaction: Transaction,
        ack_config: Optional[OutlookAckActionConfig] = None,
    ) -> None:
        config = ack_config or (self._consumer_config.ack_config if self._consumer_config else None)
        if config is None:
            return
        if config.delete:
            await self.service.delete_message(transaction.id)
            return
        patch = build_ack_patch(config)
        if patch:
            await self.service.update_message(transaction.id, patch)
        if config.move_to_folder_id:
            await self.service.move_message(transaction.id, config.move_to_folder_id)

    async def nack_transaction(
        self,
        transaction: Transaction,
        requeue: bool = True,
        nack_config: Optional[OutlookNackActionConfig] = None,
    ) -> None:
        config = nack_config or (self._consumer_config.nack_config if self._consumer_config else None)
        if config is None:
            if not requeue:
                raise ValueError("nack_config with a failure category or folder is required when requeue=False")
            config = OutlookNackActionConfig()
        if not requeue and not config.move_to_folder_id and not config.categories:
            raise ValueError("nack_config with a failure category or folder is required when requeue=False")

        patch = build_nack_patch(config)
        if patch:
            await self.service.update_message(transaction.id, patch)
        if config.move_to_folder_id:
            await self.service.move_message(transaction.id, config.move_to_folder_id)
        if requeue:
            await self.service.reset_pagination()
            logger.debug("Outlook message %s was made available for refetch", transaction.id)

    async def close(self) -> None:
        await self.service.close()
