import asyncio
import logging
from typing import Any, Dict, List, Optional

from ...connector import AsyncConnector
from ...transaction import Transaction
from .._client import create_ergon_client
from ..models import ErgonPlatformClient
from ._mode import DEFAULT_NACK_REASON, DEFAULT_NACK_RELEASE_DELAY_SECONDS, ConsumerModeState
from ._operations import ErgonPlatformWorkflowsService
from .models import (
    CreateItemPayload,
    ErgonPlatformConsumerConfig,
    ErgonPlatformProducerConfig,
)
from .utils import get_value, normalize_create_payload

logger = logging.getLogger(__name__)


class AsyncErgonPlatformConnector(AsyncConnector):
    def __init__(
        self,
        client: ErgonPlatformClient,
        consumer_config: Optional[ErgonPlatformConsumerConfig] = None,
        producer_config: Optional[ErgonPlatformProducerConfig] = None,
    ) -> None:
        self.client = create_ergon_client(client)
        self.service = ErgonPlatformWorkflowsService(client, self.client)
        self._operations = self.service
        self._consumer_config = consumer_config
        self._producer_config = producer_config or ErgonPlatformProducerConfig()
        self._mode = ConsumerModeState(client, consumer_config, self.service, self._set_client)

    def _set_client(self, client: Any) -> None:
        self.client = client

    async def is_pull_async(self) -> bool:
        """True when the consumed phase is Pull only (resolved once, on first use)."""
        if self._mode.resolved is not None:
            return self._mode.resolved
        return await asyncio.to_thread(self._mode.is_pull)

    @property
    def consumer_id(self) -> Optional[str]:
        """The ``X-Ergon-Consumer-Id`` this connector claims under; None on standard phases."""
        return self._mode.consumer_id

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
        if await self.is_pull_async():
            if kwargs:
                logger.debug("Ignoring list params %s on Pull only phase %s", sorted(kwargs), config.phase_id)
            return await asyncio.to_thread(
                lambda: self._operations.pull_items(
                    config.workflow_id,
                    config.phase_id,
                    limit=limit,
                    filters=config.pull_filters,
                    wait_seconds=config.pull_wait_seconds,
                )
            )

        params: Dict[str, Any] = {**config.list_params, **kwargs}
        if config.unassigned:
            params.pop("assigned_to", None)
            params["assigned"] = "no"

        transactions = await asyncio.to_thread(
            lambda: self._operations.fetch_items(
                config.workflow_id,
                config.phase_id,
                limit=limit,
                offset=config.offset,
                **params,
            )
        )
        if not config.unassigned:
            return transactions

        claimed_transactions: List[Transaction] = []
        for transaction in transactions:
            try:
                await asyncio.to_thread(lambda: self.client.workflows.items.claim(transaction.id, {}))
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
        return await asyncio.to_thread(lambda: self._operations.fetch_child_items(parent_item_id, **kwargs))

    async def fetch_transaction_by_id_async(self, transaction_id: str, *args, **kwargs) -> Transaction:
        workflow_id = self._consumer_config.workflow_id if self._consumer_config else ""
        return await asyncio.to_thread(
            lambda: self._operations.get_item_transaction(transaction_id, workflow_id, **kwargs)
        )

    async def fetch_items_by_query(
        self, workflow_id: str, query: Optional[Dict[str, Any]] = None, **fields: Any
    ) -> List[Transaction]:
        return await asyncio.to_thread(lambda: self._operations.fetch_items_by_query(workflow_id, query, **fields))

    async def get_transactions_count_async(self, *args, **kwargs) -> int:
        if self._consumer_config is None:
            raise ValueError("AsyncErgonPlatformConnector requires a consumer_config to count transactions")

        config = self._consumer_config
        params: Dict[str, Any] = {**config.list_params, **kwargs}
        return await asyncio.to_thread(
            lambda: self._operations.get_phase_items_count(
                config.workflow_id,
                config.phase_id,
                **params,
            )
        )

    async def ack_transaction(self, transaction: Transaction, phase_id: Optional[str] = None) -> None:
        """Route to ``phase_id`` / ``ack_phase_id``; on a Pull only phase with no target, complete the stay."""
        target_phase = phase_id
        if target_phase is None and self._consumer_config is not None:
            target_phase = self._consumer_config.ack_phase_id
        if target_phase:
            await asyncio.to_thread(lambda: self.client.workflows.items.route(transaction.id, to_phase_id=target_phase))
            return
        if await self.is_pull_async():
            await asyncio.to_thread(lambda: self._operations.complete_item(transaction.id))

    async def release_item(
        self,
        item_id: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        delay_seconds: Optional[int] = None,
        **fields: Any,
    ) -> Any:
        """Give the item back. ``delay_seconds`` > 0 is a defer on Pull only phases."""
        if delay_seconds and await self.is_pull_async():
            return await asyncio.to_thread(lambda: self._operations.defer_item(item_id, delay_seconds=delay_seconds))
        return await asyncio.to_thread(
            lambda: self._operations.release_item(
                item_id,
                data,
                delay_seconds=delay_seconds,
                **fields,
            )
        )

    async def nack_transaction(
        self,
        transaction: Transaction,
        requeue: bool = True,
        delay_seconds: Optional[int] = None,
        *,
        reason: Optional[str] = None,
        details: Optional[str] = None,
    ) -> None:
        """Hand the item back after a failure; see :meth:`ErgonPlatformConnector.nack_transaction`."""
        if requeue:
            if await self.is_pull_async():
                await asyncio.to_thread(
                    lambda: self._operations.fail_item(
                        transaction.id,
                        reason=reason or DEFAULT_NACK_REASON,
                        delay_seconds=delay_seconds,
                        details=details,
                    )
                )
                return
            resolved_delay = DEFAULT_NACK_RELEASE_DELAY_SECONDS if delay_seconds is None else delay_seconds
            await asyncio.to_thread(lambda: self._operations.release_item(transaction.id, delay_seconds=resolved_delay))
            return

        target_phase = self._consumer_config.nack_phase_id if self._consumer_config is not None else None
        if not target_phase:
            raise ValueError("nack_phase_id is required when nack_transaction is called with requeue=False")

        await asyncio.to_thread(lambda: self.client.workflows.items.route(transaction.id, to_phase_id=target_phase))

    # -- Pull only outcomes ----------------------------------------------------

    async def complete_transaction(self, transaction: Transaction, *, reason: Optional[str] = None) -> Any:
        await self.is_pull_async()
        return await asyncio.to_thread(lambda: self._operations.complete_item(transaction.id, reason=reason))

    async def fail_transaction(
        self,
        transaction: Transaction,
        *,
        reason: str,
        delay_seconds: Optional[int] = None,
        details: Optional[str] = None,
    ) -> Any:
        await self.is_pull_async()
        return await asyncio.to_thread(
            lambda: self._operations.fail_item(
                transaction.id, reason=reason, delay_seconds=delay_seconds, details=details
            )
        )

    async def defer_transaction(
        self, transaction: Transaction, *, delay_seconds: int, reason: Optional[str] = None
    ) -> Any:
        await self.is_pull_async()
        return await asyncio.to_thread(
            lambda: self._operations.defer_item(transaction.id, delay_seconds=delay_seconds, reason=reason)
        )

    async def extend_claim(self, transaction: Transaction, *, seconds: Optional[int] = None) -> Any:
        await self.is_pull_async()
        return await asyncio.to_thread(lambda: self._operations.extend_claim(transaction.id, seconds=seconds))

    async def close(self) -> None:
        await asyncio.to_thread(self.client.close)

    async def list_phase_fields(
        self,
        phase_id: str,
        *,
        workflow_id: Optional[str] = None,
        include_workflow_fields: bool = True,
        **params: Any,
    ) -> Any:
        return await asyncio.to_thread(
            lambda: self._operations.list_phase_fields(
                phase_id,
                workflow_id=workflow_id,
                include_workflow_fields=include_workflow_fields,
                **params,
            )
        )

    async def get_pipeline_result(
        self,
        workflow_id: str,
        item_id: str,
        field_id: str,
        buckets_file_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self._operations.get_pipeline_result,
            workflow_id,
            item_id,
            field_id,
            buckets_file_id,
        )

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

        return await asyncio.to_thread(
            lambda: self._operations.create_item(
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
        )

    @staticmethod
    def _created_item_id(result: Any) -> str:
        if isinstance(result, dict) and "item" in result:
            return str(get_value(result["item"], "id", ""))
        return str(get_value(result, "id", ""))
