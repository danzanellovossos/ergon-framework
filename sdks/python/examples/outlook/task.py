import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ergon.connector import Transaction
from ergon.connector.outlook import AsyncOutlookGraphConnector
from ergon.task import policies
from ergon.task.mixins import AsyncConsumerTask

logger = logging.getLogger(__name__)

ATTACHMENT_DIR = Path(__file__).parent / "downloads"


@dataclass
class OutlookAttachment:
    id: Optional[str]
    filename: str
    content_type: Optional[str] = None
    size: Optional[int] = None
    content: bytes = b""
    saved_path: Optional[Path] = None

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "OutlookAttachment":
        content = item.get("content")
        return cls(
            id=item.get("id"),
            filename=str(item.get("name") or "attachment"),
            content_type=item.get("content_type"),
            size=item.get("size"),
            content=content if isinstance(content, bytes) else b"",
        )


@dataclass(repr=False)
class ProcessedOutlookMessage:
    message_id: str
    subject: str
    from_address: Optional[str]
    attachments: list[OutlookAttachment] = field(default_factory=list)

    @property
    def attachment_count(self) -> int:
        return len(self.attachments)

    def __repr__(self) -> str:
        message_id = self._compact(self.message_id, 24)
        subject = self._compact(self.subject, 60)
        saved = sum(1 for attachment in self.attachments if attachment.saved_path)
        return (
            f"ProcessedOutlookMessage(id={message_id!r}, subject={subject!r}, "
            f"from={self.from_address!r}, attachments={self.attachment_count}, saved={saved})"
        )

    @staticmethod
    def _compact(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        edge = (limit - 1) // 2
        return f"{value[:edge]}…{value[-edge:]}"


class OutlookEmailTask(AsyncConsumerTask):
    """Consume Outlook messages, download attachments, and save them locally."""

    name = "outlook-email-processor"
    consumer_connector: AsyncOutlookGraphConnector
    consumer_policy: policies.ConsumerPolicy
    attachment_dir: Path = ATTACHMENT_DIR

    async def execute(self) -> Any:
        return await self.consume_transactions(self.consumer_policy)

    async def process_transaction(self, transaction: Transaction) -> ProcessedOutlookMessage:
        attachments: list[OutlookAttachment] = []
        for item in transaction.metadata.get("attachments") or []:
            attachment = OutlookAttachment.from_item(item)
            if not attachment.content:
                logger.warning("Anexo %s sem conteúdo baixado; pulando", attachment.filename)
                attachments.append(attachment)
                continue
            attachment.saved_path = await self.consumer_connector.save_attachment(
                item,
                self.attachment_dir,
            )
            logger.info("Anexo salvo em %s", attachment.saved_path)
            attachments.append(attachment)

        result = ProcessedOutlookMessage(
            message_id=transaction.id,
            subject=str(transaction.metadata.get("subject") or ""),
            from_address=transaction.metadata.get("from_email"),
            attachments=attachments,
        )
        logger.info("Processando %s", result)
        return result

    async def handle_process_success(
        self,
        transaction: Transaction,
        result: Any,
    ) -> None:
        await self.consumer_connector.ack_transaction(transaction)
        logger.info("ACK %s", result)

    async def handle_process_exception(
        self,
        transaction: Transaction,
        exc: Exception,
    ) -> None:
        logger.error("Erro ao processar a mensagem %s: %s", transaction.id, exc)
        await self.consumer_connector.nack_transaction(transaction, requeue=True)

    async def exit(self) -> None:
        await self.consumer_connector.close()
