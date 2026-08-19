import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..connector import Connector
from ..transaction import Transaction
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
from .service import OutlookGraphService
from .utils import build_ack_patch, build_nack_patch, merge_query

logger = logging.getLogger(__name__)


class OutlookGraphConnector(Connector):
    service: OutlookGraphService

    def __init__(
        self,
        client: OutlookGraphClient,
        consumer_config: Optional[OutlookConsumerConfig] = None,
        producer_config: Optional[OutlookProducerConfig] = None,
    ) -> None:
        self.service = OutlookGraphService(client)
        self._consumer_config = consumer_config
        self._producer_config = producer_config or OutlookProducerConfig()

    def fetch_transactions(
        self,
        batch_size: Optional[int] = None,
        query_overrides: Optional[OutlookMessageQuery] = None,
        *args: Any,
        **kwargs: Any,
    ) -> List[Transaction]:
        if self._consumer_config is None:
            raise ValueError("OutlookGraphConnector requires a consumer_config to fetch transactions")
        config = self._consumer_config
        return self.service.fetch_items(
            merge_query(config, query_overrides, **kwargs),
            batch_size or config.batch_size,
            download_attachments=config.download_attachments,
        )

    def dispatch_transactions(
        self,
        transactions: List[Transaction],
        *args: Any,
        **kwargs: Any,
    ) -> List[str]:
        dispatched_ids: List[str] = []
        for transaction in transactions:
            self.service.send_message(
                transaction.payload,
                producer_config=self._producer_config,
            )
            dispatched_ids.append(transaction.id)
        return dispatched_ids

    def send_message(self, payload: OutlookSendMessagePayload) -> None:
        self.service.send_message(payload, producer_config=self._producer_config)

    def mark_as_read(self, message_id: str) -> Dict[str, Any]:
        return self.service.mark_as_read(message_id)

    def mark_as_unread(self, message_id: str) -> Dict[str, Any]:
        return self.service.mark_as_unread(message_id)

    def set_flag(
        self,
        message_id: str,
        status: OutlookFlagStatus | str = OutlookFlagStatus.FLAGGED,
    ) -> Dict[str, Any]:
        return self.service.set_flag(message_id, status)

    def set_categories(self, message_id: str, categories: List[str]) -> Dict[str, Any]:
        return self.service.set_categories(message_id, categories)

    def move_message(
        self,
        message_id: str,
        folder_id: OutlookWellKnownFolder | str,
    ) -> Dict[str, Any]:
        return self.service.move_message(message_id, folder_id)

    def delete_message(self, message_id: str) -> None:
        self.service.delete_message(message_id)

    def reply(self, message_id: str, *, comment: str = "") -> None:
        self.service.reply(message_id, comment=comment)

    def reply_all(self, message_id: str, *, comment: str = "") -> None:
        self.service.reply_all(message_id, comment=comment)

    def forward(
        self,
        message_id: str,
        to: List[OutlookEmailAddress],
        comment: str = "",
    ) -> None:
        self.service.forward(message_id, to, comment=comment)

    def list_attachments(
        self,
        message_id: str,
        download_content: bool = False,
    ) -> List[Dict[str, Any]]:
        return self.service.list_attachments(message_id, download_content=download_content)

    def save_attachment(
        self,
        attachment: Dict[str, Any],
        destination: str | Path,
        overwrite: bool = False,
    ) -> Path:
        return self.service.save_attachment(attachment, destination, overwrite=overwrite)

    def list_mail_folders(self, *, include_hidden: bool = False) -> List[Dict[str, Any]]:
        return self.service.list_mail_folders(include_hidden=include_hidden)

    def get_mail_folder(self, folder_id: OutlookWellKnownFolder | str) -> Dict[str, Any]:
        return self.service.get_mail_folder(folder_id)

    def reset_pagination(self) -> None:
        self.service.reset_pagination()

    def fetch_transaction_by_id(
        self,
        transaction_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> Transaction:
        config = self._consumer_config
        return self.service.find_message_transaction(
            transaction_id,
            select=kwargs.get("select"),
            download_attachments=config.download_attachments if config else False,
        )

    def get_transactions_count(self, *args: Any, **kwargs: Any) -> int:
        if self._consumer_config is None:
            raise ValueError("OutlookGraphConnector requires a consumer_config to count transactions")
        query_overrides = kwargs.pop("query_overrides", None)
        max_pages = kwargs.pop("max_pages", None)
        query = merge_query(self._consumer_config, query_overrides, **kwargs)
        return self.service.get_messages_count(query, max_pages=max_pages)

    def ack_transaction(
        self,
        transaction: Transaction,
        ack_config: Optional[OutlookAckActionConfig] = None,
    ) -> None:
        config = ack_config or (self._consumer_config.ack_config if self._consumer_config else None)
        if config is None:
            return
        if config.delete:
            self.service.delete_message(transaction.id)
            return
        patch = build_ack_patch(config)
        if patch:
            self.service.update_message(transaction.id, patch)
        if config.move_to_folder_id:
            self.service.move_message(transaction.id, config.move_to_folder_id)

    def nack_transaction(
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
            self.service.update_message(transaction.id, patch)
        if config.move_to_folder_id:
            self.service.move_message(transaction.id, config.move_to_folder_id)
        if requeue:
            self.service.reset_pagination()
            logger.debug("Outlook message %s was made available for refetch", transaction.id)

    def close(self) -> None:
        self.service.close()
