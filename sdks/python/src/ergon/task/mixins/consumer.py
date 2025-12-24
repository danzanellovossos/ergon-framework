import asyncio
import logging
import time
from abc import ABC, abstractmethod
from concurrent import futures
from typing import Any, List

from opentelemetry import context as otel_context

from ... import connector, telemetry
from .. import base, exceptions, policies
from . import helpers, metrics as mixin_metrics, producer, utils

logger = logging.getLogger(__name__)
tracer = telemetry.tracing.get_tracer(__name__)


class ConsumerMixin(ABC):
    @abstractmethod
    def process_transaction(self, transaction: connector.Transaction) -> Any:
        raise NotImplementedError

    # User hooks
    def handle_process_success(self, transaction, result):
        logger.debug(f"[{self.name}] SUCCESS → {transaction.id}")

    def handle_process_exception(self, transaction, exc):
        logger.error(f"[{self.name}] EXCEPTION → {transaction.id}: {exc}")

    # =====================================================================
    # PROCESS LIFECYCLE
    # =====================================================================
    def _start_processing(self, transaction: connector.Transaction, policy: policies.ConsumerPolicy):
        """
        PROCESS → SUCCESS or EXCEPTION
        """
        tx_start = time.perf_counter()
        final_status = "success"

        try:
            # -----------------------
            # 1) PROCESS STEP
            # -----------------------
            process_ok, process_result = self._handle_process(transaction, policy.process.retry)

            # -----------------------
            # 2) EXCEPTION HANDLER
            # -----------------------
            if not process_ok:
                final_status = "exception"
                if isinstance(process_result, exceptions.TransactionException):
                    process_result = process_result
                elif isinstance(process_result, futures.TimeoutError):
                    process_result = exceptions.TransactionException(str(process_result), exceptions.ExceptionType.TIMEOUT)
                else:
                    process_result = exceptions.TransactionException(str(process_result), exceptions.ExceptionType.SYSTEM)
                return self._handle_exception(transaction, process_result, policy.exception.retry)

            # -----------------------
            # 3) SUCCESS HANDLER
            # -----------------------
            success_ok, success_result = self._handle_success(transaction, process_result, policy.success.retry)

            if not success_ok:
                final_status = "exception"
                if isinstance(success_result, exceptions.TransactionException):
                    success_result = success_result
                elif isinstance(success_result, futures.TimeoutError):
                    success_result = exceptions.TransactionException(str(success_result), exceptions.ExceptionType.TIMEOUT)
                else:
                    success_result = exceptions.TransactionException(str(success_result), exceptions.ExceptionType.SYSTEM)
                return self._handle_exception(transaction, success_result, policy.exception.retry)

            return True, success_result
        finally:
            # Record transaction-level metrics
            tx_duration = time.perf_counter() - tx_start
            mixin_metrics.record_consumer_transaction(
                task_name=getattr(self, "name", self.__class__.__name__),
                transaction_id=transaction.id,
                duration=tx_duration,
                status=final_status,
            )

    # =====================================================================
    # PROCESS HANDLER
    # =====================================================================
    def _handle_process(self, transaction, retry: policies.RetryPolicy):
        logger.info(f"Transaction {transaction.id} processing started")
        stage_start = time.perf_counter()
        success, result = helpers.run_fn(
            fn=lambda: self.process_transaction(transaction),
            retry=retry,
            trace_name=f"{self.__class__.__name__}.process",
            trace_attrs={"transaction_id": transaction.id},
        )
        # Record lifecycle metrics
        mixin_metrics.record_consumer_lifecycle(
            task_name=getattr(self, "name", self.__class__.__name__),
            stage="process",
            duration=time.perf_counter() - stage_start,
            outcome="ok" if success else "error",
        )
        return success, result

    # =====================================================================
    # SUCCESS HANDLER
    # =====================================================================
    def _handle_success(self, transaction, result, retry: policies.RetryPolicy):
        stage_start = time.perf_counter()
        success, handler_result = helpers.run_fn(
            fn=lambda: self.handle_process_success(transaction, result),
            retry=retry,
            trace_name=f"{self.__class__.__name__}.handle_process_success",
            trace_attrs={"transaction_id": transaction.id},
        )
        # Record lifecycle metrics
        mixin_metrics.record_consumer_lifecycle(
            task_name=getattr(self, "name", self.__class__.__name__),
            stage="success",
            duration=time.perf_counter() - stage_start,
            outcome="ok" if success else "error",
        )
        logger.info(f"Transaction {transaction.id} processed successfully")
        return success, handler_result

    # =====================================================================
    # EXCEPTION HANDLER
    # =====================================================================
    def _handle_exception(self, transaction, exc, retry: policies.RetryPolicy):
        logger.error(f"Transaction {transaction.id} processed with exception: {exc}")
        stage_start = time.perf_counter()
        success, result = helpers.run_fn(
            fn=lambda: self.handle_process_exception(transaction, exc),
            retry=retry,
            trace_name=f"{self.__class__.__name__}.handle_process_exception",
            trace_attrs={"transaction_id": transaction.id},
        )
        # Record lifecycle metrics
        mixin_metrics.record_consumer_lifecycle(
            task_name=getattr(self, "name", self.__class__.__name__),
            stage="exception",
            duration=time.perf_counter() - stage_start,
            outcome="ok" if success else "error",
        )
        return success, result

    # =====================================================================
    # FETCH HANDLER
    # =====================================================================
    def _handle_fetch(self, conn, policy: policies.FetchPolicy, batch_size: int):
        logger.info(f"Fetching transactions with batch size {batch_size}", extra=policy.extra)
        fetch_start = time.perf_counter()
        success, result = helpers.run_fn(
            fn=lambda: conn.fetch_transactions(batch_size, **policy.extra),
            retry=policy.retry,
            trace_name=f"{self.__class__.__name__}.fetch_transactions",
            trace_attrs={"batch_size": batch_size},
        )
        # Record fetch metrics
        fetched_count = len(result) if success and result else 0
        mixin_metrics.record_consumer_fetch(
            task_name=getattr(self, "name", self.__class__.__name__),
            connector_name=conn.__class__.__name__,
            batch_size=batch_size,
            fetched_count=fetched_count,
            duration=time.perf_counter() - fetch_start,
            success=success,
        )
        return success, result

    # =====================================================================
    # CONNECTOR RESOLUTION
    # =====================================================================
    def _resolve_connector(self, name: str):
        if name:
            return self.connectors[name]
        if len(self.connectors) == 1:
            return next(iter(self.connectors.values()))
        raise ValueError("Multiple connectors configured; specify one in policy")

    # =====================================================================
    # PUBLIC CONSUME LOOP
    # =====================================================================
    def consume_transactions(self, policy: policies.ConsumerPolicy = None):
        if policy is None:
            policy = policies.ConsumerPolicy()

        def _consume():
            start_time = time.perf_counter()
            processed = 0
            empty_count = 0
            batch_number = 0

            conn = self._resolve_connector(policy.fetch.connector_name)
            executor = futures.ThreadPoolExecutor(max_workers=policy.loop.concurrency.value)

            def submit_start_processing(tr, pol):
                return helpers.run_fn(
                    fn=lambda: self._start_processing(tr, pol),
                    executor=executor,
                    trace_name=f"{self.__class__.__name__}.start_processing",
                    trace_attrs={"transaction_id": tr.id},
                )

            while True:
                batch_number += 1

                # -------------------------
                # FETCH
                # -------------------------
                success, result = self._handle_fetch(conn, policy.fetch, policy.loop.batch.size)
                if not success:
                    logger.error(f"Fetch failed → {result}")
                    break

                transactions = result

                # -------------------------
                # EMPTY QUEUE HANDLING
                # -------------------------
                if not transactions:
                    if not policy.loop.streaming:
                        break
                    # Record empty queue wait metric
                    mixin_metrics.record_consumer_empty_queue_wait(
                        task_name=getattr(self, "name", self.__class__.__name__),
                        wait_count=empty_count,
                    )
                    utils.backoff(
                        policy.loop.empty_queue.backoff,
                        policy.loop.empty_queue.backoff_multiplier,
                        policy.loop.empty_queue.backoff_cap,
                        empty_count,
                    )
                    empty_count += 1
                    continue

                empty_count = 0

                # Record batch metric
                mixin_metrics.record_consumer_batch(
                    task_name=getattr(self, "name", self.__class__.__name__),
                    batch_number=batch_number,
                    batch_size=len(transactions),
                    streaming=policy.loop.streaming,
                )

                # ============================================================
                #  RUN CONCURRENTLY WITH REFILL (with batch-level span)
                # ============================================================
                # For streaming mode, start each batch as a fresh root span (new trace)
                # This prevents "root span not received" issues in long-running consumers
                if policy.loop.streaming:
                    # Create a fresh empty context with no active span
                    # This forces the tracer to generate a new trace ID for each batch
                    batch_context = otel_context.Context()
                else:
                    batch_context = None  # Use current context

                with tracer.start_as_current_span(
                    f"{self.__class__.__name__}.process_batch",
                    context=batch_context,
                    attributes={
                        "batch_number": batch_number,
                        "batch_size": len(transactions),
                        "streaming": policy.loop.streaming,
                    },
                ):
                    count = helpers.run_concurrently_with_refill(
                        data=transactions,
                        it=iter((tr, policy) for tr in transactions),
                        submit_fn=submit_start_processing,
                        concurrency=policy.loop.concurrency.value,
                        limit=policy.loop.limit,
                        count=processed,
                        timeout=policy.loop.transaction_timeout,
                    )

                processed += count

                if policy.loop.limit and processed >= policy.loop.limit:
                    break

            executor.shutdown()
            elapsed_time = time.perf_counter() - start_time
            logger.info(f"[Consume] Finished. Processed={processed} in {elapsed_time:.2f} seconds")
            return processed

        # For streaming mode, run without wrapping span (batches have their own spans)
        # For non-streaming mode, wrap entire consume in a span
        if policy.loop.streaming:
            try:
                return _consume()
            except futures.TimeoutError as e:
                raise exceptions.ConsumerLoopTimeoutException(str(e))
            except exceptions.TransactionException:
                raise
            except BaseException as e:
                raise exceptions.ConsumerLoopException(str(e))
        else:
            success, result = helpers.run_fn(
                fn=lambda: _consume(),
                retry=policies.RetryPolicy(timeout=policy.loop.timeout),
                trace_name=f"{self.__class__.__name__}.consume_transactions",
                trace_attrs={},
            )

            if not success:
                if isinstance(result, exceptions.TransactionException):
                    result = result
                elif isinstance(result, futures.TimeoutError):
                    result = exceptions.ConsumerLoopTimeoutException(str(result))
                else:
                    result = exceptions.ConsumerLoopException(str(result))
                raise result
            return result


