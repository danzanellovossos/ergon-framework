"""Tests for ergon.task.helpers — run_fn, run_fn_async, multithread_execute, async_execute."""

import asyncio
import threading
import time
from concurrent import futures
from unittest.mock import AsyncMock, patch

import pytest

from ergon.task.exceptions import NonRetryableException
from ergon.task.helpers import async_execute, multithread_execute, run_fn, run_fn_async
from ergon.task.policies import RetryPolicy

# =====================================================================
#   run_fn (sync execution envelope)
# =====================================================================


class TestRunFn:
    def test_single_attempt_success(self):
        success, result = run_fn(fn=lambda: 42)
        assert success is True
        assert result == 42

    def test_single_attempt_failure(self):
        def fail():
            raise ValueError("boom")

        success, result = run_fn(fn=fail)
        assert success is False
        assert isinstance(result, ValueError)

    def test_retry_exhaustion(self):
        call_count = [0]

        def always_fail():
            call_count[0] += 1
            raise ValueError("boom")

        policy = RetryPolicy(max_attempts=3)
        success, result = run_fn(fn=always_fail, retry=policy)

        assert success is False
        assert isinstance(result, ValueError)
        assert call_count[0] == 3

    def test_retry_then_succeed(self):
        call_count = [0]

        def flaky():
            call_count[0] += 1
            if call_count[0] < 3:
                raise ValueError("not yet")
            return "ok"

        policy = RetryPolicy(max_attempts=3)
        success, result = run_fn(fn=flaky, retry=policy)

        assert success is True
        assert result == "ok"
        assert call_count[0] == 3

    def test_per_attempt_timeout(self):
        def slow_fn():
            time.sleep(0.5)

        policy = RetryPolicy(max_attempts=1, timeout=0.1)
        success, result = run_fn(fn=slow_fn, retry=policy)

        assert success is False
        assert isinstance(result, futures.TimeoutError)

    @patch("ergon.task.utils.backoff")
    def test_backoff_called_between_retries(self, mock_backoff):
        def always_fail():
            raise ValueError("boom")

        policy = RetryPolicy(max_attempts=2, backoff=1.0, backoff_multiplier=2.0)
        success, result = run_fn(fn=always_fail, retry=policy)

        assert success is False
        mock_backoff.assert_called_once_with(1.0, 2.0, 0.0, 0)

    def test_non_retryable_exception(self):
        call_count = [0]

        def non_retryable():
            call_count[0] += 1
            raise NonRetryableException("stop immediately")

        policy = RetryPolicy(max_attempts=3)
        success, result = run_fn(fn=non_retryable, retry=policy)

        assert success is False
        assert isinstance(result, NonRetryableException)
        assert call_count[0] == 1

    def test_executor_submission(self):
        executor = futures.ThreadPoolExecutor(max_workers=1)

        result = run_fn(fn=lambda: 42, executor=executor)

        assert isinstance(result, futures.Future)
        success, value = result.result(timeout=5)
        assert success is True
        assert value == 42

        executor.shutdown()


class TestRunFnDecorator:
    def test_decorator_success(self):
        @run_fn(retry=RetryPolicy(max_attempts=1))
        def my_fn():
            return 42

        assert my_fn() == 42

    def test_decorator_failure(self):
        @run_fn(retry=RetryPolicy(max_attempts=1))
        def my_fn():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            my_fn()


# =====================================================================
#   run_fn_async (async execution envelope)
# =====================================================================


