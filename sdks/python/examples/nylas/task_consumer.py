"""
Exemplo com framework Ergon — AsyncConsumerTask + TaskConfig + runner.

Uso:
    cd sdks/python
    pip install -e ".[nylas]"
    cp exemplo/nylas/.env.example exemplo/nylas/.env
    # preencha NYLAS_API_KEY e NYLAS_GRANT_ID no .env
    py exemplo/nylas/task_consumer.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from ergon.connector import ConnectorConfig, Transaction
from ergon.connector.nylas import (
    AckActionConfig,
    AsyncNylasConnector,
    ClientSideFilter,
    NylasClient,
    NylasConsumerConfig,
)
from ergon.task import policies
from ergon.task.base import TaskConfig
from ergon.task.mixins import AsyncConsumerTask
from ergon.task.runner import run_task

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        logger.error("Variável de ambiente obrigatória não definida: %s", name)
        logger.error("Copie .env.example para .env e preencha os valores.")
        sys.exit(1)
    return value


def _optional_env(name: str) -> Optional[str]:
    value = os.getenv(name, "").strip()
    return value or None


def _build_consumer_config() -> NylasConsumerConfig:
    processed_folder_id = _optional_env("NYLAS_PROCESSED_FOLDER_ID")
    subject_filter = _optional_env("NYLAS_SUBJECT_FILTER")
    inbox_folder_id = _optional_env("NYLAS_INBOX_FOLDER_ID")
    attachment_filename_filter = _optional_env("NYLAS_ATTACHMENT_FILENAME_FILTER")

    kwargs: Dict[str, Any] = {
        "unread": True,
        "has_attachment": True,
        "batch_size": 5,
        "download_attachments": False,
        "ack_config": AckActionConfig(
            mark_as_read=False,
            move_to_folder_id=processed_folder_id,
        ),
    }
    if subject_filter:
        kwargs["subject"] = subject_filter
    if inbox_folder_id:
        kwargs["in_"] = inbox_folder_id
    if attachment_filename_filter:
        kwargs["client_side_filter"] = ClientSideFilter(
            attachment_filename_contains=attachment_filename_filter,
        )

    return NylasConsumerConfig(**kwargs)


def _build_task_config() -> TaskConfig:
    api_key = _require_env("NYLAS_API_KEY")
    grant_id = _require_env("NYLAS_GRANT_ID")
    api_uri = os.getenv("NYLAS_API_URI", "https://api.us.nylas.com").strip()

    consumer_policy = policies.ConsumerPolicy()
    consumer_policy.name = "consumer"
    consumer_policy.fetch.connector_name = "inbox"
    consumer_policy.fetch.batch.size = 5
    consumer_policy.loop.limit = None
    consumer_policy.loop.streaming = True

    return TaskConfig(
        name="email-processor",
        task=EmailProcessorTask,
        max_workers=1,
        connectors={    
            "inbox": ConnectorConfig(
                connector=AsyncNylasConnector,
                kwargs={
                    "client": NylasClient(
                        api_key=api_key,
                        grant_id=grant_id,
                        api_uri=api_uri,
                    ),
                    "consumer_config": _build_consumer_config(),
                },
            ),
        },
        policies=[consumer_policy],
    )


class EmailProcessorTask(AsyncConsumerTask):
    """Processa e-mails da inbox e faz ack após sucesso."""

    name = "email-processor"

    async def execute(self) -> Any:
        return await self.consume_transactions(self.consumer_policy)

    async def process_transaction(self, transaction: Transaction) -> Dict[str, Any]:
        message = transaction.payload
        subject = message.get("subject", "")
        attachments = message.get("attachments") or []

        logger.info("Processando mensagem %s — assunto: %s", transaction.id, subject)
        if attachments:
            names = [att.get("filename", "?") for att in attachments]
            logger.info("  Anexos: %s", ", ".join(names))

        return {
            "message_id": transaction.id,
            "subject": subject,
            "attachment_count": len(attachments),
        }

    async def handle_process_success(self, transaction: Transaction, result: Any) -> None:
        await self.inbox_connector.ack_transaction(transaction)
        logger.info(
            "Ack aplicado em %s (assunto: %s)",
            transaction.id,
            result.get("subject"),
        )

    async def exit(self) -> None:
        if hasattr(self, "inbox_connector") and hasattr(self.inbox_connector, "close"):
            await self.inbox_connector.close()


if __name__ == "__main__":
    config = _build_task_config()
    exit_code = run_task(config, debug=True)
    sys.exit(exit_code)
