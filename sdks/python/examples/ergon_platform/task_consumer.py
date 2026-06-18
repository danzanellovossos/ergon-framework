"""
Exemplo com framework Ergon — AsyncConsumerTask + TaskConfig + runner.

Uso:
    cd sdks/python
    pip install -e .
    # instale/disponibilize o pacote ergon-platform-sdk no mesmo ambiente
    cp examples/ergon_platform/.env.example examples/ergon_platform/.env
    # preencha ERGON_CLIENT_ID, ERGON_CLIENT_SECRET, ERGON_WORKFLOW_ID, ERGON_PHASE_ID
    py examples/ergon_platform/task_consumer.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from ergon.connector import ConnectorConfig, Transaction
from ergon.connector.ergon_platform import (
    AsyncErgonPlatformConnector,
    ErgonPlatformClient,
    ErgonPlatformConsumerConfig,
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


def _build_consumer_config() -> ErgonPlatformConsumerConfig:
    return ErgonPlatformConsumerConfig(
        workflow_id=_require_env("ERGON_WORKFLOW_ID"),
        phase_id=_require_env("ERGON_PHASE_ID"),
        batch_size=5,
        ack_phase_id=_optional_env("ERGON_ACK_PHASE_ID"),
    )


def _build_task_config() -> TaskConfig:
    client = ErgonPlatformClient(
        client_id=_require_env("ERGON_CLIENT_ID"),
        client_secret=_require_env("ERGON_CLIENT_SECRET"),
        base_url=os.getenv("ERGON_BASE_URL", "http://localhost").strip()
    )

    consumer_policy = policies.ConsumerPolicy()
    consumer_policy.name = "consumer"
    consumer_policy.fetch.connector_name = "workflow"
    consumer_policy.fetch.batch.size = 5
    consumer_policy.loop.limit = None
    consumer_policy.loop.streaming = True

    return TaskConfig(
        name="workflow-item-processor",
        task=WorkflowItemTask,
        max_workers=1,
        connectors={
            "workflow": ConnectorConfig(
                connector=AsyncErgonPlatformConnector,
                kwargs={
                    "client": client,
                    "consumer_config": _build_consumer_config(),
                },
            ),
        },
        policies=[consumer_policy],
    )


class WorkflowItemTask(AsyncConsumerTask):
    """Consome itens de uma fase do workflow e faz ack (move de fase) após sucesso."""

    name = "workflow-item-processor"

    async def execute(self) -> Any:
        return await self.consume_transactions(self.consumer_policy)

    async def process_transaction(self, transaction: Transaction) -> Dict[str, Any]:
        payload = transaction.payload
        title = payload.get("title", "")
        logger.info("Processando item %s — título: %s", transaction.id, title)
        return {"item_id": transaction.id, "title": title}

    async def handle_process_success(self, transaction: Transaction, result: Any) -> None:
        await self.workflow_connector.ack_transaction(transaction)
        logger.info("Ack aplicado em %s (título: %s)", transaction.id, result.get("title"))

    async def exit(self) -> None:
        if hasattr(self, "workflow_connector") and hasattr(self.workflow_connector, "close"):
            await self.workflow_connector.close()


if __name__ == "__main__":
    config = _build_task_config()
    exit_code = run_task(config, debug=True)
    sys.exit(exit_code)
