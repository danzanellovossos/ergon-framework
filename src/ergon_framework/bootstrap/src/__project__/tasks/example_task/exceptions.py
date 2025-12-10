from ergon_framework import ExceptionType, TransactionException


class ExampleTaskException(TransactionException):
    message: str
    category: ExceptionType = ExceptionType.BUSINESS
