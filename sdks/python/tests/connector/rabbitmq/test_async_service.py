"""Tests for AsyncRabbitMQService — connection lifecycle, consume, publish, ack/nack."""

import asyncio
import builtins
import json
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncContextManager
from unittest.mock import AsyncMock, MagicMock, patch

import aio_pika.exceptions
import aiormq.exceptions
import pytest

from ergon.connector.rabbitmq.async_service import (
    AsyncRabbitMQService,
    _is_recoverable_broker_interruption,
)
from ergon.connector.rabbitmq.models import (
    AsyncRabbitmqClient,
    AsyncRabbitmqConsumerConfig,
    AsyncRabbitmqExchangeBinding,
    AsyncRabbitmqProducerConfig,
    AsyncRabbitmqQueueSubscription,
)
from ergon.task.exceptions import (
    AckOnDeadChannelError,
    DeadChannelError,
    NackOnDeadChannelError,
)


def _make_client(**overrides: Any) -> AsyncRabbitmqClient:
    defaults: dict[str, Any] = {
        "username": "guest",
        "password": "guest",
        "host": "localhost",
    }
    defaults.update(overrides)
    return AsyncRabbitmqClient(**defaults)


def _mock_message(body: bytes = b'{"key":"val"}', routing_key: str = "test.key", delivery_tag: int = 1):
    msg = MagicMock()
    msg.body = body
    msg.routing_key = routing_key
    msg.delivery_tag = delivery_tag
    msg.headers = {"x-custom": "header"}
    msg.content_type = "application/json"
    msg.message_id = "msg-001"
    msg.correlation_id = "corr-001"
    msg.ack = AsyncMock()
    msg.nack = AsyncMock()
    return msg


def _mock_channel() -> AsyncMock:
    """Mock channel that mirrors the sync/async surface ``AsyncRabbitMQService`` uses."""
    channel = AsyncMock()
    channel.is_closed = False
    # add_close_callback is a sync method on real aio_pika channels; using
    # AsyncMock here would create unawaited-coroutine warnings.
    channel.add_close_callback = MagicMock()
    channel.set_qos = AsyncMock()
    return channel


class TestClientUrl:
    def test_url_from_individual_params(self):
        client = _make_client()
        assert client.get_url() == "amqp://guest:guest@localhost:5672//"

    def test_explicit_url_takes_precedence(self):
        client = _make_client(url="amqp://other:other@rmq:5673/vhost")
        assert client.get_url() == "amqp://other:other@rmq:5673/vhost"

    def test_heartbeat_and_ack_timeout_defaults_fail_fast(self):
        """Defaults must be low enough to detect a half-open socket in seconds."""
        client = _make_client()
        assert client.heartbeat == 60
        assert client.ack_timeout == 30


