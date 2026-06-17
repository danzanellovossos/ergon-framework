import logging
from typing import Any, Dict, List, Optional

from ..connector import Connector
from ..transaction import Transaction
from .models import (
    CreateItemPayload,
    ErgonPlatformClient,
    ErgonPlatformConsumerConfig,
    ErgonPlatformProducerConfig,
)
from .service import ErgonPlatformService
from .utils import get_value, normalize_create_payload

logger = logging.getLogger(__name__)


class ErgonPlatformConnector(Connector):
    service: ErgonPlatformService

    def __init__(
        self,
        client: ErgonPlatformClient,
        consumer_config: Optional[ErgonPlatformConsumerConfig] = None,
        producer_config: Optional[ErgonPlatformProducerConfig] = None,
    ) -> None:
        self.service = ErgonPlatformService(client)
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
        return self.service.fetch_items(
            config.workflow_id,
            config.phase_id,
            limit=limit,
            offset=config.offset,
            **params,
        )

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
        return self.service.fetch_child_items(parent_item_id, **kwargs)

    def fetch_transaction_by_id(self, transaction_id: str, *args, **kwargs) -> Transaction:
        workflow_id = self._consumer_config.workflow_id if self._consumer_config else ""
        return self.service.get_item_transaction(transaction_id, workflow_id, **kwargs)

    def list_workflows(self, *, limit: int = 50, offset: int = 0, **params: Any) -> Any:
        return self.service.list_workflows(limit=limit, offset=offset, **params)

    def list_workflow_phases(self, workflow_id: str, **params: Any) -> Any:
        return self.service.list_workflow_phases(workflow_id, **params)

    def list_phase_fields(self, phase_id: str, **params: Any) -> Any:
        return self.service.list_phase_fields(phase_id, **params)

    def move_item_to_phase(self, item_id: str, phase_id: str) -> Any:
        return self.service.move_item_to_phase(item_id, phase_id)

    def list_item_children(self, item_id: str, **params: Any) -> Any:
        return self.service.list_item_children(item_id, **params)

    def list_item_child_targets(self, item_id: str, **params: Any) -> Any:
        return self.service.list_item_child_targets(item_id, **params)

    def get_item_child_capabilities(self, item_id: str, **params: Any) -> Any:
        return self.service.get_item_child_capabilities(item_id, **params)

    def unlink_item_child(self, item_id: str, child_item_id: str) -> None:
        self.service.unlink_item_child(item_id, child_item_id)

    def get_pipeline_result(
        self,
        workflow_id: str,
        item_id: str,
        field_id: str,
        buckets_file_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.service.get_pipeline_result(workflow_id, item_id, field_id, buckets_file_id)

    def ack_transaction(self, transaction: Transaction, phase_id: Optional[str] = None) -> None:
        target_phase = phase_id
        if target_phase is None and self._consumer_config is not None:
            target_phase = self._consumer_config.ack_phase_id
        if not target_phase:
            return
        self.service.move_item_to_phase(transaction.id, target_phase)

    def nack_transaction(self, transaction: Transaction, requeue: bool = True) -> None:
        logger.debug(
            "nack_transaction is a no-op for Ergon Platform; item %s stays in its current phase",
            transaction.id,
        )

    def close(self) -> None:
        self.service.close()

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

        return self.service.create_item(
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
