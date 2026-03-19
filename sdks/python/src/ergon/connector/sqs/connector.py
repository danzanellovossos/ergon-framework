import json
import logging
from typing import Any, Dict, List, Optional

from ..connector import Connector
from ..transaction import Transaction
from .models import SQSClient, SQSConsumerConfig, SQSProducerConfig
from .service import SQSService

logger = logging.getLogger(__name__)


class SQSConnector(Connector):
    service: SQSService

    def __init__(
        self,
        client: SQSClient,
        consumer_config: Optional[SQSConsumerConfig] = None,
        producer_config: Optional[SQSProducerConfig] = None,
    ) -> None:
        self.service = SQSService(client)
        self._consumer_config = consumer_config or SQSConsumerConfig()
        self._producer_config = producer_config or SQSProducerConfig()

    def fetch_transactions(
        self,
        batch_size: int = 1,
        queue_url: Optional[str] = None,
        wait_time_seconds: Optional[int] = None,
        visibility_timeout: Optional[int] = None,
        attribute_names: Optional[List[str]] = None,
        message_attribute_names: Optional[List[str]] = None,
        *args,
        **kwargs,
    ) -> List[Transaction]:
        cfg = self._consumer_config

        resolved_queue_url = queue_url or cfg.queue_url
        resolved_wait_time = wait_time_seconds if wait_time_seconds is not None else cfg.wait_time_seconds
        resolved_visibility = visibility_timeout if visibility_timeout is not None else cfg.visibility_timeout
        resolved_attr_names = attribute_names or cfg.attribute_names
        resolved_msg_attr_names = message_attribute_names or cfg.message_attribute_names

        messages = self.service.receive_messages(
            queue_url=resolved_queue_url,
            max_number_of_messages=batch_size,
            wait_time_seconds=resolved_wait_time,
            visibility_timeout=resolved_visibility,
            attribute_names=resolved_attr_names,
            message_attribute_names=resolved_msg_attr_names,
        )

        if not messages:
            return []

        effective_queue_url = resolved_queue_url or self.service.client.queue_url

        return [
            Transaction(
                id=msg["MessageId"],
                payload=msg,
                metadata={
                    "receipt_handle": msg["ReceiptHandle"],
                    "queue_url": effective_queue_url,
                },
            )
            for msg in messages
        ]

    def dispatch_transactions(
        self,
        transactions: List[Transaction],
        queue_url: Optional[str] = None,
        delay_seconds: Optional[int] = None,
        message_group_id: Optional[str] = None,
        message_deduplication_id: Optional[str] = None,
        message_attributes: Optional[Dict[str, Dict]] = None,
        *args,
        **kwargs,
    ) -> Dict[str, Any]:
        cfg = self._producer_config

        resolved_queue_url = queue_url or cfg.queue_url
        resolved_delay = delay_seconds if delay_seconds is not None else cfg.delay_seconds
        resolved_group_id = message_group_id or cfg.message_group_id
        resolved_dedup_id = message_deduplication_id or cfg.message_deduplication_id
        resolved_msg_attrs = message_attributes or cfg.message_attributes

        entries: List[Dict[str, Any]] = []
        for i, txn in enumerate(transactions):
            body = txn.payload
            if not isinstance(body, str):
                body = json.dumps(body)

            entry: Dict[str, Any] = {
                "Id": str(i),
                "MessageBody": body,
                "DelaySeconds": resolved_delay,
            }
            if resolved_msg_attrs:
                entry["MessageAttributes"] = resolved_msg_attrs
            if resolved_group_id:
                entry["MessageGroupId"] = resolved_group_id
            if resolved_dedup_id:
                entry["MessageDeduplicationId"] = resolved_dedup_id

            entries.append(entry)

        return self.service.send_message_batch(entries=entries, queue_url=resolved_queue_url)

    def get_transactions_count(self, queue_url: Optional[str] = None, *args, **kwargs) -> int:
        attrs = self.service.get_queue_attributes(
            attribute_names=["ApproximateNumberOfMessages"],
            queue_url=queue_url,
        )
        return int(attrs.get("ApproximateNumberOfMessages", 0))
