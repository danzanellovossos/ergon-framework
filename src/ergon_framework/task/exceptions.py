from enum import Enum


class ExceptionType(str, Enum):
    BUSINESS = "BUSINESS"
    SYSTEM = "SYSTEM"
    TIMEOUT = "TIMEOUT"


class GeneralException(Exception):
    """
    Exception raised when a general error occurs.
    """

    message: str = "A general error occurred."
    category: ExceptionType = ExceptionType.SYSTEM

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


class ConsumerLoopException(Exception):
    """
    Exception raised when a consumer loop fails.
    """

    message: str = "A consumer loop failed."
    category: ExceptionType = ExceptionType.SYSTEM

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

class ProducerLoopException(Exception):
    """
    Exception raised when a producer loop fails.
    """

    message: str = "A producer loop failed."
    category: ExceptionType = ExceptionType.SYSTEM

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
    """

    message: str = "A transaction error occurred."
    category: ExceptionType = ExceptionType.SYSTEM

    def __init__(
        self,
        message: str | None = None,
        category: ExceptionType | None = None,
        transaction_id: str | None = None,
    ):
        self.message = message or self.message
        self.category = category or self.category
        self.transaction_id = transaction_id
        super().__init__(self.message)

    def __str__(self):
        if self.transaction_id:
            return f"({self.category.value} - {self.transaction_id}) {self.message}"
        return f"({self.category.value}) {self.message}"

    def to_dict(self):
        return {
            "message": self.message,
            "category": self.category.value,
            "type": self.__class__.__name__,
            "transaction_id": self.transaction_id,
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
