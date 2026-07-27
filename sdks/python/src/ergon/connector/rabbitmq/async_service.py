import asyncio
import json
import logging
import ssl as ssl_module
import time
from collections import deque
from typing import Any, AsyncContextManager, Callable, Dict, List, Optional, cast

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
        self._prefetched_messages: deque[Dict[str, Any]] = deque()

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

    async def _teardown_consume_channel(self, reason: str = "explicit teardown") -> None:
        """Deterministically tear down the consume channel.

        Unlike :meth:`_invalidate_consume_channel` (which only drops Python
        references), this snapshots the live channel, clears the cache, then
        explicitly ``close()``-es the channel. Closing the channel:

        * drops every consumer registered on it at the broker — this is what
          eliminates the *zombie consumer* left behind by a broker-initiated
          ``Basic.Cancel`` that the per-fetch iterator could not cancel
          cleanly; and
        * for a ``RobustChannel`` removes it from aio_pika's reconnection set,
          so the robust layer does not silently restore the dead consumer on
          the next reconnect.

        It also prevents the channel leak: previously the dropped channel
        object was orphaned without ever being closed, so its broker-side
        channel lingered and ``ChannelCount`` climbed over time.
        """
        channel = self._consume_channel
        self._invalidate_consume_channel(reason)
        self._active_consumer_tag = None
        self._active_consumer_tags.clear()
        self._consumer_epoch += 1
        self._consumer_state = "channel_dead"
        self._in_flight_message_ids.clear()
        self._prefetched_messages.clear()
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
        self._invalidate_consume_channel(reason)
        self._active_consumer_tag = None
        self._active_consumer_tags.clear()
        self._consumer_epoch += 1
        self._consumer_state = "channel_dead"
        self._in_flight_message_ids.clear()
        self._prefetched_messages.clear()
        if channel is None or channel.is_closed:
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
        """
        Fetch up to batch_size messages from the configured queue.

        Declares every configured exchange/queue/binding on first call, then
        concurrently iterates the queues until batch_size is reached or
        consume_timeout elapses.

        Returns raw message dicts without acknowledging — the caller
        is responsible for ack/nack via the delivery_tag in metadata.
        """
        self._last_poll_started_ts = time.time()
        self._consumer_state = "subscribing"
        poll_completed = False

        try:
            # Ensure the consume channel exists with the requested prefetch QoS.
            # The return value is unused here because declarations re-fetch the
            # channel from the cache.
            await self._get_consume_channel(prefetch_count=config.prefetch_count)

            queues: list[tuple[str, AbstractQueue]] = []
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
                queues.append((subscription.queue_name, queue))
        except Exception as exc:
            if not _is_recoverable_broker_interruption(exc):
                raise
            await self._teardown_consume_channel(f"subscription setup interrupted: {exc!r}")
            return []

        buffer: List[Dict[str, Any]] = []
        timeout = config.consume_timeout
        while self._prefetched_messages and len(buffer) < batch_size:
            buffer.append(self._prefetched_messages.popleft())

        try:
            self._consumer_state = "polling"
            if len(buffer) < batch_size:
                async with timeout_after(timeout):
                    await self._consume_queues(
                        queues,
                        buffer,
                        batch_size=batch_size,
                        auto_ack=config.auto_ack,
                    )
        except TimeoutError:
            poll_completed = True
        except Exception as exc:
            if not _is_recoverable_broker_interruption(exc):
                raise
            # Broker cancelled this subscription mid-iteration; deterministically
            # tear down (cancel consumers + close) the dead channel so the next
            # call rebuilds against a fresh consumer, the broker redelivers what
            # we had prefetched, and no zombie consumer / leaked channel is left
            # behind.
            await self._teardown_consume_channel(f"consume aborted: {exc!r}")
            return buffer
        else:
            poll_completed = True
        finally:
            self._active_consumer_tag = None
            self._active_consumer_tags.clear()
            # If the channel was closed during iteration, make sure we don't
            # hand back a stale cache to the next caller.
            if self._consume_channel is not None and self._consume_channel.is_closed:
                await self._teardown_consume_channel("consume channel observed closed after iteration")
            if poll_completed:
                self._last_poll_completed_ts = time.time()
                self._consumer_state = "polling"

        return buffer

    async def _consume_queues(
        self,
        queues: list[tuple[str, AbstractQueue]],
        buffer: List[Dict[str, Any]],
        *,
        batch_size: int,
        auto_ack: bool,
    ) -> None:
        """Wait for one delivery, then drain only messages already available.

        ``consume_timeout`` bounds the idle wait in :meth:`consume`; it must
        not become a batch-assembly delay after the first delivery. Once any
        message is buffered, give newly scheduled iterator reads one event-loop
        turn to complete and return immediately if none are ready.
        """
        pending: dict[asyncio.Task, tuple[str, Any]] = {}
        iterator_contexts: list[AsyncContextManager[Any]] = []
        try:
            for queue_name, queue in queues:
                iterator_context = cast(
                    AsyncContextManager[Any],
                    # The framework owns consumer teardown/resubscription.
                    # Letting aio-pika restore these short-lived per-fetch
                    # consumers can resurrect an iterator with no pending
                    # reader after a broker restart, creating a zombie tag.
                    queue.iterator(no_ack=auto_ack, robust=False),
                )
                iterator = await iterator_context.__aenter__()
                iterator_contexts.append(iterator_context)
                self._register_consumer_cancel_callback(iterator)
                consumer_tag = getattr(iterator, "_consumer_tag", None) or getattr(iterator, "consumer_tag", None)
                if consumer_tag:
                    self._active_consumer_tags[queue_name] = consumer_tag
                pending[asyncio.create_task(anext(iterator))] = (queue_name, iterator)

            self._active_consumer_tag = next(iter(self._active_consumer_tags.values()), None)
            while pending and len(buffer) < batch_size:
                if buffer:
                    done = {task for task in pending if task.done()}
                    if not done:
                        await asyncio.sleep(0)
                        done = {task for task in pending if task.done()}
                    if not done:
                        break
                else:
                    done, _ = await asyncio.wait(
                        pending,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                for task in done:
                    queue_name, iterator = pending.pop(task)
                    try:
                        message = task.result()
                    except StopAsyncIteration:
                        self._active_consumer_tags.pop(queue_name, None)
                        continue

                    message_dict = self._message_to_dict(message, queue_name=queue_name)
                    self._in_flight_message_ids.add(id(message))
                    self._last_fetch_ts = time.time()
                    self._consumer_state = "delivering"
                    if len(buffer) < batch_size:
                        buffer.append(message_dict)
                    else:
                        self._prefetched_messages.append(message_dict)
                    if len(buffer) < batch_size:
                        pending[asyncio.create_task(anext(iterator))] = (queue_name, iterator)
        finally:
            cleanup_timeout = min(1.0, self.client.channel_timeout)
            cleanup_failed = False
            for task in pending:
                task.cancel()
            if pending:
                try:
                    async with timeout_after(cleanup_timeout):
                        await asyncio.gather(*pending, return_exceptions=True)
                except TimeoutError:
                    cleanup_failed = True
                    logger.warning("Timed out cancelling RabbitMQ iterator reads")
            cleanup_failed = await self._close_iterator_contexts(iterator_contexts) or cleanup_failed
            if cleanup_failed:
                await self._teardown_consume_channel("queue iterator cleanup failed")

    async def _close_iterator_contexts(
        self,
        iterator_contexts: list[AsyncContextManager[Any]],
    ) -> bool:
        """Bound queue-iterator cleanup so a dead broker cannot stall a poll."""
        cleanup_timeout = min(1.0, self.client.channel_timeout)
        cleanup_failed = False
        for iterator_context in reversed(iterator_contexts):
            try:
                async with timeout_after(cleanup_timeout):
                    await iterator_context.__aexit__(None, None, None)
            except Exception as exc:
                cleanup_failed = True
                logger.warning("RabbitMQ queue iterator cleanup failed: %r", exc)
        return cleanup_failed

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
            self._schedule_teardown("consumer cancelled by broker (Basic.Cancel)")

        try:
            candidate(_on_cancel)
        except (TypeError, AttributeError):
            logger.debug("Iterator consumer does not accept cancel callback; skipping")

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

        connection = self._connection
        self._connection = None
        await self._close_connection_safely(connection, "service close")

        self._active_consumer_tag = None
        self._active_consumer_tags.clear()
        self._in_flight_message_ids.clear()
        self._prefetched_messages.clear()
        self._consumer_state = "closed"
        logger.info("RabbitMQ connection closed")
