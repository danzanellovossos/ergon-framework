"""Tests for ergon.task.mixins.consumer — AsyncConsumerMixin lifecycle, fetch, and consume loop."""

import asyncio
import time
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


class TestConsumerLiveness:
    async def test_idle_queue_uses_poll_completion_not_stale_ack(self):
        conn = MockAsyncConnector([])
        conn.health = lambda: {
            "state": "polling",
            "last_poll_started_ts": time.time() - 1,
            "last_poll_completed_ts": time.time() - 1,
            "seconds_since_last_poll_started": 1,
            "seconds_since_last_ack": 86_400,
        }
        consumer = MockAsyncConsumer(connectors={"default": conn})

        snapshot = consumer.liveness_snapshot()

        assert snapshot.healthy is True

    async def test_unfinished_stale_poll_is_unhealthy(self):
        conn = MockAsyncConnector([])
        conn.health = lambda: {
            "state": "reconnecting",
            "last_poll_started_ts": time.time() - 180,
            "last_poll_completed_ts": time.time() - 300,
            "seconds_since_last_poll_started": 180,
        }
        consumer = MockAsyncConsumer(connectors={"default": conn})
        consumer._consumer_fetch_stale_after = 60

        snapshot = consumer.liveness_snapshot()

        assert snapshot.healthy is False
        assert snapshot.state == "poll_stalled"

    async def test_generic_connector_stalled_fetch_is_unhealthy(self):
        consumer = MockAsyncConsumer(connectors={"default": MockAsyncConnector([])})
        consumer._consumer_liveness_state = "fetching"
        consumer._consumer_last_progress_monotonic = time.monotonic() - 180
        consumer._consumer_fetch_stale_after = 60

        snapshot = consumer.liveness_snapshot()

        assert snapshot.healthy is False
        assert snapshot.state == "fetch_stalled"

    async def test_a_transaction_that_never_ends_is_unhealthy_while_fetches_keep_returning(self):
        consumer = MockAsyncConsumer(connectors={"default": MockAsyncConnector([])})
        consumer._consumer_in_flight = {1}
        consumer._consumer_active_stale_after = 60
        consumer._consumer_last_transaction_progress_monotonic = time.monotonic() - 180
        # The continuous loop fetched a moment ago, beside the transaction.
        consumer._consumer_liveness_state = "polling"
        consumer._consumer_last_progress_monotonic = time.monotonic()

        snapshot = consumer.liveness_snapshot()

        assert snapshot.healthy is False
        assert snapshot.state == "processing_stalled"

    async def test_a_fetch_that_never_returns_is_unhealthy_while_transactions_start_and_end(self):
        consumer = MockAsyncConsumer(connectors={"default": MockAsyncConnector([])})
        consumer._consumer_fetch_stale_after = 60
        consumer._consumer_fetch_started_monotonic = time.monotonic() - 180
        # A transaction ended a moment ago, which moved the loop's state on.
        consumer._consumer_liveness_state = "processing"
        consumer._consumer_last_progress_monotonic = time.monotonic()

        snapshot = consumer.liveness_snapshot()

        assert snapshot.healthy is False
        assert snapshot.state == "fetch_stalled"

    async def test_a_running_fetch_and_a_running_transaction_within_their_limits_are_healthy(self):
        release = asyncio.Event()

        class Waiting(MockAsyncConnector):
            async def fetch_transactions_async(self, batch_size=1, **kwargs):
                batch = await super().fetch_transactions_async(batch_size, **kwargs)
                if not batch:
                    await asyncio.sleep(0.02)
                return batch

        async def process(tx):
            await release.wait()
            return tx.payload

        consumer = MockAsyncConsumer(connectors={"default": Waiting(make_transactions(1))}, process_fn=process)
        policy = policies.ConsumerPolicy(
            loop=policies.ConsumerLoopPolicy(
                mode="continuous",
                streaming=True,
                limit=1,
                concurrency=policies.ConcurrencyPolicy(value=2),
            ),
            transaction_runtime=policies.TransactionRuntimePolicy(timeout=30),
        )
        consuming = asyncio.create_task(consumer.consume_transactions(policy=policy))
        await asyncio.sleep(0.2)

        snapshot = consumer.liveness_snapshot()
        release.set()
        await asyncio.wait_for(consuming, timeout=5)

        assert snapshot.healthy is True
        assert snapshot.details["in_flight_count"] == 1
        assert consumer._consumer_fetch_started_monotonic is None


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

    async def test_transaction_runtime_timeout_cancels_and_routes_to_exception_handler(self):
        async def forever_process(tx):
            await asyncio.Event().wait()

        failures = []

        async def track_exception(tx, exc):
            failures.append(exc)

        conn = MockAsyncConnector(make_transactions(1))
        consumer = MockAsyncConsumer(
            connectors={"default": conn},
            process_fn=forever_process,
            exception_fn=track_exception,
        )
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=1)),
            transaction_runtime=policies.TransactionRuntimePolicy(timeout=0.05),
        )

        result = await asyncio.wait_for(consumer.consume_transactions(policy=policy), timeout=0.5)

        assert result == 1
        assert len(failures) == 1
        assert isinstance(failures[0], exceptions.TransactionTimeoutException)

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
#   Consume loop — continuous refill
# =====================================================================


