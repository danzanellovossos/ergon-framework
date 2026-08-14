import logging
from pathlib import Path
from typing import Any, Dict

from ergon.connector import Transaction
from ergon.connector.ergon_platform.channels import AsyncErgonPlatformChannelsConnector
from ergon.task import policies
from ergon.task.mixins import AsyncConsumerTask

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path(__file__).resolve().parent / "downloads"


class ChannelsEventTask(AsyncConsumerTask):
    """Exemplo: consome eventos da inbox via consumer_connector."""

    name = "channels-event-processor"
    consumer_connector: AsyncErgonPlatformChannelsConnector
    consumer_policy: policies.ConsumerPolicy

    async def execute(self) -> Any:
        return await self.consume_transactions(self.consumer_policy)

    async def process_transaction(self, transaction: Transaction) -> Dict[str, Any]:
        transaction = await self.consumer_connector.fetch_transaction_by_id_async(transaction.id)
        message_payload = transaction.metadata.get("message_payload") or {}

        logger.info(
            "Evento %s | status=%s | from=%s | subject=%s",
            transaction.id,
            transaction.metadata.get("status"),
            transaction.metadata.get("from_address"),
            transaction.metadata.get("subject"),
        )
        if message_payload.get("text"):
            preview = str(message_payload["text"]).replace("\n", " ")[:300]
            logger.info(
                "Corpo (text): %s%s",
                preview,
                "..." if len(str(message_payload["text"])) > 300 else "",
            )

        saved = await self.consumer_connector.download_attachments(transaction, dest=DOWNLOAD_DIR)
        for file in saved:
            logger.info("Baixou anexo %s (%s bytes) -> %s", file.filename, len(file.content), file.path)

        return {
            "event_id": transaction.id,
            "has_text": bool(message_payload.get("text")),
            "has_html": bool(message_payload.get("html")),
            "attachment_count": len(saved),
            "downloaded": [file.path for file in saved],
        }

    async def handle_process_success(self, transaction: Transaction, result: Any) -> None:
        await self.consumer_connector.ack_transaction(transaction)
        logger.info("Evento %s processado | result=%s", transaction.id, result)

    async def handle_process_exception(self, transaction: Transaction, exc: Exception) -> None:
        logger.error("Erro ao processar evento %s: %s", transaction.id, exc)
        await self.consumer_connector.nack_transaction(transaction, requeue=True)

    async def exit(self) -> None:
        connector = getattr(self, "consumer_connector", None)
        if connector is not None and hasattr(connector, "close"):
            await connector.close()
