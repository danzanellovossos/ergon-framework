# metrics.py
"""
Ergon Task Framework - Built-in Metrics Instrumentation

This module provides pre-configured OpenTelemetry metrics for Consumer and Producer
lifecycle stages. Metrics are lazily initialized on first use after the MeterProvider
has been configured by the task runner.

All metrics follow OpenTelemetry semantic conventions where applicable.
"""

import time
from contextlib import contextmanager
from functools import wraps
from typing import Callable, Optional

from ... import telemetry

# ============================================================
# LAZY METER INITIALIZATION
# ============================================================

_meter = None
_instruments = {}


def _get_meter():
    """
    Lazily initialize the meter after telemetry configuration is applied.
    Must be called AFTER runner initializes telemetry.
    """
    global _meter
    if _meter is None:
        _meter = telemetry.metrics.get_metric_meter("ergon.task")
    return _meter


def _get_instrument(name: str, factory: Callable):
    """
    Lazily create and cache metric instruments.
    """
    if name not in _instruments:
        _instruments[name] = factory(_get_meter())
    return _instruments[name]


# ============================================================
# CONSUMER METRICS
# ============================================================


def _consumer_transactions_total():
    return _get_instrument(
        "consumer.transactions.total",
        lambda m: m.create_counter(
            name="consumer.transactions.total",
            description="Total number of transactions processed by the consumer",
            unit="1",
        ),
    )


def _consumer_transactions_duration():
    return _get_instrument(
        "consumer.transactions.duration",
        lambda m: m.create_histogram(
            name="consumer.transactions.duration",
            description="Time spent processing each transaction end-to-end",
            unit="s",
        ),
    )


def _consumer_fetch_total():
    return _get_instrument(
        "consumer.fetch.total",
        lambda m: m.create_counter(
            name="consumer.fetch.total",
            description="Total number of fetch operations performed",
            unit="1",
        ),
    )


def _consumer_fetch_batch_size():
    return _get_instrument(
        "consumer.fetch.batch_size",
        lambda m: m.create_histogram(
            name="consumer.fetch.batch_size",
            description="Number of transactions fetched per fetch operation",
            unit="1",
        ),
    )


def _consumer_fetch_duration():
    return _get_instrument(
        "consumer.fetch.duration",
        lambda m: m.create_histogram(
            name="consumer.fetch.duration",
            description="Time spent on each fetch operation",
            unit="s",
        ),
    )


def _consumer_lifecycle_duration():
    return _get_instrument(
        "consumer.lifecycle.duration",
        lambda m: m.create_histogram(
            name="consumer.lifecycle.duration",
            description="Duration of each consumer lifecycle stage",
            unit="s",
        ),
    )


def _consumer_lifecycle_outcome():
    return _get_instrument(
        "consumer.lifecycle.outcome",
        lambda m: m.create_counter(
            name="consumer.lifecycle.outcome",
            description="Outcome counts for each consumer lifecycle stage",
            unit="1",
        ),
    )


def _consumer_batches_total():
    return _get_instrument(
        "consumer.batches.total",
        lambda m: m.create_counter(
            name="consumer.batches.total",
            description="Total number of batches processed",
            unit="1",
        ),
    )


def _consumer_empty_queue_waits():
    return _get_instrument(
        "consumer.empty_queue.waits",
        lambda m: m.create_counter(
            name="consumer.empty_queue.waits",
            description="Number of times consumer waited on empty queue",
            unit="1",
        ),
    )


# ============================================================
# PRODUCER METRICS
# ============================================================


def _producer_transactions_total():
    return _get_instrument(
        "producer.transactions.total",
        lambda m: m.create_counter(
            name="producer.transactions.total",
            description="Total number of transactions produced",
            unit="1",
        ),
    )


def _producer_transactions_duration():
    return _get_instrument(
        "producer.transactions.duration",
        lambda m: m.create_histogram(
            name="producer.transactions.duration",
            description="Time spent producing each transaction end-to-end",
            unit="s",
        ),
    )


