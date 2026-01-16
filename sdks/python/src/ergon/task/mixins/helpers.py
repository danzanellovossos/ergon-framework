# helper.py
import asyncio
import logging
from concurrent import futures
from typing import Callable, Iterable, Optional

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
                    logger.debug(f"Attempt {attempt_no} to run function {fn.__qualname__} completed with outcome: 'ok'")
                    return True, result

            except exceptions.NonRetryableException as e:
                return False, e
            except futures.TimeoutError as e:
                logger.error(f"Attempt {attempt_no} to run function {fn.__qualname__} failed with timeout: {e}")
                last_exc = e
            except Exception as e:
                logger.error(f"Attempt {attempt_no} to run function {fn.__qualname__} failed with exception: {e}")
                last_exc = e

            if attempt_no < retry.max_attempts:
                logger.warning(
                    f"Attempt {attempt_no} to run function {fn.__qualname__} failed with exception: {last_exc}."
                    "Calling backoff."
                )
                utils.backoff(retry.backoff, retry.backoff_multiplier, retry.backoff_cap, attempt_no - 1)

        logger.warning(
            f"Attempt {retry.max_attempts} to run function {fn.__qualname__} failed with exception: {last_exc}"
        )
        return False, last_exc

    if executor:
        return executor.submit(run)

    return run()


def multithread_execute(
    submissions: Iterable[Callable[[], futures.Future]],
    *,
    concurrency: int,
    limit: Optional[int] = None,
    timeout: Optional[float] = None,
) -> int:
    """
    Execute submitted callables with bounded concurrency and refill.

    - submissions yields callables that RETURN a Future when called
    - this function manages in-flight futures only
    - no domain semantics, no retries, no tracing

    Returns:
        Number of completed submissions
    """

    in_flight: set[futures.Future] = set()
    completed = 0

    submit_iter = iter(submissions)

    # ============================================================
    # INITIAL FILL
    # ============================================================
    while len(in_flight) < concurrency:
        if limit is not None and completed + len(in_flight) >= limit:
            break
        try:
            submit = next(submit_iter)
        except StopIteration:
            break
        try:
            in_flight.add(submit())
        except Exception as e:
            logger.error(f"Submission failed before execution: {e}")

    # ============================================================
    # MAIN LOOP
    # ============================================================
    while in_flight:
        done, in_flight = futures.wait(
            in_flight,
            return_when=futures.FIRST_COMPLETED,
            timeout=timeout,
        )

        for fut in done:
            try:
                fut.result()
            except futures.TimeoutError:
                logger.error("Execution timeout")
            except Exception as e:
                logger.error(f"Execution error: {e}")
            finally:
                completed += 1

        # ============================================================
        # REFILL
        # ============================================================
        while len(in_flight) < concurrency:
            if limit is not None and completed + len(in_flight) >= limit:
                break
            try:
                submit = next(submit_iter)
            except StopIteration:
                break
            try:
                in_flight.add(submit())
            except Exception as e:
                logger.error(f"Submission failed before execution: {e}")

    return completed


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
    """
    Async OTEL context attach/detach wrapper.
    Mirrors with_context (sync).
    """

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
    """
    Smallest async execution primitive.
    Mirrors run_with_context (sync).
    """

    if ctx is None:
        ctx = context.get_current()

    return await with_context_async(
        *args,
        fn=fn,
        ctx=ctx,
        trace_name=trace_name,
        trace_attrs=trace_attrs,
        **kwargs,
    )


# ============================================================
#  RUN FN (ASYNC EXECUTION FACTORY) — FUNCTION + DECORATOR
# ============================================================
def run_fn_async(
    *args,
    fn: Optional[Callable] = None,
    retry: Optional[policies.RetryPolicy] = None,
    ctx: Optional[context.Context] = None,
    trace_name: Optional[str] = None,
    trace_attrs: dict = {},
    **kwargs,
):
    """
    Async equivalent of run_fn.

    Returns:
        (success: bool, result: Any | Exception)

    Same invariants as sync:
      - retries decided by policy
      - no concurrency here
      - no scheduling
    """

    # ============================================================
    # DECORATOR MODE
    # ============================================================
    if fn is None:

        def decorator(func: Callable):
            async def wrapper(*wrapper_args, **wrapper_kwargs):
                success, result = await run_fn_async(
                    *wrapper_args,
                    fn=func,
                    retry=retry,
                    ctx=ctx,
                    trace_name=trace_name or func.__qualname__,
                    trace_attrs=trace_attrs,
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

    async def attempt(attempt_no: int):
        return await run_with_context_async(
            *args,
            fn=fn,
            ctx=ctx,
            trace_name=f"{trace_name}.attempt.{attempt_no}",
            trace_attrs={**trace_attrs, "attempt": attempt_no},
            **kwargs,
        )

    async def run():
        last_exc = None

        for attempt_no in range(1, retry.max_attempts + 1):
            logger.debug(f"[async] Attempt {attempt_no} → {fn.__qualname__}")
            try:
                if retry.timeout:
                    result = await asyncio.wait_for(
                        attempt(attempt_no),
                        timeout=retry.timeout,
                    )
                else:
                    result = await attempt(attempt_no)

                return True, result

            except exceptions.NonRetryableException as e:
                return False, e

            except asyncio.TimeoutError as e:
                last_exc = e
                logger.error(f"[async] Timeout on attempt {attempt_no}")

            except Exception as e:
                last_exc = e
                logger.error(f"[async] Error on attempt {attempt_no}: {e}")

            if attempt_no < retry.max_attempts:
                await utils.sleep_async(
                    retry.backoff,
                    retry.backoff_multiplier,
                    retry.backoff_cap,
                    attempt_no - 1,
                )

        return False, last_exc

    return run()


# ============================================================
#  ASYNC CONCURRENT EXECUTION WITH REFILL
# ============================================================
async def async_execute(
    submissions: Iterable[Callable[[], asyncio.Task]],
    *,
    concurrency: int,
    limit: Optional[int] = None,
    timeout: Optional[float] = None,
) -> int:
    """
    Async equivalent of multithread_execute.

    - submissions yield callables that RETURN an asyncio.Task
    - manages in-flight tasks only
    - no retries
    - no tracing
    - no domain semantics

    Returns:
        Number of completed tasks
    """

    in_flight: set[asyncio.Task] = set()
    completed = 0
    submit_iter = iter(submissions)

    # ============================================================
    # INITIAL FILL
    # ============================================================
    while len(in_flight) < concurrency:
        if limit is not None and completed + len(in_flight) >= limit:
            break
        try:
            submit = next(submit_iter)
            in_flight.add(submit())
        except StopIteration:
            break
        except Exception as e:
            logger.error(f"[async] Submission failed: {e}")

    # ============================================================
    # MAIN LOOP
    # ============================================================
    while in_flight:
        done, in_flight = await asyncio.wait(
            in_flight,
            return_when=asyncio.FIRST_COMPLETED,
            timeout=timeout,
        )

        for task in done:
            try:
                await task
            except asyncio.TimeoutError:
                logger.error("[async] Execution timeout")
            except Exception as e:
                logger.error(f"[async] Execution error: {e}")
            finally:
                completed += 1

        # ============================================================
        # REFILL
        # ============================================================
        while len(in_flight) < concurrency:
            if limit is not None and completed + len(in_flight) >= limit:
                break
            try:
                submit = next(submit_iter)
                in_flight.add(submit())
            except StopIteration:
                break
            except Exception as e:
                logger.error(f"[async] Submission failed: {e}")

    return completed
