import asyncio
import json
import logging
import ssl as ssl_module
from typing import Any, Callable, Dict, List, Optional

import aio_pika
import aio_pika.exceptions
import aiormq.exceptions
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractRobustConnection,
)

from .models import AsyncRabbitmqClient, AsyncRabbitmqConsumerConfig, AsyncRabbitmqProducerConfig

logger = logging.getLogger(__name__)


# Exception classes that signal the underlying channel is no longer usable.
# We catch these on ack/nack and surface a typed DeadChannelError so the
# consumer mixin can short-circuit cleanly instead of cascading further
# failures (nack on the same dead channel).
_DEAD_CHANNEL_EXCEPTIONS: tuple[type[BaseException], ...] = (
    aio_pika.exceptions.MessageProcessError,
    aio_pika.exceptions.ChannelClosed,
    aio_pika.exceptions.ChannelInvalidStateError,
    aiormq.exceptions.ChannelInvalidStateError,
)


class AsyncRabbitMQService:
    def __init__(self, client: AsyncRabbitmqClient) -> None:
        self.client = client

        self._connection: Optional[AbstractRobustConnection] = None
        # Separate channels for consume and publish so a consumer-side outage
        # (e.g. Basic.Cancel from broker after a force-recreate) cannot poison
        # the publish path, and vice-versa.
        self._consume_channel: Optional[AbstractChannel] = None
        self._publish_channel: Optional[AbstractChannel] = None
        # Caches keyed off the consume channel; invalidated whenever the
        # consume channel is closed / cancelled so the next consume() rebuilds
        # the subscription on a fresh channel and the broker redelivers any
        # in-flight prefetch.
        self._exchanges: Dict[str, AbstractExchange] = {}
        self._queues: Dict[str, AbstractQueue] = {}

    # ---------- Connection / Channel ----------

    async def _get_connection(self) -> AbstractRobustConnection:
        if self._connection is None or self._connection.is_closed:
            url = self.client.get_url()

            kwargs: Dict[str, Any] = {}
            if self.client.ssl_enabled:
                ctx = ssl_module.create_default_context()
                if self.client.ssl_ca_certs:
                    ctx.load_verify_locations(self.client.ssl_ca_certs)
                kwargs["ssl"] = True
                kwargs["ssl_context"] = ctx

            self._connection = await aio_pika.connect_robust(
                url,
                heartbeat=self.client.heartbeat,
                **kwargs,
            )
            logger.info("Connected to RabbitMQ at %s", url.split("@")[-1] if "@" in url else url)

        return self._connection

    def _invalidate_consume_channel(self, reason: str = "explicit invalidation") -> None:
        """Drop cached consume channel + queue/exchange handles.

        Called when the consume channel is closed by the broker (e.g. after
        a Basic.Cancel during force-recreate) or when an ack/nack discovers
        the channel is dead. The next ``consume()`` call will then rebuild
        the subscription on a fresh channel, which causes the broker to
        redeliver any messages that were stuck in the previous prefetch
        buffer to the dead consumer tag.
        """
        if self._consume_channel is None and not self._queues and not self._exchanges:
            return
        logger.warning("Invalidating consume channel cache (%s)", reason)
        self._consume_channel = None
        self._queues.clear()
        self._exchanges.clear()

    def _on_consume_channel_close(self, *args: Any, **kwargs: Any) -> None:
        """Callback registered with ``channel.add_close_callback``.

        ``aio_pika`` invokes close callbacks with varying signatures across
        versions; accept ``*args, **kwargs`` to stay compatible.
        """
        exc = args[1] if len(args) >= 2 else kwargs.get("exc")
        reason = f"channel closed: {exc!r}" if exc is not None else "channel closed"
        self._invalidate_consume_channel(reason)

    async def _get_consume_channel(self, prefetch_count: Optional[int] = None) -> AbstractChannel:
        if self._consume_channel is None or self._consume_channel.is_closed:
            connection = await self._get_connection()
            self._consume_channel = await connection.channel()
            # ``add_close_callback`` is not part of the abstract interface
            # but is provided by the concrete ``Channel`` class. Older
            # aio_pika versions and test mocks may not expose it, so we
            # access via getattr and skip silently when absent — the
            # consume() path also self-heals via is_closed checks.
            register_close = getattr(self._consume_channel, "add_close_callback", None)
            if callable(register_close):
                try:
                    register_close(self._on_consume_channel_close)
                except TypeError:
                    logger.debug("Consume channel close-callback registration rejected; skipping")
            else:
                logger.debug("Consume channel does not support add_close_callback; skipping registration")
            if prefetch_count is not None:
                await self._consume_channel.set_qos(prefetch_count=prefetch_count)
                logger.debug("Consume channel QoS set to prefetch_count=%d", prefetch_count)

        return self._consume_channel

    async def _get_publish_channel(self) -> AbstractChannel:
        if self._publish_channel is None or self._publish_channel.is_closed:
            connection = await self._get_connection()
            self._publish_channel = await connection.channel()

        return self._publish_channel

    # Backwards-compatible alias preserved for any external callers / tests
    # that referenced the original single-channel accessor. Defaults to the
    # consume channel since that was the original behaviour for consume().
    async def _get_channel(self, prefetch_count: Optional[int] = None) -> AbstractChannel:
        return await self._get_consume_channel(prefetch_count=prefetch_count)

    # ---------- Declarations ----------

    async def declare_exchange(
        self,
        name: str,
        exchange_type: str = "topic",
        durable: bool = True,
    ) -> AbstractExchange:
        if name in self._exchanges:
            return self._exchanges[name]

        channel = await self._get_consume_channel()
        ex_type = aio_pika.ExchangeType(exchange_type)
        exchange = await channel.declare_exchange(name, ex_type, durable=durable)
        self._exchanges[name] = exchange
        logger.debug("Declared exchange: name=%s type=%s durable=%s", name, exchange_type, durable)
        return exchange

    async def declare_queue(
        self,
        name: str,
        durable: bool = True,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> AbstractQueue:
        # Cache key includes ``arguments`` so two configs that target the same
        # queue name but with different x-arguments (e.g. different DLX wiring)
        # cannot silently share a cached handle. Without this, the second call
        # would get the first declaration's queue object back regardless of its
        # ``arguments`` payload — masking misconfiguration in tests and dev.
        cache_key = self._queue_cache_key(name, arguments)
        if cache_key in self._queues:
            return self._queues[cache_key]

        channel = await self._get_consume_channel()
        queue = await channel.declare_queue(name, durable=durable, arguments=arguments)
        self._queues[cache_key] = queue
        logger.debug(
            "Declared queue: name=%s durable=%s arguments=%s",
            name,
            durable,
            arguments or {},
        )
        return queue

    @staticmethod
    def _queue_cache_key(name: str, arguments: Optional[Dict[str, Any]]) -> str:
        if not arguments:
            return name
        # Sort for stable hashing regardless of insertion order; values are
        # rendered via repr to handle non-hashable values (lists, dicts) that
        # AMQP allows in the x-arguments table.
        rendered = ",".join(f"{k}={arguments[k]!r}" for k in sorted(arguments))
        return f"{name}#{rendered}"

    async def bind_queue(
        self,
        queue: AbstractQueue,
        exchange: AbstractExchange,
        routing_key: str,
    ) -> None:
        await queue.bind(exchange, routing_key=routing_key)
        logger.debug("Bound queue=%s to exchange=%s with key=%s", queue.name, exchange.name, routing_key)

    # ---------- Consume ----------

    async def consume(
        self,
        config: AsyncRabbitmqConsumerConfig,
        batch_size: int = 1,
    ) -> List[Dict[str, Any]]:
        """
        Fetch up to batch_size messages from the configured queue.

        Declares exchange/queue/bindings on first call, then iterates the
        queue collecting messages until batch_size is reached or
        consume_timeout elapses.

        Returns raw message dicts without acknowledging — the caller
        is responsible for ack/nack via the delivery_tag in metadata.
        """
        # Ensure the consume channel exists with the requested prefetch QoS.
        # The return value is unused here because declare_exchange/declare_queue
        # re-fetch the channel from the cache.
        await self._get_consume_channel(prefetch_count=config.prefetch_count)

        if config.exchange_name:
            exchange = await self.declare_exchange(
                config.exchange_name,
                config.exchange_type,
                durable=config.durable,
            )
        else:
            exchange = None

        queue = await self.declare_queue(
            config.queue_name,
            durable=config.durable,
            arguments=config.queue_arguments or None,
        )

        if exchange is not None:
            for key in config.binding_keys:
                await self.bind_queue(queue, exchange, key)

        buffer: List[Dict[str, Any]] = []
        timeout = config.consume_timeout

        try:
            async with asyncio.timeout(timeout):  # type: ignore[attr-defined]
                async with queue.iterator(no_ack=config.auto_ack) as iterator:
                    self._register_consumer_cancel_callback(iterator)
                    async for message in iterator:
                        msg_dict = self._message_to_dict(message)
                        buffer.append(msg_dict)
                        if len(buffer) >= batch_size:
                            break
        except TimeoutError:
            pass
        except _DEAD_CHANNEL_EXCEPTIONS as exc:
            # Broker cancelled this subscription mid-iteration; invalidate
            # the cached channel so the next call rebuilds against a fresh
            # consumer and the broker redelivers what we had prefetched.
            self._invalidate_consume_channel(f"consume aborted: {exc!r}")
            return buffer
        finally:
            # If the channel was closed during iteration, make sure we don't
            # hand back a stale cache to the next caller.
            if self._consume_channel is not None and self._consume_channel.is_closed:
                self._invalidate_consume_channel("consume channel observed closed after iteration")

        return buffer

    def _register_consumer_cancel_callback(self, iterator: Any) -> None:
        """Best-effort registration of an on-cancel callback on the iterator's consumer.

        ``aio_pika`` does not expose a stable public API for this across
        versions; we probe for known attributes and fall back silently.
        The channel close callback in :meth:`_get_consume_channel` provides
        the primary defence — this is belt-and-braces.
        """
        candidate: Optional[Callable[[Callable[..., Any]], Any]] = None
        for attr in ("add_on_cancel_callback", "add_close_callback"):
            consumer_obj = getattr(iterator, "_consumer", None) or getattr(iterator, "consumer", None)
            if consumer_obj is not None:
                candidate = getattr(consumer_obj, attr, None)
                if candidate is not None:
                    break
            candidate = getattr(iterator, attr, None)
            if candidate is not None:
                break

        if candidate is None:
            return

        def _on_cancel(*_args: Any, **_kwargs: Any) -> None:
            self._invalidate_consume_channel("consumer cancelled by broker (Basic.Cancel)")

        try:
            candidate(_on_cancel)
        except (TypeError, AttributeError):
            logger.debug("Iterator consumer does not accept cancel callback; skipping")

    @staticmethod
    def _message_to_dict(message: AbstractIncomingMessage) -> Dict[str, Any]:
        try:
            body = json.loads(message.body.decode("utf-8"))
        except Exception:
            body = message.body

        return {
            "body": body,
            "routing_key": message.routing_key or "",
            "delivery_tag": message.delivery_tag,
            "headers": dict(message.headers) if message.headers else {},
            "content_type": message.content_type,
            "message_id": message.message_id,
            "correlation_id": message.correlation_id,
            "_message": message,
        }

    # ---------- Publish ----------

    async def publish(
        self,
        config: AsyncRabbitmqProducerConfig,
        body: bytes,
        routing_key: Optional[str] = None,
        headers: Optional[Dict[str, Any]] = None,
    ) -> None:
        channel = await self._get_publish_channel()

        if config.exchange_name:
            exchange = await self._declare_publish_exchange(
                channel,
                config.exchange_name,
                config.exchange_type,
                durable=config.durable,
            )
        else:
            exchange = channel.default_exchange

        rk = routing_key or config.routing_key

        delivery_mode = (
            aio_pika.DeliveryMode.PERSISTENT if config.delivery_mode == 2 else aio_pika.DeliveryMode.NOT_PERSISTENT
        )

        message = aio_pika.Message(
            body=body,
            content_type=config.content_type,
            delivery_mode=delivery_mode,
            headers=headers,
        )

        await exchange.publish(message, routing_key=rk)
        logger.debug("Published message to exchange=%s routing_key=%s", config.exchange_name or "(default)", rk)

    async def _declare_publish_exchange(
        self,
        channel: AbstractChannel,
        name: str,
        exchange_type: str,
        durable: bool,
    ) -> AbstractExchange:
        # Publish exchanges live on the publish channel and are not cached
        # alongside consume-side declarations; declare-on-demand is cheap
        # because RabbitMQ treats matching declarations as idempotent.
        ex_type = aio_pika.ExchangeType(exchange_type)
        return await channel.declare_exchange(name, ex_type, durable=durable)

    # ---------- Ack / Nack ----------

    async def ack(self, message: AbstractIncomingMessage) -> None:
        # Imported lazily to avoid a circular import between
        # ergon.connector and ergon.task at package init time.
        from ...task import exceptions as task_exceptions

        try:
            await message.ack()
        except _DEAD_CHANNEL_EXCEPTIONS as exc:
            self._invalidate_consume_channel(f"ack failed: {exc!r}")
            raise task_exceptions.AckOnDeadChannelError(
                delivery_tag=getattr(message, "delivery_tag", None),
                queue=getattr(message, "routing_key", None),
                cause=exc,
            ) from exc

    async def nack(self, message: AbstractIncomingMessage, requeue: bool = True) -> None:
        from ...task import exceptions as task_exceptions

        try:
            await message.nack(requeue=requeue)
        except _DEAD_CHANNEL_EXCEPTIONS as exc:
            self._invalidate_consume_channel(f"nack failed: {exc!r}")
            raise task_exceptions.NackOnDeadChannelError(
                delivery_tag=getattr(message, "delivery_tag", None),
                queue=getattr(message, "routing_key", None),
                cause=exc,
            ) from exc

    # ---------- Lifecycle ----------

    async def close(self) -> None:
        self._exchanges.clear()
        self._queues.clear()

        for attr_name in ("_consume_channel", "_publish_channel"):
            channel = getattr(self, attr_name)
            if channel is not None and not channel.is_closed:
                try:
                    await channel.close()
                except Exception as exc:
                    logger.warning("Error closing %s: %r", attr_name, exc)
            setattr(self, attr_name, None)

        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()
            self._connection = None

        logger.info("RabbitMQ connection closed")