class TestRunFnAsync:
    @pytest.mark.asyncio
    async def test_single_attempt_success(self):
        async def ok():
            return 42

        success, result = await run_fn_async(fn=ok)
        assert success is True
        assert result == 42

    @pytest.mark.asyncio
    async def test_single_attempt_failure(self):
        async def fail():
            raise ValueError("boom")

        success, result = await run_fn_async(fn=fail)
        assert success is False
        assert isinstance(result, ValueError)

    @pytest.mark.asyncio
    async def test_retry_exhaustion(self):
        call_count = [0]

        async def always_fail():
            call_count[0] += 1
            raise ValueError("boom")

        policy = RetryPolicy(max_attempts=3)
        success, result = await run_fn_async(fn=always_fail, retry=policy)

        assert success is False
        assert isinstance(result, ValueError)
        assert call_count[0] == 3

    @pytest.mark.asyncio
    async def test_retry_then_succeed(self):
        call_count = [0]

        async def flaky():
            call_count[0] += 1
            if call_count[0] < 3:
                raise ValueError("not yet")
            return "ok"

        policy = RetryPolicy(max_attempts=3)
        success, result = await run_fn_async(fn=flaky, retry=policy)

        assert success is True
        assert result == "ok"
        assert call_count[0] == 3

    @pytest.mark.asyncio
    async def test_per_attempt_timeout(self):
        async def slow_fn():
            await asyncio.sleep(10)

        policy = RetryPolicy(max_attempts=1, timeout=0.2)
        success, result = await run_fn_async(fn=slow_fn, retry=policy)

        assert success is False
        assert isinstance(result, (asyncio.TimeoutError, TimeoutError))

    @pytest.mark.asyncio
    async def test_backoff_called_between_retries(self):
        async def always_fail():
            raise ValueError("boom")

        policy = RetryPolicy(max_attempts=2, backoff=1.0, backoff_multiplier=2.0)

        with patch("ergon.task.utils.backoff_async", new_callable=AsyncMock) as mock_backoff:
            success, result = await run_fn_async(fn=always_fail, retry=policy)

        assert success is False
        mock_backoff.assert_awaited_once_with(1.0, 2.0, 0.0, 0)

    @pytest.mark.asyncio
    async def test_non_retryable_exception(self):
        call_count = [0]

        async def non_retryable():
            call_count[0] += 1
            raise NonRetryableException("stop immediately")

        policy = RetryPolicy(max_attempts=3)
        success, result = await run_fn_async(fn=non_retryable, retry=policy)

        assert success is False
        assert isinstance(result, NonRetryableException)
        assert call_count[0] == 1


class TestRunFnAsyncDecorator:
    @pytest.mark.asyncio
    async def test_decorator_success(self):
        @run_fn_async(retry=RetryPolicy(max_attempts=1))
        async def my_fn():
            return 42

        assert await my_fn() == 42

    @pytest.mark.asyncio
    async def test_decorator_failure(self):
        @run_fn_async(retry=RetryPolicy(max_attempts=1))
        async def my_fn():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await my_fn()


# =====================================================================
#   multithread_execute (sync engine)
# =====================================================================


