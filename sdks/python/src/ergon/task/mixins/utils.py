# utils.py
import asyncio
import logging
import time
import math
from datetime import datetime

from ... import telemetry

logger = logging.getLogger(__name__)
tracer = telemetry.tracing.get_tracer(__name__)


def _get_wake_time_iso(delay: float) -> str:
    return datetime.fromtimestamp(time.time() + delay).isoformat()


# ============================================================
#  BACKOFF / SLEEP HELPERS
# ============================================================
def compute_backoff(backoff: float, multiplier: float, cap: float, attempt: int) -> float:
    with tracer.start_as_current_span("compute_backoff"):
        logger.info(
            f"Computing backoff with arguments: "
            f"attempt {attempt}, "
            f"backoff {backoff}, "
            f"multiplier {multiplier}, "
            f"and cap {cap}"
        )

        if cap > 0 and multiplier > 1 and backoff > 0:
            # max attempt that won't exceed cap
            max_attempt = math.floor(
                math.log(cap / backoff, multiplier)
            ) if cap > backoff else 0
            safe_attempt = min(attempt, max_attempt)
        else:
            safe_attempt = attempt

        delay = backoff * (multiplier ** safe_attempt)
        computed_delay = min(delay, cap) if cap > 0 else delay

        logger.info(f"Computed backoff: {computed_delay} seconds")
        return computed_delay


def backoff(backoff: float, multiplier: float, cap: float, attempt: int):
    """Blocking sleep with computed backoff."""
    delay = compute_backoff(backoff, multiplier, cap, attempt)
    if delay > 0:
        with tracer.start_as_current_span("sleep", attributes={"delay": delay}):
            estimated_wake_time_iso = _get_wake_time_iso(delay)
            logger.info(f"Sleeping for {delay} seconds until {estimated_wake_time_iso}")
            time.sleep(delay)
            logger.info(f"Woke up from {delay} second{'' if delay == 1 else 's'} sleep")


async def backoff_async(backoff: float, multiplier: float, cap: float, attempt: int):
    """Async backoff with computed backoff."""
    delay = compute_backoff(backoff, multiplier, cap, attempt)
    if delay > 0:
        with tracer.start_as_current_span("sleep", attributes={"delay": delay}):
            estimated_wake_time_iso = _get_wake_time_iso(delay)
            logger.info(f"Sleeping for {delay} seconds until {estimated_wake_time_iso}")
            await asyncio.sleep(delay)
            logger.info(f"Woke up from {delay} second{'' if delay == 1 else 's'} sleep")
