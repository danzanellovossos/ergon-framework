"""Tests for ergon.task.mixins.consumer — AsyncConsumerMixin lifecycle, fetch, and consume loop."""

import asyncio
from concurrent import futures
from unittest.mock import patch

import pytest

from ergon.connector import Transaction
from ergon.task import exceptions, policies
from tests.task.mocks import MockAsyncConnector, MockAsyncConsumer, make_transactions

pytestmark = pytest.mark.asyncio(loop_scope="function")


# =====================================================================
#   Transaction lifecycle (_start_processing)
# =====================================================================


class TestTransactionLifecycle:
    async def test_happy_path(self):
        consumer = MockAsyncConsumer()
        tx = Transaction(id="1", payload="data")
        policy = policies.ConsumerPolicy()

        success, result = await consumer._start_processing(tx, policy)

        assert success is True
        assert tx in consumer.processed

    async def test_process_fails(self):
        async def failing_process(tx):
            raise ValueError("process error")

        exception_called = []

        async def track_exception(tx, exc):
            exception_called.append(exc)

        consumer = MockAsyncConsumer(
            process_fn=failing_process,
            exception_fn=track_exception,
        )
        tx = Transaction(id="1", payload="data")
        policy = policies.ConsumerPolicy()

        await consumer._start_processing(tx, policy)

        assert len(exception_called) == 1
        assert isinstance(exception_called[0], exceptions.TransactionException)
        assert exception_called[0].category == exceptions.ExceptionType.SYSTEM

    async def test_process_timeout(self):
        async def timeout_process(tx):
            raise futures.TimeoutError("timed out")

        exception_called = []

        async def track_exception(tx, exc):
            exception_called.append(exc)

        consumer = MockAsyncConsumer(
            process_fn=timeout_process,
            exception_fn=track_exception,
        )
        tx = Transaction(id="1", payload="data")
        policy = policies.ConsumerPolicy()

        await consumer._start_processing(tx, policy)

        assert len(exception_called) == 1
        assert isinstance(exception_called[0], exceptions.TransactionException)
        assert exception_called[0].category == exceptions.ExceptionType.TIMEOUT

    async def test_success_handler_fails(self):
        async def failing_success(tx, result):
            raise RuntimeError("success handler broke")

        exception_called = []

        async def track_exception(tx, exc):
            exception_called.append(exc)

        consumer = MockAsyncConsumer(
            success_fn=failing_success,
            exception_fn=track_exception,
        )
        tx = Transaction(id="1", payload="data")
        policy = policies.ConsumerPolicy()

        await consumer._start_processing(tx, policy)

        assert len(exception_called) == 1
        assert isinstance(exception_called[0], exceptions.TransactionException)


# =====================================================================
#   Fetch behavior (_handle_fetch)
# =====================================================================


class TestFetch:
    async def test_normal_fetch(self):
        conn = MockAsyncConnector(make_transactions(10))
        consumer = MockAsyncConsumer()
        fetch_policy = policies.FetchPolicy(batch=policies.BatchPolicy(size=5))

        success, result = await consumer._handle_fetch(conn, fetch_policy)

        assert success is True
        assert len(result) == 5
        assert result[0].id == "0"
        assert result[4].id == "4"

    async def test_empty_fetch(self):
        conn = MockAsyncConnector([])
        consumer = MockAsyncConsumer()
        fetch_policy = policies.FetchPolicy()

        success, result = await consumer._handle_fetch(conn, fetch_policy)

        assert success is True
        assert result == []

    async def test_fetch_failure(self):
        conn = MockAsyncConnector()

        async def exploding_fetch(*args, **kwargs):
            raise RuntimeError("connection lost")

        conn.fetch_transactions_async = exploding_fetch

        consumer = MockAsyncConsumer()
        fetch_policy = policies.FetchPolicy()

        success, result = await consumer._handle_fetch(conn, fetch_policy)

        assert success is False
        assert isinstance(result, RuntimeError)


# =====================================================================
#   Consume loop — non-streaming
# =====================================================================


