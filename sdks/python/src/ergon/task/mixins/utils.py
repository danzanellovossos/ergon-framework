# utils.py
import logging
import asyncio
import time

from ... import telemetry

logger = logging.getLogger(__name__)
tracer = telemetry.tracing.get_tracer(__name__)


# ============================================================
#  BACKOFF / SLEEP HELPERS
# ============================================================
def compute_backoff(backoff: float, multiplier: float, cap: float, attempt: int) -> float:
    """Compute exponential backoff with multiplier and optional cap."""
    with tracer.start_as_current_span("compute_backoff"):
        delay = backoff * (multiplier**attempt)
        return min(delay, cap) if cap > 0 else delay


def backoff(backoff: float, multiplier: float, cap: float, attempt: int):
    """Blocking sleep with computed backoff."""
    logger.debug(f"Computing backoff for attempt {attempt} with backoff {backoff}, multiplier {multiplier}, and cap {cap}")
    delay = compute_backoff(backoff, multiplier, cap, attempt)
    logger.debug(f"Computed backoff: {delay} seconds")
    if delay > 0:
        with tracer.start_as_current_span("sleep", attributes={"delay": delay}):
            logger.debug(f"Sleeping for {delay} seconds")
            time.sleep(delay)
        logger.debug(f"Woke up from sleep")


async def backoff_async(backoff: float, multiplier: float, cap: float, attempt: int):
    """Async backoff with computed backoff."""
    delay = compute_backoff(backoff, multiplier, cap, attempt)
    if delay > 0:
        with tracer.start_as_current_span("sleep", attributes={"delay": delay}):
            await asyncio.sleep(delay)