class ConsumerTask(ConsumerMixin, base.BaseTask):
    """
    Backwards-compatible consumer task.
    You can still inherit from this if you're only a consumer.
    """

    pass


class HybridTask(producer.ProducerMixin, ConsumerMixin, base.BaseTask):
    """
    Hybrid task that can produce and consume transactions.
    """

    pass


# =====================================================================
#   ASYNC CONSUMER MIXIN
# =====================================================================


class AsyncConsumerMixin(ABC):
    # =====================================================================
    # HOOKS
    # =====================================================================
    @abstractmethod
    async def process_transaction(self, transaction: connector.Transaction) -> Any:
        raise NotImplementedError

    async def handle_transaction_success(self, transaction, result):
        logger.debug(f"[{self.name}] SUCCESS → {transaction.id}")

    async def handle_transaction_exception(self, transaction, exc):
        logger.error(f"[{self.name}] EXCEPTION → {transaction.id}: {exc}")

    # =====================================================================
    #   FETCH WITH RETRIES (ASYNC)
    # =====================================================================
    async def _fetch_transactions(
        self, conn, policy: policies.FetchPolicy, batch_size: int
    ) -> tuple[bool, List[connector.Transaction]]:
        logger.info(f"Fetching transactions with batch size {batch_size}", extra=policy.extra)
        fetch_start = time.perf_counter()
        success, result = await helpers.run_fn_async(
            fn=lambda: conn.fetch_transactions_async(batch_size, **policy.extra),
            retry=policy.retry,
            trace_name=f"{self.__class__.__name__}.fetch_transactions",
            trace_attrs={"batch_size": batch_size},
        )
        # Record fetch metrics
        fetched_count = len(result) if success and result else 0
        mixin_metrics.record_consumer_fetch(
            task_name=getattr(self, "name", self.__class__.__name__),
            connector_name=conn.__class__.__name__,
            batch_size=batch_size,
            fetched_count=fetched_count,
            duration=time.perf_counter() - fetch_start,
            success=success,
        )
        return success, result

    # =====================================================================
    #   PROCESS OR ROUTE INTO SUCCESS / EXCEPTION
    # =====================================================================
    async def _start_processing(self, transaction, policy: policies.ConsumerPolicy):
        tx_start = time.perf_counter()
        final_status = "success"

        try:
            process_ok, process_result = await self._handle_process(transaction, policy.process.retry)

            if not process_ok:
                final_status = "exception"
                if isinstance(process_result, exceptions.TransactionException):
                    process_result = process_result
                elif isinstance(process_result, futures.TimeoutError):
                    process_result = exceptions.TransactionException(str(process_result), exceptions.ExceptionType.TIMEOUT)
                else:
                    process_result = exceptions.TransactionException(str(process_result), exceptions.ExceptionType.SYSTEM)
                return await self._handle_exception(transaction, process_result, policy.exception.retry)

            success_ok, success_result = await self._handle_success(
                transaction, process_result, policy.success.retry, policy.exception.retry
            )

            if not success_ok:
                final_status = "exception"
                if isinstance(success_result, exceptions.TransactionException):
                    success_result = success_result
                elif isinstance(success_result, futures.TimeoutError):
                    success_result = exceptions.TransactionException(str(success_result), exceptions.ExceptionType.TIMEOUT)
                else:
                    success_result = exceptions.TransactionException(str(success_result), exceptions.ExceptionType.SYSTEM)
                return await self._handle_exception(transaction, success_result, policy.exception.retry)

            return True, success_result
        finally:
            # Record transaction-level metrics
            tx_duration = time.perf_counter() - tx_start
            mixin_metrics.record_consumer_transaction(
                task_name=getattr(self, "name", self.__class__.__name__),
                transaction_id=transaction.id,
                duration=tx_duration,
                status=final_status,
            )

    # =====================================================================
    #   PROCESS HANDLER WITH RETRIES
    # =====================================================================
    async def _handle_process(self, transaction, retry: policies.RetryPolicy):
        logger.info(f"Transaction {transaction.id} processing started")
        stage_start = time.perf_counter()
        success, result = await helpers.run_fn_async(
            fn=lambda: self.process_transaction(transaction),
            retry=retry,
            trace_name=f"{self.__class__.__name__}.handle_process",
            trace_attrs={"transaction_id": transaction.id},
        )
        # Record lifecycle metrics
        mixin_metrics.record_consumer_lifecycle(
            task_name=getattr(self, "name", self.__class__.__name__),
            stage="process",
            duration=time.perf_counter() - stage_start,
            outcome="ok" if success else "error",
        )
        return success, result

    # =====================================================================
    #   SUCCESS HANDLER
    # =====================================================================
    async def _handle_success(self, transaction, result, retry, exception_retry):
        logger.info(f"Transaction {transaction.id} processed successfully")
        stage_start = time.perf_counter()
        success, handler_result = await helpers.run_fn_async(
            fn=lambda: self.handle_transaction_success(transaction, result),
            retry=retry,
            trace_name=f"{self.__class__.__name__}.handle_success",
            trace_attrs={"transaction_id": transaction.id},
        )
        # Record lifecycle metrics
        mixin_metrics.record_consumer_lifecycle(
            task_name=getattr(self, "name", self.__class__.__name__),
            stage="success",
            duration=time.perf_counter() - stage_start,
            outcome="ok" if success else "error",
        )
        return success, handler_result

    # =====================================================================
    #   EXCEPTION HANDLER
    # =====================================================================
    async def _handle_exception(self, transaction, exc, retry: policies.RetryPolicy):
        logger.error(f"Transaction {transaction.id} processed with exception: {exc}")
        stage_start = time.perf_counter()
        success, result = await helpers.run_fn_async(
            fn=lambda: self.handle_transaction_exception(transaction, exc),
            retry=retry,
            trace_name=f"{self.__class__.__name__}.handle_exception",
            trace_attrs={"transaction_id": transaction.id},
        )
        # Record lifecycle metrics
        mixin_metrics.record_consumer_lifecycle(
            task_name=getattr(self, "name", self.__class__.__name__),
            stage="exception",
            duration=time.perf_counter() - stage_start,
            outcome="ok" if success else "error",
        )
        return success, result

    # =====================================================================
    #   ASYNC PUBLIC CONSUME LOOP (MIRRORS ASYNC PRODUCER)
    # =====================================================================
    async def consume_transactions(self, conn, policy: policies.ConsumerPolicy = None):
        if policy is None:
            policy = policies.ConsumerPolicy()

        async def _consume():
            start_time = time.perf_counter()
            processed = 0
            empty_count = 0
            batch_number = 0

            async def submit_start_processing(tr, pol):
                return await helpers.run_fn_async(
                    fn=lambda: self._start_processing(tr, pol),
                    retry=policy.process.retry,
                    trace_name=f"{self.__class__.__name__}.start_processing",
                    trace_attrs={"transaction_id": tr.id},
                )

            while True:
                batch_number += 1

                # ============================================================
                #  FETCH
                # ============================================================
                success, result = await self._fetch_transactions(conn, policy.fetch, policy.loop.batch.size)

                if not success:
                    logger.error(f"Fetch failed → {result}")
                    break

                transactions = result

                # ============================================================
                #  EMPTY QUEUE HANDLING
                # ============================================================
                if not transactions:
                    if not policy.loop.streaming:
                        break

                    # Record empty queue wait metric
                    mixin_metrics.record_consumer_empty_queue_wait(
                        task_name=getattr(self, "name", self.__class__.__name__),
                        wait_count=empty_count,
                    )
                    await utils.backoff_async(
                        backoff=policy.loop.empty_queue.backoff,
                        backoff_multiplier=policy.loop.empty_queue.backoff_multiplier,
                        backoff_cap=policy.loop.empty_queue.backoff_cap,
                        attempt=empty_count,
                    )
                    empty_count += 1
                    continue

                empty_count = 0

                # Record batch metric
                mixin_metrics.record_consumer_batch(
                    task_name=getattr(self, "name", self.__class__.__name__),
                    batch_number=batch_number,
                    batch_size=len(transactions),
                    streaming=policy.loop.streaming,
                )

                # ============================================================
                #  RUN CONCURRENTLY WITH REFILL (with batch-level span)
                # ============================================================
                # For streaming mode, start each batch as a fresh root span (new trace)
                # This prevents "root span not received" issues in long-running consumers
                if policy.loop.streaming:
                    # Create a fresh empty context with no active span
                    # This forces the tracer to generate a new trace ID for each batch
                    batch_context = otel_context.Context()
                else:
                    batch_context = None  # Use current context

                with tracer.start_as_current_span(
                    f"{self.__class__.__name__}.process_batch",
                    context=batch_context,
                    attributes={
                        "batch_number": batch_number,
                        "batch_size": len(transactions),
                        "streaming": policy.loop.streaming,
                    },
                ):
                    count = await helpers.run_concurrently_with_refill_async(
                        data=transactions,
                        it=iter((tr, policy) for tr in transactions),
                        submit_fn=submit_start_processing,
                        concurrency=policy.loop.concurrency.value,
                        limit=policy.loop.limit,
                        count=processed,
                        timeout=policy.loop.transaction_timeout,
                    )

                processed += count

                if policy.loop.limit and processed >= policy.loop.limit:
                    break

            elapsed_time = time.perf_counter() - start_time
            logger.info(f"[Consume] Finished. Processed={processed} in {elapsed_time:.2f} seconds")
            return processed

        # For streaming mode, run without wrapping span (batches have their own spans)
        # For non-streaming mode, wrap entire consume in a span
        if policy.loop.streaming:
            try:
                return await _consume()
            except asyncio.TimeoutError as e:
                logger.error(f"[Consume] Timeout: {e}")
                raise exceptions.ConsumerLoopTimeoutException(str(e))
            except exceptions.TransactionException:
                raise
            except BaseException as e:
                logger.error(f"[Consume] Error: {e}")
                raise exceptions.TransactionException(str(e), exceptions.ExceptionType.SYSTEM)
        else:
            try:
                return await helpers.run_fn_async(
                    fn=_consume,
                    retry=policies.RetryPolicy(timeout=policy.loop.timeout),
                    trace_name=f"{self.__class__.__name__}.consume_transactions",
                    trace_attrs={},
                )
            except asyncio.TimeoutError as e:
                logger.error(f"[Consume] Timeout: {e}")
                raise exceptions.ConsumerLoopTimeoutException(str(e))
            except BaseException as e:
                logger.error(f"[Consume] Error: {e}")
                raise exceptions.TransactionException(str(e), exceptions.ExceptionType.SYSTEM)


class AsyncConsumerTask(AsyncConsumerMixin, base.BaseAsyncTask):
    pass


class AsyncHybridTask(AsyncConsumerMixin, producer.AsyncProducerMixin, base.BaseAsyncTask):
    pass