class TestConnection:
    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_connect_hang_is_bounded(self, mock_connect):
        async def never_connects(*args, **kwargs):
            await asyncio.Event().wait()

        mock_connect.side_effect = never_connects
        service = AsyncRabbitMQService(_make_client(connect_timeout=0.02))

        with pytest.raises(TimeoutError):
            await service._get_connection()

        assert service.health()["state"] == "connect_stalled"

    async def test_existing_robust_connection_recovery_is_bounded_and_reset(self):
        connected = asyncio.Event()
        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.connected = connected
        mock_conn.close = AsyncMock()

        service = AsyncRabbitMQService(_make_client(connect_timeout=0.02))
        service._connection = mock_conn

        with pytest.raises(TimeoutError):
            await service._get_connection()

        mock_conn.close.assert_awaited_once()
        assert service._connection is None
        assert service.health()["consumer_epoch"] == 1

    async def test_reset_closes_robust_connection_with_closed_transport(self):
        mock_conn = AsyncMock()
        mock_conn.is_closed = True
        mock_conn.close = AsyncMock()

        service = AsyncRabbitMQService(_make_client())
        service._connection = mock_conn

        await service._reset_connection("broker shutdown")

        mock_conn.close.assert_awaited_once()
        assert service._connection is None

    async def test_reset_bounds_robust_connection_close(self):
        async def never_closes():
            await asyncio.Event().wait()

        mock_conn = AsyncMock()
        mock_conn.is_closed = True
        mock_conn.close = AsyncMock(side_effect=never_closes)

        service = AsyncRabbitMQService(_make_client(connect_timeout=0.02))
        service._connection = mock_conn

        started = time.monotonic()
        await asyncio.wait_for(service._reset_connection("network blackhole"), timeout=0.2)

        assert time.monotonic() - started < 0.2
        assert service._connection is None

    async def test_existing_robust_connection_returns_after_recovery(self):
        connected = asyncio.Event()
        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.connected = connected

        service = AsyncRabbitMQService(_make_client(connect_timeout=0.2))
        service._connection = mock_conn

        async def restore_connection():
            await asyncio.sleep(0.01)
            connected.set()

        restore_task = asyncio.create_task(restore_connection())
        result = await service._get_connection()
        await restore_task

        assert result is mock_conn

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_lazy_connection(self, mock_connect):
        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        assert service._connection is None

        conn = await service._get_connection()
        assert conn is mock_conn
        mock_connect.assert_awaited_once()

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_connection_reuse(self, mock_connect):
        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        await service._get_connection()
        await service._get_connection()
        mock_connect.assert_awaited_once()

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_ssl_connection(self, mock_connect):
        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_connect.return_value = mock_conn

        client = _make_client(ssl_enabled=True)
        service = AsyncRabbitMQService(client)
        await service._get_connection()
        mock_connect.assert_awaited_once()
        _, call_kwargs = mock_connect.call_args
        assert call_kwargs.get("ssl") is True
        assert call_kwargs.get("ssl_context") is not None


