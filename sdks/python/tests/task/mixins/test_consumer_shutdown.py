"""Sync consume loop under a shutdown request (SIGTERM from systemd, Ctrl+C).

The contract: stop fetching, give back what was fetched but never started right away,
let the in-flight transactions finish, and never sit out an idle sleep.
"""

import logging
import signal
import threading
import time
from typing import List

from ergon.connector import Transaction
from ergon.task import policies, shutdown
from tests.task.mocks import MockConnector, MockConsumer, make_transactions


def request_shutdown():
    shutdown._handle_signal(signal.SIGTERM, None)


class ReleasingConnector(MockConnector):
    def __init__(self, transactions=None, on_fetch=None):
        super().__init__(transactions)
        self.fetches = 0
        self.released: List[str] = []
        self.release_times: List[float] = []
        self._on_fetch = on_fetch

    def fetch_transactions(self, batch_size: int = 1, **kwargs):
        self.fetches += 1
        batch = super().fetch_transactions(batch_size, **kwargs)
        if self._on_fetch:
            self._on_fetch()
        return batch

    def release_transaction(self, transaction: Transaction) -> None:
        self.released.append(transaction.id)
        self.release_times.append(time.monotonic())


class NackingConnector(MockConnector):
    def __init__(self, transactions=None):
        super().__init__(transactions)
        self.nacked: List[tuple] = []

    def nack_transaction(self, transaction: Transaction, requeue: bool = True, **kwargs) -> None:
        self.nacked.append((transaction.id, requeue))


def consumer_policy(
    *,
    batch: int,
    concurrency: int = 1,
    streaming: bool = True,
    empty_backoff: float = 0.0,
    interval: float = 0.0,
    loop_timeout=None,
    limit=None,
    runtime_timeout=60.0,
):
    return policies.ConsumerPolicy(
        loop=policies.ConsumerLoopPolicy(
            streaming=streaming,
            timeout=loop_timeout,
            limit=limit,
            concurrency=policies.ConcurrencyPolicy(value=concurrency, max=concurrency),
        ),
        fetch=policies.FetchPolicy(
            batch=policies.BatchPolicy(
                size=batch,
                max_size=batch,
                interval=policies.BatchIntervalPolicy(backoff=interval),
            ),
            empty=policies.EmptyFetchPolicy(backoff=empty_backoff),
        ),
        transaction_runtime=policies.TransactionRuntimePolicy(timeout=runtime_timeout),
    )


def ids(transactions) -> List[str]:
    return [t.id for t in transactions]


class TestStopsFetching:
    def test_requested_before_start_never_fetches(self):
        conn = ReleasingConnector(make_transactions(3))
        consumer = MockConsumer(connectors={"default": conn})
        request_shutdown()

        assert consumer.consume_transactions(consumer_policy(batch=3)) == 0
        assert conn.fetches == 0
        assert consumer.processed == []

    def test_streaming_loop_stops_after_the_current_batch(self):
        conn = ReleasingConnector(make_transactions(10))

        def process(tr):
            if tr.id == "1":
                request_shutdown()

        consumer = MockConsumer(connectors={"default": conn}, process_fn=process)

        consumer.consume_transactions(consumer_policy(batch=2))

        assert ids(consumer.processed) == ["0", "1"]
        assert conn.fetches == 1
        assert conn.released == []
        assert ids(conn._queue) == [str(i) for i in range(2, 10)]

    def test_batch_fetched_while_stopping_is_handed_back_untouched(self):
        conn = ReleasingConnector(make_transactions(5), on_fetch=request_shutdown)
        consumer = MockConsumer(connectors={"default": conn})

        assert consumer.consume_transactions(consumer_policy(batch=3)) == 0

        assert consumer.processed == []
        assert conn.released == ["0", "1", "2"]
        assert conn.fetches == 1

    def test_fetch_failure_while_stopping_is_a_clean_stop(self):
        class FailingFetch(ReleasingConnector):
            def fetch_transactions(self, batch_size: int = 1, **kwargs):
                request_shutdown()
                raise ConnectionError("platform down")

        consumer = MockConsumer(connectors={"default": FailingFetch()})

        assert consumer.consume_transactions(consumer_policy(batch=3)) == 0


