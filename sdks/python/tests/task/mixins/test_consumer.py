"""Tests for ergon.task.mixins.consumer — ConsumerMixin lifecycle, fetch, and consume loop."""

import threading
import time
from concurrent import futures
from unittest.mock import patch

import pytest

from ergon.connector import Transaction
from ergon.task import exceptions, policies
from tests.task.mocks import MockConnector, MockConsumer, make_transactions

# =====================================================================
#   Transaction lifecycle (_start_processing)
# =====================================================================


class TestTransactionLifecycle:
    def test_happy_path(self):
        consumer = MockConsumer()
        tx = Transaction(id="1", payload="data")
        policy = policies.ConsumerPolicy()

        success, result = consumer._start_processing(tx, policy)

        assert success is True
        assert tx in consumer.processed

    def test_process_fails(self):
        def failing_process(tx):
            raise ValueError("process error")

        exception_called = []

        def track_exception(tx, exc):
            exception_called.append(exc)

        consumer = MockConsumer(
            process_fn=failing_process,
            exception_fn=track_exception,
        )
        tx = Transaction(id="1", payload="data")
        policy = policies.ConsumerPolicy()

        consumer._start_processing(tx, policy)

        assert len(exception_called) == 1
        assert isinstance(exception_called[0], exceptions.TransactionException)
        assert exception_called[0].category == exceptions.ExceptionType.SYSTEM

    def test_process_timeout(self):
        def timeout_process(tx):
            raise futures.TimeoutError("timed out")

        exception_called = []

        def track_exception(tx, exc):
            exception_called.append(exc)

        consumer = MockConsumer(
            process_fn=timeout_process,
            exception_fn=track_exception,
        )
        tx = Transaction(id="1", payload="data")
        policy = policies.ConsumerPolicy()

        consumer._start_processing(tx, policy)

        assert len(exception_called) == 1
        assert isinstance(exception_called[0], exceptions.TransactionException)
        assert exception_called[0].category == exceptions.ExceptionType.TIMEOUT

    def test_success_handler_fails(self):
        def failing_success(tx, result):
            raise RuntimeError("success handler broke")

        exception_called = []

        def track_exception(tx, exc):
            exception_called.append(exc)

        consumer = MockConsumer(
            success_fn=failing_success,
            exception_fn=track_exception,
        )
        tx = Transaction(id="1", payload="data")
        policy = policies.ConsumerPolicy()

        consumer._start_processing(tx, policy)

        assert len(exception_called) == 1
        assert isinstance(exception_called[0], exceptions.TransactionException)


# =====================================================================
#   Fetch behavior (_handle_fetch)
# =====================================================================


class TestFetch:
    def test_normal_fetch(self):
        conn = MockConnector(make_transactions(10))
        consumer = MockConsumer()
        fetch_policy = policies.FetchPolicy(batch=policies.BatchPolicy(size=5))

        success, result = consumer._handle_fetch(conn, fetch_policy)

        assert success is True
        assert len(result) == 5
        assert result[0].id == "0"
        assert result[4].id == "4"

    def test_empty_fetch(self):
        conn = MockConnector([])
        consumer = MockConsumer()
        fetch_policy = policies.FetchPolicy()

        success, result = consumer._handle_fetch(conn, fetch_policy)

        assert success is True
        assert result == []

    def test_fetch_failure(self):
        conn = MockConnector()

        def exploding_fetch(*args, **kwargs):
            raise RuntimeError("connection lost")

        conn.fetch_transactions = exploding_fetch

        consumer = MockConsumer()
        fetch_policy = policies.FetchPolicy()

        success, result = consumer._handle_fetch(conn, fetch_policy)

        assert success is False
        assert isinstance(result, RuntimeError)


# =====================================================================
#   Consume loop — non-streaming
# =====================================================================