class TestConsume:
    def test_internal_connection_error_is_recoverable(self):
        exc = aiormq.exceptions.ConnectionInternalError(
            541,
            "broker internal error",
        )

        assert _is_recoverable_broker_interruption(exc) is True

    def test_legacy_config_resolves_to_one_subscription(self):
        config = AsyncRabbitmqConsumerConfig(
            queue_name="test-queue",
            exchange_name="test-exchange",
            binding_keys=["test.#"],
            queue_arguments={"x-dead-letter-exchange": "dlx"},
        )

        subscriptions = config.resolved_subscriptions()

        assert len(subscriptions) == 1
        assert subscriptions[0].queue_name == "test-queue"
        assert subscriptions[0].queue_arguments == {"x-dead-letter-exchange": "dlx"}
        assert subscriptions[0].bindings[0].exchange_name == "test-exchange"
        assert subscriptions[0].bindings[0].routing_keys == ["test.#"]

    def test_multi_subscription_requires_unique_queue_names(self):
        duplicate = AsyncRabbitmqQueueSubscription(queue_name="same")
        with pytest.raises(ValueError, match="only once"):
            AsyncRabbitmqConsumerConfig(subscriptions=[duplicate, duplicate])

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_consume_returns_messages(self, mock_connect):
        msg = _mock_message()
        iterator_kwargs: dict[str, Any] = {}

        @asynccontextmanager
        async def _iterator_cm(**kwargs):
            iterator_kwargs.update(kwargs)

            async def _gen():
                yield msg

            yield _gen()

        mock_queue = MagicMock()
        mock_queue.name = "test-queue"
        mock_queue.iterator = _iterator_cm
        mock_queue.bind = AsyncMock()

        mock_exchange = AsyncMock()
        mock_exchange.name = "test-exchange"

        mock_channel = _mock_channel()
        mock_channel.declare_exchange = AsyncMock(return_value=mock_exchange)
        mock_channel.declare_queue = AsyncMock(return_value=mock_queue)

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        config = AsyncRabbitmqConsumerConfig(
            queue_name="test-queue",
            exchange_name="test-exchange",
            binding_keys=["test.#"],
            consume_timeout=1.0,
        )

        result = await service.consume(config, batch_size=1)

        assert len(result) == 1
        assert result[0]["body"] == {"key": "val"}
        assert result[0]["routing_key"] == "test.key"
        assert result[0]["delivery_tag"] == 1
        assert iterator_kwargs == {"no_ack": False, "robust": False}

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_consume_returns_first_delivery_without_waiting_to_fill_batch(self, mock_connect):
        first_message = _mock_message(delivery_tag=1)
        second_message = _mock_message(delivery_tag=2)
        release_second = asyncio.Event()

        @asynccontextmanager
        async def _iterator_cm(**kwargs):
            async def _gen():
                yield first_message
                await release_second.wait()
                yield second_message

            yield _gen()

        mock_queue = MagicMock()
        mock_queue.iterator = _iterator_cm

        mock_channel = _mock_channel()
        mock_channel.declare_queue = AsyncMock(return_value=mock_queue)

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        config = AsyncRabbitmqConsumerConfig(
            queue_name="test-queue",
            consume_timeout=10,
        )

        result = await asyncio.wait_for(service.consume(config, batch_size=10), timeout=0.5)

        assert [message["delivery_tag"] for message in result] == [1]

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_consume_nonblocking_drains_immediately_available_messages(self, mock_connect):
        messages = [_mock_message(delivery_tag=index) for index in range(1, 4)]

        @asynccontextmanager
        async def _iterator_cm(**kwargs):
            async def _gen():
                for message in messages:
                    yield message

            yield _gen()

        mock_queue = MagicMock()
        mock_queue.iterator = _iterator_cm

        mock_channel = _mock_channel()
        mock_channel.declare_queue = AsyncMock(return_value=mock_queue)

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        config = AsyncRabbitmqConsumerConfig(
            queue_name="test-queue",
            consume_timeout=10,
        )

        result = await asyncio.wait_for(service.consume(config, batch_size=10), timeout=0.5)

        assert [message["delivery_tag"] for message in result] == [1, 2, 3]

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_consume_merges_multiple_queue_subscriptions(self, mock_connect):
        first_message = _mock_message(routing_key="iam.created", delivery_tag=7)

        @asynccontextmanager
        async def _ready_iterator(**kwargs):
            async def _gen():
                yield first_message

            yield _gen()

        @asynccontextmanager
        async def _idle_iterator(**kwargs):
            async def _gen():
                await asyncio.sleep(10)
                return
                yield

            yield _gen()

        queue_one = MagicMock()
        queue_one.iterator = _ready_iterator
        queue_one.bind = AsyncMock()
        queue_two = MagicMock()
        queue_two.iterator = _idle_iterator
        queue_two.bind = AsyncMock()

        exchanges = {
            "iam": MagicMock(name="iam"),
            "conversations": MagicMock(name="conversations"),
            "agents": MagicMock(name="agents"),
        }
        mock_channel = _mock_channel()
        mock_channel.declare_queue = AsyncMock(side_effect=[queue_one, queue_two])
        mock_channel.declare_exchange = AsyncMock(side_effect=lambda name, *_args, **_kwargs: exchanges[name])

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        config = AsyncRabbitmqConsumerConfig(
            subscriptions=[
                AsyncRabbitmqQueueSubscription(
                    queue_name="audit.iam",
                    bindings=[
                        AsyncRabbitmqExchangeBinding(
                            exchange_name="iam",
                            routing_keys=["#"],
                        )
                    ],
                ),
                AsyncRabbitmqQueueSubscription(
                    queue_name="audit.cross-service",
                    bindings=[
                        AsyncRabbitmqExchangeBinding(
                            exchange_name="conversations",
                            routing_keys=["conversations.#"],
                        ),
                        AsyncRabbitmqExchangeBinding(
                            exchange_name="agents",
                            routing_keys=["agents.#"],
                        ),
                    ],
                ),
            ],
            consume_timeout=1,
        )

        result = await service.consume(config, batch_size=1)

        assert len(result) == 1
        assert result[0]["queue_name"] == "audit.iam"
        assert result[0]["routing_key"] == "iam.created"
        assert mock_channel.declare_queue.await_count == 2
        assert mock_channel.declare_exchange.await_count == 3
        queue_one.bind.assert_awaited_once_with(exchanges["iam"], routing_key="#")
        assert queue_two.bind.await_count == 2

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_simultaneous_deliveries_do_not_exceed_batch_size(self, mock_connect):
        def queue_with_message(message):
            @asynccontextmanager
            async def _iterator(**kwargs):
                async def _gen():
                    yield message

                yield _gen()

            queue = MagicMock()
            queue.iterator = _iterator
            return queue

        queue_one = queue_with_message(_mock_message(delivery_tag=1))
        queue_two = queue_with_message(_mock_message(delivery_tag=2))
        mock_channel = _mock_channel()
        mock_channel.declare_queue = AsyncMock(side_effect=[queue_one, queue_two])

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        config = AsyncRabbitmqConsumerConfig(
            subscriptions=[
                AsyncRabbitmqQueueSubscription(queue_name="one"),
                AsyncRabbitmqQueueSubscription(queue_name="two"),
            ],
        )

        first = await service.consume(config, batch_size=1)
        second = await service.consume(config, batch_size=1)

        assert len(first) == 1
        assert len(second) == 1
        assert {first[0]["delivery_tag"], second[0]["delivery_tag"]} == {1, 2}

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_consume_forwards_queue_arguments(self, mock_connect):
        """``queue_arguments`` must reach ``channel.declare_queue(arguments=...)``.

        Regression: prior to 0.1.1 the field did not exist on the Pydantic
        model and was silently dropped at validation, AND the service only
        forwarded ``name`` and ``durable``. Together that made any DLX /
        TTL configuration a no-op in production.
        """

        @asynccontextmanager
        async def _iterator_cm(**kwargs):
            async def _gen():
                await asyncio.sleep(10)
                return
                yield  # make it an async generator

            yield _gen()

        mock_queue = MagicMock()
        mock_queue.name = "test-queue"
        mock_queue.iterator = _iterator_cm

        mock_channel = _mock_channel()
        mock_channel.declare_queue = AsyncMock(return_value=mock_queue)

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        config = AsyncRabbitmqConsumerConfig(
            queue_name="test-queue",
            consume_timeout=0.05,
            queue_arguments={
                "x-dead-letter-exchange": "dlx.events",
                "x-dead-letter-routing-key": "events.failed",
                "x-message-ttl": 60_000,
            },
        )

        await service.consume(config, batch_size=1)

        mock_channel.declare_queue.assert_awaited_once()
        _, call_kwargs = mock_channel.declare_queue.call_args
        assert call_kwargs.get("arguments") == {
            "x-dead-letter-exchange": "dlx.events",
            "x-dead-letter-routing-key": "events.failed",
            "x-message-ttl": 60_000,
        }
        assert call_kwargs.get("durable") is True

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_consume_with_empty_queue_arguments_passes_none(self, mock_connect):
        """Empty/omitted ``queue_arguments`` must pass ``arguments=None`` so we
        do not break re-declaration on existing queues that were declared
        without an x-arguments table (RabbitMQ would otherwise reject with
        PRECONDITION_FAILED on inequivalent args).
        """

        @asynccontextmanager
        async def _iterator_cm(**kwargs):
            async def _gen():
                await asyncio.sleep(10)
                return
                yield

            yield _gen()

        mock_queue = MagicMock()
        mock_queue.name = "test-queue"
        mock_queue.iterator = _iterator_cm

        mock_channel = _mock_channel()
        mock_channel.declare_queue = AsyncMock(return_value=mock_queue)

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        config = AsyncRabbitmqConsumerConfig(
            queue_name="test-queue",
            consume_timeout=0.05,
        )

        await service.consume(config, batch_size=1)

        mock_channel.declare_queue.assert_awaited_once()
        _, call_kwargs = mock_channel.declare_queue.call_args
        assert call_kwargs.get("arguments") is None

    async def test_declare_queue_caches_per_arguments(self):
        """Two configs with the same queue name but different ``arguments``
        must NOT share a cached handle — otherwise misconfigured DLX wiring
        would silently inherit the first declaration's args.
        """
        mock_channel = _mock_channel()
        mock_channel.declare_queue = AsyncMock(side_effect=lambda name, **_: MagicMock(name=name))

        service = AsyncRabbitMQService(_make_client())
        service._consume_channel = mock_channel

        await service.declare_queue("q", arguments={"x-dead-letter-exchange": "dlx.a"})
        await service.declare_queue("q", arguments={"x-dead-letter-exchange": "dlx.b"})
        await service.declare_queue("q", arguments={"x-dead-letter-exchange": "dlx.a"})

        # Same args -> cache hit; different args -> separate declaration.
        assert mock_channel.declare_queue.await_count == 2

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_consume_empty_queue_timeout(self, mock_connect):
        @asynccontextmanager
        async def _iterator_cm(**kwargs):
            async def _gen():
                await asyncio.sleep(10)
                return
                yield  # make it an async generator

            yield _gen()

        mock_queue = MagicMock()
        mock_queue.name = "test-queue"
        mock_queue.iterator = _iterator_cm

        mock_channel = _mock_channel()
        mock_channel.declare_queue = AsyncMock(return_value=mock_queue)

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        config = AsyncRabbitmqConsumerConfig(
            queue_name="test-queue",
            consume_timeout=0.1,
        )

        result = await service.consume(config, batch_size=10)
        assert result == []
        health = service.health()
        assert health["state"] == "polling"
        assert health["last_poll_started_ts"] is not None
        assert health["last_poll_completed_ts"] is not None
        assert health["in_flight_count"] == 0

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_consume_recovers_when_connection_closes_during_subscription_setup(
        self,
        mock_connect,
    ):
        mock_channel = _mock_channel()
        mock_channel.declare_queue = AsyncMock(side_effect=aiormq.exceptions.ConnectionClosed(320, "broker shutdown"))

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        config = AsyncRabbitmqConsumerConfig(queue_name="test-queue")

        result = await service.consume(config, batch_size=10)

        assert result == []
        mock_channel.close.assert_awaited_once()
        assert service.health()["consumer_epoch"] == 1

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_consume_propagates_permanent_topology_errors(self, mock_connect):
        mock_channel = _mock_channel()
        mock_channel.declare_queue = AsyncMock(
            side_effect=aiormq.exceptions.ChannelPreconditionFailed(
                406,
                "inequivalent queue arguments",
            )
        )

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        config = AsyncRabbitmqConsumerConfig(queue_name="test-queue")

        with pytest.raises(
            aiormq.exceptions.ChannelPreconditionFailed,
            match="inequivalent queue arguments",
        ):
            await service.consume(config, batch_size=10)

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_consume_propagates_permanent_authentication_errors(self, mock_connect):
        mock_channel = _mock_channel()
        mock_channel.declare_queue = AsyncMock(side_effect=aiormq.exceptions.AuthenticationError("invalid credentials"))

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        config = AsyncRabbitmqConsumerConfig(queue_name="test-queue")

        with pytest.raises(
            aiormq.exceptions.AuthenticationError,
            match="invalid credentials",
        ):
            await service.consume(config, batch_size=10)

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_consume_bounds_iterator_cleanup_when_broker_is_unresponsive(self, mock_connect):
        class HangingIteratorContext:
            async def __aenter__(self):
                async def _gen():
                    await asyncio.sleep(10)
                    return
                    yield

                return _gen()

            async def __aexit__(self, exc_type, exc, traceback):
                await asyncio.sleep(10)

        mock_queue = MagicMock()
        mock_queue.name = "test-queue"
        mock_queue.iterator.return_value = HangingIteratorContext()

        mock_channel = _mock_channel()
        mock_channel.declare_queue = AsyncMock(return_value=mock_queue)

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client(channel_timeout=0.1))
        config = AsyncRabbitmqConsumerConfig(
            queue_name="test-queue",
            consume_timeout=0.05,
        )

        started = time.monotonic()
        result = await asyncio.wait_for(service.consume(config, batch_size=10), timeout=0.5)

        assert result == []
        assert time.monotonic() - started < 0.5
        mock_channel.close.assert_awaited_once()
        assert service.health()["consumer_epoch"] == 1

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_consume_bounds_pending_iterator_cancellation(self, mock_connect):
        class StubbornIterator:
            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    await asyncio.sleep(10)
                raise StopAsyncIteration

        class IteratorContext:
            async def __aenter__(self):
                return StubbornIterator()

            async def __aexit__(self, exc_type, exc, traceback):
                return None

        mock_queue = MagicMock()
        mock_queue.name = "test-queue"
        mock_queue.iterator.return_value = IteratorContext()

        mock_channel = _mock_channel()
        mock_channel.declare_queue = AsyncMock(return_value=mock_queue)

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client(channel_timeout=0.1))
        config = AsyncRabbitmqConsumerConfig(
            queue_name="test-queue",
            consume_timeout=0.05,
        )

        started = time.monotonic()
        result = await asyncio.wait_for(service.consume(config, batch_size=10), timeout=0.5)

        assert result == []
        assert time.monotonic() - started < 0.5
        mock_channel.close.assert_awaited_once()
        assert service.health()["consumer_epoch"] == 1

    async def test_iterator_cleanup_continues_after_exception_group(self):
        exception_group_type = getattr(builtins, "ExceptionGroup", None)
        if exception_group_type is None:
            pytest.skip("ExceptionGroup requires Python 3.11+")

        closed: list[str] = []

        class IteratorContext:
            def __init__(self, name: str, *, fail: bool = False):
                self.name = name
                self.fail = fail

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                closed.append(self.name)
                if self.fail:
                    raise exception_group_type(
                        "buffered message cleanup failed",
                        [RuntimeError("nack failed")],
                    )

        service = AsyncRabbitMQService(_make_client())
        contexts: list[AsyncContextManager[Any]] = [
            IteratorContext("remaining"),
            IteratorContext("failing", fail=True),
        ]

        cleanup_failed = await service._close_iterator_contexts(contexts)

        assert cleanup_failed is True
        assert closed == ["failing", "remaining"]


