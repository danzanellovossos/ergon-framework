from typing import List, Optional

from ..connector import AsyncConnector
from ..transaction import Transaction
from .async_service import AsyncPipefyService
from .models import PipefyClient


class AsyncPipefyConnector(AsyncConnector):
    service: AsyncPipefyService

    def __init__(self, client: PipefyClient, *args, **kwargs):
        self.service = AsyncPipefyService(client)
        self._authenticated = False

    async def _ensure_authenticated(self):
        if not self._authenticated:
            await self.service.authenticate()
            self._authenticated = True

    async def fetch_transactions_async(
        self, batch_size: int = 1, phase_id: Optional[str] = None, field_filters=None, **kwargs
    ) -> List[Transaction]:
        if not phase_id:
            raise ValueError("AsyncPipefyConnector.fetch requires a phase_id")

        await self._ensure_authenticated()

        cards = await self.service.get_next_card(
            phase_id=phase_id, field_filters=field_filters, batch_size=batch_size, **kwargs
        )
        if not cards:
            return []

        return [Transaction(id=card.get("id"), payload=card) for card in cards]

    async def dispatch_transactions_async(self, transactions: List[Transaction], *args, **kwargs):
        await self._ensure_authenticated()

        for transaction in transactions:
            await self.service.create_card(transaction.payload)
        return True

    async def fetch_transaction_by_id_async(self, transaction_id: str, *args, **kwargs) -> Transaction:
        await self._ensure_authenticated()

        transaction = await self.service.get_card_by_id(transaction_id)
        return Transaction(id=transaction.get("id"), payload=transaction)
