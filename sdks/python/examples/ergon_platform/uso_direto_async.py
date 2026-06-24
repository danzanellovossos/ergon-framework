"""
Exemplo direto do AsyncErgonPlatformConnector — sem runner do framework.

Uso:
    cd sdks/python
    pip install -e .
    # instale/disponibilize o pacote ergon-platform-sdk no mesmo ambiente
    cp examples/ergon_platform/.env.example examples/ergon_platform/.env
    # preencha ERGON_CLIENT_ID, ERGON_CLIENT_SECRET, ERGON_WORKFLOW_ID, ERGON_PHASE_ID
    # opcional: ERGON_ASSIGNED_TO=<uuid> para demonstrar filtro explícito
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


async def _run_fetch_case(
    client: ErgonPlatformClient,
    *,
    workflow_id: str,
    phase_id: str,
    ack_phase_id: Optional[str],
    label: str,
    unassigned: bool,
    assigned_to: Optional[str] = None,
) -> None:
    list_params = {"assigned_to": assigned_to} if assigned_to else {}
    consumer_config = ErgonPlatformConsumerConfig(
        workflow_id=workflow_id,
        phase_id=phase_id,
        batch_size=10,
        ack_phase_id=ack_phase_id,
        unassigned=unassigned,
        list_params=list_params,
    )
    connector = AsyncErgonPlatformConnector(client=client, consumer_config=consumer_config)
    try:
        transactions = await connector.fetch_transactions_async()
        logger.info("[%s] %d item(ns) retornado(s).", label, len(transactions))
        for tx in transactions:
            payload = tx.payload
            logger.info(
                "[%s] ID=%s | title=%s | assigned_to=%s",
                label,
                tx.id,
                payload.get("title"),
                payload.get("assigned_to"),
            )
    finally:
        await connector.close()


async def main() -> None:
    workflow_id = _require_env("ERGON_WORKFLOW_ID")
    phase_id = _require_env("ERGON_PHASE_ID")
    ack_phase_id = _optional_env("ERGON_ACK_PHASE_ID")
    explicit_assigned_to = _optional_env("ERGON_ASSIGNED_TO")

    client = ErgonPlatformClient(
        client_id=_require_env("ERGON_CLIENT_ID"),
        client_secret=_require_env("ERGON_CLIENT_SECRET"),
        company_id=_optional_env("ERGON_COMPANY_ID"),
    )

    consumer_config = ErgonPlatformConsumerConfig(
        workflow_id=workflow_id,
        phase_id=phase_id,
        batch_size=50,
        ack_phase_id=ack_phase_id,
    )

    connector = AsyncErgonPlatformConnector(client=client, consumer_config=consumer_config)
    create_phase_id = os.getenv("ERGON_CREATE_PHASE_ID", consumer_config.phase_id)

    try:
        workflows = await asyncio.to_thread(lambda: connector.client.workflows.list())
        logger.info("%d workflow(s) encontrado(s).", len(workflows))

        phases = await asyncio.to_thread(lambda: connector.client.workflows.workflow(workflow_id).phases())

        fields = await connector.list_phase_fields(
            phase_id=phases[0]["id"],
            workflow_id=workflow_id,
        )

        cards = await connector.fetch_transactions_async()
        if cards:
            pipeline_result = await connector.get_pipeline_result(
                workflow_id=_require_env("ERGON_WORKFLOW_ID"),
                field_id=fields[0]["id"],
                item_id=cards[0].id,
            )
            logger.info("Status do pipeline: %s", pipeline_result["state"])
        else:
            logger.info("Nenhum item encontrado na fase atual.")

        logger.info("Buscando itens da fase %s...", consumer_config.phase_id)
        transactions = await connector.fetch_transactions_async()

        if not transactions:
            logger.info("Nenhum item encontrado na fase atual.")

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

    # -----------------------------------------------------------------
    # Cenários de consumo (assigned/unassigned)
    # -----------------------------------------------------------------
    logger.info("---")
    logger.info("Cenário 1: unassigned=True (somente não atribuídos + claim no fetch)")
    await _run_fetch_case(
        client,
        workflow_id=workflow_id,
        phase_id=phase_id,
        ack_phase_id=ack_phase_id,
        label="unassigned=true",
        unassigned=True,
    )

    logger.info("---")
    logger.info("Cenário 2: unassigned=False sem assigned_to (principal id M2M)")
    await _run_fetch_case(
        client,
        workflow_id=workflow_id,
        phase_id=phase_id,
        ack_phase_id=ack_phase_id,
        label="assigned_to=principal_id",
        unassigned=False,
    )

    logger.info("---")
    if explicit_assigned_to:
        logger.info("Cenário 3: unassigned=False com assigned_to explícito (%s)", explicit_assigned_to)
        await _run_fetch_case(
            client,
            workflow_id=workflow_id,
            phase_id=phase_id,
            ack_phase_id=ack_phase_id,
            label="assigned_to=explicito",
            unassigned=False,
            assigned_to=explicit_assigned_to,
        )
    else:
        logger.info("Cenário 3 ignorado: defina ERGON_ASSIGNED_TO no .env para testar assigned_to explícito.")


if __name__ == "__main__":
    asyncio.run(main())
