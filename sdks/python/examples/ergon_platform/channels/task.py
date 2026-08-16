"""Exemplo: como usar ``AsyncErgonPlatformChannelsConnector`` numa task.

O framework recebe cada evento completo por uma claim atômica. Corpo, anexos e
o lease de ACK/NACK pertencem à mesma ``Transaction``; não há um segundo GET.
Com ``download_attachments=True`` no consumer (ver ``config.py``),
``transaction.metadata["attachments"]`` já vem com os bytes dos anexos.
(``id``, ``filename``, ``content_type``, ``size``, ``content: bytes``).

Depois: ``ack_transaction`` em sucesso, ``nack_transaction`` em erro.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from ergon.connector import Transaction
from ergon.connector.ergon_platform.channels import AsyncErgonPlatformChannelsConnector
from ergon.task import policies
from ergon.task.mixins import AsyncConsumerTask

logger = logging.getLogger(__name__)


@dataclass
class ChannelEventAttachment:
    id: Optional[str]
    filename: str
    content_type: Optional[str] = None
    size: Optional[int] = None
    content: bytes = b""

    @classmethod
    def from_item(cls, item: dict) -> "ChannelEventAttachment":
        content = item.get("content")
        return cls(
            id=item.get("id"),
            filename=str(item.get("filename") or "attachment"),
            content_type=item.get("content_type"),
            size=item.get("size"),
            content=content if isinstance(content, bytes) else b"",
        )

    def __repr__(self) -> str:
        nbytes = self.size if self.size is not None else len(self.content)
        id_repr = repr(self.id) if self.id is not None else "None"
        content_type_repr = repr(self.content_type) if self.content_type is not None else "None"
        return (
            f"ChannelEventAttachment(id={id_repr}, filename={self.filename!r}, "
            f"content_type={content_type_repr}, size={nbytes})"
        )


@dataclass
class ProcessedChannelEvent:
    event_id: str
    subject: Optional[str]
    from_address: Optional[str]
    has_text: bool
    has_html: bool
    attachments: list[ChannelEventAttachment] = field(default_factory=list)

    @property
    def attachment_count(self) -> int:
        return len(self.attachments)


class ChannelsEventTask(AsyncConsumerTask):
    """Task to process channels events."""

    name = "channels-event-processor"
    consumer_connector: AsyncErgonPlatformChannelsConnector
    consumer_policy: policies.ConsumerPolicy

    async def execute(self) -> Any:
        return await self.consume_transactions(self.consumer_policy)

    async def process_transaction(self, transaction: Transaction) -> ProcessedChannelEvent:
        metadata = transaction.metadata or {}
        message = metadata.get("message_payload") or {}
        attachments = [ChannelEventAttachment.from_item(item) for item in metadata.get("attachments") or []]

        return ProcessedChannelEvent(
            event_id=transaction.id or "",
            subject=metadata.get("subject"),
            from_address=metadata.get("from_address"),
            has_text=bool(message.get("text")),
            has_html=bool(message.get("html")),
            attachments=attachments,
        )

    async def handle_process_success(self, transaction: Transaction, result: Any) -> None:
        await self.consumer_connector.ack_transaction(transaction)
        logger.info("Ack %s | anexos=%s", transaction.id, getattr(result, "attachment_count", 0))

    async def handle_process_exception(self, transaction: Transaction, exc: Exception) -> None:
        logger.error("Erro no evento %s: %s", transaction.id, exc)
        await self.consumer_connector.nack_transaction(transaction, requeue=True)

    async def exit(self) -> None:
        await self.consumer_connector.close()
