import asyncio
import json
import logging
import ssl as ssl_module
import time
from collections.abc import Mapping
from typing import Any, Awaitable, Callable, Dict, List, Optional, cast

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
from async_timeout import timeout as timeout_after

from .models import AsyncRabbitmqClient, AsyncRabbitmqConsumerConfig, AsyncRabbitmqProducerConfig

logger = logging.getLogger(__name__)


# Invalid channel state always means the active transport disappeared. Broader
# AMQP connection hierarchies also contain permanent authentication/protocol
# errors, so consume-time recovery uses the classifier below instead.
_CHANNEL_INVALID_EXCEPTIONS: tuple[type[BaseException], ...] = (
    aio_pika.exceptions.ChannelInvalidStateError,
    aiormq.exceptions.ChannelInvalidStateError,
)


def _is_recoverable_broker_interruption(exc: BaseException) -> bool:
    if isinstance(exc, _CHANNEL_INVALID_EXCEPTIONS):
        return True
    if type(exc) is aiormq.exceptions.AMQPConnectionError:
        return True
    if type(exc) is aiormq.exceptions.ConnectionClosed:
        reply_code = exc.args[0] if exc.args else None
        return reply_code == 320
    if type(exc) is aiormq.exceptions.ConnectionInternalError:
        return True
    return False


# Settlement can additionally discover a channel-level failure after delivery.
# Surface those as DeadChannelError so the consumer mixin does not attempt a
# second settlement on the same unusable channel.
_DEAD_CHANNEL_EXCEPTIONS: tuple[type[BaseException], ...] = (
    aio_pika.exceptions.AMQPConnectionError,
    *_CHANNEL_INVALID_EXCEPTIONS,
    aio_pika.exceptions.MessageProcessError,
    aio_pika.exceptions.ChannelClosed,
)

# Same as above plus ``TimeoutError`` (raised by the async timeout context when an
# ack/nack stalls on a half-open socket). A stalled ack is functionally a dead
# channel: we tear down and let the broker redeliver instead of blocking until
# the heartbeat eventually fires.
_DEAD_CHANNEL_TIMEOUT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    *_DEAD_CHANNEL_EXCEPTIONS,
    TimeoutError,
)


