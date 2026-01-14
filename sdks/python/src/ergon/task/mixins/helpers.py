# helper.py
import asyncio
import logging
from concurrent import futures
from typing import Any, Callable, Iterator, Optional, Tuple

from opentelemetry import context

from ... import telemetry
from .. import exceptions, policies
from . import utils

logger = logging.getLogger(__name__)
tracer = telemetry.tracing.get_tracer(__name__)


# ============================================================
#  SYNC CONTEXT WRAPPER
# ============================================================
def with_context(
    *args,
    fn: Callable,
    ctx: context.Context = None,
    trace_name: str = None,
    trace_attrs: dict = {},
    **kwargs,
):
    """Sync OTEL context attach/detach wrapper."""

    if ctx is None:
        ctx = context.get_current()

    token = context.attach(ctx)
    try:
        with tracer.start_as_current_span(trace_name, attributes=trace_attrs):
            return fn(*args, **kwargs)
    finally:
        context.detach(token)


def get_current_context():
    return context.get_current()


# ============================================================
#  RUN WITH CONTEXT (SYNC) WRAPPER
# ============================================================
def run_with_context(
    *args,
    fn: Callable,
    ctx: context.Context = None,
    trace_name: str = None,
    trace_attrs: dict = {},
    **kwargs,
):
    if ctx is None:
        ctx = context.get_current()

    return with_context(*args, fn=fn, ctx=ctx, trace_name=trace_name, trace_attrs=trace_attrs, **kwargs)


