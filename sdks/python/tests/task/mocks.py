"""Shared mocks for consumer and producer tests."""

from typing import Any, List

from ergon.connector import Connector, Transaction
from ergon.task.mixins.consumer import ConsumerMixin
from ergon.task.mixins.producer import ProducerMixin


def make_transactions(n: int) -> List[Transaction]:
    return [Transaction(id=str(i), payload={"i": i}) for i in range(n)]


class MockConnector(Connector):
    """In-memory connector that pops items from an internal queue."""

    def __init__(self, transactions: List[Transaction] | None = None):
        self._queue: List[Transaction] = list(transactions or [])

    def fetch_transactions(self, batch_size: int = 1, **kwargs) -> List[Transaction]:
        batch = self._queue[:batch_size]
        self._queue = self._queue[batch_size:]
        return batch

    def dispatch_transactions(self, transactions: List[Transaction], **kwargs) -> Any:
        pass


class MockConsumer(ConsumerMixin):
    """
    Concrete ConsumerMixin that bypasses BaseTask/TaskMeta.

    Accepts pluggable callables for process, success, and exception hooks.
    Tracks processed transactions in `self.processed`.
    """

    def __init__(
        self,
        connectors: dict | None = None,
        process_fn=None,
        success_fn=None,
        exception_fn=None,
    ):
        self.name = "test-consumer"
        self.connectors = connectors or {}
        self.processed: List[Transaction] = []
        self._process_fn = process_fn
        self._success_fn = success_fn
        self._exception_fn = exception_fn

    def process_transaction(self, transaction: Transaction) -> Any:
        self.processed.append(transaction)
        if self._process_fn:
            return self._process_fn(transaction)
        return transaction.payload

    def handle_process_success(self, transaction, result):
        if self._success_fn:
            return self._success_fn(transaction, result)
        return super().handle_process_success(transaction, result)

    def handle_process_exception(self, transaction, exc):
        if self._exception_fn:
            return self._exception_fn(transaction, exc)
        return super().handle_process_exception(transaction, exc)


class MockProducer(ProducerMixin):
    """
    Concrete ProducerMixin that bypasses BaseTask/TaskMeta.

    Accepts pluggable callables for prepare, success, and exception hooks.
    Tracks prepared transactions in `self.prepared`.
    """

    def __init__(
        self,
        prepare_fn=None,
        success_fn=None,
        exception_fn=None,
    ):
        self.name = "test-producer"
        self.prepared: List[Transaction] = []
        self._prepare_fn = prepare_fn
        self._success_fn = success_fn
        self._exception_fn = exception_fn

    def prepare_transaction(self, transaction: Transaction) -> Any:
        self.prepared.append(transaction)
        if self._prepare_fn:
            return self._prepare_fn(transaction)
        return transaction.payload

    def handle_prepare_success(self, transaction, result):
        if self._success_fn:
            return self._success_fn(transaction, result)
        return super().handle_prepare_success(transaction, result)

    def handle_prepare_exception(self, transaction, exc):
        if self._exception_fn:
            return self._exception_fn(transaction, exc)
        return super().handle_prepare_exception(transaction, exc)
