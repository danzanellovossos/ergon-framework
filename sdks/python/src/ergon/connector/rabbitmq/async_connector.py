from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ergon.connector import Transaction
from ergon.connector.connector import AsyncConnector

from .models import RabbitmqClient
from .async_service import AsyncRabbitMQService

logger = logging.getLogger(__name__)


class AsyncRabbitMQConnector(AsyncConnector):
    """Async RabbitMQ connector using aio-pika."""

    service: AsyncRabbitMQService

    def __init__(
        self,
        client: RabbitmqClient,
        default_queue: Optional[str] = None,
        auto_ack: bool = False,
    ) -> None:
        self.service = AsyncRabbitMQService(client)
        self.default_queue = default_queue or client.queue_name
        self.auto_ack = auto_ack

    async def fetch_transactions_async(
        self,
        batch_size: int = 1,
        queue_name: Optional[str] = None,
        auto_ack: bool = False,
        *args,
        **kwargs,
    ) -> List[Transaction]:
        q = queue_name or self.default_queue

        try:
            await self.service.declare_queue(q)
        except Exception:
            logger.debug("Queue %s may already exist; continuing.", q)

        queue_items = await self.service.consume(
            queue_name=q,
            auto_ack=auto_ack or self.auto_ack,
            batch_size=batch_size,
        )

        if not queue_items:
            return []

        transactions: List[Transaction] = []
        for item in queue_items:
            payload = {
                "Body": json.dumps(item["data"]) if not isinstance(item["data"], str) else item["data"],
                "queue_name": q,
                "delivery_tag": item["delivery_tag"],
            }
            metadata = {
                "delivery_tag": item["delivery_tag"],
                "queue_name": q,
                "routing_key": item["routing_key"],
            }
            transactions.append(
                Transaction(id=str(item["delivery_tag"]), payload=payload, metadata=metadata)
            )

        return transactions

    async def dispatch_transactions_async(
        self,
        transactions: List[Transaction],
        queue_name: Optional[str] = None,
        *args,
        **kwargs,
    ) -> None:
        q = queue_name or self.default_queue
        for txn in transactions:
            body = txn.payload
            if isinstance(body, dict):
                body = json.dumps(body, ensure_ascii=False)
            await self.service.publish(queue_name=q, body=body)

    async def get_transactions_count_async(self, *args, **kwargs) -> int:
        raise NotImplementedError