class TestConsumeNonStreaming:
    def test_single_batch_all_processed(self):
        conn = MockConnector(make_transactions(5))
        consumer = MockConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=5)),
        )

        result = consumer.consume_transactions(policy=policy)

        assert result == 5
        assert len(consumer.processed) == 5

    def test_multiple_batches(self):
        conn = MockConnector(make_transactions(10))
        consumer = MockConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=3)),
        )

        result = consumer.consume_transactions(policy=policy)

        assert result == 10
        assert len(consumer.processed) == 10

    def test_concurrency(self):
        lock = threading.Lock()
        active = [0]
        peak = [0]

        def tracked_process(tx):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.03)
            with lock:
                active[0] -= 1
            return tx.payload

        conn = MockConnector(make_transactions(9))
        consumer = MockConsumer(connectors={"default": conn}, process_fn=tracked_process)
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=9)),
            loop=policies.ConsumerLoopPolicy(
                concurrency=policies.ConcurrencyPolicy(value=3),
            ),
        )

        result = consumer.consume_transactions(policy=policy)

        assert result == 9
        assert peak[0] <= 3

    def test_limit(self):
        conn = MockConnector(make_transactions(20))
        consumer = MockConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=10)),
            loop=policies.ConsumerLoopPolicy(limit=5),
        )

        result = consumer.consume_transactions(policy=policy)

        assert result == 5

    def test_transaction_timeout(self):
        def blocking_process(tx):
            time.sleep(0.5)
            return tx.payload

        conn = MockConnector(make_transactions(3))
        consumer = MockConsumer(connectors={"default": conn}, process_fn=blocking_process)
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=3)),
            transaction_runtime=policies.TransactionRuntimePolicy(timeout=0.1),
            loop=policies.ConsumerLoopPolicy(timeout=5.0),
        )

        result = consumer.consume_transactions(policy=policy)

        assert result == 3

    def test_fetch_failure_raises(self):
        conn = MockConnector()

        def exploding_fetch(*args, **kwargs):
            raise RuntimeError("connection lost")

        conn.fetch_transactions = exploding_fetch

        consumer = MockConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy()

        with pytest.raises(exceptions.FetchException):
            consumer.consume_transactions(policy=policy)

    def test_non_streaming_breaks_on_empty(self):
        conn = MockConnector(make_transactions(3))
        consumer = MockConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=3)),
        )

        result = consumer.consume_transactions(policy=policy)

        assert result == 3
        assert len(consumer.processed) == 3

    def test_loop_timeout(self):
        def forever_process(tx):
            time.sleep(10)

        conn = MockConnector(make_transactions(1))
        consumer = MockConsumer(connectors={"default": conn}, process_fn=forever_process)
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=1)),
            loop=policies.ConsumerLoopPolicy(timeout=0.3),
        )

        with pytest.raises(exceptions.ConsumerLoopTimeoutException):
            consumer.consume_transactions(policy=policy)

    @patch("ergon.task.utils.backoff")
    def test_batch_interval(self, mock_backoff):
        conn = MockConnector(make_transactions(6))
        consumer = MockConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(
                batch=policies.BatchPolicy(
                    size=3,
                    interval=policies.BatchIntervalPolicy(backoff=0.5),
                ),
            ),
        )

        result = consumer.consume_transactions(policy=policy)

        assert result == 6
        assert mock_backoff.call_count >= 1
        first_call_kwargs = mock_backoff.call_args_list[0][1]
        assert first_call_kwargs["backoff"] == 0.5


# =====================================================================
#   Consume loop — streaming
# =====================================================================


