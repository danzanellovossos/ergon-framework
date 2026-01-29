# utils.py
import asyncio
import logging
import time
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
    """Compute exponential backoff with multiplier and optional cap."""
    with tracer.start_as_current_span("compute_backoff"):
        logger.info(
            f"Computing backoff with arguments: "
            f"attempt {attempt}, "
            f"backoff {backoff}, "
            f"multiplier {multiplier}, "
            f"and cap {cap}"
        )
        delay = backoff * (multiplier**attempt)
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
            wake_time_iso = _get_wake_time_iso(0)
            logger.info(f"Woke up from sleep at {wake_time_iso}, estimated wake time was {estimated_wake_time_iso}")


async def backoff_async(backoff: float, multiplier: float, cap: float, attempt: int):
    """Async backoff with computed backoff."""
    delay = compute_backoff(backoff, multiplier, cap, attempt)
    if delay > 0:
        with tracer.start_as_current_span("sleep", attributes={"delay": delay}):
            estimated_wake_time_iso = _get_wake_time_iso(delay)
            logger.info(f"Sleeping for {delay} seconds until {estimated_wake_time_iso}")
            await asyncio.sleep(delay)
            wake_time_iso = _get_wake_time_iso(0)
            logger.info(f"Woke up from sleep at {wake_time_iso}, estimated wake time was {estimated_wake_time_iso}")
