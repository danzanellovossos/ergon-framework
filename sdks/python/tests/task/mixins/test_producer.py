"""Tests for ergon.task.mixins.producer — ProducerMixin lifecycle and produce loop."""

import threading
import time
from concurrent import futures

import pytest

from ergon.task import exceptions, policies
from tests.task.mocks import MockProducer, make_transactions

# =====================================================================
#   Transaction lifecycle (_start_producing)
# =====================================================================


class TestProducerLifecycle:
    def test_happy_path(self):
        producer = MockProducer()
        tx = make_transactions(1)[0]
        policy = policies.ProducerPolicy()

        success, result = producer._start_producing(tx, policy)

        assert success is True
        assert tx in producer.prepared

    def test_prepare_fails(self):
        def failing_prepare(tx):
            raise ValueError("prepare error")

        exception_called = []

        def track_exception(tx, exc):
            exception_called.append(exc)

        producer = MockProducer(
            prepare_fn=failing_prepare,
            exception_fn=track_exception,
        )
        tx = make_transactions(1)[0]
        policy = policies.ProducerPolicy()

        producer._start_producing(tx, policy)

        assert len(exception_called) == 1
        assert isinstance(exception_called[0], exceptions.TransactionException)
        assert exception_called[0].category == exceptions.ExceptionType.SYSTEM

    def test_prepare_timeout(self):
        def timeout_prepare(tx):
            raise futures.TimeoutError("timed out")

        exception_called = []

        def track_exception(tx, exc):
            exception_called.append(exc)

        producer = MockProducer(
            prepare_fn=timeout_prepare,
            exception_fn=track_exception,
        )
        tx = make_transactions(1)[0]
        policy = policies.ProducerPolicy()

        producer._start_producing(tx, policy)

        assert len(exception_called) == 1
        assert isinstance(exception_called[0], exceptions.TransactionException)
        assert exception_called[0].category == exceptions.ExceptionType.TIMEOUT

    def test_success_handler_fails(self):
        def failing_success(tx, result):
            raise RuntimeError("success handler broke")

        exception_called = []

        def track_exception(tx, exc):
            exception_called.append(exc)

        producer = MockProducer(
            success_fn=failing_success,
            exception_fn=track_exception,
        )
        tx = make_transactions(1)[0]
        policy = policies.ProducerPolicy()

        producer._start_producing(tx, policy)

        assert len(exception_called) == 1
        assert isinstance(exception_called[0], exceptions.TransactionException)


# =====================================================================
#   produce_transactions end-to-end
# =====================================================================


class TestProduceTransactions:
    def test_all_processed(self):
        producer = MockProducer()
        txs = make_transactions(5)
        policy = policies.ProducerPolicy()

        result = producer.produce_transactions(txs, policy=policy)

        assert result == 5
        assert len(producer.prepared) == 5

    def test_batching(self):
        producer = MockProducer()
        txs = make_transactions(10)
        policy = policies.ProducerPolicy(
            loop=policies.ProducerLoopPolicy(
                batch=policies.BatchPolicy(size=3),
            ),
        )

        result = producer.produce_transactions(txs, policy=policy)

        assert result == 10
        assert len(producer.prepared) == 10

    def test_concurrency(self):
        lock = threading.Lock()
        active = [0]
        peak = [0]

        def tracked_prepare(tx):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.03)
            with lock:
                active[0] -= 1
            return tx.payload

        producer = MockProducer(prepare_fn=tracked_prepare)
        txs = make_transactions(9)
        policy = policies.ProducerPolicy(
            loop=policies.ProducerLoopPolicy(
                concurrency=policies.ConcurrencyPolicy(value=3),
            ),
        )

        producer.produce_transactions(txs, policy=policy)

        assert len(producer.prepared) == 9
        assert peak[0] <= 3

    def test_limit(self):
        producer = MockProducer()
        txs = make_transactions(20)
        policy = policies.ProducerPolicy(
            loop=policies.ProducerLoopPolicy(limit=5),
        )

        result = producer.produce_transactions(txs, policy=policy)

        assert result == 5

    def test_transaction_timeout(self):
        def blocking_prepare(tx):
            time.sleep(0.5)
            return tx.payload

        producer = MockProducer(prepare_fn=blocking_prepare)
        txs = make_transactions(3)
        policy = policies.ProducerPolicy(
            transaction_runtime=policies.TransactionRuntimePolicy(timeout=0.1),
            loop=policies.ProducerLoopPolicy(timeout=5.0),
        )

        result = producer.produce_transactions(txs, policy=policy)

        assert result == 3

    def test_loop_timeout(self):
        def forever_prepare(tx):
            time.sleep(10)

        producer = MockProducer(prepare_fn=forever_prepare)
        txs = make_transactions(1)
        policy = policies.ProducerPolicy(
            loop=policies.ProducerLoopPolicy(timeout=0.3),
        )

        with pytest.raises(exceptions.ProducerLoopTimeoutException):
            producer.produce_transactions(txs, policy=policy)

    def test_empty_transactions(self):
        producer = MockProducer()
        policy = policies.ProducerPolicy()

        result = producer.produce_transactions([], policy=policy)

        assert result == 0
        assert len(producer.prepared) == 0
