import json
import logging
from typing import Any, Dict, List, Optional

from ..connector import AsyncConnector
from ..transaction import Transaction
from .async_service import AsyncRabbitMQService
from .models import AsyncRabbitmqClient, AsyncRabbitmqConsumerConfig, AsyncRabbitmqProducerConfig

logger = logging.getLogger(__name__)


class AsyncRabbitMQConnector(AsyncConnector):
    service: AsyncRabbitMQService

    def __init__(
        self,
        client: AsyncRabbitmqClient,
        consumer_config: Optional[AsyncRabbitmqConsumerConfig] = None,
        producer_config: Optional[AsyncRabbitmqProducerConfig] = None,
    ) -> None:
        self.service = AsyncRabbitMQService(client)
        self._consumer_config = consumer_config
        self._producer_config = producer_config or AsyncRabbitmqProducerConfig()

    async def fetch_transactions_async(
        self,
        batch_size: int = 1,
        queue_name: Optional[str] = None,
        exchange_name: Optional[str] = None,
        binding_keys: Optional[List[str]] = None,
        *args,
        **kwargs,
    ) -> List[Transaction]:
        if self._consumer_config is None:
            raise ValueError("AsyncRabbitMQConnector requires a consumer_config to fetch transactions")

        config = self._consumer_config
        if queue_name or exchange_name or binding_keys:
            overrides: Dict[str, Any] = {}
            if queue_name is not None:
                overrides["queue_name"] = queue_name
            if exchange_name is not None:
                overrides["exchange_name"] = exchange_name
            if binding_keys is not None:
                overrides["binding_keys"] = binding_keys
            config = config.model_copy(update=overrides)

        messages = await self.service.consume(config, batch_size)

        if not messages:
            return []

        transactions = []
        for msg in messages:
            delivery_tag = msg.get("delivery_tag")

            transactions.append(
                Transaction(
                    id=str(delivery_tag),
                    payload=msg.get("body"),
                    metadata={
                        "delivery_tag": delivery_tag,
                        "routing_key": msg.get("routing_key", ""),
                        "headers": msg.get("headers", {}),
                        "content_type": msg.get("content_type"),
                        "message_id": msg.get("message_id"),
                        "correlation_id": msg.get("correlation_id"),
                        "queue_name": config.queue_name,
                        "_message": msg.get("_message"),
                    },
                )
            )

        return transactions

    async def dispatch_transactions_async(
        self,
        transactions: List[Transaction],
        routing_key: Optional[str] = None,
        exchange_name: Optional[str] = None,
        headers: Optional[Dict[str, Any]] = None,
        *args,
        **kwargs,
    ) -> None:
        config = self._producer_config
        if exchange_name:
            config = config.model_copy(update={"exchange_name": exchange_name})

        for txn in transactions:
            body = txn.payload
            if not isinstance(body, (str, bytes)):
                body = json.dumps(body, default=str)
            if isinstance(body, str):
                body = body.encode("utf-8")

            rk = routing_key or (txn.metadata.get("routing_key") if txn.metadata else None) or config.routing_key
            msg_headers = headers or (txn.metadata.get("headers") if txn.metadata else None)

            await self.service.publish(
                config=config,
                body=body,
                routing_key=rk,
                headers=msg_headers,
            )

    async def ack_transaction(self, transaction: Transaction) -> None:
        raw_message = transaction.metadata.get("_message") if transaction.metadata else None
        if raw_message is None:
            raise ValueError(f"Cannot ack transaction {transaction.id}: no raw message in metadata")
        await self.service.ack(raw_message)

    async def nack_transaction(self, transaction: Transaction, requeue: bool = True) -> None:
        raw_message = transaction.metadata.get("_message") if transaction.metadata else None
        if raw_message is None:
            raise ValueError(f"Cannot nack transaction {transaction.id}: no raw message in metadata")
        await self.service.nack(raw_message, requeue=requeue)

    async def close(self) -> None:
        await self.service.close()
