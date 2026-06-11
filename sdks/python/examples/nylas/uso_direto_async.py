"""
Exemplo direto do AsyncNylasConnector — sem runner do framework.

Uso:
    cd sdks/python
    pip install -e ".[nylas]"
    cp exemplo/nylas/.env.example exemplo/nylas/.env
    # preencha NYLAS_API_KEY e NYLAS_GRANT_ID no .env
    py exemplo/nylas/uso_direto_async.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from ergon.connector.nylas import (
    AckActionConfig,
    AsyncNylasConnector,
    ClientSideFilter,
    NylasClient,
    NylasConsumerConfig,
)

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


def _format_addresses(addresses: Optional[List[Dict[str, Any]]]) -> str:
    if not addresses:
        return "-"
    parts = []
    for addr in addresses:
        name = addr.get("name") or ""
        email = addr.get("email") or ""
        parts.append(f"{name} <{email}>".strip() if name else email)
    return ", ".join(parts) or "-"


async def list_folders(connector: AsyncNylasConnector) -> None:
    folders = await connector.service.list_folders()
    if not folders:
        logger.info("Nenhuma pasta encontrada.")
        return

    logger.info("Pastas disponíveis:")
    for folder in folders:
        logger.info("  - id=%s | name=%s", folder.get("id"), folder.get("name"))


async def main() -> None:
    api_key = _require_env("NYLAS_API_KEY")
    grant_id = _require_env("NYLAS_GRANT_ID")
    api_uri = os.getenv("NYLAS_API_URI", "https://api.us.nylas.com").strip()
    inbox_folder_id = _optional_env("NYLAS_INBOX_FOLDER_ID")
    processed_folder_id = _optional_env("NYLAS_PROCESSED_FOLDER_ID")
    subject_filter = _optional_env("NYLAS_SUBJECT_FILTER")
    attachment_filename_filter = _optional_env("NYLAS_ATTACHMENT_FILENAME_FILTER")

    client = NylasClient(
        api_key=api_key,
        grant_id=grant_id,
        api_uri=api_uri,
    )

    consumer_kwargs: Dict[str, Any] = {
        "unread": True,
        "has_attachment": True,
        "batch_size": 200,
        "download_attachments": True,
        "ack_config": AckActionConfig(
            mark_as_read=True,
            move_to_folder_id=processed_folder_id,
        ),
    }
    if subject_filter:
        consumer_kwargs["subject"] = subject_filter
    if inbox_folder_id:
        consumer_kwargs["in_"] = inbox_folder_id
    if attachment_filename_filter:
        consumer_kwargs["client_side_filter"] = ClientSideFilter(
            attachment_filename_contains=attachment_filename_filter,
        )
    connector = AsyncNylasConnector(
        client=client,
        consumer_config=NylasConsumerConfig(**consumer_kwargs),
    )

    try:
        # -----------------------------------------------------------------
        # Envio de e-mail
        # -----------------------------------------------------------------

        if not inbox_folder_id:
            logger.info("NYLAS_INBOX_FOLDER_ID não definido — listando pastas:")
            await list_folders(connector)

        if attachment_filename_filter:
            logger.info(
                "Buscando mensagens não lidas com anexos (filename contém: %s)...",
                attachment_filename_filter,
            )
        else:
            logger.info("Buscando mensagens não lidas com anexos...")
        transactions = await connector.fetch_transactions_async()

        if not transactions:
            logger.info("Nenhuma mensagem encontrada com os filtros atuais.")
            return

        logger.info("%d mensagem(ns) encontrada(s).", len(transactions))

        for tx in transactions:
            message = tx.payload
            logger.info("---")
            logger.info("ID: %s", tx.id)
            logger.info("Assunto: %s", message.get("subject"))
            logger.info("De: %s", _format_addresses(message.get("from")))
            attachments = message.get("attachments") or []
            if attachments:
                names = [att.get("filename", "?") for att in attachments]
                logger.info("Anexos: %s", ", ".join(names))
            else:
                logger.info("Anexos: nenhum")

            await connector.ack_transaction(tx)
            logger.info("Mensagem %s marcada como processada (ack).", tx.id)

        # -----------------------------------------------------------------
        # Envio de e-mail (descomente para testar dispatch)
        # -----------------------------------------------------------------
        # from ergon.connector import Transaction
        #
        # outbound = Transaction(
        #     id="outbound-demo",
        #     payload={
        #         "to": [{"email": "destinatario@example.com"}],
        #         "subject": "Teste Ergon Nylas",
        #         "body": "E-mail enviado pelo exemplo uso_direto_async.py",
        #     },
        # )
        # sent_ids = await connector.dispatch_transactions_async([outbound])
        # logger.info("E-mail enviado. IDs: %s", sent_ids)

    finally:
        await connector.close()


if __name__ == "__main__":
    asyncio.run(main())
