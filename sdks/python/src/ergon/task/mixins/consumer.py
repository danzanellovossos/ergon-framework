import asyncio
import logging
import time
from abc import ABC, abstractmethod
from concurrent import futures
from datetime import datetime
from typing import Any, List

from opentelemetry import context as otel_context

from ... import connector, telemetry
from .. import base, exceptions, helpers, policies, utils
from . import metrics as mixin_metrics
from . import producer

logger = logging.getLogger(__name__)
tracer = telemetry.tracing.get_tracer(__name__)


def _wrap_handler_failure(result: Any) -> exceptions.TransactionException:
    """Normalise a handler failure value into a TransactionException.

    Preserves the original exception (and its traceback) on ``__cause__`` so
    downstream ``logger.exception`` can render the real stack instead of
    ``NoneType: None``. Uses ``repr`` of the cause to derive a diagnostic
    message when the cause has an empty ``str()``.
    """
    if isinstance(result, exceptions.TransactionException):
        return result
    if isinstance(result, futures.TimeoutError):
        return exceptions.TransactionException(
            message=repr(result),
            category=exceptions.ExceptionType.TIMEOUT,
            cause=result if isinstance(result, BaseException) else None,
        )
    if isinstance(result, BaseException):
        return exceptions.TransactionException(
            message=None,  # let constructor derive from cause via repr()
            category=exceptions.ExceptionType.SYSTEM,
            cause=result,
        )
    return exceptions.TransactionException(
        message=repr(result),
        category=exceptions.ExceptionType.SYSTEM,
    )


