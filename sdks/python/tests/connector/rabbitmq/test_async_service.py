"""Tests for AsyncRabbitMQService — connection lifecycle, consume, publish, ack/nack."""

import asyncio
import gc
import json
import sys
import time
from typing import Any, cast
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
    channel.close_callbacks = MagicMock()
    channel.close_callbacks.add = MagicMock()
    # Legacy fallback is also synchronous on older aio-pika channels.
    channel.add_close_callback = MagicMock()
    channel.set_qos = AsyncMock()
    return channel


class _HangingInitialRobustConnection:
    """Model aio-pika's reconnect task before ``connect_robust`` returns."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.is_closed = False
        self.close_calls = 0
        self.reconnect_started = asyncio.Event()
        self.reconnect_task: asyncio.Task[None] | None = None

    async def _reconnect_forever(self) -> None:
        self.reconnect_started.set()
        await asyncio.Event().wait()

    async def connect(self, timeout: float | None = None) -> None:
        del timeout
        self.reconnect_task = asyncio.create_task(
            self._reconnect_forever(),
            name="fake-aio-pika-reconnect",
        )
        await self.reconnect_started.wait()
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.close_calls += 1
        self.is_closed = True
        if self.reconnect_task is not None:
            self.reconnect_task.cancel()
            await asyncio.gather(self.reconnect_task, return_exceptions=True)


class _FailingInitialRobustConnection(_HangingInitialRobustConnection):
    async def connect(self, timeout: float | None = None) -> None:
        del timeout
        self.reconnect_task = asyncio.create_task(
            self._reconnect_forever(),
            name="fake-aio-pika-reconnect",
        )
        await self.reconnect_started.wait()
        raise aiormq.exceptions.AuthenticationError("invalid credentials")


class _SuccessfulInitialRobustConnection(_HangingInitialRobustConnection):
    async def connect(self, timeout: float | None = None) -> None:
        del timeout


def _install_initial_connect_double(
    monkeypatch: pytest.MonkeyPatch,
    connection_type: Any,
) -> list[_HangingInitialRobustConnection]:
    """Install a faithful ``connect_robust`` ownership boundary test double."""

    created: list[_HangingInitialRobustConnection] = []

    async def connect_robust(
        url: str,
        *,
        connection_class: Any = connection_type,
        timeout: float | None = None,
        loop: Any = None,
        ssl_context: Any = None,
        **kwargs: Any,
    ) -> Any:
        connection = connection_class(
            url,
            loop=loop,
            ssl_context=ssl_context,
            **kwargs,
        )
        created.append(connection)
        await connection.connect(timeout=timeout)
        return connection

    monkeypatch.setattr(aio_pika, "RobustConnection", connection_type)
    monkeypatch.setattr(aio_pika, "connect_robust", connect_robust)
    return created


def _mock_consumer_queue(
    *,
    name: str = "test-queue",
    messages: list[Any] | None = None,
    consumer_tag: str | None = None,
) -> tuple[MagicMock, list[Any]]:
    """Build a queue whose long-lived consumer can receive test deliveries."""
    callbacks: list[Any] = []

    async def consume(callback, **_kwargs):
        callbacks.append(callback)
        for message in messages or []:
            await callback(message)
        return consumer_tag or f"ctag-{name}"

    queue = MagicMock()
    queue.name = name
    queue.consume = AsyncMock(side_effect=consume)
    queue.bind = AsyncMock()
    return queue, callbacks


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
    async def test_initial_connect_timeout_closes_reconnect_task(self, monkeypatch):
        created = _install_initial_connect_double(monkeypatch, _HangingInitialRobustConnection)
        service = AsyncRabbitMQService(_make_client(connect_timeout=0.02))

        try:
            with pytest.raises(asyncio.TimeoutError):
                await service._get_connection()

            assert len(created) == 1
            connection = created[0]
            assert connection.close_calls == 1
            assert connection.is_closed is True
            assert connection.reconnect_task is not None
            assert connection.reconnect_task.done()
            assert service._connection is None
            assert service.health()["state"] == "connect_stalled"
        finally:
            for connection in created:
                await connection.close()

    async def test_initial_connect_cancellation_closes_reconnect_task(self, monkeypatch):
        created = _install_initial_connect_double(monkeypatch, _HangingInitialRobustConnection)
        service = AsyncRabbitMQService(_make_client(connect_timeout=30))
        connecting = asyncio.create_task(service._get_connection())

        try:
            while not created:
                await asyncio.sleep(0)
            await created[0].reconnect_started.wait()
            connecting.cancel()

            with pytest.raises(asyncio.CancelledError):
                await connecting

            connection = created[0]
            assert connection.close_calls == 1
            assert connection.is_closed is True
            assert connection.reconnect_task is not None
            assert connection.reconnect_task.done()
            assert service._connection is None
            assert service.health()["state"] == "disconnected"
        finally:
            if not connecting.done():
                connecting.cancel()
                await asyncio.gather(connecting, return_exceptions=True)
            for connection in created:
                await connection.close()

    async def test_initial_connect_error_closes_reconnect_task(self, monkeypatch):
        created = _install_initial_connect_double(monkeypatch, _FailingInitialRobustConnection)
        service = AsyncRabbitMQService(_make_client(connect_timeout=1))

        try:
            with pytest.raises(aiormq.exceptions.AuthenticationError, match="invalid credentials"):
                await service._get_connection()

            assert len(created) == 1
            connection = created[0]
            assert connection.close_calls == 1
            assert connection.is_closed is True
            assert connection.reconnect_task is not None
            assert connection.reconnect_task.done()
            assert service._connection is None
            assert service.health()["state"] == "disconnected"
        finally:
            for connection in created:
                await connection.close()

    async def test_successful_initial_connect_transfers_ownership_to_service(self, monkeypatch):
        created = _install_initial_connect_double(monkeypatch, _SuccessfulInitialRobustConnection)
        service = AsyncRabbitMQService(_make_client(connect_timeout=1))

        connection = cast(
            _SuccessfulInitialRobustConnection,
            await service._get_connection(),
        )

        assert created == [connection]
        assert connection.close_calls == 0
        assert connection.is_closed is False
        assert service._connection is connection
        assert service.health()["state"] == "connected"

        await service.close()

        assert connection.close_calls == 1
        assert connection.is_closed is True
        assert service._connection is None

    async def test_retry_after_initial_timeout_uses_fresh_owned_connection(self, monkeypatch):
        connection_types = iter(
            [
                _HangingInitialRobustConnection,
                _SuccessfulInitialRobustConnection,
            ]
        )

        def connection_factory(*args: Any, **kwargs: Any) -> _HangingInitialRobustConnection:
            return next(connection_types)(*args, **kwargs)

        created = _install_initial_connect_double(monkeypatch, connection_factory)
        service = AsyncRabbitMQService(_make_client(connect_timeout=0.02))

        with pytest.raises(asyncio.TimeoutError):
            await service._get_connection()

        connection = cast(
            _SuccessfulInitialRobustConnection,
            await service._get_connection(),
        )

        assert len(created) == 2
        assert created[0].close_calls == 1
        assert created[0].reconnect_task is not None
        assert created[0].reconnect_task.done()
        assert connection is created[1]
        assert connection.close_calls == 0
        assert service._connection is connection

        await service.close()
        assert connection.close_calls == 1

    async def test_real_aio_pika_handshake_timeout_releases_reconnect_task(self, monkeypatch):
        created = []
        accepted_connections: set[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = set()
        real_connection_class = aio_pika.RobustConnection

        def record_connection(*args: Any, **kwargs: Any) -> Any:
            connection = real_connection_class(*args, **kwargs)
            created.append(connection)
            return connection

        def accept_without_amqp_handshake(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            accepted_connections.add((reader, writer))

        server = await asyncio.start_server(
            accept_without_amqp_handshake,
            host="127.0.0.1",
            port=0,
        )
        port = server.sockets[0].getsockname()[1]
        service = AsyncRabbitMQService(
            _make_client(
                url=f"amqp://guest:guest@127.0.0.1:{port}/",
                connect_timeout=0.05,
            )
        )
        monkeypatch.setattr(aio_pika, "RobustConnection", record_connection)

        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(service._get_connection(), timeout=1)

            assert len(created) == 1
            assert (
                getattr(
                    created[0],
                    "_RobustConnection__reconnection_task",
                )
                is None
            )
            assert service._connection is None
            assert service.health()["state"] == "connect_stalled"
            assert accepted_connections

            # On production Python (3.12+) and current runtimes, aiormq also
            # closes the partial handshake socket when its task is cancelled.
            # Python 3.10's asyncio transport defers that final socket cleanup
            # to collection, but the reconnect task that can wedge
            # asyncio.run() is already deterministically gone above.
            if sys.version_info >= (3, 11):
                for reader, _writer in accepted_connections:
                    await asyncio.wait_for(reader.read(), timeout=1)
        finally:
            await service.close()
            for _reader, writer in accepted_connections:
                writer.close()
            await asyncio.gather(
                *(writer.wait_closed() for _reader, writer in accepted_connections),
                return_exceptions=True,
            )
            server.close()
            await server.wait_closed()
            created.clear()
            gc.collect()
            await asyncio.sleep(0)

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_connect_hang_is_bounded(self, mock_connect):
        async def never_connects(*args, **kwargs):
            await asyncio.Event().wait()

        mock_connect.side_effect = never_connects
        service = AsyncRabbitMQService(_make_client(connect_timeout=0.02))

        with pytest.raises(asyncio.TimeoutError):
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

        with pytest.raises(asyncio.TimeoutError):
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
    async def test_consume_channel_registers_current_close_callback_api(self, mock_connect):
        mock_channel = _mock_channel()
        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        await service._get_consume_channel()

        mock_channel.close_callbacks.add.assert_called_once_with(
            service._on_consume_channel_close,
            weak=True,
        )
        mock_channel.add_close_callback.assert_not_called()

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

    def test_prefetch_must_bound_the_delivery_buffer(self):
        with pytest.raises(ValueError, match="greater than 0"):
            AsyncRabbitmqConsumerConfig(queue_name="test-queue", prefetch_count=0)

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_consume_returns_messages(self, mock_connect):
        msg = _mock_message()
        mock_queue, _callbacks = _mock_consumer_queue(messages=[msg])

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
        mock_queue.consume.assert_awaited_once()
        _, consume_kwargs = mock_queue.consume.call_args
        assert consume_kwargs == {"no_ack": False, "robust": False}

    def test_auto_ack_is_rejected_without_broker_backpressure(self):
        with pytest.raises(ValueError, match="bypasses prefetch backpressure"):
            AsyncRabbitmqConsumerConfig(
                queue_name="test-queue",
                auto_ack=True,
            )

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_consume_returns_first_delivery_without_waiting_to_fill_batch(self, mock_connect):
        first_message = _mock_message(delivery_tag=1)
        mock_queue, _callbacks = _mock_consumer_queue(messages=[first_message])

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
        mock_queue, _callbacks = _mock_consumer_queue(messages=messages)

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
        queue_one, _callbacks_one = _mock_consumer_queue(
            name="audit.iam",
            messages=[first_message],
        )
        queue_two, _callbacks_two = _mock_consumer_queue(name="audit.cross-service")

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
        queue_one, _callbacks_one = _mock_consumer_queue(
            name="one",
            messages=[_mock_message(delivery_tag=1)],
        )
        queue_two, _callbacks_two = _mock_consumer_queue(
            name="two",
            messages=[_mock_message(delivery_tag=2)],
        )
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

        mock_queue, _callbacks = _mock_consumer_queue()

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

        mock_queue, _callbacks = _mock_consumer_queue()

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
        mock_queue, _callbacks = _mock_consumer_queue()

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
    async def test_repeated_fetches_reuse_one_registered_consumer(self, mock_connect):
        mock_queue, _callbacks = _mock_consumer_queue()
        mock_channel = _mock_channel()
        mock_channel.declare_queue = AsyncMock(return_value=mock_queue)

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        config = AsyncRabbitmqConsumerConfig(
            queue_name="test-queue",
            consume_timeout=0.001,
        )

        for _ in range(100):
            assert await service.consume(config, batch_size=10) == []

        mock_queue.consume.assert_awaited_once()
        assert mock_queue.iterator.call_count == 0
        assert service.health()["active_consumer_tags"] == {"test-queue": "ctag-test-queue"}

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_broker_cancelled_consumer_is_rebuilt_on_next_fetch(self, mock_connect):
        first_queue, _first_callbacks = _mock_consumer_queue(consumer_tag="ctag-old")
        second_queue, _second_callbacks = _mock_consumer_queue(
            messages=[_mock_message(delivery_tag=2)],
            consumer_tag="ctag-new",
        )
        underlay_channel = MagicMock()
        underlay_channel.consumers = {"ctag-old": MagicMock()}

        first_channel = _mock_channel()
        first_channel.declare_queue = AsyncMock(return_value=first_queue)
        first_channel.get_underlay_channel = AsyncMock(return_value=underlay_channel)
        second_channel = _mock_channel()
        second_channel.declare_queue = AsyncMock(return_value=second_queue)

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(side_effect=[first_channel, second_channel])
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        config = AsyncRabbitmqConsumerConfig(
            queue_name="test-queue",
            consume_timeout=0.001,
        )

        assert await service.consume(config) == []
        underlay_channel.consumers.clear()
        result = await service.consume(config)

        assert result[0]["delivery_tag"] == 2
        first_channel.close.assert_awaited_once()
        assert service.health()["active_consumer_tags"] == {"test-queue": "ctag-new"}
        assert service.health()["consumer_epoch"] == 1

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_cancelled_fetch_keeps_subscription_alive(self, mock_connect):
        mock_queue, callbacks = _mock_consumer_queue()
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

        waiting_fetch = asyncio.create_task(service.consume(config, batch_size=1))
        await asyncio.sleep(0)
        waiting_fetch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting_fetch

        await callbacks[0](_mock_message(delivery_tag=42))
        result = await service.consume(config, batch_size=1)

        assert result[0]["delivery_tag"] == 42
        mock_queue.consume.assert_awaited_once()

    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_teardown_discards_buffer_and_stale_epoch_deliveries(self, mock_connect):
        first_queue, first_callbacks = _mock_consumer_queue(name="test-queue", consumer_tag="ctag-old")
        second_queue, second_callbacks = _mock_consumer_queue(name="test-queue", consumer_tag="ctag-new")
        first_channel = _mock_channel()
        first_channel.declare_queue = AsyncMock(return_value=first_queue)
        second_channel = _mock_channel()
        second_channel.declare_queue = AsyncMock(return_value=second_queue)

        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(side_effect=[first_channel, second_channel])
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        config = AsyncRabbitmqConsumerConfig(
            queue_name="test-queue",
            consume_timeout=0.01,
        )

        await service.consume(config)
        await first_callbacks[0](_mock_message(delivery_tag=1))
        await service._teardown_consume_channel("test reset")
        await first_callbacks[0](_mock_message(delivery_tag=2))
        assert service.health()["buffered_delivery_count"] == 0

        setup_fetch = asyncio.create_task(service.consume(config))
        await asyncio.sleep(0)
        await second_callbacks[0](_mock_message(delivery_tag=3))
        result = await setup_fetch

        assert [message["delivery_tag"] for message in result] == [3]


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
    async def test_close_wakes_pending_fetch_without_overwriting_closed_state(self, mock_connect):
        mock_queue, _callbacks = _mock_consumer_queue()
        mock_channel = _mock_channel()
        mock_channel.declare_queue = AsyncMock(return_value=mock_queue)
        mock_conn = AsyncMock()
        mock_conn.is_closed = False
        mock_conn.channel = AsyncMock(return_value=mock_channel)
        mock_conn.close = AsyncMock()
        mock_connect.return_value = mock_conn

        service = AsyncRabbitMQService(_make_client())
        config = AsyncRabbitmqConsumerConfig(
            queue_name="test-queue",
            consume_timeout=30,
        )
        pending_fetch = asyncio.create_task(service.consume(config))
        for _ in range(10):
            if service._fetch_waiters:
                break
            await asyncio.sleep(0)

        await service.close()
        result = await asyncio.wait_for(pending_fetch, timeout=0.5)

        assert result == []
        assert service.health()["state"] == "closed"

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