class TestMidBatch:
    def test_unstarted_transactions_are_released_and_started_one_finishes(self):
        conn = ReleasingConnector(make_transactions(10))
        finished = []

        def process(tr):
            if tr.id == "0":
                request_shutdown()
                time.sleep(0.05)
            finished.append(tr.id)

        consumer = MockConsumer(connectors={"default": conn}, process_fn=process)

        consumer.consume_transactions(consumer_policy(batch=5))

        assert finished == ["0"]
        assert conn.released == ["1", "2", "3", "4"]
        assert conn.fetches == 1

    def test_unstarted_transactions_are_released_before_the_in_flight_one_finishes(self):
        """A long transaction must not keep the rest of the batch claimed for its whole runtime."""
        conn = ReleasingConnector(make_transactions(5))
        finished_at = []

        def process(tr):
            request_shutdown()
            time.sleep(1.5)
            finished_at.append(time.monotonic())

        consumer = MockConsumer(connectors={"default": conn}, process_fn=process)

        consumer.consume_transactions(consumer_policy(batch=5))

        assert conn.released == ["1", "2", "3", "4"]
        assert max(conn.release_times) < finished_at[0] - 0.5

    def test_every_in_flight_transaction_runs_to_completion(self):
        conn = ReleasingConnector(make_transactions(4))
        second_started = threading.Event()
        succeeded = []
        lock = threading.Lock()

        def process(tr):
            if tr.id == "0":
                assert second_started.wait(2)
                request_shutdown()
                time.sleep(0.1)
            elif tr.id == "1":
                second_started.set()
                time.sleep(0.3)
            return tr.id

        def success(tr, result):
            with lock:
                succeeded.append(result)

        consumer = MockConsumer(connectors={"default": conn}, process_fn=process, success_fn=success)

        consumer.consume_transactions(consumer_policy(batch=4, concurrency=2))

        assert sorted(succeeded) == ["0", "1"]
        assert conn.released == ["2", "3"]

    def test_every_fetched_transaction_is_either_processed_or_released(self):
        conn = ReleasingConnector(make_transactions(20))
        seen = []
        lock = threading.Lock()

        def process(tr):
            with lock:
                seen.append(tr.id)
                if len(seen) == 3:
                    request_shutdown()
            time.sleep(0.02)

        consumer = MockConsumer(connectors={"default": conn}, process_fn=process)

        consumer.consume_transactions(consumer_policy(batch=8, concurrency=2))

        fetched = [str(i) for i in range(8)]
        assert sorted(seen + conn.released, key=int) == fetched
        assert not set(seen) & set(conn.released)

    def test_runtime_eviction_during_the_drain_still_stops(self):
        conn = ReleasingConnector(make_transactions(4))

        def process(tr):
            request_shutdown()
            time.sleep(0.6)

        consumer = MockConsumer(connectors={"default": conn}, process_fn=process)

        started = time.monotonic()
        consumer.consume_transactions(consumer_policy(batch=4, runtime_timeout=0.2))

        assert conn.released == ["1", "2", "3"]
        assert conn.fetches == 1
        assert time.monotonic() - started < 3

    def test_limit_and_stop_together(self):
        conn = ReleasingConnector(make_transactions(10))

        def process(tr):
            if tr.id == "0":
                request_shutdown()

        consumer = MockConsumer(connectors={"default": conn}, process_fn=process)

        consumer.consume_transactions(consumer_policy(batch=5, limit=3))

        assert ids(consumer.processed) == ["0"]
        assert conn.released == ["1", "2", "3", "4"]
        assert conn.fetches == 1


