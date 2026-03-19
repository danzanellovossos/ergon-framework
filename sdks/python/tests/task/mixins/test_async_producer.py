"""Tests for ergon.task.mixins.producer — AsyncProducerMixin lifecycle and produce loop."""

import asyncio
from concurrent import futures

import pytest

from ergon.task import exceptions, policies
from tests.task.mocks import MockAsyncProducer, make_transactions

pytestmark = pytest.mark.asyncio(loop_scope="function")

# =====================================================================
#   Transaction lifecycle (_start_producing)
# =====================================================================


class TestAsyncProducerLifecycle:
    async def test_happy_path(self):
        producer = MockAsyncProducer()
        tx = make_transactions(1)[0]
        policy = policies.ProducerPolicy()

        success, result = await producer._start_producing(tx, policy)

        assert success is True
        assert tx in producer.prepared

    async def test_prepare_fails(self):
        async def failing_prepare(tx):
            raise ValueError("prepare error")

        exception_called = []

        async def track_exception(tx, exc):
            exception_called.append(exc)

        producer = MockAsyncProducer(
            prepare_fn=failing_prepare,
            exception_fn=track_exception,
        )
        tx = make_transactions(1)[0]
        policy = policies.ProducerPolicy()

        await producer._start_producing(tx, policy)

        assert len(exception_called) == 1
        assert isinstance(exception_called[0], exceptions.TransactionException)
        assert exception_called[0].category == exceptions.ExceptionType.SYSTEM

    async def test_prepare_asyncio_timeout(self):
        async def timeout_prepare(tx):
            raise asyncio.TimeoutError("timed out")

        exception_called = []

        async def track_exception(tx, exc):
            exception_called.append(exc)

        producer = MockAsyncProducer(
            prepare_fn=timeout_prepare,
            exception_fn=track_exception,
        )
        tx = make_transactions(1)[0]
        policy = policies.ProducerPolicy()

        await producer._start_producing(tx, policy)

        assert len(exception_called) == 1
        assert isinstance(exception_called[0], exceptions.TransactionException)
        assert exception_called[0].category == exceptions.ExceptionType.TIMEOUT

    async def test_prepare_futures_timeout(self):
        async def timeout_prepare(tx):
            raise futures.TimeoutError("timed out")

        exception_called = []

        async def track_exception(tx, exc):
            exception_called.append(exc)

        producer = MockAsyncProducer(
            prepare_fn=timeout_prepare,
            exception_fn=track_exception,
        )
        tx = make_transactions(1)[0]
        policy = policies.ProducerPolicy()

        await producer._start_producing(tx, policy)

        assert len(exception_called) == 1
        assert isinstance(exception_called[0], exceptions.TransactionException)
        assert exception_called[0].category == exceptions.ExceptionType.TIMEOUT

    async def test_success_handler_fails(self):
        async def failing_success(tx, result):
            raise RuntimeError("success handler broke")

        exception_called = []

        async def track_exception(tx, exc):
            exception_called.append(exc)

        producer = MockAsyncProducer(
            success_fn=failing_success,
            exception_fn=track_exception,
        )
        tx = make_transactions(1)[0]
        policy = policies.ProducerPolicy()

        await producer._start_producing(tx, policy)

        assert len(exception_called) == 1
        assert isinstance(exception_called[0], exceptions.TransactionException)


# =====================================================================
#   produce_transactions end-to-end
# =====================================================================


class TestAsyncProduceTransactions:
    async def test_all_processed(self):
        producer = MockAsyncProducer()
        txs = make_transactions(5)
        policy = policies.ProducerPolicy()

        result = await producer.produce_transactions(txs, policy=policy)

        assert result == 5
        assert len(producer.prepared) == 5

    async def test_batching(self):
        producer = MockAsyncProducer()
        txs = make_transactions(10)
        policy = policies.ProducerPolicy(
            loop=policies.ProducerLoopPolicy(
                batch=policies.BatchPolicy(size=3),
            ),
        )

        result = await producer.produce_transactions(txs, policy=policy)

        assert result == 10
        assert len(producer.prepared) == 10

    async def test_concurrency(self):
        active = 0
        peak = 0

        async def tracked_prepare(tx):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1
            return tx.payload

        producer = MockAsyncProducer(prepare_fn=tracked_prepare)
        txs = make_transactions(9)
        policy = policies.ProducerPolicy(
            loop=policies.ProducerLoopPolicy(
                concurrency=policies.ConcurrencyPolicy(value=3),
            ),
        )

        await producer.produce_transactions(txs, policy=policy)

        assert len(producer.prepared) == 9
        assert peak <= 3

    async def test_limit(self):
        producer = MockAsyncProducer()
        txs = make_transactions(20)
        policy = policies.ProducerPolicy(
            loop=policies.ProducerLoopPolicy(limit=5),
        )

        result = await producer.produce_transactions(txs, policy=policy)

        assert result == 5

    async def test_transaction_timeout(self):
        async def blocking_prepare(tx):
            await asyncio.sleep(0.5)
            return tx.payload

        producer = MockAsyncProducer(prepare_fn=blocking_prepare)
        txs = make_transactions(3)
        policy = policies.ProducerPolicy(
            transaction_runtime=policies.TransactionRuntimePolicy(timeout=0.1),
            loop=policies.ProducerLoopPolicy(timeout=5.0),
        )

        result = await producer.produce_transactions(txs, policy=policy)

        assert result == 3

    async def test_loop_timeout(self):
        async def forever_prepare(tx):
            await asyncio.sleep(10)

        producer = MockAsyncProducer(prepare_fn=forever_prepare)
        txs = make_transactions(1)
        policy = policies.ProducerPolicy(
            loop=policies.ProducerLoopPolicy(timeout=0.3),
        )

        with pytest.raises(exceptions.ProducerLoopTimeoutException):
            await producer.produce_transactions(txs, policy=policy)

    async def test_empty_transactions(self):
        producer = MockAsyncProducer()
        policy = policies.ProducerPolicy()

        result = await producer.produce_transactions([], policy=policy)

        assert result == 0
        assert len(producer.prepared) == 0
