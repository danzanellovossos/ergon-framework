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
#  RUN WITH TIMEOUT (SYNC) + CONTEXT WRAPPER
# ============================================================
def run_with_timeout(
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

    def _wrapper():
        return run_with_context(*args, fn=fn, ctx=ctx, trace_name=trace_name, trace_attrs=trace_attrs, **kwargs)

    if timeout == 0:
        return _wrapper()

    ex = futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(_wrapper).result(timeout=timeout)
    finally:
        ex.shutdown(wait=False)


# ============================================================
#  RUN WITH RETRY + TIMEOUT (SYNC) + CONTEXT WRAPPER
# ============================================================
def run_with_retry_and_timeout(
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
                result = run_with_timeout(
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

            run_with_context(
                fn=lambda: utils.backoff(retry.backoff, retry.backoff_multiplier, retry.backoff_cap, attempt),
                ctx=ctx,
                trace_name=f"backoff.attempt.{attempt + 1}",
                trace_attrs={"attempt": attempt + 1, "max_attempts": retry.max_attempts},
            )


# ============================================================
#  RUN FN (SYNC EXECUTION FACTORY) - Works as function AND decorator
# ============================================================
def run_fn(
    *args,
    fn: Optional[Callable] = None,
    retry: Optional[policies.RetryPolicy] = None,
    executor: Optional[futures.Executor] = None,
    ctx: context.Context = None,
    trace_name: Optional[str] = None,
    trace_attrs: dict = {},
    **kwargs,
):
    """
    Executes a function with context + span + timeout + retries.
    
    Can be used as:
    1. Normal function: run_fn(fn=my_func, retry=..., *args, **kwargs)
    2. Decorator: @run_fn(retry=...) or @run_fn
    """
    
    # DECORATOR MODE: called without fn, returns decorator
    if fn is None:
        def decorator(func: Callable) -> Callable:
            def wrapper(*wrapper_args, **wrapper_kwargs) -> Any:
                result = run_fn(
                    fn=func,
                    retry=retry,
                    executor=executor,
                    ctx=ctx,
                    trace_name=trace_name or f"{func.__module__}.{func.__name__}",
                    trace_attrs=trace_attrs,
                    *wrapper_args,
                    **wrapper_kwargs,
                )
                # If executor is provided, result is a Future
                if executor:
                    success, result = result.result()
                else:
                    success, result = result
                
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

    def _run(*run_args, **run_kwargs) -> Tuple[bool, Any]:
        # NO RETRY / NO TIMEOUT
        if retry is None or (retry.max_attempts == 1 and retry.timeout is None):
            try:
                return True, run_with_context(
                    *run_args, fn=fn, ctx=ctx, trace_name=trace_name, trace_attrs=trace_attrs, **run_kwargs
                )
            except BaseException as e:
                return False, e

        # SINGLE ATTEMPT + TIMEOUT
        if retry.max_attempts == 1 and retry.timeout:
            try:
                return True, run_with_timeout(
                    *run_args,
                    fn=fn,
                    ctx=ctx,
                    timeout=retry.timeout,
                    trace_name=trace_name,
                    trace_attrs=trace_attrs,
                    **run_kwargs,
                )
            except BaseException as e:
                return False, e

        # FULL RETRY + TIMEOUT
        return run_with_retry_and_timeout(
            *run_args, fn=fn, retry=retry, ctx=ctx, trace_name=trace_name, trace_attrs=trace_attrs, **run_kwargs
        )

    if executor:
        return executor.submit(_run, *args, **kwargs)

    return _run(*args, **kwargs)


def run_concurrently(
    data: Any, callback: Callable, submit_fn: Callable, concurrency: int, limit: int = None, count: int = 0, timeout: float = None
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