def _producer_lifecycle_duration():
    return _get_instrument(
        "producer.lifecycle.duration",
        lambda m: m.create_histogram(
            name="producer.lifecycle.duration",
            description="Duration of each producer lifecycle stage",
            unit="s",
        ),
    )


def _producer_lifecycle_outcome():
    return _get_instrument(
        "producer.lifecycle.outcome",
        lambda m: m.create_counter(
            name="producer.lifecycle.outcome",
            description="Outcome counts for each producer lifecycle stage",
            unit="1",
        ),
    )


def _producer_batches_total():
    return _get_instrument(
        "producer.batches.total",
        lambda m: m.create_counter(
            name="producer.batches.total",
            description="Total number of batches produced",
            unit="1",
        ),
    )


# ============================================================
# CONSUMER RECORDING HELPERS
# ============================================================


def record_consumer_transaction(
    task_name: str,
    transaction_id: str,
    duration: float,
    status: str,  # "success" | "exception"
):
    """Record a completed consumer transaction."""
    attrs = {"task": task_name, "status": status}
    _consumer_transactions_total().add(1, attrs)
    _consumer_transactions_duration().record(duration, attrs)


def record_consumer_fetch(
    task_name: str,
    connector_name: str,
    batch_size: int,
    fetched_count: int,
    duration: float,
    success: bool,
):
    """Record a fetch operation."""
    attrs = {
        "task": task_name,
        "connector": connector_name,
        "success": str(success).lower(),
    }
    _consumer_fetch_total().add(1, attrs)
    _consumer_fetch_duration().record(duration, attrs)

    if success and fetched_count > 0:
        _consumer_fetch_batch_size().record(fetched_count, {"task": task_name, "connector": connector_name})


def record_consumer_lifecycle(
    task_name: str,
    stage: str,  # "process" | "success" | "exception"
    duration: float,
    outcome: str,  # "ok" | "error" | "timeout" | "retry"
    attempt: int = 1,
):
    """Record a consumer lifecycle stage execution."""
    attrs = {
        "task": task_name,
        "stage": stage,
        "outcome": outcome,
        "attempt": attempt,
    }
    _consumer_lifecycle_duration().record(duration, {"task": task_name, "stage": stage})
    _consumer_lifecycle_outcome().add(1, attrs)


def record_consumer_batch(task_name: str, batch_number: int, batch_size: int, streaming: bool):
    """Record a batch being processed."""
    attrs = {
        "task": task_name,
        "streaming": str(streaming).lower(),
    }
    _consumer_batches_total().add(1, attrs)


def record_consumer_empty_queue_wait(task_name: str, wait_count: int):
    """Record an empty queue wait event."""
    _consumer_empty_queue_waits().add(1, {"task": task_name, "consecutive_waits": wait_count})


# ============================================================
# PRODUCER RECORDING HELPERS
# ============================================================


def record_producer_transaction(
    task_name: str,
    transaction_id: str,
    duration: float,
    status: str,  # "success" | "exception"
):
    """Record a completed producer transaction."""
    attrs = {"task": task_name, "status": status}
    _producer_transactions_total().add(1, attrs)
    _producer_transactions_duration().record(duration, attrs)


def record_producer_lifecycle(
    task_name: str,
    stage: str,  # "prepare" | "success" | "exception"
    duration: float,
    outcome: str,  # "ok" | "error" | "timeout" | "retry"
    attempt: int = 1,
):
    """Record a producer lifecycle stage execution."""
    attrs = {
        "task": task_name,
        "stage": stage,
        "outcome": outcome,
        "attempt": attempt,
    }
    _producer_lifecycle_duration().record(duration, {"task": task_name, "stage": stage})
    _producer_lifecycle_outcome().add(1, attrs)


def record_producer_batch(task_name: str, batch_number: int, batch_size: int):
    """Record a batch being produced."""
    attrs = {"task": task_name, "batch_size": batch_size}
    _producer_batches_total().add(1, attrs)