class AsyncRabbitMQService:
    def __init__(self, client: AsyncRabbitmqClient) -> None:
        self.client = client

        self._connection: Optional[AbstractRobustConnection] = None
        self._connection_lock = asyncio.Lock()
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

        # Liveness signals so services can wire a real health check instead of
        # silently running with a zombie/dead consumer. Updated on every
        # successful fetch/ack and reset when the consume channel is torn down.
        self._last_fetch_ts: Optional[float] = None
        self._last_ack_ts: Optional[float] = None
        self._last_settlement_ts: Optional[float] = None
        self._last_poll_started_ts: Optional[float] = None
        self._last_poll_completed_ts: Optional[float] = None
        self._active_consumer_tag: Optional[str] = None
        self._active_consumer_tags: Dict[str, str] = {}
        self._consumer_state = "disconnected"
        self._consumer_epoch = 0
        self._in_flight_message_ids: set[int] = set()
        # AMQP is push-based: one long-lived basic.consume registration per
        # queue feeds this local buffer. Broker QoS bounds the number of
        # unacknowledged deliveries, so fetches only drain memory.
        self._delivery_buffer: asyncio.Queue[tuple[int, Dict[str, Any]]] = asyncio.Queue()
        self._consumer_setup_lock = asyncio.Lock()
        self._consumer_config_signature: Optional[str] = None

    # ---------- Connection / Channel ----------

    async def _get_connection(self) -> AbstractRobustConnection:
        async with self._connection_lock:
            if self._connection is not None and not self._connection.is_closed:
                connected = getattr(self._connection, "connected", None)
                if isinstance(connected, asyncio.Event) and not connected.is_set():
                    self._consumer_state = "reconnecting"
                    try:
                        async with timeout_after(self.client.connect_timeout):
                            await connected.wait()
                    except TimeoutError:
                        self._consumer_state = "connect_stalled"
                        await self._reset_connection("robust reconnect timed out")
                        raise
                return self._connection

            url = self.client.get_url()
            kwargs: Dict[str, Any] = {"reconnect_interval": self.client.reconnect_interval}
            if self.client.ssl_enabled:
                ctx = ssl_module.create_default_context()
                if self.client.ssl_ca_certs:
                    ctx.load_verify_locations(self.client.ssl_ca_certs)
                kwargs["ssl"] = True
                kwargs["ssl_context"] = ctx

            self._consumer_state = "connecting"
            try:
                async with timeout_after(self.client.connect_timeout):
                    connection = await aio_pika.connect_robust(  # type: ignore[call-overload]
                        url,
                        heartbeat=self.client.heartbeat,
                        timeout=self.client.connect_timeout,
                        **kwargs,
                    )
            except TimeoutError:
                self._consumer_state = "connect_stalled"
                raise
            except BaseException:
                self._consumer_state = "disconnected"
                raise

            self._connection = connection
            self._consumer_state = "connected"
            logger.info("Connected to RabbitMQ at %s", url.split("@")[-1] if "@" in url else url)
            return connection

    async def _reset_connection(self, reason: str) -> None:
        """Drop every channel and close a connection that cannot make progress."""
        logger.warning("Resetting RabbitMQ connection (%s)", reason)
        await self._teardown_consume_channel(reason)
        publish_channel = self._publish_channel
        self._publish_channel = None
        await self._close_channel_safely(publish_channel, reason)

        connection = self._connection
        self._connection = None
        await self._close_connection_safely(connection, reason)

    async def _close_connection_safely(
        self,
        connection: Optional[AbstractRobustConnection],
        reason: str,
    ) -> None:
        """Stop transport and robust-reconnect ownership within a fixed deadline.

        ``RobustConnection.is_closed`` only describes its current transport. Its
        background reconnection task can still be alive after the broker closes
        that transport, so ``close()`` must always run for an owned connection.
        """
        if connection is None:
            return
        close_timeout = min(5.0, self.client.connect_timeout)
        try:
            async with timeout_after(close_timeout):
                await connection.close()
        except TimeoutError:
            logger.error(
                "Timed out closing RabbitMQ connection after %.1fs (%s)",
                close_timeout,
                reason,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort reset
            logger.warning("Error closing RabbitMQ connection (%s): %r", reason, exc)

    def _invalidate_consume_channel(self, reason: str = "explicit invalidation") -> None:
        """Drop the consume topology and buffered deliveries.

        Called when the consume channel is closed by the broker (e.g. after
        a Basic.Cancel during force-recreate) or when an ack/nack discovers
        the channel is dead. The next ``consume()`` call will then rebuild
        long-lived subscriptions on a fresh channel. Closing the old channel
        makes RabbitMQ redeliver every unacknowledged buffered message.
        """
        if (
            self._consume_channel is None
            and not self._queues
            and not self._exchanges
            and not self._active_consumer_tags
            and self._delivery_buffer.empty()
        ):
            return
        logger.warning("Invalidating consume channel cache (%s)", reason)
        self._consume_channel = None
        self._queues.clear()
        self._exchanges.clear()
        self._active_consumer_tag = None
        self._active_consumer_tags.clear()
        self._consumer_config_signature = None
        while not self._delivery_buffer.empty():
            try:
                self._delivery_buffer.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _teardown_consume_channel(self, reason: str = "explicit teardown") -> None:
        """Deterministically tear down the consume channel.

        Unlike :meth:`_invalidate_consume_channel` (which only drops Python
        references), this snapshots the live channel, clears the cache, then
        explicitly ``close()``-es the channel. Closing the channel:

        * drops every long-lived consumer registered on it at the broker; and
        * for a ``RobustChannel`` removes it from aio_pika's reconnection set,
          so the robust layer does not silently restore dead consumers on
          the next reconnect.
        """
        channel = self._consume_channel
        self._invalidate_consume_channel(reason)
        self._consumer_epoch += 1
        self._consumer_state = "channel_dead"
        self._in_flight_message_ids.clear()
        await self._close_channel_safely(channel, reason)

    async def _close_channel_safely(self, channel: Optional[AbstractChannel], reason: str) -> None:
        """Best-effort close of a (possibly already-dead) channel."""
        if channel is None:
            return
        try:
            if not channel.is_closed:
                async with timeout_after(min(1.0, self.client.channel_timeout)):
                    await channel.close()
                logger.info("Closed dead consume channel (%s)", reason)
        except Exception as exc:  # noqa: BLE001 - best-effort teardown
            logger.warning("Error closing consume channel during teardown (%s): %r", reason, exc)

    def _schedule_teardown(self, reason: str) -> None:
        """Tear down the consume channel from a sync callback context.

        ``add_close_callback`` / consumer-cancel callbacks are synchronous, so
        we cannot ``await``. We invalidate the cache synchronously (so the next
        consume never sees a stale channel) and, when a running event loop is
        available, schedule the explicit ``close()`` as a task to drop the dead
        channel's consumers at the broker and avoid the channel leak.
        """
        channel = self._consume_channel
        if channel is None:
            return
        self._invalidate_consume_channel(reason)
        self._consumer_epoch += 1
        self._consumer_state = "channel_dead"
        self._in_flight_message_ids.clear()
        if channel.is_closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._close_channel_safely(channel, reason))

    def _on_consume_channel_close(self, *args: Any, **kwargs: Any) -> None:
        """Callback registered with ``channel.add_close_callback``.

        ``aio_pika`` invokes close callbacks with varying signatures across
        versions; accept ``*args, **kwargs`` to stay compatible.
        """
        exc = args[1] if len(args) >= 2 else kwargs.get("exc")
        reason = f"channel closed: {exc!r}" if exc is not None else "channel closed"
        self._schedule_teardown(reason)

    async def _get_consume_channel(self, prefetch_count: Optional[int] = None) -> AbstractChannel:
        if self._consume_channel is None or self._consume_channel.is_closed:
            connection = await self._get_connection()
            try:
                async with timeout_after(self.client.channel_timeout):
                    self._consume_channel = await connection.channel()
            except TimeoutError:
                self._consumer_state = "connect_stalled"
                await self._reset_connection("consume channel creation timed out")
                raise
            # aio-pika exposes close_callbacks on current releases; retain the
            # legacy add_close_callback fallback for older supported versions.
            close_callbacks = getattr(self._consume_channel, "close_callbacks", None)
            register_close = getattr(close_callbacks, "add", None)
            if callable(register_close):
                try:
                    register_close(self._on_consume_channel_close, weak=True)
                except TypeError:
                    logger.debug("Consume channel close-callback registration rejected; skipping")
            else:
                register_close = getattr(self._consume_channel, "add_close_callback", None)
                if callable(register_close):
                    try:
                        register_close(self._on_consume_channel_close)
                    except TypeError:
                        logger.debug("Legacy consume channel close-callback registration rejected; skipping")
                else:
                    logger.debug("Consume channel does not expose close callbacks; skipping registration")
            if prefetch_count is not None:
                await self._consume_channel.set_qos(prefetch_count=prefetch_count)
                logger.debug("Consume channel QoS set to prefetch_count=%d", prefetch_count)
            self._consumer_state = "subscribed"

        return self._consume_channel

    async def _get_publish_channel(self) -> AbstractChannel:
        if self._publish_channel is None or self._publish_channel.is_closed:
            connection = await self._get_connection()
            try:
                async with timeout_after(self.client.channel_timeout):
                    self._publish_channel = await connection.channel()
            except TimeoutError:
                await self._reset_connection("publish channel creation timed out")
                raise

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
        """Read up to ``batch_size`` deliveries from persistent consumers.

        Queue subscriptions are registered once per consume-channel epoch.
        RabbitMQ pushes deliveries into a local buffer subject to channel QoS;
        this method waits for the first delivery and then drains only messages
        already available. The caller remains responsible for ack/nack.
        """
        if batch_size <= 0:
            return []

        self._last_poll_started_ts = time.time()
        self._consumer_state = "subscribing"
        poll_completed = False

        try:
            await self._ensure_consumers(config)
        except Exception as exc:
            if not _is_recoverable_broker_interruption(exc):
                raise
            if self._consume_channel is not None:
                await self._teardown_consume_channel(f"subscription setup interrupted: {exc!r}")
            return []

        buffer: List[Dict[str, Any]] = []
        try:
            self._consumer_state = "polling"
            async with timeout_after(config.consume_timeout):
                buffer.append(await self._next_current_delivery())

            while len(buffer) < batch_size:
                try:
                    epoch, delivery = self._delivery_buffer.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if epoch == self._consumer_epoch:
                    buffer.append(delivery)
        except TimeoutError:
            poll_completed = True
        else:
            poll_completed = True
        finally:
            if self._consume_channel is not None and self._consume_channel.is_closed:
                await self._teardown_consume_channel("consume channel observed closed after iteration")
            if poll_completed:
                self._last_poll_completed_ts = time.time()
                self._consumer_state = "polling"

        return buffer

    async def _ensure_consumers(self, config: AsyncRabbitmqConsumerConfig) -> None:
        """Declare topology and register one persistent consumer per queue."""
        signature = config.model_dump_json()

        async with self._consumer_setup_lock:
            if (
                self._consumer_config_signature == signature
                and self._active_consumer_tags
                and self._consume_channel is not None
                and not self._consume_channel.is_closed
            ):
                if await self._consumer_tags_registered():
                    return
                await self._teardown_consume_channel("broker cancelled a registered consumer")

            if self._consumer_config_signature is not None and self._consumer_config_signature != signature:
                await self._teardown_consume_channel("consumer configuration changed")

            await self._get_consume_channel(prefetch_count=config.prefetch_count)
            epoch = self._consumer_epoch
            registered_tags: Dict[str, str] = {}

            try:
                for subscription in config.resolved_subscriptions():
                    queue = await self.declare_queue(
                        subscription.queue_name,
                        durable=config.durable,
                        arguments=subscription.queue_arguments or None,
                    )
                    for binding in subscription.bindings:
                        exchange = await self.declare_exchange(
                            binding.exchange_name,
                            binding.exchange_type,
                            durable=config.durable,
                        )
                        for key in binding.routing_keys:
                            await self.bind_queue(queue, exchange, key)

                    queue_name = subscription.queue_name

                    async def on_delivery(
                        message: AbstractIncomingMessage,
                        *,
                        delivery_queue_name: str = queue_name,
                        delivery_epoch: int = epoch,
                        delivery_auto_ack: bool = config.auto_ack,
                    ) -> None:
                        if delivery_epoch != self._consumer_epoch:
                            return
                        if not delivery_auto_ack:
                            self._in_flight_message_ids.add(id(message))
                        self._last_fetch_ts = time.time()
                        self._consumer_state = "delivering"
                        self._delivery_buffer.put_nowait(
                            (
                                delivery_epoch,
                                self._message_to_dict(message, queue_name=delivery_queue_name),
                            )
                        )

                    consumer_tag = await queue.consume(
                        on_delivery,
                        no_ack=config.auto_ack,
                        robust=False,  # type: ignore[call-arg]
                    )
                    registered_tags[queue_name] = consumer_tag
            except BaseException:
                await self._teardown_consume_channel("consumer subscription failed")
                raise

            self._active_consumer_tags = registered_tags
            self._active_consumer_tag = next(iter(registered_tags.values()), None)
            self._consumer_config_signature = signature
            self._consumer_state = "subscribed"

    async def _consumer_tags_registered(self) -> bool:
        """Best-effort detection of broker-initiated Basic.Cancel.

        aio-pika does not surface Basic.Cancel to queue callbacks, while
        aiormq removes the tag from its local consumer registry. Inspecting
        that local mapping lets the next fetch rebuild a cancelled topology
        without another broker round trip.
        """
        channel = self._consume_channel
        if channel is None:
            return False
        get_underlay_channel = getattr(channel, "get_underlay_channel", None)
        if not callable(get_underlay_channel):
            return True
        get_underlay_channel = cast(Callable[[], Awaitable[Any]], get_underlay_channel)
        try:
            async with timeout_after(self.client.channel_timeout):
                underlay_channel = await get_underlay_channel()
        except Exception:
            return False
        consumers = getattr(underlay_channel, "consumers", None)
        if not isinstance(consumers, Mapping):
            return True
        return set(self._active_consumer_tags.values()).issubset(consumers)

    async def _next_current_delivery(self) -> Dict[str, Any]:
        """Discard stale-epoch entries and return the next live delivery."""
        while True:
            epoch, delivery = await self._delivery_buffer.get()
            if epoch == self._consumer_epoch:
                return delivery

    @staticmethod
    def _message_to_dict(message: AbstractIncomingMessage, queue_name: Optional[str] = None) -> Dict[str, Any]:
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
            "queue_name": queue_name,
            "exchange_name": str(getattr(message, "exchange", "") or ""),
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

        self._consumer_state = "acking"
        try:
            # Bound the ack so a half-open socket is detected in seconds rather
            # than blocking until the (much longer) heartbeat timeout fires.
            async with timeout_after(self.client.ack_timeout):
                await message.ack()
            self._last_ack_ts = time.time()
            self._last_settlement_ts = self._last_ack_ts
            self._in_flight_message_ids.discard(id(message))
            self._consumer_state = "polling"
        except _DEAD_CHANNEL_TIMEOUT_EXCEPTIONS as exc:
            await self._teardown_consume_channel(f"ack failed: {exc!r}")
            raise task_exceptions.AckOnDeadChannelError(
                delivery_tag=getattr(message, "delivery_tag", None),
                queue=getattr(message, "routing_key", None),
                cause=exc,
            ) from exc

    async def nack(self, message: AbstractIncomingMessage, requeue: bool = True) -> None:
        from ...task import exceptions as task_exceptions

        self._consumer_state = "acking"
        try:
            async with timeout_after(self.client.ack_timeout):
                await message.nack(requeue=requeue)
            self._last_settlement_ts = time.time()
            self._in_flight_message_ids.discard(id(message))
            self._consumer_state = "polling"
        except _DEAD_CHANNEL_TIMEOUT_EXCEPTIONS as exc:
            await self._teardown_consume_channel(f"nack failed: {exc!r}")
            raise task_exceptions.NackOnDeadChannelError(
                delivery_tag=getattr(message, "delivery_tag", None),
                queue=getattr(message, "routing_key", None),
                cause=exc,
            ) from exc

    # ---------- Health / Liveness ----------

    def health(self) -> Dict[str, Any]:
        """Snapshot of consumer liveness for external health checks.

        ``last_poll_completed_ts`` advances even when every queue is empty, so
        callers can distinguish healthy idle polling from a wedged consumer.
        Message-delivery and settlement timestamps remain activity diagnostics,
        not standalone liveness gates.
        """
        now = time.time()
        connection_open = self._connection is not None and not self._connection.is_closed
        consume_channel_open = self._consume_channel is not None and not self._consume_channel.is_closed
        return {
            "state": self._consumer_state,
            "consumer_epoch": self._consumer_epoch,
            "connection_open": connection_open,
            "consume_channel_open": consume_channel_open,
            "active_consumer_tag": self._active_consumer_tag,
            "active_consumer_tags": dict(self._active_consumer_tags),
            "buffered_delivery_count": self._delivery_buffer.qsize(),
            "in_flight_count": len(self._in_flight_message_ids),
            "last_poll_started_ts": self._last_poll_started_ts,
            "last_poll_completed_ts": self._last_poll_completed_ts,
            "last_fetch_ts": self._last_fetch_ts,
            "last_ack_ts": self._last_ack_ts,
            "last_settlement_ts": self._last_settlement_ts,
            "seconds_since_last_poll_started": (
                now - self._last_poll_started_ts if self._last_poll_started_ts is not None else None
            ),
            "seconds_since_last_poll_completed": (
                now - self._last_poll_completed_ts if self._last_poll_completed_ts is not None else None
            ),
            "seconds_since_last_fetch": (now - self._last_fetch_ts) if self._last_fetch_ts is not None else None,
            "seconds_since_last_ack": (now - self._last_ack_ts) if self._last_ack_ts is not None else None,
            "seconds_since_last_settlement": (
                now - self._last_settlement_ts if self._last_settlement_ts is not None else None
            ),
        }

    # ---------- Lifecycle ----------

    async def close(self) -> None:
        await self._teardown_consume_channel("service close")

        publish_channel = self._publish_channel
        self._publish_channel = None
        await self._close_channel_safely(publish_channel, "service close")

        connection = self._connection
        self._connection = None
        await self._close_connection_safely(connection, "service close")

        self._in_flight_message_ids.clear()
        self._consumer_state = "closed"
        logger.info("RabbitMQ connection closed")