class TestConsumeStreaming:
    @patch("ergon.task.utils.backoff")
    def test_streaming_continues_on_empty(self, mock_backoff):
        txs = make_transactions(3)
        conn = MockConnector(txs)

        refill_done = [False]

        def process_and_refill(tx):
            if tx.id == "2" and not refill_done[0]:
                conn._queue.extend(make_transactions(2))
                refill_done[0] = True
            return tx.payload

        consumer = MockConsumer(connectors={"default": conn}, process_fn=process_and_refill)
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(
                batch=policies.BatchPolicy(size=3),
                empty=policies.EmptyFetchPolicy(backoff=0.01),
            ),
            loop=policies.ConsumerLoopPolicy(streaming=True, limit=5),
        )

        result = consumer.consume_transactions(policy=policy)

        assert result == 5

    @patch("ergon.task.utils.backoff")
    def test_empty_backoff_called(self, mock_backoff):
        conn = MockConnector([])

        def side_effect_refill(*args, **kwargs):
            if mock_backoff.call_count >= 3:
                conn._queue.extend(make_transactions(1))

        mock_backoff.side_effect = side_effect_refill

        consumer = MockConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(
                batch=policies.BatchPolicy(size=5),
                empty=policies.EmptyFetchPolicy(backoff=1.0),
            ),
            loop=policies.ConsumerLoopPolicy(streaming=True, limit=1),
        )

        result = consumer.consume_transactions(policy=policy)

        assert result >= 1
        assert mock_backoff.call_count >= 3
        assert mock_backoff.call_args_list[0][0][3] == 0
        assert mock_backoff.call_args_list[1][0][3] == 1
        assert mock_backoff.call_args_list[2][0][3] == 2

    @patch("ergon.task.utils.backoff")
    def test_empty_count_resets_after_items(self, mock_backoff):
        conn = MockConnector([Transaction(id="0", payload={"i": 0})])

        def backoff_side_effect(*args, **kwargs):
            conn._queue.append(
                Transaction(id=str(100 + mock_backoff.call_count), payload={"i": mock_backoff.call_count})
            )

        mock_backoff.side_effect = backoff_side_effect

        consumer = MockConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(
                batch=policies.BatchPolicy(size=1),
                empty=policies.EmptyFetchPolicy(backoff=0.01),
            ),
            loop=policies.ConsumerLoopPolicy(streaming=True, limit=3),
        )

        result = consumer.consume_transactions(policy=policy)

        assert result == 3
        backoff_attempts = [c[0][3] for c in mock_backoff.call_args_list]
        assert backoff_attempts.count(0) >= 2


# =====================================================================
#   Headroom
# =====================================================================


class TestHeadroom:
    def test_headroom_provides_extra_capacity(self):
        gate = threading.Event()
        lock = threading.Lock()
        active = [0]
        peak = [0]

        def process_fn(tx):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            if tx.id in ("0", "1"):
                gate.wait(timeout=2)
            else:
                time.sleep(0.05)
            with lock:
                active[0] -= 1
            return tx.payload

        conn = MockConnector(make_transactions(6))
        consumer = MockConsumer(connectors={"default": conn}, process_fn=process_fn)
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=6)),
            loop=policies.ConsumerLoopPolicy(
                concurrency=policies.ConcurrencyPolicy(value=2, headroom=2),
                timeout=10.0,
            ),
            transaction_runtime=policies.TransactionRuntimePolicy(timeout=0.3),
        )

        result = consumer.consume_transactions(policy=policy)

        gate.set()
        assert result == 6
        assert peak[0] > 2


# =====================================================================
#   Connector resolution
# =====================================================================


class TestConnectorResolution:
    def test_single_connector_auto_resolved(self):
        conn = MockConnector(make_transactions(1))
        consumer = MockConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=1)),
        )

        result = consumer.consume_transactions(policy=policy)

        assert result == 1

    def test_named_connector_resolved(self):
        conn_foo = MockConnector(make_transactions(2))
        conn_bar = MockConnector(make_transactions(5))
        consumer = MockConsumer(connectors={"foo": conn_foo, "bar": conn_bar})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(
                connector_name="foo",
                batch=policies.BatchPolicy(size=10),
            ),
        )

        result = consumer.consume_transactions(policy=policy)

        assert result == 2

    def test_multiple_without_name_raises(self):
        conn_a = MockConnector()
        conn_b = MockConnector()
        consumer = MockConsumer(connectors={"a": conn_a, "b": conn_b})
        policy = policies.ConsumerPolicy()

        with pytest.raises(ValueError):
            consumer.consume_transactions(policy=policy)