# ============================================================
# CONTEXT MANAGERS FOR TIMING
# ============================================================


@contextmanager
def timed_consumer_transaction(task_name: str, transaction_id: str):
    """
    Context manager to time and record a consumer transaction.

    Usage:
        with timed_consumer_transaction("MyTask", tx.id) as ctx:
            # process transaction
            ctx["status"] = "success"  # or "exception"
    """
    ctx = {"status": "success", "start": time.perf_counter()}
    try:
        yield ctx
    except Exception:
        ctx["status"] = "exception"
        raise
    finally:
        duration = time.perf_counter() - ctx["start"]
        record_consumer_transaction(task_name, transaction_id, duration, ctx["status"])


@contextmanager
def timed_consumer_lifecycle(task_name: str, stage: str, attempt: int = 1):
    """
    Context manager to time and record a consumer lifecycle stage.

    Usage:
        with timed_consumer_lifecycle("MyTask", "process") as ctx:
            # do work
            ctx["outcome"] = "ok"  # or "error", "timeout", "retry"
    """
    ctx = {"outcome": "ok", "start": time.perf_counter()}
    try:
        yield ctx
    except Exception:
        ctx["outcome"] = "error"
        raise
    finally:
        duration = time.perf_counter() - ctx["start"]
        record_consumer_lifecycle(task_name, stage, duration, ctx["outcome"], attempt)


@contextmanager
def timed_consumer_fetch(task_name: str, connector_name: str, batch_size: int):
    """
    Context manager to time and record a fetch operation.

    Usage:
        with timed_consumer_fetch("MyTask", "rabbitmq", 100) as ctx:
            transactions = connector.fetch()
            ctx["fetched_count"] = len(transactions)
            ctx["success"] = True
    """
    ctx = {"fetched_count": 0, "success": False, "start": time.perf_counter()}
    try:
        yield ctx
    finally:
        duration = time.perf_counter() - ctx["start"]
        record_consumer_fetch(
            task_name,
            connector_name,
            batch_size,
            ctx["fetched_count"],
            duration,
            ctx["success"],
        )


@contextmanager
def timed_producer_transaction(task_name: str, transaction_id: str):
    """
    Context manager to time and record a producer transaction.

    Usage:
        with timed_producer_transaction("MyTask", tx.id) as ctx:
            # produce transaction
            ctx["status"] = "success"  # or "exception"
    """
    ctx = {"status": "success", "start": time.perf_counter()}
    try:
        yield ctx
    except Exception:
        ctx["status"] = "exception"
        raise
    finally:
        duration = time.perf_counter() - ctx["start"]
        record_producer_transaction(task_name, transaction_id, duration, ctx["status"])


@contextmanager
def timed_producer_lifecycle(task_name: str, stage: str, attempt: int = 1):
    """
    Context manager to time and record a producer lifecycle stage.

    Usage:
        with timed_producer_lifecycle("MyTask", "prepare") as ctx:
            # do work
            ctx["outcome"] = "ok"  # or "error", "timeout", "retry"
    """
    ctx = {"outcome": "ok", "start": time.perf_counter()}
    try:
        yield ctx
    except Exception:
        ctx["outcome"] = "error"
        raise
    finally:
        duration = time.perf_counter() - ctx["start"]
        record_producer_lifecycle(task_name, stage, duration, ctx["outcome"], attempt)


# ============================================================
# PUBLIC API
# ============================================================

__all__ = [
    # Consumer recording
    "record_consumer_transaction",
    "record_consumer_fetch",
    "record_consumer_lifecycle",
    "record_consumer_batch",
    "record_consumer_empty_queue_wait",
    # Producer recording
    "record_producer_transaction",
    "record_producer_lifecycle",
    "record_producer_batch",
    # Context managers
    "timed_consumer_transaction",
    "timed_consumer_lifecycle",
    "timed_consumer_fetch",
    "timed_producer_transaction",
    "timed_producer_lifecycle",
]