# ============================================================
#  RUN FN (SYNC EXECUTION FACTORY) - Works as function AND decorator
# ============================================================
def run_fn(
    *args,
    fn: Optional[Callable] = None,
    retry: Optional[policies.RetryPolicy] = None,
    ctx: Optional[context.Context] = None,
    trace_name: Optional[str] = None,
    trace_attrs: dict = {},
    executor: Optional[futures.Executor] = None,
    **kwargs,
):
    """
    Execute a callable inside Ergon's execution envelope.

    This function is a *foundational execution primitive* and provides a
    consistent, observable, policy-driven way to run synchronous code with:

      - OpenTelemetry context propagation
      - Structured tracing
      - Retry semantics
      - Optional per-attempt timeouts

    It can be used either as a normal function or as a decorator.

    --------------------------------------------------------------------
    EXECUTION MODES
    --------------------------------------------------------------------

    1) DIRECT EXECUTION (executor is None)

        The callable is executed *immediately* in the current thread.
        The function returns a tuple:

            (success: bool, result: Any | Exception)

        This mode is used when:
          - You are already inside an execution engine (e.g. Consumer loop)
          - You want deterministic, inline execution
          - You need explicit control over error handling
          - You are composing higher-level execution semantics

        This is the most common mode inside the framework itself.

    2) SUBMITTED EXECUTION (executor is provided)

        The callable is submitted to the provided Executor and executed
        asynchronously. In this case, `run_fn` returns a `Future` whose
        result will be:

            (success: bool, result: Any | Exception)

        This mode is used when:
          - You are *orchestrating concurrency*, not business logic
          - You want to parallelize independent executions
          - You are building a runner, scheduler, or dispatcher
          - You explicitly want execution to escape the current thread

        The executor defines *where* execution happens, but **not how**.
        All retries, timeouts, tracing, and context propagation still occur
        inside the execution envelope.

    --------------------------------------------------------------------
    DECORATOR MODE
    --------------------------------------------------------------------

    When used as a decorator:

        @run_fn(retry=...)
        def my_function(...):

    The decorator is syntactic sugar over direct execution:

      - The wrapped function is executed inline
      - Failures raise exceptions instead of returning (success, result)
      - `executor` is intentionally ignored

    Decorator mode is intended for:
      - Service methods
      - Task internals
      - Domain-level logic

    It must NOT be used to introduce concurrency.

    --------------------------------------------------------------------
    IMPORTANT INVARIANTS
    --------------------------------------------------------------------

    - `run_fn` never decides *whether* retries or timeouts apply
      — policies define behavior, not control flow.

    - Passing an executor controls *where* execution happens,
      never *how* it behaves.

    - The execution envelope (context, tracing, retries, timeouts)
      is identical in all modes.

    In short:
      - Use `executor` at orchestration boundaries
      - Do NOT use `executor` inside domain or business logic
    """

    # ============================================================
    # DECORATOR MODE
    # ============================================================
    if fn is None:

        def decorator(func: Callable):
            def wrapper(*wrapper_args, **wrapper_kwargs):
                success, result = run_fn(
                    *wrapper_args,
                    fn=func,
                    retry=retry,
                    ctx=ctx,
                    trace_name=trace_name or func.__qualname__,
                    trace_attrs=trace_attrs,
                    executor=None,  # decorators always execute inline
                    **wrapper_kwargs,
                )

                if not success:
                    if isinstance(result, BaseException):
                        raise result
                    raise RuntimeError(f"Function {func.__qualname__} failed: {result}")

                return result

            return wrapper

        return decorator

    # ============================================================
    # EXECUTION MODE
    # ============================================================
    ctx = ctx or context.get_current()
    trace_name = trace_name or fn.__qualname__
    retry = retry or policies.RetryPolicy(max_attempts=1)

    def attempt(attempt_no: int):
        return run_with_context(
            *args,
            fn=fn,
            ctx=ctx,
            trace_name=f"{trace_name}.attempt.{attempt_no}",
            trace_attrs={**trace_attrs, "attempt": attempt_no},
            **kwargs,
        )

    def run():
        last_exc = None

        with tracer.start_as_current_span(
            trace_name,
            context=ctx,
            attributes={
                **trace_attrs,
                **(retry.model_dump(exclude_none=True) if retry else {}),
            },
        ):
            for attempt_no in range(1, retry.max_attempts + 1):
                logger.debug(f"Attempt {attempt_no} to run function {fn.__qualname__} started")
                try:
                    if retry.timeout:
                        with futures.ThreadPoolExecutor(max_workers=1) as ex:
                            future = ex.submit(attempt, attempt_no)
                            result = future.result(timeout=retry.timeout)
                            logger.debug(
                                f"Attempt {attempt_no} to run function {fn.__qualname__} completed with outcome: 'ok'"
                            )
                            return True, result
                    else:
                        result = attempt(attempt_no)
                        logger.debug(
                            f"Attempt {attempt_no} to run function {fn.__qualname__} completed with outcome: 'ok'"
                        )
                        return True, result

                except exceptions.NonRetryableException as e:
                    return False, e

                except Exception as e:
                    logger.error(f"Attempt {attempt_no} to run function {fn.__qualname__} failed with exception: {e}")
                    last_exc = e

                if attempt_no < retry.max_attempts:
                    logger.warning(
                        f"Attempt {attempt_no} to run function {fn.__qualname__} failed with exception: {last_exc}."
                        "Calling backoff."
                    )
                    with tracer.start_as_current_span(
                        "backoff",
                        attributes={
                            "backoff": retry.backoff,
                            "multiplier": retry.backoff_multiplier,
                            "cap": retry.backoff_cap,
                            "attempt": attempt_no - 1,
                        },
                    ):
                        utils.backoff(
                            retry.backoff,
                            retry.backoff_multiplier,
                            retry.backoff_cap,
                            attempt_no - 1,
                        )
            logger.warning(
                f"Attempt {retry.max_attempts} to run function {fn.__qualname__} failed with exception: {last_exc}"
            )
            return False, last_exc

    if executor:
        return executor.submit(run)

    return run()