class TestConsumeContinuous:
    async def test_refills_while_slow_transaction_is_running(self):
        slow_running = asyncio.Event()
        release_slow = asyncio.Event()
        refilled_while_slow = False

        class TrackingConnector(MockAsyncConnector):
            async def fetch_transactions_async(self, batch_size=1, **kwargs):
                nonlocal refilled_while_slow
                if slow_running.is_set() and not release_slow.is_set():
                    refilled_while_slow = True
                    release_slow.set()
                return await super().fetch_transactions_async(batch_size, **kwargs)

        async def process(tx):
            if tx.id == "0":
                slow_running.set()
                await release_slow.wait()
            else:
                await asyncio.sleep(0.01)
            return tx.payload

        conn = TrackingConnector(make_transactions(4))
        consumer = MockAsyncConsumer(connectors={"default": conn}, process_fn=process)
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(batch=policies.BatchPolicy(size=1)),
            loop=policies.ConsumerLoopPolicy(
                mode="continuous",
                concurrency=policies.ConcurrencyPolicy(value=2),
            ),
        )

        result = await asyncio.wait_for(consumer.consume_transactions(policy=policy), timeout=1)

        assert result == 4
        assert refilled_while_slow is True

    async def test_never_exceeds_concurrency_bound(self):
        active = 0
        peak = 0

        async def tracked_process(tx):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return tx.payload

        conn = MockAsyncConnector(make_transactions(9))
        consumer = MockAsyncConsumer(connectors={"default": conn}, process_fn=tracked_process)
        policy = policies.ConsumerPolicy(
            loop=policies.ConsumerLoopPolicy(
                mode="continuous",
                concurrency=policies.ConcurrencyPolicy(value=3),
            ),
        )

        result = await consumer.consume_transactions(policy=policy)

        assert result == 9
        assert peak == 3

    async def test_non_streaming_drains_queue_and_in_flight_work(self):
        completed = []

        async def process(tx):
            await asyncio.sleep(0.01)
            completed.append(tx.id)
            return tx.payload

        conn = MockAsyncConnector(make_transactions(7))
        consumer = MockAsyncConsumer(connectors={"default": conn}, process_fn=process)
        policy = policies.ConsumerPolicy(
            loop=policies.ConsumerLoopPolicy(
                mode="continuous",
                concurrency=policies.ConcurrencyPolicy(value=3),
            ),
        )

        result = await consumer.consume_transactions(policy=policy)

        assert result == 7
        assert sorted(completed, key=int) == [str(i) for i in range(7)]
        assert conn._queue == []

    @patch("ergon.task.utils.backoff_async")
    async def test_streaming_backs_off_then_resumes(self, mock_backoff):
        conn = MockAsyncConnector([])

        def refill(*args, **kwargs):
            if mock_backoff.call_count == 2:
                conn._queue.extend(make_transactions(2))

        mock_backoff.side_effect = refill
        consumer = MockAsyncConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(
                empty=policies.EmptyFetchPolicy(backoff=0.01),
            ),
            loop=policies.ConsumerLoopPolicy(
                mode="continuous",
                streaming=True,
                limit=2,
                concurrency=policies.ConcurrencyPolicy(value=2),
            ),
        )

        result = await consumer.consume_transactions(policy=policy)

        assert result == 2
        assert mock_backoff.call_count == 2
        assert [call.kwargs["attempt"] for call in mock_backoff.call_args_list] == [0, 1]

    async def test_fetch_failure_drains_in_flight_before_raising(self):
        completed = []

        class FailingConnector(MockAsyncConnector):
            calls = 0

            async def fetch_transactions_async(self, batch_size=1, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return await super().fetch_transactions_async(batch_size, **kwargs)
                raise RuntimeError("connection lost")

        async def process(tx):
            await asyncio.sleep(0.05)
            completed.append(tx.id)
            return tx.payload

        conn = FailingConnector(make_transactions(2))
        consumer = MockAsyncConsumer(connectors={"default": conn}, process_fn=process)
        policy = policies.ConsumerPolicy(
            loop=policies.ConsumerLoopPolicy(
                mode="continuous",
                streaming=True,
                concurrency=policies.ConcurrencyPolicy(value=2),
            ),
        )

        with pytest.raises(exceptions.FetchException):
            await consumer.consume_transactions(policy=policy)

        assert sorted(completed) == ["0", "1"]

    async def test_limit_stops_fetching_and_drains_submitted_work(self):
        conn = MockAsyncConnector(make_transactions(20))
        consumer = MockAsyncConsumer(connectors={"default": conn})
        policy = policies.ConsumerPolicy(
            loop=policies.ConsumerLoopPolicy(
                mode="continuous",
                limit=5,
                concurrency=policies.ConcurrencyPolicy(value=3),
            ),
        )

        result = await consumer.consume_transactions(policy=policy)

        assert result == 5
        assert len(consumer.processed) == 5
        assert len(conn._queue) == 15

    async def test_a_delivery_that_arrives_while_a_transaction_runs_starts_before_it_ends(self):
        """A free slot takes new work while another slot is still busy.

        The loop used to stop fetching after one empty fetch with a
        transaction in flight, until that transaction ended. A delivery that
        arrived in between waited for as long as the running one took.
        """
        first_running = asyncio.Event()
        release_first = asyncio.Event()
        empty_fetches_while_running = 0

        class LateDelivery(MockAsyncConnector):
            async def fetch_transactions_async(self, batch_size=1, **kwargs):
                nonlocal empty_fetches_while_running
                batch = await super().fetch_transactions_async(batch_size, **kwargs)
                if not batch and first_running.is_set():
                    empty_fetches_while_running += 1
                    if empty_fetches_while_running == 2:
                        # Arrives after the loop has already seen the source empty.
                        self._queue.append(Transaction(id="late", payload={}))
                    await asyncio.sleep(0.01)
                return batch

        async def process(tx):
            if tx.id == "0":
                first_running.set()
                await release_first.wait()
            else:
                # Only reachable while the first transaction is still running.
                release_first.set()
            return tx.payload

        conn = LateDelivery(make_transactions(1))
        consumer = MockAsyncConsumer(connectors={"default": conn}, process_fn=process)
        policy = policies.ConsumerPolicy(
            loop=policies.ConsumerLoopPolicy(
                mode="continuous",
                streaming=True,
                limit=2,
                concurrency=policies.ConcurrencyPolicy(value=2),
            ),
        )

        result = await asyncio.wait_for(consumer.consume_transactions(policy=policy), timeout=5)

        assert result == 2
        assert [tx.id for tx in consumer.processed] == ["0", "late"]

    async def test_a_drain_fetches_again_when_a_transaction_ended_while_the_fetch_was_out(self):
        """The empty answer of a fetch can be older than the work a transaction left behind."""
        first_done = asyncio.Event()

        class SlowToAnswer(MockAsyncConnector):
            async def fetch_transactions_async(self, batch_size=1, **kwargs):
                batch = await super().fetch_transactions_async(batch_size, **kwargs)
                if not batch and not first_done.is_set():
                    # Saw the source empty, and answers after the transaction ended.
                    await first_done.wait()
                    await asyncio.sleep(0.02)
                return batch

        async def process(tx):
            if tx.id == "0":
                await asyncio.sleep(0.02)
                conn._queue.append(Transaction(id="chained", payload={}))
                first_done.set()
            return tx.payload

        conn = SlowToAnswer(make_transactions(1))
        consumer = MockAsyncConsumer(connectors={"default": conn}, process_fn=process)
        policy = policies.ConsumerPolicy(
            loop=policies.ConsumerLoopPolicy(
                mode="continuous",
                concurrency=policies.ConcurrencyPolicy(value=2),
            ),
        )

        result = await asyncio.wait_for(consumer.consume_transactions(policy=policy), timeout=5)

        assert result == 2
        assert [tx.id for tx in consumer.processed] == ["0", "chained"]

    async def test_a_full_consumer_does_not_fetch(self):
        release = asyncio.Event()
        fetch_sizes: list[int] = []

        class Counting(MockAsyncConnector):
            async def fetch_transactions_async(self, batch_size=1, **kwargs):
                fetch_sizes.append(batch_size)
                return await super().fetch_transactions_async(batch_size, **kwargs)

        async def process(tx):
            await release.wait()
            return tx.payload

        conn = Counting(make_transactions(5))
        consumer = MockAsyncConsumer(connectors={"default": conn}, process_fn=process)
        policy = policies.ConsumerPolicy(
            loop=policies.ConsumerLoopPolicy(
                mode="continuous",
                streaming=True,
                limit=5,
                concurrency=policies.ConcurrencyPolicy(value=2),
            ),
        )

        consuming = asyncio.create_task(consumer.consume_transactions(policy=policy))
        await asyncio.sleep(0.3)

        # Both slots are busy: one fetch filled them and no other was made.
        assert fetch_sizes == [2]
        assert len(conn._queue) == 3

        release.set()
        assert await asyncio.wait_for(consuming, timeout=5) == 5
        assert max(fetch_sizes) == 2

    async def test_empty_fetches_do_not_spin_while_a_transaction_runs(self):
        fetches = 0

        class AlwaysEmptyAfterFirst(MockAsyncConnector):
            async def fetch_transactions_async(self, batch_size=1, **kwargs):
                nonlocal fetches
                fetches += 1
                return await super().fetch_transactions_async(batch_size, **kwargs)

        async def process(tx):
            await asyncio.sleep(0.45)
            return tx.payload

        conn = AlwaysEmptyAfterFirst(make_transactions(1))
        consumer = MockAsyncConsumer(connectors={"default": conn}, process_fn=process)
        policy = policies.ConsumerPolicy(
            loop=policies.ConsumerLoopPolicy(
                mode="continuous",
                streaming=True,
                concurrency=policies.ConcurrencyPolicy(value=2),
            ),
        )
        consuming = asyncio.create_task(consumer.consume_transactions(policy=policy))
        await asyncio.sleep(0.4)
        seen = fetches
        consuming.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consuming

        # One fetch that returned the transaction, then at most one every 0.1s.
        assert 2 <= seen <= 6

    async def test_empty_fetch_backoff_sets_the_pause_while_a_transaction_runs(self):
        fetches = 0

        class Counting(MockAsyncConnector):
            async def fetch_transactions_async(self, batch_size=1, **kwargs):
                nonlocal fetches
                fetches += 1
                return await super().fetch_transactions_async(batch_size, **kwargs)

        async def process(tx):
            await asyncio.Event().wait()

        conn = Counting(make_transactions(1))
        consumer = MockAsyncConsumer(connectors={"default": conn}, process_fn=process)
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(empty=policies.EmptyFetchPolicy(backoff=10)),
            loop=policies.ConsumerLoopPolicy(
                mode="continuous",
                streaming=True,
                concurrency=policies.ConcurrencyPolicy(value=2),
            ),
        )
        consuming = asyncio.create_task(consumer.consume_transactions(policy=policy))
        await asyncio.sleep(0.4)
        seen = fetches
        consuming.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consuming

        # The fetch that returned the transaction and one empty fetch; the
        # next one is ten seconds away.
        assert seen == 2

    async def test_a_finished_transaction_ends_the_pause(self):
        fetched_after_first_ended = asyncio.Event()
        first_ended = asyncio.Event()

        class Refilled(MockAsyncConnector):
            async def fetch_transactions_async(self, batch_size=1, **kwargs):
                if first_ended.is_set():
                    fetched_after_first_ended.set()
                return await super().fetch_transactions_async(batch_size, **kwargs)

        async def process(tx):
            if tx.id == "0":
                await asyncio.sleep(0.05)
                first_ended.set()
                return tx.payload
            await asyncio.Event().wait()

        conn = Refilled(make_transactions(2))
        consumer = MockAsyncConsumer(connectors={"default": conn}, process_fn=process)
        policy = policies.ConsumerPolicy(
            fetch=policies.FetchPolicy(empty=policies.EmptyFetchPolicy(backoff=30)),
            loop=policies.ConsumerLoopPolicy(
                mode="continuous",
                streaming=True,
                concurrency=policies.ConcurrencyPolicy(value=3),
            ),
        )
        consuming = asyncio.create_task(consumer.consume_transactions(policy=policy))

        # The empty fetch started a 30-second pause; the slot that frees up
        # is offered to the source at once.
        await asyncio.wait_for(fetched_after_first_ended.wait(), timeout=5)

        consuming.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consuming

    async def test_cancelling_the_loop_cancels_its_fetch_and_its_transactions(self):
        fetch_waiting = asyncio.Event()
        fetch_cancelled = False
        transaction_cancelled = False

        class Waiting(MockAsyncConnector):
            async def fetch_transactions_async(self, batch_size=1, **kwargs):
                nonlocal fetch_cancelled
                batch = await super().fetch_transactions_async(batch_size, **kwargs)
                if batch:
                    return batch
                fetch_waiting.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    fetch_cancelled = True
                    raise
                return []

        async def process(tx):
            nonlocal transaction_cancelled
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                transaction_cancelled = True
                raise

        conn = Waiting(make_transactions(1))
        consumer = MockAsyncConsumer(connectors={"default": conn}, process_fn=process)
        policy = policies.ConsumerPolicy(
            loop=policies.ConsumerLoopPolicy(
                mode="continuous",
                streaming=True,
                concurrency=policies.ConcurrencyPolicy(value=2),
            ),
        )
        consuming = asyncio.create_task(consumer.consume_transactions(policy=policy))
        await asyncio.wait_for(fetch_waiting.wait(), timeout=5)

        consuming.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consuming

        assert fetch_cancelled is True
        assert transaction_cancelled is True


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


