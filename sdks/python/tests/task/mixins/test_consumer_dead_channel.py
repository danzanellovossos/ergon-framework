"""Regression tests for the dead-channel / cause-chain fixes.

These cover the exact failure mode from the agents-processor incident:

1. The success handler tries to ack on a cancelled RabbitMQ channel and the
   connector raises an :class:`AckOnDeadChannelError`. The consumer mixin
   must short-circuit (not invoke the exception handler — which would only
   re-fail on nack against the same dead channel).

2. When an underlying exception with an empty ``str()`` reaches the consumer
   mixin, the wrap into ``TransactionException`` must preserve the original
   type/message/traceback so downstream ``logger.exception`` produces a
   real stack instead of ``NoneType: None``.
"""

import logging

from ergon.connector import Transaction
from ergon.task import exceptions, policies
from tests.task.mocks import MockAsyncConsumer, MockConsumer

# ---------------------------------------------------------------------------
# Async — production path that wedged in the incident
# ---------------------------------------------------------------------------


class TestAsyncDeadChannelShortCircuit:
    async def test_success_handler_ack_on_dead_channel_skips_exception_handler(self, caplog):
        """The exception handler must NOT run when ack hit a dead channel.

        In the incident, routing to the exception handler caused a second
        nack on the same dead channel, which then ALSO failed silently and
        left the transaction permanently in flight.
        """
        ack_error = exceptions.AckOnDeadChannelError(
            delivery_tag=120,
            queue="agents.processor",
        )

        async def success_that_fails_to_ack(tx, result):
            raise ack_error

        exception_handler_calls: list = []

        async def exception_handler(tx, exc):
            exception_handler_calls.append(exc)

        consumer = MockAsyncConsumer(
            success_fn=success_that_fails_to_ack,
            exception_fn=exception_handler,
        )
        tx = Transaction(id="120", payload="data")
        policy = policies.ConsumerPolicy()

        with caplog.at_level(logging.WARNING, logger="ergon.task.mixins.consumer"):
            success, result = await consumer._start_processing(tx, policy)

        assert success is False
        assert result is ack_error
        # The exception handler must NOT have been invoked.
        assert exception_handler_calls == []
        # A WARNING explaining "broker will redeliver" must be emitted, with
        # the delivery tag visible for diagnostics.
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("broker will redeliver" in r.getMessage() for r in warning_records)
        assert any("120" in r.getMessage() for r in warning_records)

    async def test_exception_handler_dead_channel_is_logged_not_re_routed(self, caplog):
        """If the exception handler itself hits a dead channel, log WARNING and stop."""

        async def failing_process(tx):
            raise RuntimeError("process exploded")

        async def exception_handler_fails(tx, exc):
            raise exceptions.NackOnDeadChannelError(delivery_tag=tx.id, queue="q")

        consumer = MockAsyncConsumer(
            process_fn=failing_process,
            exception_fn=exception_handler_fails,
        )
        tx = Transaction(id="55", payload="x")
        policy = policies.ConsumerPolicy()

        with caplog.at_level(logging.WARNING, logger="ergon.task.mixins.consumer"):
            success, result = await consumer._start_processing(tx, policy)

        assert success is False
        assert isinstance(result, exceptions.NackOnDeadChannelError)
        assert any("broker will redeliver" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)

    async def test_dead_channel_error_is_non_retryable(self):
        """Sanity: helpers.run_fn_async treats DeadChannelError as non-retryable."""
        from ergon.task.helpers import run_fn_async

        ack_error = exceptions.AckOnDeadChannelError(delivery_tag=1, queue="q")
        attempts = {"count": 0}

        async def always_dead():
            attempts["count"] += 1
            raise ack_error

        retry = policies.RetryPolicy(max_attempts=5)
        success, result = await run_fn_async(fn=always_dead, retry=retry)

        assert success is False
        assert result is ack_error
        # max_attempts=5 but DeadChannelError is NonRetryableException → 1 try only.
        assert attempts["count"] == 1


# ---------------------------------------------------------------------------
# Async — diagnostics regression: empty-str() exceptions must not erase info
# ---------------------------------------------------------------------------


class _EmptyStrException(Exception):
    """Mirrors aio_pika.exceptions.MessageProcessError's empty str() shape."""

    def __str__(self) -> str:
        return ""


class TestEmptyExceptionDiagnostics:
    async def test_success_handler_empty_exception_preserves_cause(self, caplog):
        """Wrapping an empty-str() exception must keep the cause and class name.

        Before the fix, this scenario produced
        ``TransactionException("A transaction error occurred.", SYSTEM)``
        with no ``__cause__`` and no traceback. The exception handler then
        logged ``NoneType: None``, hiding the real failure.
        """
        underlying = _EmptyStrException()

        async def success_raises_empty(tx, result):
            raise underlying

        captured: list = []

        async def exception_handler(tx, exc):
            captured.append(exc)

        consumer = MockAsyncConsumer(
            success_fn=success_raises_empty,
            exception_fn=exception_handler,
        )
        tx = Transaction(id="1", payload="data")
        policy = policies.ConsumerPolicy()

        with caplog.at_level(logging.ERROR, logger="ergon.task.helpers"):
            await consumer._start_processing(tx, policy)

        assert len(captured) == 1
        wrapped = captured[0]
        assert isinstance(wrapped, exceptions.TransactionException)
        # The original exception must survive on __cause__ so a downstream
        # logger.exception() can render the real chain.
        assert wrapped.__cause__ is underlying
        assert wrapped.cause is underlying
        # The wrapped message must NOT degrade to the placeholder when the
        # cause has an empty __str__.
        assert "A transaction error occurred" not in wrapped.message
        assert "_EmptyStrException" in wrapped.message

        # Phase 1 diagnostics: helpers must log with exc_info attached so the
        # next on-call sees the real stack instead of an empty-tail line.
        helper_errors = [r for r in caplog.records if r.name == "ergon.task.helpers"]
        assert helper_errors, "helpers.run_fn_async must log the failure"
        assert any(
            r.exc_info is not None for r in helper_errors
        ), "helpers must attach exc_info so the traceback survives the swallow"

    async def test_transaction_exception_str_includes_cause(self):
        underlying = _EmptyStrException()
        wrapped = exceptions.TransactionException(cause=underlying)
        rendered = str(wrapped)
        assert "_EmptyStrException" in rendered
        assert "(SYSTEM)" in rendered


# ---------------------------------------------------------------------------
# Sync mirror — same short-circuit behaviour for ConsumerMixin
# ---------------------------------------------------------------------------


class TestSyncDeadChannelShortCircuit:
    def test_sync_success_handler_ack_on_dead_channel_skips_exception_handler(self, caplog):
        ack_error = exceptions.AckOnDeadChannelError(delivery_tag=7, queue="q")

        def success_that_fails(tx, result):
            raise ack_error

        exception_handler_calls: list = []

        def exception_handler(tx, exc):
            exception_handler_calls.append(exc)

        consumer = MockConsumer(
            success_fn=success_that_fails,
            exception_fn=exception_handler,
        )
        tx = Transaction(id="7", payload="data")
        policy = policies.ConsumerPolicy()

        with caplog.at_level(logging.WARNING, logger="ergon.task.mixins.consumer"):
            success, result = consumer._start_processing(tx, policy)

        assert success is False
        assert result is ack_error
        assert exception_handler_calls == []
        assert any("broker will redeliver" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
