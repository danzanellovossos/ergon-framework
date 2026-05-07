from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

from .helper import headers_generator
from .models import RabbitmqClient

logger = logging.getLogger(__name__)


class AsyncRabbitMQService:
    """Async RabbitMQ service using aio-pika."""

    def __init__(self, client: RabbitmqClient) -> None:
        self.client = client
        self._connection: Optional[aio_pika.RobustConnection] = None
        self._channel: Optional[aio_pika.RobustChannel] = None

    async def _ensure_connection(self) -> None:
        if self._connection and not self._connection.is_closed:
            return

        self._connection = await aio_pika.connect_robust(
            host=self.client.host,
            port=self.client.port,
            virtualhost=self.client.virtual_host or "/",
            login=self.client.username,
            password=self.client.password,
        )
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=self.client.prefetch_count)

    async def declare_queue(self, queue_name: str, durable: bool = True) -> None:
        await self._ensure_connection()
        assert self._channel is not None
        await self._channel.declare_queue(queue_name, durable=durable)

    async def close(self) -> None:
        if self._connection and not self._connection.is_closed:
            await self._connection.close()

    # ---------- Consumo ----------

    async def consume(
        self,
        queue_name: str,
        auto_ack: bool = False,
        batch_size: int = 1,
        timeout: float = 2.0,
    ) -> List[Dict[str, Any]]:
        await self._ensure_connection()
        assert self._channel is not None

        queue = await self._channel.get_queue(queue_name)

        messages: List[Dict[str, Any]] = []

        async with queue.iterator() as queue_iter:
            async for message in queue_iter:
                async with message.process(requeue=True):
                    try:
                        body = message.body.decode("utf-8") if message.body else None
                        try:
                            data = json.loads(body) if body else None
                        except (json.JSONDecodeError, TypeError):
                            data = body

                        messages.append({
                            "data": data,
                            "routing_key": message.routing_key,
                            "delivery_tag": message.delivery_tag,
                            "headers": dict(message.headers) if message.headers else {},
                        })

                        if not auto_ack:
                            await message.ack()

                    except Exception:
                        if not auto_ack:
                            await message.nack(requeue=True)

                if len(messages) >= batch_size:
                    break

        return messages

    async def ack_message(self, delivery_tag: int) -> None:
        logger.info("Acking message with delivery_tag=%s (no-op in async iterator mode)", delivery_tag)

    # ---------- Publicação ----------

    async def publish(
        self,
        queue_name: str,
        body: Any,
        headers: Optional[Dict[str, Any]] = None,
    ) -> None:
        await self._ensure_connection()
        assert self._channel is not None

        await self._channel.declare_queue(queue_name, durable=True)

        if not isinstance(body, (str, bytes)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")

        msg_headers = headers or headers_generator(id=None, source="ergon")

        await self._channel.default_exchange.publish(
            aio_pika.Message(body=body, headers=msg_headers),
            routing_key=queue_name,
        )
        logger.info("Mensagem publicada na fila %s", queue_name)
