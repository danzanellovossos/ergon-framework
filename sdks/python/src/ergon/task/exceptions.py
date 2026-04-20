from enum import Enum


class ExceptionType(str, Enum):
    BUSINESS = "BUSINESS"
    SYSTEM = "SYSTEM"
    TIMEOUT = "TIMEOUT"


class ConsumerLoopTimeoutException(Exception):
    """
    Exception raised when a consumer loop times out.
    """

    message: str = "A consumer loop timed out."
    category: ExceptionType = ExceptionType.TIMEOUT

    def __init__(self, message: str | None = None):
        self.message = message or self.message
        super().__init__(self.message)

    def __str__(self):
        return self.message

    def to_dict(self):
        return {
            "message": self.message,
            "category": self.category.value,
            "type": self.__class__.__name__,
        }


class ProducerLoopTimeoutException(Exception):
    """
    Exception raised when a producer loop times out.
    """

    message: str = "A producer loop timed out."
    category: ExceptionType = ExceptionType.TIMEOUT

    def __init__(self, message: str | None = None):
        self.message = message or self.message
        super().__init__(self.message)

    def __str__(self):
        return self.message

    def to_dict(self):
        return {
            "message": self.message,
            "category": self.category.value,
            "type": self.__class__.__name__,
        }


class TransactionException(Exception):
    """
    Exception raised during the processing of a single transaction.
    These errors are caught by the framework and routed to handle_transaction_error().

    When wrapping an underlying exception, pass it as ``cause`` so the original
    type, message, and traceback are preserved on ``__cause__`` / ``__traceback__``.
    This avoids the failure mode where an upstream exception with an empty
    ``str()`` would degrade the wrapped error to the default placeholder message.
    """

    message: str = "A transaction error occurred."
    category: ExceptionType = ExceptionType.SYSTEM

    def __init__(
        self,
        message: str | None = None,
        category: ExceptionType | None = None,
        transaction_id: str | None = None,
        cause: BaseException | None = None,
    ):
        # If no explicit message was supplied but a cause is given, derive a
        # diagnostic message from the cause (using repr to survive empty str()).
        if not message and cause is not None:
            message = repr(cause)
        self.message = message or self.message
        self.category = category or self.category
        self.transaction_id = transaction_id
        self.cause = cause
        super().__init__(self.message)
        if cause is not None:
            self.__cause__ = cause
            if getattr(cause, "__traceback__", None) is not None and self.__traceback__ is None:
                self.__traceback__ = cause.__traceback__

    def __str__(self):
        prefix = (
            f"({self.category.value} - {self.transaction_id})" if self.transaction_id else f"({self.category.value})"
        )
        if self.cause is not None:
            return f"{prefix} {self.message} [cause: {type(self.cause).__name__}: {self.cause!s}]"
        return f"{prefix} {self.message}"

    def to_dict(self):
        return {
            "message": self.message,
            "category": self.category.value,
            "type": self.__class__.__name__,
            "transaction_id": self.transaction_id,
            "cause": repr(self.cause) if self.cause is not None else None,
        }


class FetchTimeoutException(Exception):
    """
    Exception raised when a fetch operation times out.
    """

    message: str = "A fetch operation timed out."
    category: ExceptionType = ExceptionType.TIMEOUT

    def __init__(self, message: str | None = None):
        self.message = message or self.message
        super().__init__(self.message)

    def __str__(self):
        return self.message


class FetchException(Exception):
    """
    Exception raised when a fetch operation fails.
    """

    message: str = "A fetch operation failed."
    category: ExceptionType = ExceptionType.SYSTEM

    def __init__(self, message: str | None = None):
        self.message = message or "A fetch operation failed."
        super().__init__(self.message)

    def __str__(self):
        return self.message


class NonRetryableException(Exception):
    """
    Exception raised to explicitly signal that an execution
    must NOT be retried, regardless of retry policy.

    This exception is execution-scoped, not transaction-scoped.
    """

    message: str = "Execution failed and should not be retried."

    def __init__(self, message: str | None = None):
        self.message = message or self.message
        super().__init__(self.message)

    def __str__(self):
        return self.message


class DeadChannelError(NonRetryableException):
    """
    Base class for ack/nack failures against a cancelled or closed broker channel.

    Raised by connector implementations when the underlying message-broker
    channel is no longer usable. The framework's consumer mixin treats these
    as a signal that the broker will redeliver the message and short-circuits
    the lifecycle accordingly: it does NOT route to the exception handler
    (which would typically fail the same way).
    """

    operation: str = "ack/nack"
    message: str = "Cannot ack/nack: broker channel is closed or cancelled. Broker will redeliver."

    def __init__(
        self,
        message: str | None = None,
        delivery_tag: int | str | None = None,
        queue: str | None = None,
        cause: BaseException | None = None,
    ):
        self.delivery_tag = delivery_tag
        self.queue = queue
        self.cause = cause
        if not message:
            message = (
                f"Cannot {self.operation} delivery_tag={delivery_tag} on queue={queue!r}: "
                "broker channel is closed or cancelled. Broker will redeliver."
            )
        super().__init__(message)
        if cause is not None:
            self.__cause__ = cause

    def __str__(self):
        return self.message


class AckOnDeadChannelError(DeadChannelError):
    """
    Raised when ``ack`` is attempted against a closed or cancelled channel.

    Carries the original delivery tag and queue name for diagnostics. The
    consumer mixin recognises this error and skips the exception handler,
    since the broker will redeliver the message to a fresh consumer.
    """

    operation = "ack"


class NackOnDeadChannelError(DeadChannelError):
    """
    Raised when ``nack`` is attempted against a closed or cancelled channel.

    Same semantics as :class:`AckOnDeadChannelError` but for negative
    acknowledgements. Allows the consumer mixin to log a single WARNING
    instead of cascading further failures.
    """

    operation = "nack"
