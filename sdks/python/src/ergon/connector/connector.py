from abc import ABC, abstractmethod
from typing import Any, List, Union

from pydantic import BaseModel, model_validator

from . import transaction


# ---------------------------------------------------------
# SYNC CONNECTOR
# ---------------------------------------------------------
class Connector(ABC):
    @abstractmethod
    def fetch_transactions(self, *args, **kwargs) -> List[transaction.Transaction]:
        """Fetch one or many transactions (sync)."""

    @abstractmethod
    def dispatch_transactions(self, transactions: List[transaction.Transaction], *args, **kwargs) -> Any:
        """Publish a transaction (sync)."""
        pass

    def fetch_transaction_by_id(self, transaction_id: str, *args, **kwargs) -> transaction.Transaction:
        """
        Get a transaction by its id.

        Args:
            transaction_id: The id of the transaction to get.

        Returns:
            The transaction.


        Use cases:
        - Testing
        - Debugging
        - Replaying

        """
        pass

    def get_transactions_count(self, *args, **kwargs) -> int:
        """Get the total number of transactions in the connector target system."""
        pass


# ---------------------------------------------------------
# ASYNC CONNECTOR
# ---------------------------------------------------------
class AsyncConnector(ABC):
    @abstractmethod
    async def fetch_transactions_async(self, *args, **kwargs) -> List[transaction.Transaction]:
        """Fetch transactions asynchronously."""
        pass

    @abstractmethod
    async def dispatch_transactions_async(self, transactions: List[transaction.Transaction], *args, **kwargs) -> Any:
        """Publish a transaction asynchronously."""
        pass

    async def get_transactions_count_async(self, *args, **kwargs) -> int:
        """Get the total number of transactions in the connector target system asynchronously."""
        pass


# ---------------------------------------------------------
# CONNECTOR CONFIG (supports both sync + async)
# ---------------------------------------------------------
class ConnectorConfig(BaseModel):
    """
    Generic connector configuration.

    connector: class implementing either Connector or AsyncConnector
    args: positional arguments passed to connector's __init__
    kwargs: keyword arguments passed to connector's __init__
    """

    connector: Union[type[Connector], type[AsyncConnector]]
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = {}

    @model_validator(mode="after")
    def validate_connector(self) -> "ConnectorConfig":
        """Validate that the connector inherits from Connector or AsyncConnector."""
        if not issubclass(self.connector, Connector) and not issubclass(self.connector, AsyncConnector):
            raise ValueError(
                f"Connector '{self.connector}' must inherit from Connector or AsyncConnector. Got: {self.connector}"
            )
        return self
