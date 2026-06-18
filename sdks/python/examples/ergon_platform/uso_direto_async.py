"""
Exemplo direto do AsyncErgonPlatformConnector — sem runner do framework.

Uso:
    cd sdks/python
    pip install -e ".[ergon_platform]"
    cp examples/ergon_platform/.env.example examples/ergon_platform/.env
    # preencha ERGON_CLIENT_ID, ERGON_CLIENT_SECRET, ERGON_WORKFLOW_ID, ERGON_PHASE_ID
    py examples/ergon_platform/uso_direto_async.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from ergon.connector import Transaction
from ergon.connector.ergon_platform import (
    AsyncErgonPlatformConnector,
    CreateItemInput,
    ErgonPlatformClient,
    ErgonPlatformConsumerConfig,
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


async def main() -> None:
    client = ErgonPlatformClient(
        client_id=_require_env("ERGON_CLIENT_ID"),
        client_secret=_require_env("ERGON_CLIENT_SECRET"),
        company_id=_optional_env("ERGON_COMPANY_ID"),
    )

    consumer_config = ErgonPlatformConsumerConfig(
        workflow_id=_require_env("ERGON_WORKFLOW_ID"),
        phase_id=_require_env("ERGON_PHASE_ID"),
        batch_size=50,
        ack_phase_id=_optional_env("ERGON_ACK_PHASE_ID"),
    )

    connector = AsyncErgonPlatformConnector(client=client, consumer_config=consumer_config)
    create_phase_id = os.getenv("ERGON_CREATE_PHASE_ID", consumer_config.phase_id)

    try:
        workflows = await connector.list_workflows()
        logger.info("%d workflow(s) encontrado(s).", len(workflows))

        phases = await connector.list_workflow_phases(workflow_id=_require_env("ERGON_WORKFLOW_ID"))

        fields = await connector.list_phase_fields(phase_id=phases[0]["id"])

        cards = await connector.service.fetch_items(
            workflow_id=_require_env("ERGON_WORKFLOW_ID"),
            phase_id=phases[0]["id"],
        )

        pipeline_result = await connector.get_pipeline_result(
            workflow_id=_require_env("ERGON_WORKFLOW_ID"),
            field_id=fields[0]["id"],
            item_id=cards[0].__dict__["id"],
        )
        logger.info("Status do pipeline: %s", pipeline_result["state"])

        logger.info("Buscando itens da fase %s...", consumer_config.phase_id)
        transactions = await connector.fetch_transactions_async()

        if not transactions:
            logger.info("Nenhum item encontrado na fase atual.")
            return

        logger.info("%d item(ns) encontrado(s).", len(transactions))
        for tx in transactions:
            payload = tx.payload
            logger.info("---")
            logger.info("ID: %s", tx.id)
            logger.info("Título: %s", payload.get("title"))
            logger.info("Fase: %s", payload.get("phase_id"))

            # Após processar, mova o item para a fase de ack (se configurada).
            await connector.ack_transaction(tx)

        # -----------------------------------------------------------------
        # Criação de item (descomente para testar dispatch)
        # -----------------------------------------------------------------
        outbound = Transaction(
            id="novo-item-teste-connector",
            payload=CreateItemInput(
                title="Item criado pelo exemplo",
                workflow_id=consumer_config.workflow_id,
                phase_id=create_phase_id,
                attachment=r"L:\TRAMPO\JSL\Florestal\FE_SP_RDZ_20260301_20260315_2001663.pdf",
                attachment_field_id=os.getenv("ERGON_ATTACHMENT_FIELD_ID"),
            ),
        )
        created_ids = await connector.dispatch_transactions_async([outbound])
        logger.info("Item criado. IDs: %s", created_ids)

        # -----------------------------------------------------------------
        # Dispatch em bulk (vários cards em uma chamada do connector)
        # -----------------------------------------------------------------
        bulk_transactions = [
            Transaction(
                id="bulk-item-1",
                payload=CreateItemInput(
                    title="Item bulk 1 criado pelo exemplo",
                    workflow_id=consumer_config.workflow_id,
                    phase_id=create_phase_id,
                ),
            ),
            Transaction(
                id="bulk-item-2",
                payload=CreateItemInput(
                    title="Item bulk 2 criado pelo exemplo",
                    workflow_id=consumer_config.workflow_id,
                    phase_id=create_phase_id,
                ),
            ),
        ]
        bulk_created_ids = await connector.dispatch_transactions_async(bulk_transactions)
        logger.info("Itens criados em bulk. IDs: %s", bulk_created_ids)

        # -----------------------------------------------------------------
        # Criação de card filho
        # -----------------------------------------------------------------
        parent_item_id = _optional_env("ERGON_PARENT_ITEM_ID") or transactions[0].id
        child = Transaction(
            id="card-filho-teste-connector",
            payload=CreateItemInput(
                title="Card filho criado pelo exemplo",
                workflow_id=consumer_config.workflow_id,
                phase_id=create_phase_id,
                parent_item_id=parent_item_id,
            ),
        )
        child_created_ids = await connector.dispatch_transactions_async([child])
        logger.info("Card filho criado. Parent: %s. IDs: %s", parent_item_id, child_created_ids)

    finally:
        await connector.close()


if __name__ == "__main__":
    asyncio.run(main())