class TestNonStreaming:
    def test_stop_mid_batch(self):
        conn = ReleasingConnector(make_transactions(6))

        def process(tr):
            if tr.id == "0":
                request_shutdown()

        consumer = MockConsumer(connectors={"default": conn}, process_fn=process)

        consumer.consume_transactions(consumer_policy(batch=3, streaming=False))

        assert ids(consumer.processed) == ["0"]
        assert conn.released == ["1", "2"]
        assert conn.fetches == 1

    def test_stop_mid_batch_with_loop_timeout(self):
        """``loop.timeout`` runs the loop in a helper thread; the stop is process-wide."""
        conn = ReleasingConnector(make_transactions(6))

        def process(tr):
            if tr.id == "0":
                request_shutdown()

        consumer = MockConsumer(connectors={"default": conn}, process_fn=process)

        consumer.consume_transactions(consumer_policy(batch=3, streaming=False, loop_timeout=30))

        assert ids(consumer.processed) == ["0"]
        assert conn.released == ["1", "2"]
        assert conn.fetches == 1


class TestHandBackFallbacks:
    def test_nack_with_requeue_when_connector_cannot_release(self):
        conn = NackingConnector(make_transactions(3))

        def process(tr):
            request_shutdown()

        consumer = MockConsumer(connectors={"default": conn}, process_fn=process)

        consumer.consume_transactions(consumer_policy(batch=3))

        assert conn.nacked == [("1", True), ("2", True)]

    def test_connector_without_hand_back_only_warns(self, caplog):
        conn = MockConnector(make_transactions(3))

        def process(tr):
            request_shutdown()

        consumer = MockConsumer(connectors={"default": conn}, process_fn=process)

        with caplog.at_level(logging.WARNING):
            consumer.consume_transactions(consumer_policy(batch=3))

        assert ids(consumer.processed) == ["0"]
        assert "cannot hand back transaction 1" in caplog.text

    def test_a_failing_release_does_not_stop_the_others_or_the_shutdown(self, caplog):
        class FlakyRelease(ReleasingConnector):
            def release_transaction(self, transaction):
                if transaction.id == "1":
                    raise RuntimeError("platform down")
                super().release_transaction(transaction)

        conn = FlakyRelease(make_transactions(4))

        def process(tr):
            request_shutdown()

        consumer = MockConsumer(connectors={"default": conn}, process_fn=process)

        with caplog.at_level(logging.ERROR):
            consumer.consume_transactions(consumer_policy(batch=4))

        assert conn.released == ["2", "3"]
        assert "Could not hand back transaction 1" in caplog.text


class TestIdleSleepsAreInterrupted:
    def test_empty_queue_backoff(self, stop_later):
        conn = ReleasingConnector([])
        consumer = MockConsumer(connectors={"default": conn})
        stop_later(0.1)

        started = time.monotonic()
        consumer.consume_transactions(consumer_policy(batch=1, empty_backoff=30))

        assert time.monotonic() - started < 3
        assert conn.fetches == 1

    def test_batch_interval_backoff(self, stop_later):
        conn = ReleasingConnector(make_transactions(5))
        consumer = MockConsumer(connectors={"default": conn})
        stop_later(0.2)

        started = time.monotonic()
        consumer.consume_transactions(consumer_policy(batch=1, interval=30))

        assert time.monotonic() - started < 3
        assert ids(consumer.processed) == ["0"]
        assert conn.fetches == 1


class TestWithoutShutdown:
    def test_streaming_consumes_everything_then_keeps_polling(self):
        conn = ReleasingConnector(make_transactions(6))
        consumer = MockConsumer(connectors={"default": conn})

        def stop_when_drained():
            deadline = time.monotonic() + 5
            while conn._queue and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.1)
            request_shutdown()

        threading.Thread(target=stop_when_drained, daemon=True).start()
        consumer.consume_transactions(consumer_policy(batch=4, concurrency=2, empty_backoff=0.05))

        assert sorted(ids(consumer.processed), key=int) == [str(i) for i in range(6)]
        assert conn.released == []
        assert conn.fetches >= 3