class TestMultithreadExecute:
    def test_all_complete_fast(self):
        executor = futures.ThreadPoolExecutor(max_workers=10)

        def submissions():
            for _ in range(10):
                yield lambda: executor.submit(lambda: None)

        count = multithread_execute(submissions=submissions(), concurrency=5)

        executor.shutdown()
        assert count == 10

    def test_concurrency_bound(self):
        executor = futures.ThreadPoolExecutor(max_workers=10)
        lock = threading.Lock()
        active = [0]
        peak = [0]

        def work():
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1

        def submissions():
            for _ in range(6):
                yield lambda: executor.submit(work)

        count = multithread_execute(submissions=submissions(), concurrency=2)

        executor.shutdown()
        assert count == 6
        assert peak[0] <= 2

    def test_refill_after_completion(self):
        executor = futures.ThreadPoolExecutor(max_workers=10)
        processed = []
        lock = threading.Lock()

        def make_work(i):
            def work():
                with lock:
                    processed.append(i)

            return work

        def submissions():
            for i in range(6):
                yield lambda i=i: executor.submit(make_work(i))

        count = multithread_execute(submissions=submissions(), concurrency=2)

        executor.shutdown()
        assert count == 6
        assert len(processed) == 6

    def test_limit_enforcement(self):
        executor = futures.ThreadPoolExecutor(max_workers=10)

        def submissions():
            for _ in range(10):
                yield lambda: executor.submit(lambda: None)

        count = multithread_execute(submissions=submissions(), concurrency=5, limit=3)

        executor.shutdown()
        assert count == 3

    def test_limit_stops_consumption(self):
        executor = futures.ThreadPoolExecutor(max_workers=10)
        consumed_from_iter = [0]

        def submissions():
            for _ in range(10):
                consumed_from_iter[0] += 1
                yield lambda: executor.submit(lambda: None)

        count = multithread_execute(submissions=submissions(), concurrency=2, limit=2)

        executor.shutdown()
        assert count == 2
        assert consumed_from_iter[0] <= 4

    def test_timeout_eviction(self):
        executor = futures.ThreadPoolExecutor(max_workers=5)
        gate = threading.Event()

        def submissions():
            for _ in range(2):
                yield lambda: executor.submit(lambda: gate.wait())

        count = multithread_execute(submissions=submissions(), concurrency=2, timeout=0.1)

        gate.set()
        executor.shutdown()
        assert count == 2

    def test_mixed_fast_and_slow(self):
        executor = futures.ThreadPoolExecutor(max_workers=10)
        gate = threading.Event()

        def submissions():
            for _ in range(3):
                yield lambda: executor.submit(lambda: None)
            for _ in range(2):
                yield lambda: executor.submit(lambda: gate.wait())

        count = multithread_execute(submissions=submissions(), concurrency=3, timeout=0.1)

        gate.set()
        executor.shutdown()
        assert count == 5

    def test_empty_submissions(self):
        count = multithread_execute(submissions=iter([]), concurrency=5)
        assert count == 0

    def test_fewer_than_concurrency(self):
        executor = futures.ThreadPoolExecutor(max_workers=10)

        def submissions():
            for _ in range(2):
                yield lambda: executor.submit(lambda: None)

        count = multithread_execute(submissions=submissions(), concurrency=5)

        executor.shutdown()
        assert count == 2

    def test_submission_callable_raises(self):
        executor = futures.ThreadPoolExecutor(max_workers=5)

        def bad_submit():
            raise RuntimeError("submit failed")

        def submissions():
            yield lambda: executor.submit(lambda: None)
            yield bad_submit
            yield lambda: executor.submit(lambda: None)

        count = multithread_execute(submissions=submissions(), concurrency=3)

        executor.shutdown()
        assert count >= 1


# =====================================================================
#   async_execute (async engine)
# =====================================================================


class TestAsyncExecute:
    @pytest.mark.asyncio
    async def test_all_complete_fast(self):
        async def work():
            return "done"

        def submissions():
            for _ in range(10):
                yield lambda: asyncio.create_task(work())

        count = await async_execute(submissions=submissions(), concurrency=5)
        assert count == 10

    @pytest.mark.asyncio
    async def test_concurrency_bound(self):
        active = [0]
        peak = [0]

        async def work():
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            await asyncio.sleep(0.05)
            active[0] -= 1

        def submissions():
            for _ in range(5):
                yield lambda: asyncio.create_task(work())

        count = await async_execute(submissions=submissions(), concurrency=2)

        assert count == 5
        assert peak[0] <= 2

    @pytest.mark.asyncio
    async def test_refill_after_completion(self):
        processed = []

        async def work(i):
            processed.append(i)
            await asyncio.sleep(0.01)

        def submissions():
            for i in range(6):
                yield lambda i=i: asyncio.create_task(work(i))

        count = await async_execute(submissions=submissions(), concurrency=2)

        assert count == 6
        assert len(processed) == 6

    @pytest.mark.asyncio
    async def test_limit_enforcement(self):
        async def work():
            return "done"

        def submissions():
            for _ in range(10):
                yield lambda: asyncio.create_task(work())

        count = await async_execute(submissions=submissions(), concurrency=5, limit=3)
        assert count == 3

    @pytest.mark.asyncio
    async def test_timeout_no_eviction(self):
        async def blocking():
            await asyncio.Event().wait()

        def submissions():
            for _ in range(2):
                yield lambda: asyncio.create_task(blocking())

        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await asyncio.wait_for(
                async_execute(submissions=submissions(), concurrency=2, timeout=0.1),
                timeout=0.5,
            )

    @pytest.mark.asyncio
    async def test_empty_submissions(self):
        count = await async_execute(submissions=iter([]), concurrency=5)
        assert count == 0

    @pytest.mark.asyncio
    async def test_fewer_than_concurrency(self):
        async def work():
            return "done"

        def submissions():
            for _ in range(2):
                yield lambda: asyncio.create_task(work())

        count = await async_execute(submissions=submissions(), concurrency=5)
        assert count == 2
