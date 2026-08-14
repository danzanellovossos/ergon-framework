"""Exemplo: como usar ``AsyncErgonPlatformChannelsConnector`` numa task.

O framework chama ``fetch_transactions`` (lista da inbox). A lista da API não
traz corpo nem anexos — por isso o processamento começa no detalhe:

    tx = await connector.fetch_transaction_by_id_async(tx.id)

Com ``download_attachments=True`` no consumer (ver ``config.py``),
``tx.metadata["attachments"]`` já vem com os bytes dos anexos.
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
        return (
            f"ChannelEventAttachment(id={self.id!r}, filename={self.filename!r}, "
            f"content_type={self.content_type!r}, size={nbytes})"
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
    auth_code_connector: AsyncErgonPlatformChannelsConnector
    consumer_policy: policies.ConsumerPolicy

    async def execute(self) -> Any:
        return await self.consume_transactions(self.consumer_policy)

    async def process_transaction(self, transaction: Transaction) -> ProcessedChannelEvent:
        transaction = await self.consumer_connector.fetch_transaction_by_id_async(transaction.id)

        metadata = transaction.metadata or {}
        message = metadata.get("message_payload") or {}
        attachments = [ChannelEventAttachment.from_item(item) for item in metadata.get("attachments") or []]

        logger.info(
            "Evento %s | %s | de %s | %s anexo(s)",
            transaction.id,
            metadata.get("subject"),
            metadata.get("from_address"),
            len(attachments),
        )

        inbox_events = await self.auth_code_connector.fetch_transactions_async()
        logger.info("Caixa secundária: %s evento(s)", len(inbox_events))

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
        for connector in self.connectors.values():
            close = getattr(connector, "close", None)
            if close is not None:
                await close()
