import asyncio
import logging
from typing import Any, List, Optional

from ..connector import AsyncConnector
from ..transaction import Transaction
from .async_service import AsyncPostgresService
from .models import PostgresClient, PostgresConsumerConfig, PostgresProducerConfig

logger = logging.getLogger(__name__)


class AsyncPostgresConnector(AsyncConnector):
    service: AsyncPostgresService

    def __init__(
        self,
        client: PostgresClient,
        consumer_config: Optional[PostgresConsumerConfig] = None,
        producer_config: Optional[PostgresProducerConfig] = None,
    ) -> None:
        self.service = AsyncPostgresService(client)
        self._consumer_config = consumer_config
        self._producer_config = producer_config
        self._notify_event: Optional[asyncio.Event] = None

    async def init_async(self) -> None:
        """
        Called by the task runner on startup.

        Sets up PG LISTEN if a listen_channel is configured, using an
        asyncio.Event to allow the consume loop to react to notifications.
        """
        if self._consumer_config and self._consumer_config.listen_channel:
            self._notify_event = asyncio.Event()

            def _on_notify(connection, pid, channel, payload):
                if self._notify_event is not None:
                    self._notify_event.set()

            await self.service.listen(self._consumer_config.listen_channel, _on_notify)

    async def fetch_transactions_async(
        self,
        batch_size: Optional[int] = None,
        fetch_query: Optional[str] = None,
        fetch_params: Optional[List[Any]] = None,
        *args,
        **kwargs,
    ) -> List[Transaction]:
        if self._consumer_config is None:
            raise ValueError("AsyncPostgresConnector requires a consumer_config to fetch transactions")

        config = self._consumer_config
        query = fetch_query or config.fetch_query
        params = fetch_params or config.fetch_params
        limit = batch_size or config.batch_size

        rows = await self.service.fetch(query, params, limit=limit)

        if not rows:
            return []

        if self._notify_event is not None:
            self._notify_event.clear()

        return [
            Transaction(
                id=str(row.get(config.id_column, "")),
                payload=row,
                metadata={},
            )
            for row in rows
        ]

    async def dispatch_transactions_async(
        self,
        transactions: List[Transaction],
        dispatch_query: Optional[str] = None,
        *args,
        **kwargs,
    ) -> None:
        if self._producer_config is None:
            raise ValueError("AsyncPostgresConnector requires a producer_config to dispatch transactions")

        query = dispatch_query or self._producer_config.dispatch_query

        for txn in transactions:
            params = self._build_dispatch_params(txn)
            await self.service.execute(query, params)

    @staticmethod
    def _build_dispatch_params(transaction: Transaction) -> List[Any]:
        """
        Build parameter list for the dispatch query from the transaction.

        Passes transaction.id as the first param. Subclasses or callers
        can override this for more complex parameter derivation.
        """
        return [transaction.id]

    async def wait_for_notify(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for a PG NOTIFY event. Returns True if notified, False on timeout.

        Useful for the platform layer to build outbox-relay-style tasks
        that sleep until the DB signals new rows.
        """
        if self._notify_event is None:
            return False

        try:
            await asyncio.wait_for(self._notify_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def close(self) -> None:
        await self.service.close()