class TestPublish:
    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_publish_to_exchange(self, mock_connect):
        mock_exchange = AsyncMock()
        mock_exchange.publish = AsyncMock()

        mock_channel = _mock_channel()
        mock_channel.declare_exchange = AsyncMock(return_value=mock_exchange)

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        config = AsyncRabbitmqProducerConfig(
            exchange_name="events",
            exchange_type="topic",
        )

        body = json.dumps({"event": "test"}).encode()
        await service.publish(config, body=body, routing_key="test.event")

        mock_exchange.publish.assert_awaited_once()


class TestAckNack:
    async def test_ack_calls_message_ack(self):
        msg = _mock_message()
        service = AsyncRabbitMQService(_make_client())
        await service.ack(msg)
        msg.ack.assert_awaited_once()

    async def test_nack_calls_message_nack(self):
        msg = _mock_message()
        service = AsyncRabbitMQService(_make_client())
        await service.nack(msg, requeue=False)
        msg.nack.assert_awaited_once_with(requeue=False)


class TestClose:
    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_close_cleans_up(self, mock_connect):
        mock_channel = _mock_channel()
        mock_channel.close = AsyncMock()

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_conn.close = AsyncMock()
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        await service._get_consume_channel()

        await service.close()

        mock_channel.close.assert_awaited_once()
        mock_conn.close.assert_awaited_once()
        assert service._connection is None
        assert service._consume_channel is None
        assert service._publish_channel is None

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_close_closes_both_channels(self, mock_connect):
        consume_ch = _mock_channel()
        consume_ch.close = AsyncMock()
        publish_ch = _mock_channel()
        publish_ch.close = AsyncMock()

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        # Each ``connection.channel()`` call returns a fresh channel.
        mock_conn.channel = AsyncMock(side_effect=[consume_ch, publish_ch])
        mock_conn.close = AsyncMock()
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        await service._get_consume_channel()
        await service._get_publish_channel()

        await service.close()

        consume_ch.close.assert_awaited_once()
        publish_ch.close.assert_awaited_once()
        mock_conn.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Dead-channel ack/nack handling