# =====================================================================
#   Prefetch vs concurrency guardrail (#6)
# =====================================================================


class TestPrefetchGuardrail:
    async def test_warns_when_prefetch_exceeds_concurrency(self, caplog):
        import logging

        from ergon.connector.rabbitmq.models import AsyncRabbitmqConsumerConfig
        from ergon.task.mixins.consumer import _warn_if_prefetch_exceeds_concurrency

        class _FakeConn:
            _consumer_config = AsyncRabbitmqConsumerConfig(queue_name="agents.processor", prefetch_count=40)

        policy = policies.ConsumerPolicy()
        policy.loop.concurrency.value = 20

        with caplog.at_level(logging.WARNING, logger="ergon.task.mixins.consumer"):
            _warn_if_prefetch_exceeds_concurrency(_FakeConn(), policy, "EventWorkerTask")

        assert any("exceeds loop concurrency" in r.getMessage() for r in caplog.records)

    async def test_no_warning_when_aligned(self, caplog):
        import logging

        from ergon.connector.rabbitmq.models import AsyncRabbitmqConsumerConfig
        from ergon.task.mixins.consumer import _warn_if_prefetch_exceeds_concurrency

        class _FakeConn:
            _consumer_config = AsyncRabbitmqConsumerConfig(queue_name="q", prefetch_count=20)

        policy = policies.ConsumerPolicy()
        policy.loop.concurrency.value = 20

        with caplog.at_level(logging.WARNING, logger="ergon.task.mixins.consumer"):
            _warn_if_prefetch_exceeds_concurrency(_FakeConn(), policy, "task")

        assert not any("exceeds loop concurrency" in r.getMessage() for r in caplog.records)

    async def test_skips_connector_without_consumer_config(self, caplog):
        import logging

        from ergon.task.mixins.consumer import _warn_if_prefetch_exceeds_concurrency

        class _FakeConn:
            pass

        policy = policies.ConsumerPolicy()

        with caplog.at_level(logging.WARNING, logger="ergon.task.mixins.consumer"):
            _warn_if_prefetch_exceeds_concurrency(_FakeConn(), policy, "task")

        assert not any("exceeds loop concurrency" in r.getMessage() for r in caplog.records)
