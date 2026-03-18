"""SQS connector for ergon-framework."""

import json
import logging
from typing import Any, Dict, List, Optional

from ..connector import Connector
from ..transaction import Transaction

from .models import SQSClient
from .service import SQSService

logger = logging.getLogger(__name__)


class SQSConnector(Connector):
    """
    Connector for consuming and producing messages on AWS SQS queues.

    Wraps SQSService and exposes the standard ergon Connector interface
    (fetch_transactions / dispatch_transactions), plus a delete_message
    helper equivalent to RabbitMQ's ack.
    """

    service: SQSService

    def __init__(self, client: SQSClient) -> None:
        self.service = SQSService(client)
        self.default_queue_url = client.queue_url

    # ---------- Fetch (consume) ----------

    def fetch_transactions(
        self,
        batch_size: int = 1,
        queue_url: Optional[str] = None,
        metadata: Optional[dict] = None,
        *args,
        **kwargs,
    ) -> List[Transaction]:
        """
        Receive up to ``batch_size`` messages and return them as Transactions.

        Each Transaction has:
        - id: the SQS MessageId
        - payload: dict with keys data, receipt_handle, message_id,
                   attributes, message_attributes
        """
        messages = self.service.receive(
            queue_url=queue_url,
            max_messages=batch_size,
        )

        if not messages:
            return []

        return [
            Transaction(
                id=msg["message_id"],
                payload=msg,
                metadata=metadata or {},
            )
            for msg in messages
        ]

    # ---------- Dispatch (produce) ----------

    def dispatch_transactions(
        self,
        transactions: List[Transaction],
        *args,
        **kwargs,
    ) -> None:
        """
        Publish each Transaction as a message on SQS.

        The transaction payload is expected to follow the same wrapper
        convention used by RabbitMQ (payload.payload with queue_url/body),
        or be a plain dict/string that will be JSON-serialized.
        """
        for tx in transactions:
            payload = tx.payload

            queue_url: Optional[str] = None
            body: str
            msg_attrs: Dict[str, Any] = {}
            group_id: Optional[str] = None
            dedup_id: Optional[str] = None

            # Support the RabbitMQMessageWrapper-style payload (has .payload attr)
            inner = getattr(payload, "payload", None)
            if isinstance(inner, dict):
                queue_url = inner.get("queue_name") or inner.get("queue_url")
                raw_body = inner.get("body", "")
                body = raw_body if isinstance(raw_body, str) else json.dumps(raw_body, ensure_ascii=False)
                msg_attrs = inner.get("message_attributes", {})
                group_id = inner.get("message_group_id")
                dedup_id = inner.get("message_deduplication_id")
            elif isinstance(payload, dict):
                queue_url = payload.get("queue_name") or payload.get("queue_url")
                raw_body = payload.get("body", payload)
                body = raw_body if isinstance(raw_body, str) else json.dumps(raw_body, ensure_ascii=False)
                msg_attrs = payload.get("message_attributes", {})
                group_id = payload.get("message_group_id")
                dedup_id = payload.get("message_deduplication_id")
            else:
                body = str(payload)

            self.service.send(
                body=body,
                queue_url=queue_url,
                message_attributes=msg_attrs or None,
                message_group_id=group_id,
                message_deduplication_id=dedup_id,
            )

    # ---------- Delete (ack equivalent) ----------

    def delete_message(
        self,
        receipt_handle: str,
        queue_url: Optional[str] = None,
    ) -> None:
        """
        Delete a message from the queue after successful processing.

        This is the SQS equivalent of RabbitMQ's ack_message.
        """
        self.service.delete(
            receipt_handle=receipt_handle,
            queue_url=queue_url,
        )