# ---------------------------------------------------------------------------


class TestDeadChannelAckNack:
    @pytest.mark.parametrize(
        "exc_factory",
        [
            lambda: aio_pika.exceptions.MessageProcessError("dead", None),
            lambda: aiormq.exceptions.ChannelInvalidStateError("dead"),
        ],
    )
    async def test_ack_on_dead_channel_raises_typed_error(self, exc_factory):
        msg = _mock_message(delivery_tag=42)
        msg.ack = AsyncMock(side_effect=exc_factory())
        service = AsyncRabbitMQService(_make_client())
        # Pre-populate the consume cache so we can verify invalidation.
        service._consume_channel = _mock_channel()
        service._queues["q"] = MagicMock()
        service._exchanges["x"] = MagicMock()

        with pytest.raises(AckOnDeadChannelError) as info:
            await service.ack(msg)

        assert info.value.delivery_tag == 42
        assert info.value.queue == "test.key"
        assert info.value.__cause__ is not None
        # Cache must be invalidated so the next consume rebuilds the
        # subscription on a fresh channel and the broker redelivers.
        assert service._consume_channel is None
        assert service._queues == {}
        assert service._exchanges == {}

    async def test_ack_on_dead_channel_closes_channel(self):
        """Teardown must explicitly close the dead channel (kills zombie + leak)."""
        msg = _mock_message(delivery_tag=7)
        msg.ack = AsyncMock(side_effect=aio_pika.exceptions.MessageProcessError("dead", None))
        service = AsyncRabbitMQService(_make_client())
        dead_channel = _mock_channel()
        dead_channel.close = AsyncMock()
        service._consume_channel = dead_channel

        with pytest.raises(AckOnDeadChannelError):
            await service.ack(msg)

        dead_channel.close.assert_awaited_once()
        assert service._consume_channel is None
        assert service._active_consumer_tag is None

    async def test_ack_timeout_raises_dead_channel_and_tears_down(self):
        """A stalled ack (half-open socket) must fail fast as a dead channel."""

        async def _slow_ack():
            await asyncio.sleep(1)

        msg = _mock_message(delivery_tag=11)
        msg.ack = _slow_ack
        service = AsyncRabbitMQService(_make_client(ack_timeout=0.05))
        dead_channel = _mock_channel()
        dead_channel.close = AsyncMock()
        service._consume_channel = dead_channel

        with pytest.raises(AckOnDeadChannelError):
            await service.ack(msg)

        dead_channel.close.assert_awaited_once()
        assert service._consume_channel is None

    async def test_ack_success_records_liveness(self):
        msg = _mock_message()
        service = AsyncRabbitMQService(_make_client())
        service._in_flight_message_ids.add(id(msg))
        assert service._last_ack_ts is None
        await service.ack(msg)
        assert service._last_ack_ts is not None
        assert service.health()["in_flight_count"] == 0

    async def test_teardown_consume_channel_closes_and_clears(self):
        service = AsyncRabbitMQService(_make_client())
        ch = _mock_channel()
        ch.close = AsyncMock()
        service._consume_channel = ch
        service._queues["q"] = MagicMock()
        service._exchanges["x"] = MagicMock()
        service._active_consumer_tag = "ctag-1"

        await service._teardown_consume_channel("test")

        ch.close.assert_awaited_once()
        assert service._consume_channel is None
        assert service._queues == {}
        assert service._exchanges == {}
        assert service._active_consumer_tag is None

    async def test_nack_on_dead_channel_raises_typed_error(self):
        msg = _mock_message(delivery_tag=99)
        msg.nack = AsyncMock(side_effect=aio_pika.exceptions.MessageProcessError("dead", None))
        service = AsyncRabbitMQService(_make_client())
        service._consume_channel = _mock_channel()
        service._queues["q"] = MagicMock()

        with pytest.raises(NackOnDeadChannelError) as info:
            await service.nack(msg, requeue=False)

        assert info.value.delivery_tag == 99
        assert isinstance(info.value, DeadChannelError)
        assert service._consume_channel is None
        assert service._queues == {}

    async def test_ack_success_does_not_invalidate_cache(self):
        msg = _mock_message()
        service = AsyncRabbitMQService(_make_client())
        cached_channel = _mock_channel()
        service._consume_channel = cached_channel
        cached_queue = MagicMock()
        service._queues["q"] = cached_queue

        await service.ack(msg)

        msg.ack.assert_awaited_once()
        assert service._consume_channel is cached_channel
        assert service._queues == {"q": cached_queue}

    async def test_invalidate_consume_channel_resets_caches(self):
        service = AsyncRabbitMQService(_make_client())
        service._consume_channel = _mock_channel()
        service._queues["q"] = MagicMock()
        service._exchanges["x"] = MagicMock()

        service._invalidate_consume_channel("test")

        assert service._consume_channel is None
        assert service._queues == {}
        assert service._exchanges == {}

    async def test_channel_close_callback_invalidates_consume_channel_only(self):
        service = AsyncRabbitMQService(_make_client())
        consume_ch = _mock_channel()
        consume_ch.close = AsyncMock()
        publish_ch = _mock_channel()
        service._consume_channel = consume_ch
        service._publish_channel = publish_ch
        service._queues["q"] = MagicMock()
        service._exchanges["x"] = MagicMock()

        # Simulate aio_pika invoking the close callback that we registered
        # in ``_get_consume_channel``. The cache must be invalidated
        # synchronously; the explicit close is scheduled on the loop.
        service._on_consume_channel_close(consume_ch, ConnectionError("bye"))

        assert service._consume_channel is None
        assert service._queues == {}
        assert service._exchanges == {}
        # Publish channel and its state must survive a consume-side outage.
        assert service._publish_channel is publish_ch

        # Let the scheduled teardown task run; it must close the dead channel
        # so the broker drops its consumers and the channel does not leak.
        await asyncio.sleep(0)
        consume_ch.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Channel split: publish independent from consume
