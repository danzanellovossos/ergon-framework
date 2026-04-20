"""Tests for AsyncRabbitMQService — connection lifecycle, consume, publish, ack/nack."""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import aio_pika.exceptions
import aiormq.exceptions
import pytest

from ergon.connector.rabbitmq.async_service import AsyncRabbitMQService
from ergon.connector.rabbitmq.models import (
    AsyncRabbitmqClient,
    AsyncRabbitmqConsumerConfig,
    AsyncRabbitmqProducerConfig,
)
from ergon.task.exceptions import (
    AckOnDeadChannelError,
    DeadChannelError,
    NackOnDeadChannelError,
)

pytestmark = pytest.mark.asyncio(loop_scope="function")


def _make_client(**overrides) -> AsyncRabbitmqClient:
    defaults = {"username": "guest", "password": "guest", "host": "localhost"}
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


class TestConnection:
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
    @patch("ergon.connector.rabbitmq.async_service.aio_pika.connect_robust", new_callable=AsyncMock)
    async def test_consume_returns_messages(self, mock_connect):
        msg = _mock_message()

        @asynccontextmanager
        async def _iterator_cm(**kwargs):
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
        publish_ch = _mock_channel()
        service._consume_channel = consume_ch
        service._publish_channel = publish_ch
        service._queues["q"] = MagicMock()
        service._exchanges["x"] = MagicMock()

        # Simulate aio_pika invoking the close callback that we registered
        # in ``_get_consume_channel``.
        service._on_consume_channel_close(consume_ch, ConnectionError("bye"))

        assert service._consume_channel is None
        assert service._queues == {}
        assert service._exchanges == {}
        # Publish channel and its state must survive a consume-side outage.
        assert service._publish_channel is publish_ch


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