class ConsumerMixin(ABC):
    name: str
    connectors: dict[str, connector.Connector]

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
            logger.info(f"Transaction {transaction.id} processing started")
            process_ok, process_result = self._handle_process(transaction, policy.process.retry)

            # -----------------------
            # 2) EXCEPTION HANDLER
            # -----------------------
            if not process_ok:
                logger.error(
                    "Transaction %s process handler failed with outcome %r",
                    transaction.id,
                    process_result,
                )
                final_status = "exception"
                process_exc = _wrap_handler_failure(process_result)
                logger.error(
                    "Invoking exception handler for transaction %s with outcome: %s",
                    transaction.id,
                    process_exc,
                )
                exc_ok, exc_result = self._handle_exception(transaction, process_exc, policy.exception.retry)
                if not exc_ok and isinstance(exc_result, exceptions.DeadChannelError):
                    logger.warning(
                        "Transaction %s exception handler hit a dead channel "
                        "(%s); broker will redeliver. Skipping further routing.",
                        transaction.id,
                        exc_result,
                    )
                    final_status = "redeliver"
                return exc_ok, exc_result

            # -----------------------
            # 3) SUCCESS HANDLER
            # -----------------------
            logger.info(f"Invoking success handler for transaction {transaction.id} with outcome: '{process_result}'")
            success_ok, success_result = self._handle_success(transaction, process_result, policy.success.retry)

            if not success_ok:
                # ----------------------------------------------------------
                # SHORT-CIRCUIT: ack/nack against a dead broker channel.
                # Routing to the exception handler would only re-fail (it
                # would nack on the same dead channel). The broker will
                # redeliver the message to a fresh subscriber.
                # ----------------------------------------------------------
                if isinstance(success_result, exceptions.DeadChannelError):
                    logger.warning(
                        "Transaction %s success handler could not ack on a dead "
                        "channel (%s); broker will redeliver. Skipping exception handler.",
                        transaction.id,
                        success_result,
                    )
                    final_status = "redeliver"
                    return False, success_result

                logger.error(
                    "Transaction %s success handler failed with outcome %r",
                    transaction.id,
                    success_result,
                )
                final_status = "exception"
                success_exc = _wrap_handler_failure(success_result)
                logger.error(
                    "Invoking exception handler for transaction %s with outcome: %s",
                    transaction.id,
                    success_exc,
                )
                exc_ok, exc_result = self._handle_exception(transaction, success_exc, policy.exception.retry)
                if not exc_ok and isinstance(exc_result, exceptions.DeadChannelError):
                    logger.warning(
                        "Transaction %s exception handler hit a dead channel (%s); broker will redeliver.",
                        transaction.id,
                        exc_result,
                    )
                    final_status = "redeliver"
                return exc_ok, exc_result

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
        logger.info(f"Transaction {transaction.id} process handler started")
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
        logger.info(
            f"Transaction {transaction.id} process handler completed with status: {'success' if success else 'error'}"
        )
        return success, result

    # =====================================================================
    # SUCCESS HANDLER
    # =====================================================================
    def _handle_success(self, transaction, result, retry: policies.RetryPolicy):
        logger.info(f"Transaction {transaction.id} success handler started")
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
        logger.info(
            f"Transaction {transaction.id} success handler completed with status: {'success' if success else 'error'}"
        )
        return success, handler_result

    # =====================================================================
    # EXCEPTION HANDLER
    # =====================================================================
    def _handle_exception(self, transaction, exc, retry: policies.RetryPolicy):
        logger.error(f"Transaction {transaction.id} exception handler started")
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
        logger.info(
            f"Transaction {transaction.id} exception handler completed with status: {'success' if success else 'error'}"
        )
        return success, result

    # =====================================================================
    # FETCH HANDLER
    # =====================================================================
    def _handle_fetch(self, conn, policy: policies.FetchPolicy):
        logger.info(f"Fetch handler started for batch size {policy.batch.size}", extra=policy.extra)
        fetch_start = time.perf_counter()
        success, result = helpers.run_fn(
            fn=lambda: conn.fetch_transactions(policy.batch.size, **policy.extra),
            retry=policy.retry,
            trace_name=f"{self.__class__.__name__}.fetch_transactions",
            trace_attrs={"batch_size": policy.batch.size},
        )
        # Record fetch metrics
        fetched_count = len(result) if success and result else 0
        mixin_metrics.record_consumer_fetch(
            task_name=getattr(self, "name", self.__class__.__name__),
            connector_name=conn.__class__.__name__,
            batch_size=policy.batch.size,
            fetched_count=fetched_count,
            duration=time.perf_counter() - fetch_start,
            success=success,
        )
        logger.info(f"Fetch handler completed with status: {'success' if success else 'error'}")
        return success, result

    # =====================================================================
    # CONNECTOR RESOLUTION
    # =====================================================================
    def _resolve_connector(self, name: str | None):
        if name:
            return self.connectors[name]
        if len(self.connectors) == 1:
            return next(iter(self.connectors.values()))
        raise ValueError("Multiple connectors configured; specify one in policy")

    # =====================================================================
    # PUBLIC CONSUME LOOP
    # =====================================================================
    def consume_transactions(self, policy: policies.ConsumerPolicy | None = None):
        if policy is None:
            policy = policies.ConsumerPolicy()

        def _consume():
            start_time_iso = datetime.now().isoformat()
            start_time = time.perf_counter()
            processed = 0
            empty_count = 0
            batch_number = 0

            logger.info(f"Consume loop started at {start_time_iso}")
            logger.debug(f"Consume loop running with loop policy: {policy.loop.model_dump_json(indent=2)}")

            conn = self._resolve_connector(policy.fetch.connector_name)
            executor = futures.ThreadPoolExecutor(
                max_workers=policy.loop.concurrency.value + policy.loop.concurrency.headroom
            )

            ctx = otel_context.Context()

            def submit_start_processing(tr, pol):
                return helpers.run_fn(
                    fn=lambda: self._start_processing(tr, pol),
                    ctx=ctx,
                    executor=executor,
                    trace_name=f"{self.__class__.__name__}.start_processing",
                    trace_attrs={"transaction_id": tr.id},
                )

            while True:
                batch_number += 1

                # -------------------------
                # FETCH
                # -------------------------
                logger.info(f"Fetching transactions batch with fetch policy: {policy.fetch.model_dump_json(indent=2)}")
                success, result = self._handle_fetch(conn, policy.fetch)
                if not success:
                    logger.error(f"Fetch failed → {result}")
                    executor.shutdown(wait=False)
                    if isinstance(result, futures.TimeoutError):
                        raise exceptions.FetchTimeoutException(str(result))
                    raise exceptions.FetchException(str(result))

                transactions = result

                # -------------------------
                # EMPTY QUEUE HANDLING
                # -------------------------
                if not transactions:
                    logger.info(f"Empty fetch detected at {datetime.now().isoformat()}")
                    if not policy.loop.streaming:
                        logger.info("Non-streaming mode detected, breaking loop")
                        break

                    logger.debug(f"{empty_count} consecutive empty fetches so far")

                    mixin_metrics.record_consumer_empty_queue_wait(
                        task_name=getattr(self, "name", self.__class__.__name__),
                        wait_count=empty_count,
                    )

                    utils.backoff(
                        policy.fetch.empty.backoff,
                        policy.fetch.empty.backoff_multiplier,
                        policy.fetch.empty.backoff_cap,
                        empty_count,
                    )
                    empty_count += 1
                    continue

                empty_count = 0

                logger.info(
                    f"{len(transactions)} transaction{'' if len(transactions) == 1 else 's'}fetched from fetch handler"
                )

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
                if policy.loop.streaming:
                    batch_context = ctx
                else:
                    batch_context = None  # Use current context

                logger.info(
                    f"Starting batch processing of "
                    f"{len(transactions)} transaction{'' if len(transactions) == 1 else 's'} "
                    f"from fetch handler with "
                    f"with concurrency policy: {policy.loop.concurrency.model_dump_json(indent=2)}."
                )

                with tracer.start_as_current_span(
                    f"{self.__class__.__name__}.process_batch",
                    context=batch_context,
                    attributes={
                        "batch_number": batch_number,
                        "batch_size": len(transactions),
                        "streaming": policy.loop.streaming,
                    },
                ):

                    def submissions():
                        for tr in transactions:
                            yield lambda tr=tr: submit_start_processing(tr, policy)

                    logger.debug(
                        f"Submitting {len(transactions)} transactions for processing "
                        f"with concurrency policy: {policy.loop.concurrency.model_dump_json(indent=2)}."
                    )

                    count = helpers.multithread_execute(
                        submissions=submissions(),
                        concurrency=policy.loop.concurrency.value,
                        limit=policy.loop.limit,
                        timeout=policy.transaction_runtime.timeout,
                    )

                processed += count

                if policy.loop.limit and processed >= policy.loop.limit:
                    break

                if policy.fetch.batch.interval and policy.fetch.batch.interval.backoff > 0:
                    logger.info("Batch interval detected, triggering backoff")
                    utils.backoff(
                        backoff=policy.fetch.batch.interval.backoff,
                        multiplier=policy.fetch.batch.interval.backoff_multiplier,
                        cap=policy.fetch.batch.interval.backoff_cap,
                        attempt=0,
                    )

            executor.shutdown()
            elapsed_time = time.perf_counter() - start_time
            logger.info(f"[Consume] Finished. Processed={processed} in {elapsed_time:.2f} seconds")
            return processed

        # For streaming mode, run without wrapping span (batches have their own spans)
        # For non-streaming mode, wrap entire consume in a spans
        if policy.loop.streaming:
            try:
                return _consume()
            except futures.TimeoutError as e:
                raise exceptions.ConsumerLoopTimeoutException(str(e))
        else:
            success, result = helpers.run_fn(
                fn=lambda: _consume(),
                retry=policies.RetryPolicy(timeout=policy.loop.timeout),
                trace_name=f"{self.__class__.__name__}.consume_transactions",
                trace_attrs={},
            )

            if not success:
                if isinstance(result, futures.TimeoutError):
                    raise exceptions.ConsumerLoopTimeoutException(str(result))
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
    name: str
    connectors: dict[str, connector.AsyncConnector]

    # =====================================================================
    # HOOKS
    # =====================================================================
    @abstractmethod
    async def process_transaction(self, transaction: connector.Transaction) -> Any:
        raise NotImplementedError

    async def handle_process_success(self, transaction, result):
        logger.debug(f"[{self.name}] SUCCESS → {transaction.id}")

    async def handle_process_exception(self, transaction, exc):
        logger.error(f"[{self.name}] EXCEPTION → {transaction.id}: {exc}")

    # =====================================================================
    #   FETCH HANDLER (ASYNC)
    # =====================================================================
    async def _handle_fetch(self, conn, policy: policies.FetchPolicy) -> tuple[bool, List[connector.Transaction]]:
        logger.info(f"Fetch handler started for batch size {policy.batch.size}", extra=policy.extra)
        fetch_start = time.perf_counter()
        success, result = await helpers.run_fn_async(
            fn=lambda: conn.fetch_transactions_async(policy.batch.size, **policy.extra),
            retry=policy.retry,
            trace_name=f"{self.__class__.__name__}.fetch_transactions",
            trace_attrs={"batch_size": policy.batch.size},
        )
        # Record fetch metrics
        fetched_count = len(result) if success and result else 0
        mixin_metrics.record_consumer_fetch(
            task_name=getattr(self, "name", self.__class__.__name__),
            connector_name=conn.__class__.__name__,
            batch_size=policy.batch.size,
            fetched_count=fetched_count,
            duration=time.perf_counter() - fetch_start,
            success=success,
        )
        logger.info(f"Fetch handler completed with status: {'success' if success else 'error'}")
        return success, result

    # =====================================================================
    #   PROCESS OR ROUTE INTO SUCCESS / EXCEPTION
    # =====================================================================
    async def _start_processing(self, transaction, policy: policies.ConsumerPolicy):
        """
        PROCESS → SUCCESS or EXCEPTION
        """
        tx_start = time.perf_counter()
        final_status = "success"

        try:
            # -----------------------
            # 1) PROCESS STEP
            # -----------------------
            logger.info(f"Transaction {transaction.id} processing started")
            process_ok, process_result = await self._handle_process(transaction, policy.process.retry)

            # -----------------------
            # 2) EXCEPTION HANDLER
            # -----------------------
            if not process_ok:
                logger.error(
                    "Transaction %s process handler failed with outcome %r",
                    transaction.id,
                    process_result,
                )
                final_status = "exception"
                process_exc = _wrap_handler_failure(process_result)
                logger.error(
                    "Invoking exception handler for transaction %s with outcome: %s",
                    transaction.id,
                    process_exc,
                )
                exc_ok, exc_result = await self._handle_exception(transaction, process_exc, policy.exception.retry)
                if not exc_ok and isinstance(exc_result, exceptions.DeadChannelError):
                    logger.warning(
                        "Transaction %s exception handler hit a dead channel "
                        "(%s); broker will redeliver. Skipping further routing.",
                        transaction.id,
                        exc_result,
                    )
                    final_status = "redeliver"
                return exc_ok, exc_result

            # -----------------------
            # 3) SUCCESS HANDLER
            # -----------------------
            logger.info(f"Invoking success handler for transaction {transaction.id} with outcome: '{process_result}'")
            success_ok, success_result = await self._handle_success(transaction, process_result, policy.success.retry)

            if not success_ok:
                # ----------------------------------------------------------
                # SHORT-CIRCUIT: ack/nack against a dead broker channel.
                # Routing to the exception handler would only re-fail (it
                # would nack on the same dead channel). The broker will
                # redeliver the message to a fresh subscriber.
                # ----------------------------------------------------------
                if isinstance(success_result, exceptions.DeadChannelError):
                    logger.warning(
                        "Transaction %s success handler could not ack on a dead "
                        "channel (%s); broker will redeliver. Skipping exception handler.",
                        transaction.id,
                        success_result,
                    )
                    final_status = "redeliver"
                    return False, success_result

                logger.error(
                    "Transaction %s success handler failed with outcome %r",
                    transaction.id,
                    success_result,
                )
                final_status = "exception"
                success_exc = _wrap_handler_failure(success_result)
                logger.error(
                    "Invoking exception handler for transaction %s with outcome: %s",
                    transaction.id,
                    success_exc,
                )
                exc_ok, exc_result = await self._handle_exception(transaction, success_exc, policy.exception.retry)
                if not exc_ok and isinstance(exc_result, exceptions.DeadChannelError):
                    logger.warning(
                        "Transaction %s exception handler hit a dead channel (%s); broker will redeliver.",
                        transaction.id,
                        exc_result,
                    )
                    final_status = "redeliver"
                return exc_ok, exc_result

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
        logger.info(f"Transaction {transaction.id} process handler started")
        stage_start = time.perf_counter()
        success, result = await helpers.run_fn_async(
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
        logger.info(
            f"Transaction {transaction.id} process handler completed with status: {'success' if success else 'error'}"
        )
        return success, result

    # =====================================================================
    #   SUCCESS HANDLER
    # =====================================================================
    async def _handle_success(self, transaction, result, retry: policies.RetryPolicy):
        logger.info(f"Transaction {transaction.id} success handler started")
        stage_start = time.perf_counter()
        success, handler_result = await helpers.run_fn_async(
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
        logger.info(
            f"Transaction {transaction.id} success handler completed with status: {'success' if success else 'error'}"
        )
        return success, handler_result

    # =====================================================================
    #   EXCEPTION HANDLER
    # =====================================================================
    async def _handle_exception(self, transaction, exc, retry: policies.RetryPolicy):
        logger.error(f"Transaction {transaction.id} exception handler started")
        stage_start = time.perf_counter()
        success, result = await helpers.run_fn_async(
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
        logger.info(
            f"Transaction {transaction.id} exception handler completed with status: {'success' if success else 'error'}"
        )
        return success, result

    # =====================================================================
    # CONNECTOR RESOLUTION
    # =====================================================================
    def _resolve_connector(self, name: str | None):
        if name:
            return self.connectors[name]
        if len(self.connectors) == 1:
            return next(iter(self.connectors.values()))
        raise ValueError("Multiple connectors configured; specify one in policy")

    # =====================================================================
    #   ASYNC PUBLIC CONSUME LOOP
    # =====================================================================
    async def consume_transactions(self, policy: policies.ConsumerPolicy | None = None):
        if policy is None:
            policy = policies.ConsumerPolicy()

        async def _consume():
            start_time_iso = datetime.now().isoformat()
            start_time = time.perf_counter()
            processed = 0
            empty_count = 0
            batch_number = 0

            logger.info(f"Consume loop started at {start_time_iso}")
            logger.debug(f"Consume loop running with loop policy: {policy.loop.model_dump_json(indent=2)}")

            conn = self._resolve_connector(policy.fetch.connector_name)

            ctx = otel_context.Context()

            async def submit_start_processing(tr, pol):
                return await helpers.run_fn_async(
                    fn=lambda: self._start_processing(tr, pol),
                    trace_name=f"{self.__class__.__name__}.start_processing",
                    trace_attrs={"transaction_id": tr.id},
                )

            while True:
                batch_number += 1

                # ============================================================
                #  FETCH
                # ============================================================
                logger.info(f"Fetching transactions batch with fetch policy: {policy.fetch.model_dump_json(indent=2)}")
                success, result = await self._handle_fetch(conn, policy.fetch)

                if not success:
                    logger.error(f"Fetch failed → {result}")
                    if isinstance(result, (asyncio.TimeoutError, futures.TimeoutError)):
                        raise exceptions.FetchTimeoutException(str(result))
                    raise exceptions.FetchException(str(result))

                transactions = result

                # ============================================================
                #  EMPTY QUEUE HANDLING
                # ============================================================
                if not transactions:
                    logger.info(f"Empty fetch detected at {datetime.now().isoformat()}")
                    if not policy.loop.streaming:
                        logger.info("Non-streaming mode detected, breaking loop")
                        break

                    logger.debug(f"{empty_count} consecutive empty fetches so far")

                    mixin_metrics.record_consumer_empty_queue_wait(
                        task_name=getattr(self, "name", self.__class__.__name__),
                        wait_count=empty_count,
                    )

                    await utils.backoff_async(
                        backoff=policy.fetch.empty.backoff,
                        multiplier=policy.fetch.empty.backoff_multiplier,
                        cap=policy.fetch.empty.backoff_cap,
                        attempt=empty_count,
                    )
                    empty_count += 1
                    continue

                empty_count = 0

                logger.info(f"{len(transactions)} transaction(s) fetched from fetch handler")

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
                if policy.loop.streaming:
                    batch_context = ctx
                else:
                    batch_context = None  # Use current context

                logger.info(
                    f"Starting batch processing of "
                    f"{len(transactions)} transaction(s) "
                    f"from fetch handler with "
                    f"with concurrency policy: {policy.loop.concurrency.model_dump_json(indent=2)}."
                )

                with tracer.start_as_current_span(
                    f"{self.__class__.__name__}.process_batch",
                    context=batch_context,
                    attributes={
                        "batch_number": batch_number,
                        "batch_size": len(transactions),
                        "streaming": policy.loop.streaming,
                    },
                ):

                    def submissions():
                        for tr in transactions:
                            yield lambda tr=tr: asyncio.create_task(submit_start_processing(tr, policy))

                    logger.debug(
                        f"Submitting {len(transactions)} transactions for processing "
                        f"with concurrency policy: {policy.loop.concurrency.model_dump_json(indent=2)}."
                    )

                    count = await helpers.async_execute(
                        submissions=submissions(),
                        concurrency=policy.loop.concurrency.value,
                        limit=policy.loop.limit,
                        timeout=policy.transaction_runtime.timeout,
                    )

                processed += count

                if policy.loop.limit and processed >= policy.loop.limit:
                    break

                if policy.fetch.batch.interval and policy.fetch.batch.interval.backoff > 0:
                    logger.info("Batch interval detected, triggering backoff")
                    await utils.backoff_async(
                        backoff=policy.fetch.batch.interval.backoff,
                        multiplier=policy.fetch.batch.interval.backoff_multiplier,
                        cap=policy.fetch.batch.interval.backoff_cap,
                        attempt=0,
                    )

            elapsed_time = time.perf_counter() - start_time
            logger.info(f"[Consume] Finished. Processed={processed} in {elapsed_time:.2f} seconds")
            return processed

        # For streaming mode, run without wrapping span (batches have their own spans)
        # For non-streaming mode, wrap entire consume in a span
        if policy.loop.streaming:
            try:
                return await _consume()
            except asyncio.TimeoutError as e:
                raise exceptions.ConsumerLoopTimeoutException(str(e))
        else:
            success, result = await helpers.run_fn_async(
                fn=_consume,
                retry=policies.RetryPolicy(timeout=policy.loop.timeout),
                trace_name=f"{self.__class__.__name__}.consume_transactions",
                trace_attrs={},
            )
            if not success:
                if isinstance(result, asyncio.TimeoutError):
                    raise exceptions.ConsumerLoopTimeoutException(str(result))
                raise result
            return result


class AsyncConsumerTask(AsyncConsumerMixin, base.BaseAsyncTask):
    pass


class AsyncHybridTask(producer.AsyncProducerMixin, AsyncConsumerMixin, base.BaseAsyncTask):
    """
    Async hybrid task that can consume and produce transactions.
    """

    pass