# ---------------------------------------------------------------------------


class TestChannelSplit:
    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_consume_and_publish_use_separate_channels(self, mock_connect):
        consume_ch = _mock_channel()
        publish_ch = _mock_channel()

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(side_effect=[consume_ch, publish_ch])
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        ch1 = await service._get_consume_channel()
        ch2 = await service._get_publish_channel()

        assert ch1 is consume_ch
        assert ch2 is publish_ch
        assert ch1 is not ch2
        assert mock_conn.channel.await_count == 2

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_publish_survives_consume_channel_death(self, mock_connect):
        publish_ch = _mock_channel()
        publish_exchange = AsyncMock()
        publish_exchange.publish = AsyncMock()
        publish_ch.declare_exchange = AsyncMock(return_value=publish_exchange)

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=publish_ch)
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        # Simulate a previously-cached, now-dead consume channel.
        service._consume_channel = _mock_channel()
        service._invalidate_consume_channel("simulated broker cancel")

        config = AsyncRabbitmqProducerConfig(exchange_name="events", exchange_type="topic")
        await service.publish(config, body=b"{}", routing_key="r")

        publish_exchange.publish.assert_awaited_once()
        # Publish must have used a fresh, independent channel — never
        # reused the invalidated consume channel.
        assert service._publish_channel is publish_ch


# ---------------------------------------------------------------------------
# Liveness / health surface
# ---------------------------------------------------------------------------


class TestHealth:
    async def test_health_reports_initial_state(self):
        service = AsyncRabbitMQService(_make_client())
        health = service.health()
        assert health["connection_open"] is False
        assert health["consume_channel_open"] is False
        assert health["active_consumer_tag"] is None
        assert health["last_fetch_ts"] is None
        assert health["last_ack_ts"] is None
        assert health["seconds_since_last_ack"] is None

    async def test_health_reports_elapsed_since_ack(self):
        service = AsyncRabbitMQService(_make_client())
        service._last_ack_ts = time.time() - 5
        service._active_consumer_tag = "ctag-9"
        health = service.health()
        assert health["active_consumer_tag"] == "ctag-9"
        assert health["seconds_since_last_ack"] is not None
        assert health["seconds_since_last_ack"] >= 5