class TestConsumeNonStreaming:
    async def test_single_batch_all_processed(self):
        conn = MockAsyncConnector(make_transactions(5))
        consumer = MockAsyncConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=5)),
        )

        result = await consumer.consume_transactions(policy=policy)

        assert result == 5
        assert len(consumer.processed) == 5

    async def test_multiple_batches(self):
        conn = MockAsyncConnector(make_transactions(10))
        consumer = MockAsyncConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=3)),
        )

        result = await consumer.consume_transactions(policy=policy)

        assert result == 10
        assert len(consumer.processed) == 10

    async def test_concurrency(self):
        active = 0
        peak = 0

        async def tracked_process(tx):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1
            return tx.payload

        conn = MockAsyncConnector(make_transactions(9))
        consumer = MockAsyncConsumer(connectors={"default": conn}, process_fn=tracked_process)
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=9)),
            loop=policies.ConsumerLoopPolicy(
                concurrency=policies.ConcurrencyPolicy(value=3),
            ),
        )

        result = await consumer.consume_transactions(policy=policy)

        assert result == 9
        assert peak <= 3

    async def test_limit(self):
        conn = MockAsyncConnector(make_transactions(20))
        consumer = MockAsyncConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=10)),
            loop=policies.ConsumerLoopPolicy(limit=5),
        )

        result = await consumer.consume_transactions(policy=policy)

        assert result == 5

    async def test_fetch_failure_raises(self):
        conn = MockAsyncConnector()

        async def exploding_fetch(*args, **kwargs):
            raise RuntimeError("connection lost")

        conn.fetch_transactions_async = exploding_fetch

        consumer = MockAsyncConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy()

        with pytest.raises(exceptions.FetchException):
            await consumer.consume_transactions(policy=policy)

    async def test_non_streaming_breaks_on_empty(self):
        conn = MockAsyncConnector(make_transactions(3))
        consumer = MockAsyncConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=3)),
        )

        result = await consumer.consume_transactions(policy=policy)

        assert result == 3
        assert len(consumer.processed) == 3

    async def test_loop_timeout(self):
        async def forever_process(tx):
            await asyncio.sleep(10)

        conn = MockAsyncConnector(make_transactions(1))
        consumer = MockAsyncConsumer(connectors={"default": conn}, process_fn=forever_process)
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=1)),
            loop=policies.ConsumerLoopPolicy(timeout=0.3),
        )

        with pytest.raises(exceptions.ConsumerLoopTimeoutException):
            await consumer.consume_transactions(policy=policy)

    @patch("ergon.task.utils.backoff_async")
    async def test_batch_interval(self, mock_backoff):
        conn = MockAsyncConnector(make_transactions(6))
        consumer = MockAsyncConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(
                batch=policies.BatchPolicy(
                    size=3,
                    interval=policies.BatchIntervalPolicy(backoff=0.5),
                ),
            ),
        )

        result = await consumer.consume_transactions(policy=policy)

        assert result == 6
        assert mock_backoff.call_count >= 1
        first_call_kwargs = mock_backoff.call_args_list[0][1]
        assert first_call_kwargs["backoff"] == 0.5


# =====================================================================
#   Consume loop — streaming
# =====================================================================


class TestConsumeStreaming:
    @patch("ergon.task.utils.backoff_async")
    async def test_streaming_continues_on_empty(self, mock_backoff):
        txs = make_transactions(3)
        conn = MockAsyncConnector(txs)

        refill_done = False

        async def process_and_refill(tx):
            nonlocal refill_done
            if tx.id == "2" and not refill_done:
                conn._queue.extend(make_transactions(2))
                refill_done = True
            return tx.payload

        consumer = MockAsyncConsumer(connectors={"default": conn}, process_fn=process_and_refill)
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(
                batch=policies.BatchPolicy(size=3),
                empty=policies.EmptyFetchPolicy(backoff=0.01),
            ),
            loop=policies.ConsumerLoopPolicy(streaming=True, limit=5),
        )

        result = await consumer.consume_transactions(policy=policy)

        assert result == 5

    @patch("ergon.task.utils.backoff_async")
    async def test_empty_backoff_called(self, mock_backoff):
        conn = MockAsyncConnector([])

        def side_effect_refill(*args, **kwargs):
            if mock_backoff.call_count >= 3:
                conn._queue.extend(make_transactions(1))

        mock_backoff.side_effect = side_effect_refill

        consumer = MockAsyncConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(
                batch=policies.BatchPolicy(size=5),
                empty=policies.EmptyFetchPolicy(backoff=1.0),
            ),
            loop=policies.ConsumerLoopPolicy(streaming=True, limit=1),
        )

        result = await consumer.consume_transactions(policy=policy)

        assert result >= 1
        assert mock_backoff.call_count >= 3
        assert mock_backoff.call_args_list[0][1]["attempt"] == 0
        assert mock_backoff.call_args_list[1][1]["attempt"] == 1
        assert mock_backoff.call_args_list[2][1]["attempt"] == 2

    @patch("ergon.task.utils.backoff_async")
    async def test_empty_count_resets_after_items(self, mock_backoff):
        conn = MockAsyncConnector([Transaction(id="0", payload={"i": 0})])

        def backoff_side_effect(*args, **kwargs):
            conn._queue.append(
                Transaction(id=str(100 + mock_backoff.call_count), payload={"i": mock_backoff.call_count})
            )

        mock_backoff.side_effect = backoff_side_effect

        consumer = MockAsyncConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(
                batch=policies.BatchPolicy(size=1),
                empty=policies.EmptyFetchPolicy(backoff=0.01),
            ),
            loop=policies.ConsumerLoopPolicy(streaming=True, limit=3),
        )

        result = await consumer.consume_transactions(policy=policy)

        assert result == 3
        backoff_attempts = [c[1]["attempt"] for c in mock_backoff.call_args_list]
        assert backoff_attempts.count(0) >= 2


# =====================================================================
#   Connector resolution
# =====================================================================


class TestConnectorResolution:
    async def test_single_connector_auto_resolved(self):
        conn = MockAsyncConnector(make_transactions(1))
        consumer = MockAsyncConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=1)),
        )

        result = await consumer.consume_transactions(policy=policy)

        assert result == 1

    async def test_named_connector_resolved(self):
        conn_foo = MockAsyncConnector(make_transactions(2))
        conn_bar = MockAsyncConnector(make_transactions(5))
        consumer = MockAsyncConsumer(connectors={"foo": conn_foo, "bar": conn_bar})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(
                connector_name="foo",
                batch=policies.BatchPolicy(size=10),
            ),
        )

        result = await consumer.consume_transactions(policy=policy)

        assert result == 2

    async def test_multiple_without_name_raises(self):
        conn_a = MockAsyncConnector()
        conn_b = MockAsyncConnector()
        consumer = MockAsyncConsumer(connectors={"a": conn_a, "b": conn_b})
        policy = policies.ConsumerPolicy()

        with pytest.raises(ValueError):
            await consumer.consume_transactions(policy=policy)
