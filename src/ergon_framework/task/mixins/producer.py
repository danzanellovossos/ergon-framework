import asyncio
import logging
import time
from abc import ABC, abstractmethod
from concurrent import futures
from typing import Any, List

from more_itertools import chunked

from ... import connector, telemetry
from .. import base, exceptions, policies
from . import helpers, utils

logger = logging.getLogger(__name__)
tracer = telemetry.tracing.get_tracer(__name__)

# -------------------------------------------------------------------
# PRODUCER MIXIN (SYNC)
# -------------------------------------------------------------------


class ProducerMixin(ABC):
    # -------------------------------------------------------------------
    # HOOKS
    # -------------------------------------------------------------------
    @abstractmethod
    def prepare_transaction(self, transaction: connector.Transaction) -> Any:
        raise NotImplementedError

    def handle_prepare_success(self, transaction: connector.Transaction, result: Any):
        logger.debug(f"[{self.name}] SUCCESS → {transaction.id}")

    def handle_prepare_exception(self, transaction: connector.Transaction, exc: exceptions.TransactionException):
        logger.error(f"[{self.name}] EXCEPTION → {transaction.id}: {exc}")

    # -------------------------------------------------------------------
    # PRODUCE 1 ITEM (FULL RETRY LIFECYCLE)
    # -------------------------------------------------------------------
    def _start_producing(self, transaction: connector.Transaction, policy: policies.ProducerPolicy):
        """
        PRODUCE → SUCCESS | EXCEPTION
        """

        # -----------------------
        # 1) PREPARE
        # -----------------------
        prepare_success, prepare_result = self._handle_prepare(transaction, policy.prepare)

        # -----------------------
        # 2) EXCEPTION HANDLER
        # -----------------------
        if not prepare_success:
            if isinstance(prepare_result, exceptions.TransactionException):
                prepare_result = prepare_result
            elif isinstance(prepare_result, futures.TimeoutError):
                prepare_result = exceptions.TransactionException(str(prepare_result), exceptions.ExceptionType.TIMEOUT)
            else:
                prepare_result = exceptions.TransactionException(str(prepare_result), exceptions.ExceptionType.SYSTEM)
            return self._handle_prepare_exception(transaction, prepare_result, policy.exception)

        # -----------------------
        # 3) SUCCESS HANDLER
        # -----------------------
        success_success, success_result = self._handle_success(transaction, prepare_result, policy.success)
        if not success_success:
            if isinstance(success_result, exceptions.TransactionException):
                success_result = success_result
            elif isinstance(success_result, futures.TimeoutError):
                success_result = exceptions.TransactionException(str(success_result), exceptions.ExceptionType.TIMEOUT)
            else:
                success_result = exceptions.TransactionException(str(success_result), exceptions.ExceptionType.SYSTEM)
            return self._handle_exception(transaction, success_result, policy.exception)

        return True, success_result

    # -------------------------------------------------------------------
    # PREPARE HANDLER
    # -------------------------------------------------------------------
    def _handle_prepare(self, transaction: connector.Transaction, policy: policies.PreparePolicy):
        return helpers.run_fn(
            fn=lambda: self.prepare_transaction(transaction),
            retry=policy.retry,
            trace_name=f"{self.__class__.__name__}.prepare",
            trace_attrs={"transaction": transaction.id},
        )

    # -------------------------------------------------------------------
    # SUCCESS HANDLER
    # -------------------------------------------------------------------
    def _handle_prepare_success(
        self,
        transaction: connector.Transaction,
        result: Any,
        policy: policies.SuccessPolicy,
        exception_policy: policies.ExceptionPolicy,
    ):
        return helpers.run_fn(
            fn=lambda: self.handle_prepare_success(transaction, result),
            retry=policy.retry,
            trace_name=f"{self.__class__.__name__}.handle_prepare_success",
            trace_attrs={"transaction": transaction.id},
        )

    # -------------------------------------------------------------------
    # EXCEPTION HANDLER
    # -------------------------------------------------------------------
    def _handle_prepare_exception(
        self,
        transaction: connector.Transaction,
        exc: exceptions.TransactionException,
        policy: policies.ExceptionPolicy,
    ):
        return helpers.run_fn(
            fn=lambda: self.handle_prepare_exception(transaction, exc),
            retry=policy.retry,
            trace_name=f"{self.__class__.__name__}.handle_prepare_exception",
            trace_attrs={"transaction": transaction.id},
        )

    # -------------------------------------------------------------------
    # PUBLIC API — PRODUCE MANY
    # -------------------------------------------------------------------
    def produce_transactions(
        self,
        transactions: List[connector.Transaction],
        policy: policies.ProducerPolicy = None,
    ):
        if policy is None:
            policy = policies.ProducerPolicy()

        def _produce():
            start_time = time.perf_counter()
            processed = 0
            executor = futures.ThreadPoolExecutor(max_workers=policy.loop.concurrency.value)

            # ============================================================
            #  SUBMIT FUNCTION
            # ============================================================
            def submit_start_producing(transaction: connector.Transaction, policy: policies.ProducerPolicy):
                return helpers.run_fn(
                    fn=lambda: self._start_producing(transaction=transaction, policy=policy),
                    executor=executor,
                    trace_name=f"{self.__class__.__name__}.start_producing",
                    trace_attrs={"transaction_id": transaction.id},
                )

            # ============================================================
            #  PRODUCE LOOP
            # ============================================================
            batches = list(chunked(transactions, policy.loop.batch.size))
            for batch in batches:
                count = helpers.run_concurrently_with_refill(
                    data=batch,
                    it=iter((tr, policy) for tr in batch),
                    submit_fn=submit_start_producing,
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
            logger.info(f"[Produce] Finished. Processed={processed} in {elapsed_time:.2f} seconds")
            return count

        try:
            return helpers.run_fn(
                fn=_produce,
                timeout=policy.loop.timeout,
                trace_name=f"{self.__class__.__name__}.produce_transactions",
                trace_attrs={"count": len(transactions)},
            )
        except futures.TimeoutError as e:
            logger.error(f"[Produce] Timeout: {e}")
            raise exceptions.ProducerLoopTimeoutException(str(e))
        except BaseException as e:  # pylint: disable=broad-exception-caught
            logger.error(f"[Produce] Error: {e}")
            raise exceptions.TransactionException(str(e), exceptions.ExceptionType.SYSTEM)


class ProducerTask(ProducerMixin, base.BaseTask):
    """
    Backwards-compatible producer task.
    You can still inherit from this if you're only a producer.
    """

    pass


# -------------------------------------------------------------------
# ASYNC PRODUCER (MIRROR OF ASYNC CONSUMER)
# -------------------------------------------------------------------


class AsyncProducerMixin(ABC):
    # -------------------------------------------------------------------
    # HOOKS
    # -------------------------------------------------------------------

    @abstractmethod
    async def prepare_transaction(self, transaction: connector.Transaction) -> Any:
        raise NotImplementedError

    async def handle_prepare_success(self, transaction: connector.Transaction, result: Any):
        logger.debug(f"[{self.name}] SUCCESS → {transaction.id}")

    async def handle_prepare_exception(self, transaction: connector.Transaction, exc: exceptions.TransactionException):
        logger.error(f"[{self.name}] EXCEPTION → {transaction.id}: {exc}")

    # -------------------------------------------------------------------
    # PRODUCE 1 ITEM (FULL RETRY LIFECYCLE)
    # -------------------------------------------------------------------

    async def _start_producing(self, transaction: connector.Transaction, policy: policies.ProducerPolicy):
        """
        PRODUCE → SUCCESS | EXCEPTION
        Mirrors sync version exactly.
        """

        with tracer.start_as_current_span(
            f"{self.__class__.__name__}.start_producing",
            attributes={"transaction": transaction.id},
        ):
            # ---- PRODUCE ----
            with tracer.start_as_current_span(
                f"{self.__class__.__name__}.prepare",
                attributes={
                    "transaction": transaction.id,
                    **policy.prepare.retry.model_dump(),
                },
            ):
                result = await self._handle_prepare(transaction, policy.prepare)

            # ---- SUCCESS OR EXCEPTION ----
            if isinstance(result, exceptions.TransactionException):
                with tracer.start_as_current_span(
                    f"{self.__class__.__name__}.handle_prepare_exception",
                    attributes={
                        "transaction": transaction.id,
                        "exception": result.message,
                        **policy.exception.retry.model_dump(),
                    },
                ):
                    return await self._handle_prepare_exception(transaction, result, policy.exception)

            with tracer.start_as_current_span(
                f"{self.__class__.__name__}.handle_prepare_success",
                attributes={
                    "transaction": transaction.id,
                    "result": result,
                    **policy.success.retry.model_dump(),
                },
            ):
                return await self._handle_prepare_success(transaction, result, policy.success, policy.exception)

    # -------------------------------------------------------------------
    # PREPARE HANDLER
    # -------------------------------------------------------------------

    async def _handle_prepare(self, transaction: connector.Transaction, policy: policies.PreparePolicy):
        async def fn():
            return await self.prepare_transaction(transaction)

        for attempt in range(policy.retry.max_attempts):
            with tracer.start_as_current_span(
                f"{self.__class__.__name__}.prepare.attempt",
                attributes={"attempt": attempt + 1},
            ):
                try:
                    return await utils.run_with_timeout_async(fn, policy.retry.timeout)
                except exceptions.TransactionException as te:
                    if te.category != exceptions.ExceptionType.TIMEOUT:
                        return te
                    last_exc = te
                except asyncio.TimeoutError as te:
                    last_exc = exceptions.TransactionException(str(te), exceptions.ExceptionType.TIMEOUT)
                except BaseException as e:
                    last_exc = exceptions.TransactionException(str(e), exceptions.ExceptionType.SYSTEM)

                if attempt == policy.retry.max_attempts - 1:
                    return last_exc

                await utils.backoff_async(
                    policy.retry.backoff, policy.retry.backoff_multiplier, policy.retry.backoff_cap, attempt
                )

    # -------------------------------------------------------------------
    # SUCCESS HANDLER
    # -------------------------------------------------------------------

    async def _handle_prepare_success(
        self,
        transaction: connector.Transaction,
        result: Any,
        policy: policies.SuccessPolicy,
        exception_policy: policies.ExceptionPolicy,
    ):
        async def fn():
            return await self.handle_prepare_success(transaction, result)

        for attempt in range(policy.retry.max_attempts):
            with tracer.start_as_current_span(
                f"{self.__class__.__name__}.handle_prepare_success.attempt",
                attributes={"attempt": attempt + 1},
            ):
                try:
                    return await utils.run_with_timeout_async(fn, policy.retry.timeout)
                except asyncio.TimeoutError as te:
                    last_exc = exceptions.TransactionException(str(te), exceptions.ExceptionType.TIMEOUT)
                except BaseException as e:
                    last_exc = exceptions.TransactionException(str(e), exceptions.ExceptionType.SYSTEM)

                if attempt == policy.retry.max_attempts - 1:
                    return await self._handle_prepare_exception(transaction, last_exc, exception_policy)

                await utils.backoff_async(
                    policy.retry.backoff, policy.retry.backoff_multiplier, policy.retry.backoff_cap, attempt
                )

    # -------------------------------------------------------------------
    # EXCEPTION HANDLER
    # -------------------------------------------------------------------

    async def _handle_prepare_exception(
        self,
        transaction: connector.Transaction,
        exc: exceptions.TransactionException,
        policy: policies.ExceptionPolicy,
    ):
        async def fn():
            return await self.handle_prepare_exception(transaction, exc)

        for attempt in range(policy.retry.max_attempts):
            with tracer.start_as_current_span(
                f"{self.__class__.__name__}.handle_prepare_exception.attempt",
                attributes={"attempt": attempt + 1},
            ):
                try:
                    return await utils.run_with_timeout_async(
                        fn=fn,
                        timeout=policy.retry.timeout,
                    )
                except asyncio.TimeoutError as te:
                    last_exc = exceptions.TransactionException(str(te), exceptions.ExceptionType.TIMEOUT)
                except BaseException as e:
                    last_exc = exceptions.TransactionException(str(e), exceptions.ExceptionType.SYSTEM, transaction.id)

                if attempt == policy.retry.max_attempts - 1:
                    return last_exc

                await utils.backoff_async(
                    policy.retry.backoff, policy.retry.backoff_multiplier, policy.retry.backoff_cap, attempt
                )

        return last_exc

    # -------------------------------------------------------------------
    # PUBLIC API — PRODUCE MANY (ASYNC)
    # -------------------------------------------------------------------

    async def produce_transactions(self, transactions, policy=None):
        if policy is None:
            policy = policies.ProducerPolicy()

        async def _produce_transactions():
            count = 0

            # ============================================================
            #  SUBMIT FUNCTION
            # ============================================================

            sem = asyncio.Semaphore(policy.loop.concurrency.value)

            submit_fn = helpers.make_submit_async(
                self._start_producing,
                utils.get_current_context,
                sem=sem,
                timeout=policy.loop.transaction_timeout,
            )

            # ============================================================
            #  PRODUCE LOOP
            # ============================================================

            batches = list(chunked(transactions, policy.loop.batch.size))
            for batch in batches:
                it = iter((tr, policy) for tr in batch)
                active = set()

                # ============================================================
                #  INITIAL FILL
                # ============================================================
                for _ in range(min(policy.loop.concurrency.value, len(batch))):
                    try:
                        args = next(it)
                        fut = await submit_fn(*args)
                        active.add(fut)
                    except StopIteration:
                        break

                # ============================================================
                #  REFILL + WAIT (ASYNC)
                # ============================================================
                while active:
                    done, active, processed = await helpers.wait_and_process_async(
                        active,
                        policy.loop.transaction_timeout,
                    )
                    count += processed

                    active = await helpers.refill_active_async(it, active, submit_fn)

            return count

        with tracer.start_as_current_span(
            f"{self.__class__.__name__}.prepare_transactions",
            attributes={"count": len(transactions)},
        ):
            return await utils.run_with_timeout_async(
                fn=_produce_transactions,
                timeout=policy.loop.timeout,
            )


# ----------------------------------------------------------
# Concrete Async Producer Task
# ----------------------------------------------------------


class AsyncProducerTask(AsyncProducerMixin, base.BaseAsyncTask):
    pass