def run_concurrently(
    data: Any,
    callback: Callable,
    submit_fn: Callable,
    concurrency: int,
    limit: int = None,
    count: int = 0,
    timeout: float = None,
) -> int:
    active = set[futures.Future]()
    results = []
    submit_count = count
    it = iter(callback(x) for x in data)
    # ============================================================
    #  INITIAL FILL
    # ============================================================
    for _ in range(min(concurrency, len(data))):
        try:
            if limit and submit_count >= limit:
                break
            args = next(it)
            active.add(submit_fn(*args))
            submit_count += 1
        except StopIteration:
            break

    # ============================================================
    #  PROCESS LOOP
    # ============================================================
    while active:
        # ============================================================
        #  WAIT FOR FIRST COMPLETED
        # ============================================================
        done, remaining = futures.wait(active, return_when=futures.FIRST_COMPLETED)
        count = 0

        for fut in done:
            try:
                result = fut.result(timeout=timeout)
                results.append(result)
            except futures.TimeoutError:
                logger.error("[Producer] Transaction lifecycle TIMEOUT")
            except Exception as e:
                logger.error(f"[Producer] Error: {e}")
            count += 1

        active = remaining
        # ============================================================
        #  REFILL
        # ============================================================
        if limit and submit_count >= limit:
            break

        while len(active) < concurrency:
            if limit and submit_count >= limit:
                break

            try:
                args = next(it)
                active.add(submit_fn(*args))
            except StopIteration:
                break

    return results, count


# ============================================================
#  ASYNC CONTEXT WRAPPER
# ============================================================
async def with_context_async(
    *args,
    fn: Callable,
    ctx: context.Context = None,
    trace_name: str = None,
    trace_attrs: dict = {},
    **kwargs,
):
    """Async OTEL context attach/detach wrapper."""

    if ctx is None:
        ctx = context.get_current()

    token = context.attach(ctx)
    try:
        with tracer.start_as_current_span(trace_name, attributes=trace_attrs):
            return await fn(*args, **kwargs)
    finally:
        context.detach(token)


# ============================================================
#  RUN WITH CONTEXT (ASYNC) WRAPPER
# ============================================================
async def run_with_context_async(
    *args,
    fn: Callable,
    ctx: context.Context = None,
    trace_name: str = None,
    trace_attrs: dict = {},
    **kwargs,
):
    if ctx is None:
        ctx = context.get_current()

    return await with_context_async(*args, fn=fn, ctx=ctx, trace_name=trace_name, trace_attrs=trace_attrs, **kwargs)


# ============================================================
#  RUN WITH TIMEOUT (ASYNC) + CONTEXT WRAPPER
# ============================================================
async def run_with_timeout_async(
    *args,
    fn: Callable,
    timeout: int = 0,
    ctx: context.Context = None,
    trace_name: str = None,
    trace_attrs: dict = {},
    **kwargs,
):
    if ctx is None:
        ctx = context.get_current()

    async def _wrapper():
        return await run_with_context_async(
            *args, fn=fn, ctx=ctx, trace_name=trace_name, trace_attrs=trace_attrs, **kwargs
        )

    if timeout == 0:
        return await _wrapper()

    return await asyncio.wait_for(_wrapper(), timeout=timeout)


# ============================================================
#  RUN WITH RETRY + TIMEOUT (ASYNC) + CONTEXT WRAPPER
# ============================================================
async def run_with_retry_and_timeout_async(
    *args,
    fn: Callable,
    retry: policies.RetryPolicy,
    ctx: context.Context = None,
    trace_name: str = None,
    trace_attrs: dict = {},
    **kwargs,
) -> Tuple[bool, Any]:
    if ctx is None:
        ctx = context.get_current()

    with tracer.start_as_current_span(
        trace_name,
        attributes={**trace_attrs, "max_attempts": retry.max_attempts},
    ):
        last_exc = None

        for attempt in range(retry.max_attempts):
            try:
                result = await run_with_timeout_async(
                    *args,
                    fn=fn,
                    timeout=retry.timeout,
                    ctx=ctx,
                    trace_name=f"{trace_name}.attempt.{attempt + 1}",
                    trace_attrs={"attempt": attempt + 1, "max_attempts": retry.max_attempts},
                    **kwargs,
                )
                return True, result

            except exceptions.TransactionException as e:
                if e.category == exceptions.ExceptionType.BUSINESS:
                    return False, e
                last_exc = e

            except BaseException as e:
                last_exc = e

            if attempt == retry.max_attempts - 1:
                return False, last_exc

            await run_with_context_async(
                fn=lambda: utils.sleep_async(retry.backoff, retry.backoff_multiplier, retry.backoff_cap, attempt),
                ctx=ctx,
                trace_name=f"backoff.attempt.{attempt + 1}",
                trace_attrs={"attempt": attempt + 1, "max_attempts": retry.max_attempts},
            )


