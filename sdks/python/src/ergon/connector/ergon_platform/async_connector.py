import logging
from typing import Any, Dict, List, Optional

from ..connector import AsyncConnector
from ..transaction import Transaction
from .async_service import AsyncErgonPlatformService
from .models import (
    CreateItemPayload,
    ErgonPlatformClient,
    ErgonPlatformConsumerConfig,
    ErgonPlatformProducerConfig,
)
from .utils import get_value, normalize_create_payload

logger = logging.getLogger(__name__)


class AsyncErgonPlatformConnector(AsyncConnector):
    service: AsyncErgonPlatformService

    def __init__(
        self,
        client: ErgonPlatformClient,
        consumer_config: Optional[ErgonPlatformConsumerConfig] = None,
        producer_config: Optional[ErgonPlatformProducerConfig] = None,
    ) -> None:
        self.service = AsyncErgonPlatformService(client)
        self._consumer_config = consumer_config
        self._producer_config = producer_config or ErgonPlatformProducerConfig()

    async def fetch_transactions_async(
        self,
        batch_size: Optional[int] = None,
        *args,
        **kwargs,
    ) -> List[Transaction]:
        if self._consumer_config is None:
            raise ValueError("AsyncErgonPlatformConnector requires a consumer_config to fetch transactions")

        config = self._consumer_config
        limit = batch_size or config.batch_size
        params: Dict[str, Any] = {**config.list_params, **kwargs}
        if config.unassigned:
            params.pop("assigned_to", None)
            params["assigned"] = "no"

        transactions = await self.service.fetch_items(
            config.workflow_id,
            config.phase_id,
            limit=limit,
            offset=config.offset,
            **params,
        )
        if not config.unassigned:
            return transactions

        claimed_transactions: List[Transaction] = []
        for transaction in transactions:
            try:
                await self.service.claim_item(transaction.id)
                claimed_transactions.append(await self.fetch_transaction_by_id_async(transaction.id))
            except Exception:
                logger.warning(
                    "Failed to claim item %s during unassigned fetch; skipping it",
                    transaction.id,
                    exc_info=True,
                )
        return claimed_transactions

    async def dispatch_transactions_async(
        self,
        transactions: List[Transaction],
        *args,
        **kwargs,
    ) -> List[str]:
        created_ids: List[str] = []
        for transaction in transactions:
            result = await self._create_from_payload(transaction.payload)
            created_ids.append(self._created_item_id(result))
        return created_ids

    async def fetch_child_transactions_async(
        self,
        parent_item_id: str,
        *args,
        **kwargs,
    ) -> List[Transaction]:
        return await self.service.fetch_child_items(parent_item_id, **kwargs)

    async def fetch_transaction_by_id_async(self, transaction_id: str, *args, **kwargs) -> Transaction:
        workflow_id = self._consumer_config.workflow_id if self._consumer_config else ""
        return await self.service.get_item_transaction(transaction_id, workflow_id, **kwargs)

    async def fetch_items_by_query(
        self, workflow_id: str, query: Optional[Dict[str, Any]] = None, **fields: Any
    ) -> List[Transaction]:
        return await self.service.fetch_items_by_query(workflow_id, query, **fields)

    async def get_transactions_count_async(self, *args, **kwargs) -> int:
        if self._consumer_config is None:
            raise ValueError("AsyncErgonPlatformConnector requires a consumer_config to count transactions")

        config = self._consumer_config
        params: Dict[str, Any] = {**config.list_params, **kwargs}
        return await self.service.get_phase_items_count(
            config.workflow_id,
            config.phase_id,
            **params,
        )

    async def ack_transaction(self, transaction: Transaction, phase_id: Optional[str] = None) -> None:
        target_phase = phase_id
        if target_phase is None and self._consumer_config is not None:
            target_phase = self._consumer_config.ack_phase_id
        if not target_phase:
            return
        await self.service.move_item_to_phase(transaction.id, target_phase)

    async def release_item(
        self,
        item_id: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        delay_seconds: Optional[int] = None,
        **fields: Any,
    ) -> Any:
        return await self.service.release_item(
            item_id,
            data,
            delay_seconds=delay_seconds,
            **fields,
        )

    async def nack_transaction(self, transaction: Transaction, requeue: bool = True, delay_seconds: int = 10) -> None:
        if requeue:
            await self.service.release_item(transaction.id, delay_seconds=delay_seconds)
            return

        target_phase = self._consumer_config.nack_phase_id if self._consumer_config is not None else None
        if not target_phase:
            raise ValueError("nack_phase_id is required when nack_transaction is called with requeue=False")

        await self.service.move_item_to_phase(transaction.id, target_phase)

    async def close(self) -> None:
        await self.service.close()

    async def _create_from_payload(self, payload: CreateItemPayload) -> Any:
        data = normalize_create_payload(payload)
        producer = self._producer_config

        workflow_id = data.get("workflow_id") or producer.workflow_id
        phase_id = data.get("phase_id") or producer.phase_id
        if not workflow_id:
            raise ValueError("workflow_id is required to create an item (set it in payload or producer_config)")
        if not phase_id:
            raise ValueError("phase_id is required to create an item (set it in payload or producer_config)")

        attachment = data.get("attachment")
        attachment_field_id = data.get("attachment_field_id") or producer.attachment_field_id
        content_type = data.get("content_type") or producer.default_content_type
        parent_item_id = data.get("parent_item_id") or producer.parent_item_id

        return await self.service.create_item(
            workflow_id,
            phase_id,
            data["title"],
            parent_item_id=parent_item_id,
            field_values=data.get("field_values"),
            attachment=attachment,
            attachment_field_id=attachment_field_id,
            content_type=content_type,
            **data.get("fields", {}),
        )

    @staticmethod
    def _created_item_id(result: Any) -> str:
        if isinstance(result, dict) and "item" in result:
            return str(get_value(result["item"], "id", ""))
        return str(get_value(result, "id", ""))
