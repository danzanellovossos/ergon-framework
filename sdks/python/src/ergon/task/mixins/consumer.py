import asyncio
import logging
import time
from abc import ABC, abstractmethod
from concurrent import futures
from datetime import datetime
from typing import Any, List

from async_timeout import timeout as timeout_after
from opentelemetry import context as otel_context

from ... import connector, telemetry
from .. import base, exceptions, helpers, liveness, policies, utils
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


def _warn_if_prefetch_exceeds_concurrency(conn: Any, policy: policies.ConsumerPolicy, task_name: str) -> None:
    """Warn when a broker consumer holds more unacked messages than it can process.

    A prefetch larger than the loop concurrency lets idle sibling messages sit
    unacked until they age past the broker ``consumer_timeout``, which triggers
    a ``Basic.Cancel`` and the whole consumer-recovery path. The connector
    cannot see the loop concurrency and the policy cannot see the connector's
    prefetch, so the check lives here where both are visible. Best-effort:
    silently skips connectors that do not expose a consumer config.
    """
    consumer_config = getattr(conn, "_consumer_config", None)
    prefetch = getattr(consumer_config, "prefetch_count", None)
    if not isinstance(prefetch, int):
        return
    concurrency = policy.loop.concurrency.value
    if prefetch > concurrency:
        logger.warning(
            "[%s] prefetch_count=%d exceeds loop concurrency=%d: up to %d messages will be "
            "held unacked while only %d are processed concurrently. Idle siblings can age past "
            "the broker consumer_timeout and trigger a Basic.Cancel. Set prefetch_count == concurrency.",
            task_name,
            prefetch,
            concurrency,
            prefetch,
            concurrency,
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
        except asyncio.CancelledError:
            final_status = "timeout"
            raise
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
    _consumer_liveness_state: str
    _consumer_last_progress_monotonic: float
    _consumer_in_flight: set[int]
    _consumer_active_stale_after: float | None
    _consumer_fetch_stale_after: float

    def _mark_consumer_progress(self, state: str) -> None:
        self._consumer_liveness_state = state
        self._consumer_last_progress_monotonic = time.monotonic()

    def _set_transaction_in_flight(self, transaction: connector.Transaction, active: bool) -> None:
        in_flight = getattr(self, "_consumer_in_flight", None)
        if in_flight is None:
            in_flight = set()
            self._consumer_in_flight = in_flight
        if active:
            in_flight.add(id(transaction))
        else:
            in_flight.discard(id(transaction))
        self._mark_consumer_progress("processing" if in_flight else "polling")

    def liveness_snapshot(self) -> liveness.TaskLivenessSnapshot:
        """Evaluate consumer-loop progress without treating an idle queue as dead."""
        now = time.monotonic()
        state = getattr(self, "_consumer_liveness_state", "starting")
        last_progress = getattr(self, "_consumer_last_progress_monotonic", now)
        in_flight = len(getattr(self, "_consumer_in_flight", set()))
        active_stale_after = getattr(self, "_consumer_active_stale_after", None)
        fetch_stale_after = getattr(self, "_consumer_fetch_stale_after", 120.0)
        details: dict[str, Any] = {
            "in_flight_count": in_flight,
            "seconds_since_progress": now - last_progress,
            "connectors": {},
        }

        if in_flight and active_stale_after is not None and now - last_progress > active_stale_after:
            return liveness.TaskLivenessSnapshot(
                healthy=False,
                state="processing_stalled",
                reason=f"No transaction progress for {now - last_progress:.1f}s",
                details=details,
            )

        if state == "fetching" and now - last_progress > fetch_stale_after:
            return liveness.TaskLivenessSnapshot(
                healthy=False,
                state="fetch_stalled",
                reason=f"Fetch has not completed for {now - last_progress:.1f}s",
                details=details,
            )

        for name, conn in self.connectors.items():
            health_fn = getattr(conn, "health", None)
            if not callable(health_fn):
                continue
            connector_health = health_fn()
            if not isinstance(connector_health, dict):
                continue
            details["connectors"][name] = connector_health
            connector_state = connector_health.get("state")
            poll_started = connector_health.get("last_poll_started_ts")
            poll_completed = connector_health.get("last_poll_completed_ts")
            poll_age = connector_health.get("seconds_since_last_poll_started")

            if connector_state == "connect_stalled":
                return liveness.TaskLivenessSnapshot(
                    healthy=False,
                    state=connector_state,
                    reason=f"Connector {name} could not restore its connection",
                    details=details,
                )
            if (
                poll_started is not None
                and (poll_completed is None or poll_started > poll_completed)
                and poll_age is not None
                and poll_age > fetch_stale_after
            ):
                return liveness.TaskLivenessSnapshot(
                    healthy=False,
                    state="poll_stalled",
                    reason=f"Connector {name} poll has not completed for {poll_age:.1f}s",
                    details=details,
                )

        return liveness.TaskLivenessSnapshot(
            healthy=True,
            state=state,
            details=details,
        )

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
    async def _handle_fetch(
        self,
        conn,
        policy: policies.FetchPolicy,
        batch_size: int | None = None,
    ) -> tuple[bool, List[connector.Transaction]]:
        fetch_size = policy.batch.size if batch_size is None else batch_size
        self._mark_consumer_progress("fetching")
        logger.info(f"Fetch handler started for batch size {fetch_size}", extra=policy.extra)
        fetch_start = time.perf_counter()
        success, result = await helpers.run_fn_async(
            fn=lambda: conn.fetch_transactions_async(fetch_size, **policy.extra),
            retry=policy.retry,
            trace_name=f"{self.__class__.__name__}.fetch_transactions",
            trace_attrs={"batch_size": fetch_size},
        )
        # Record fetch metrics
        fetched_count = len(result) if success and result else 0
        mixin_metrics.record_consumer_fetch(
            task_name=getattr(self, "name", self.__class__.__name__),
            connector_name=conn.__class__.__name__,
            batch_size=fetch_size,
            fetched_count=fetched_count,
            duration=time.perf_counter() - fetch_start,
            success=success,
        )
        logger.info(f"Fetch handler completed with status: {'success' if success else 'error'}")
        self._mark_consumer_progress("polling")
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
        self._set_transaction_in_flight(transaction, True)

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
            self._set_transaction_in_flight(transaction, False)
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
            self._consumer_in_flight = set()
            self._mark_consumer_progress("starting")
            supervision = getattr(getattr(self, "task_config", None), "supervision", None)
            transaction_timeout = policy.transaction_runtime.timeout
            processing_override = getattr(supervision, "processing_stale_after", None)
            self._consumer_active_stale_after = processing_override or (
                max(60.0, transaction_timeout + 60.0) if transaction_timeout is not None else None
            )
            client = getattr(getattr(conn, "service", None), "client", None)
            consumer_config = getattr(conn, "_consumer_config", None)
            connect_timeout = getattr(client, "connect_timeout", 30.0)
            channel_timeout = getattr(client, "channel_timeout", 15.0)
            consume_timeout = getattr(consumer_config, "consume_timeout", 5.0)
            derived_fetch_stale_after = max(
                60.0,
                connect_timeout + channel_timeout + consume_timeout + 10.0,
            )
            self._consumer_fetch_stale_after = (
                getattr(supervision, "fetch_stale_after", None) or derived_fetch_stale_after
            )
            _warn_if_prefetch_exceeds_concurrency(conn, policy, getattr(self, "name", self.__class__.__name__))

            ctx = otel_context.Context()

            async def submit_start_processing(tr, pol):
                try:
                    if pol.transaction_runtime.timeout is None:
                        return await helpers.run_fn_async(
                            fn=lambda: self._start_processing(tr, pol),
                            trace_name=f"{self.__class__.__name__}.start_processing",
                            trace_attrs={"transaction_id": tr.id},
                        )
                    async with timeout_after(pol.transaction_runtime.timeout):
                        return await helpers.run_fn_async(
                            fn=lambda: self._start_processing(tr, pol),
                            trace_name=f"{self.__class__.__name__}.start_processing",
                            trace_attrs={"transaction_id": tr.id},
                        )
                except asyncio.TimeoutError as exc:
                    timeout_error = exceptions.TransactionTimeoutException(
                        transaction_id=tr.id,
                        cause=exc,
                    )
                    logger.error(
                        "Transaction %s exceeded runtime timeout %.2fs; invoking exception handler",
                        tr.id,
                        pol.transaction_runtime.timeout,
                    )
                    return await self._handle_exception(tr, timeout_error, pol.exception.retry)

            async def _consume_continuous():
                nonlocal batch_number, empty_count, processed

                concurrency = policy.loop.concurrency.value
                submitted = 0
                in_flight: set[asyncio.Task] = set()

                async def reap(done: set[asyncio.Task]) -> None:
                    nonlocal processed
                    for task in done:
                        try:
                            await task
                        except Exception as exc:
                            logger.error("[async] Execution error: %s", exc)
                        finally:
                            processed += 1

                async def wait_for_completion() -> None:
                    done, _ = await asyncio.wait(
                        in_flight,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    in_flight.difference_update(done)
                    await reap(done)
                    self._mark_consumer_progress("polling")

                try:
                    while True:
                        if policy.loop.limit is not None and submitted >= policy.loop.limit:
                            if in_flight:
                                await wait_for_completion()
                                continue
                            break

                        free_slots = concurrency - len(in_flight)
                        if free_slots == 0:
                            await wait_for_completion()
                            continue

                        if policy.loop.limit is not None:
                            free_slots = min(free_slots, policy.loop.limit - submitted)

                        logger.debug(
                            "Fetching transactions for %d free continuous-consumer slot(s) with fetch policy: %s",
                            free_slots,
                            policy.fetch.model_dump_json(indent=2),
                        )
                        success, result = await self._handle_fetch(
                            conn,
                            policy.fetch,
                            batch_size=free_slots,
                        )

                        if not success:
                            logger.error("Fetch failed → %s", result)
                            # Unlike shutdown/cancellation, a fetch failure must not
                            # cancel work already in progress: let in-flight
                            # transactions finish (bounded by the transaction
                            # runtime timeout) before propagating the failure.
                            if in_flight:
                                logger.warning(
                                    "Fetch failed with %d transaction(s) in flight; draining before raising",
                                    len(in_flight),
                                )
                                await asyncio.gather(*in_flight, return_exceptions=True)
                                in_flight.clear()
                            if isinstance(result, (asyncio.TimeoutError, futures.TimeoutError)):
                                raise exceptions.FetchTimeoutException(str(result))
                            raise exceptions.FetchException(str(result))

                        transactions = result
                        if transactions:
                            empty_count = 0
                            batch_number += 1
                            self._mark_consumer_progress("processing")
                            logger.info(
                                "%d transaction(s) fetched for continuous processing",
                                len(transactions),
                            )
                            mixin_metrics.record_consumer_batch(
                                task_name=getattr(self, "name", self.__class__.__name__),
                                batch_number=batch_number,
                                batch_size=len(transactions),
                                streaming=policy.loop.streaming,
                            )
                            for transaction in transactions:
                                in_flight.add(
                                    asyncio.create_task(
                                        submit_start_processing(transaction, policy),
                                    )
                                )
                                submitted += 1
                            continue

                        if in_flight:
                            await wait_for_completion()
                            continue

                        self._mark_consumer_progress("idle")
                        logger.info("Empty fetch detected at %s", datetime.now().isoformat())
                        if not policy.loop.streaming:
                            logger.info("Non-streaming mode detected, breaking loop")
                            break

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
                except BaseException:
                    for task in in_flight:
                        task.cancel()
                    if in_flight:
                        await asyncio.gather(*in_flight, return_exceptions=True)
                    raise

                elapsed_time = time.perf_counter() - start_time
                logger.info(
                    "[Consume continuous] Finished. Processed=%d in %.2f seconds",
                    processed,
                    elapsed_time,
                )
                return processed

            if policy.loop.mode == "continuous":
                return await _consume_continuous()

            while True:
                batch_number += 1

                # ============================================================
                #  FETCH
                # ============================================================
                logger.debug(f"Fetching transactions batch with fetch policy: {policy.fetch.model_dump_json(indent=2)}")
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
                    self._mark_consumer_progress("idle")
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
                self._mark_consumer_progress("processing")

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

                logger.debug(
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
                    )

                processed += count
                self._mark_consumer_progress("polling")

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