# ============================================================
#  RUN FN (ASYNC EXECUTION FACTORY) - Works as function AND decorator
# ============================================================
def run_fn_async(
    *args,
    fn: Optional[Callable] = None,
    retry: Optional[policies.RetryPolicy] = None,
    ctx: context.Context = None,
    trace_name: Optional[str] = None,
    trace_attrs: dict = {},
    **kwargs,
):
    """
    Executes an async function with context + span + timeout + retries.

    Can be used as:
    1. Normal function: run_fn_async(fn=my_func, retry=..., *args, **kwargs)
    2. Decorator: @run_fn_async(retry=...) or @run_fn_async
    """

    # DECORATOR MODE: called without fn, returns decorator
    if fn is None:

        def decorator(func: Callable) -> Callable:
            async def wrapper(*wrapper_args, **wrapper_kwargs) -> Any:
                success, result = await run_fn_async(
                    fn=func,
                    retry=retry,
                    ctx=ctx,
                    trace_name=trace_name or f"{func.__module__}.{func.__name__}",
                    trace_attrs=trace_attrs,
                    *wrapper_args,
                    **wrapper_kwargs,
                )
                if not success:
                    if isinstance(result, BaseException):
                        raise result
                    raise RuntimeError(f"Function {func.__name__} failed: {result}")
                return result

            return wrapper

        return decorator

    # FUNCTION MODE: fn provided, execute it
    if ctx is None:
        ctx = context.get_current()

    async def _run(*run_args, **run_kwargs) -> Tuple[bool, Any]:
        # NO RETRY / NO TIMEOUT
        if retry is None or (retry.max_attempts == 1 and retry.timeout is None):
            try:
                res = await run_with_context_async(
                    *run_args, fn=fn, ctx=ctx, trace_name=trace_name, trace_attrs=trace_attrs, **run_kwargs
                )
                return True, res
            except Exception as e:
                return False, e

        # SINGLE ATTEMPT + TIMEOUT
        if retry.max_attempts == 1:
            try:
                res = await run_with_timeout_async(
                    *run_args,
                    fn=fn,
                    ctx=ctx,
                    timeout=retry.timeout,
                    trace_name=trace_name,
                    trace_attrs=trace_attrs,
                    **run_kwargs,
                )
                return True, res
            except Exception as e:
                return False, e

        # FULL RETRY
        return await run_with_retry_and_timeout_async(
            *run_args, fn=fn, retry=retry, ctx=ctx, trace_name=trace_name, trace_attrs=trace_attrs, **run_kwargs
        )

    return _run(*args, **kwargs)


# ============================================================
#  RUN CONCURRENTLY WITH REFILL (ASYNC)
# ============================================================
async def run_concurrently_with_refill_async(
    data: Any, it: Iterator, submit_fn: Callable, concurrency: int, limit: int, count: int, timeout: int
) -> int:
    active = set[asyncio.Task]()
    submit_count = count

    # ============================================================
    #  INITIAL FILL
    # ============================================================
    for _ in range(min(concurrency, len(data))):
        try:
            if limit and submit_count >= limit:
                break
            args = next(it)
            task = asyncio.create_task(submit_fn(*args))
            active.add(task)
            submit_count += 1
        except StopIteration:
            break

    # ============================================================
    #  PROCESS LOOP
    # ============================================================
    while active:
        # ============================================================
        #  WAIT FOR FIRST COMPLETED
        # ============================================================
        done, remaining = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)

        for task in done:
            try:
                await asyncio.wait_for(task, timeout=timeout)
            except asyncio.TimeoutError:
                logger.error("[AsyncProducer] Transaction lifecycle TIMEOUT")
            except Exception as e:
                logger.error(f"[AsyncProducer] Error → {e}")
            count += 1

        active = remaining

        # ============================================================
        #  REFILL
        # ============================================================
        if limit and submit_count >= limit:
            continue

        while len(active) < concurrency:
            if limit and submit_count >= limit:
                break

            try:
                args = next(it)
                task = asyncio.create_task(submit_fn(*args))
                active.add(task)
                submit_count += 1
            except StopIteration:
                break

    return count
