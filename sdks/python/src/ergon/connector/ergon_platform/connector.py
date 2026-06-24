import logging
from typing import Any, Dict, List, Optional

from ..connector import Connector
from ..transaction import Transaction
from ._client import create_ergon_client
from ._operations import _ErgonPlatformOperations
from .models import (
    CreateItemPayload,
    ErgonPlatformClient,
    ErgonPlatformConsumerConfig,
    ErgonPlatformProducerConfig,
)
from .utils import get_value, normalize_create_payload

logger = logging.getLogger(__name__)


class ErgonPlatformConnector(Connector):
    def __init__(
        self,
        client: ErgonPlatformClient,
        consumer_config: Optional[ErgonPlatformConsumerConfig] = None,
        producer_config: Optional[ErgonPlatformProducerConfig] = None,
    ) -> None:
        self.client = create_ergon_client(client)
        self._operations = _ErgonPlatformOperations(client, self.client)
        self._consumer_config = consumer_config
        self._producer_config = producer_config or ErgonPlatformProducerConfig()

    def fetch_transactions(
        self,
        batch_size: Optional[int] = None,
        *args,
        **kwargs,
    ) -> List[Transaction]:
        if self._consumer_config is None:
            raise ValueError("ErgonPlatformConnector requires a consumer_config to fetch transactions")

        config = self._consumer_config
        limit = batch_size or config.batch_size
        params: Dict[str, Any] = {**config.list_params, **kwargs}
        if config.unassigned:
            params.pop("assigned_to", None)
            params["assigned"] = "no"

        transactions = self._operations.fetch_items(
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
                self.client.workflows.items.claim(transaction.id, {})
                claimed_transactions.append(self.fetch_transaction_by_id(transaction.id))
            except Exception:
                logger.warning(
                    "Failed to claim item %s during unassigned fetch; skipping it",
                    transaction.id,
                    exc_info=True,
                )
        return claimed_transactions

    def dispatch_transactions(self, transactions: List[Transaction], *args, **kwargs) -> List[str]:
        created_ids: List[str] = []
        for transaction in transactions:
            result = self._create_from_payload(transaction.payload)
            created_ids.append(self._created_item_id(result))
        return created_ids

    def fetch_child_transactions(
        self,
        parent_item_id: str,
        *args,
        **kwargs,
    ) -> List[Transaction]:
        return self._operations.fetch_child_items(parent_item_id, **kwargs)

    def fetch_transaction_by_id(self, transaction_id: str, *args, **kwargs) -> Transaction:
        workflow_id = self._consumer_config.workflow_id if self._consumer_config else ""
        return self._operations.get_item_transaction(transaction_id, workflow_id, **kwargs)

    def fetch_items_by_query(
        self, workflow_id: str, query: Optional[Dict[str, Any]] = None, **fields: Any
    ) -> List[Transaction]:
        return self._operations.fetch_items_by_query(workflow_id, query, **fields)

    def get_transactions_count(self, *args, **kwargs) -> int:
        if self._consumer_config is None:
            raise ValueError("ErgonPlatformConnector requires a consumer_config to count transactions")

        config = self._consumer_config
        params: Dict[str, Any] = {**config.list_params, **kwargs}
        return self._operations.get_phase_items_count(
            config.workflow_id,
            config.phase_id,
            **params,
        )

    def ack_transaction(self, transaction: Transaction, phase_id: Optional[str] = None) -> None:
        target_phase = phase_id
        if target_phase is None and self._consumer_config is not None:
            target_phase = self._consumer_config.ack_phase_id
        if not target_phase:
            return
        self.client.workflows.items.route(transaction.id, to_phase_id=target_phase)

    def release_item(
        self,
        item_id: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        delay_seconds: Optional[int] = None,
        **fields: Any,
    ) -> Any:
        return self._operations.release_item(
            item_id,
            data,
            delay_seconds=delay_seconds,
            **fields,
        )

    def nack_transaction(self, transaction: Transaction, requeue: bool = True, delay_seconds: int = 10) -> None:
        if requeue:
            self._operations.release_item(transaction.id, delay_seconds=delay_seconds)
            return

        target_phase = self._consumer_config.nack_phase_id if self._consumer_config is not None else None
        if not target_phase:
            raise ValueError("nack_phase_id is required when nack_transaction is called with requeue=False")

        self.client.workflows.items.route(transaction.id, to_phase_id=target_phase)
        logger.debug("Item %s moved to nack phase %s", transaction.id, target_phase)

    def close(self) -> None:
        self.client.close()

    def list_phase_fields(
        self,
        phase_id: str,
        *,
        workflow_id: Optional[str] = None,
        include_workflow_fields: bool = True,
        **params: Any,
    ) -> Any:
        return self._operations.list_phase_fields(
            phase_id,
            workflow_id=workflow_id,
            include_workflow_fields=include_workflow_fields,
            **params,
        )

    def get_pipeline_result(
        self,
        workflow_id: str,
        item_id: str,
        field_id: str,
        buckets_file_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._operations.get_pipeline_result(workflow_id, item_id, field_id, buckets_file_id)

    def _create_from_payload(self, payload: CreateItemPayload) -> Any:
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

        return self._operations.create_item(
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
