import functools
import json
import logging
import ssl as ssl_module
import time
from collections import deque
from typing import Any, Dict, List, Optional

import pika

from .helper import headers_generator
from .models import RabbitmqClient, RabbitmqProducerMessage

logger = logging.getLogger(__name__)


class RabbitMQService:
    def __init__(self, client: RabbitmqClient) -> None:
        self.client = client

        self._connection: Optional[pika.BlockingConnection] = None
        self._channel: Any = None
        self._connect()

    # ---------- Connection / Channel ----------

    def _connect(self) -> None:
        """Ensure an open connection and channel exist."""
        if self._connection and self._connection.is_open:
            return

        ssl_options = None
        if self.client.ssl_enabled:
            ctx = ssl_module.create_default_context()
            if self.client.ssl_ca_certs:
                ctx.load_verify_locations(self.client.ssl_ca_certs)
            ssl_options = pika.SSLOptions(ctx)

        params = pika.ConnectionParameters(
            host=self.client.host,
            port=self.client.port,
            virtual_host=self.client.virtual_host or "/",
            credentials=pika.PlainCredentials(self.client.username, self.client.password),
            connection_attempts=self.client.connection_attempts,
            socket_timeout=self.client.socket_timeout,
            heartbeat=int(self.client.heartbeat) if self.client.heartbeat is not None else None,
            blocked_connection_timeout=self.client.blocked_connection_timeout,
            ssl_options=ssl_options,
        )
        self._connection = pika.BlockingConnection(params)
        self._channel = self._connection.channel()

        self._channel.queue_declare(queue=self.client.queue_name, durable=True)
        self._channel.basic_qos(prefetch_count=self.client.prefetch_count)

    def close(self) -> None:
        """
        Fecha conexão/canal, se estiverem abertos.
        """
        try:
            if self._channel and self._channel.is_open:
                self._channel.close()
        finally:
            if self._connection and self._connection.is_open:
                self._connection.close()

    # ---------- Consumo ----------

    def consume(
        self,
        queue_name,
        auto_ack,
        batch_size=1,
    ) -> List[Dict[str, Any]]:
        """
        Gera mensagens da fila como dicionários.

        Cada item gerado pode conter:
        - payload: corpo da mensagem (json decodado, se possível)
        - routing_key
        - delivery_tag
        - headers
        """
        assert self._connection is not None
        assert self._channel is not None

        q = queue_name or self.client.queue_name

        buffer = deque()

        # Callback interno que vai encher o buffer
        def _internal_callback(ch, method, properties, body):
            try:
                try:
                    payload = json.loads(body.decode("utf-8")) if body else None
                except Exception:
                    payload = body

                if len(buffer) >= batch_size:
                    # Já tenho batch, só requeue ou ignora
                    if not auto_ack:
                        ch.basic_nack(method.delivery_tag, requeue=True)
                    return

                buffer.append(
                    {
                        "data": payload,
                        "routing_key": method.routing_key,
                        "delivery_tag": method.delivery_tag,
                        "headers": getattr(properties, "headers", {}) or {},
                    }
                )

                if auto_ack:
                    ch.basic_ack(method.delivery_tag)

                # Cancela consumo após capturar o batch
                if len(buffer) == batch_size:
                    self._channel.basic_cancel(consumer_tag)
            except Exception:
                if not auto_ack:
                    ch.basic_nack(method.delivery_tag, requeue=True)

        # Registra o callback
        consumer_tag = self._channel.basic_consume(
            queue=q,
            on_message_callback=_internal_callback,
            auto_ack=auto_ack,
        )

        # Loop para acumular mensagens
        start = time.time()
        timeout = 2.0

        try:
            while len(buffer) < batch_size:
                self._connection.process_data_events(time_limit=0.1)  # type: ignore[union-attr, arg-type]
                # se houver menos mensagem que o tamanho do batch sai do loop por timeout
                if time.time() - start > timeout:
                    break
        finally:
            self._channel.basic_cancel(consumer_tag)

        return list(buffer)

    def ack_msg(self, delivery_tag) -> None:
        assert self._channel is not None
        try:
            self._channel.basic_ack(delivery_tag=delivery_tag)
            logger.info(f"sucesso ao marcar como lida. tag {delivery_tag}")
        except Exception as e:
            logger.error("Falha ao ackar a mensagem")
            logger.error(f"{str(e)}")
            self._channel.basic_nack(delivery_tag, requeue=True)

    def confirm_message_received(self, delivery_tag: int) -> None:
        if not isinstance(delivery_tag, int):
            try:
                delivery_tag = int(delivery_tag)
            except ValueError:
                raise ValueError("Delivery tag must be an integer.")

        assert self._connection is not None
        logger.info(f"marcando mensagem como recebida. tag {delivery_tag}")
        ack = functools.partial(self.ack_msg, delivery_tag=delivery_tag)
        self._connection.add_callback_threadsafe(ack)

    # ---------- Publish ----------

    def publish(self, message: RabbitmqProducerMessage, exchange: str = "") -> None:
        if not (self._channel and self._channel.is_open):
            logger.warning("Channel closed, attempting reconnect before publish")
            self._connect()
            if not (self._channel and self._channel.is_open):
                raise ConnectionError("Unable to publish: RabbitMQ channel is closed after reconnect attempt")

        rk = message.payload.get("queue_name") or self.client.queue_name  # type: ignore[attr-defined]

        raw = message.payload.get("body")  # type: ignore[attr-defined]
        if not isinstance(raw, (str, bytes)):
            body = json.dumps(raw)
        elif isinstance(raw, str):
            body = raw.encode("utf-8")
        else:
            body = json.dumps(raw, ensure_ascii=False).encode("utf-8")

        properties = pika.BasicProperties(
            headers=headers_generator(id=message.id, source=message.payload.get("source")),  # type: ignore[attr-defined]
            delivery_mode=message.delivery_mode,
            content_type=message.content_type,
        )

        self._channel.basic_publish(
            exchange=exchange,
            routing_key=rk,
            body=body,
            properties=properties,
        )
        logger.info("Message published successfully")
