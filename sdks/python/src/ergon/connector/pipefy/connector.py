from typing import List, Optional

from ..connector import Connector
from ..transaction import Transaction
from .models import PipefyClient
from .service import PipefyService


class PipefyConnector(Connector):
    service: PipefyService

    def __init__(self, client: PipefyClient, *args, **kwargs):
        self.service = PipefyService(client)

    def fetch_transactions(
        self, batch_size: int = 1, phase_id: Optional[str] = None, field_filters=None, **kwargs
    ) -> List[Transaction]:
        if not phase_id:
            raise ValueError("PipefyConnector.fetch requires a phase_id")

        cards = self.service.get_next_card(
            phase_id=phase_id, field_filters=field_filters, batch_size=batch_size, **kwargs
        )
        if not cards:
            return []

        return [Transaction(id=card.get("id"), payload=card) for card in cards]

    def dispatch_transactions(self, transactions: List[Transaction], *args, **kwargs):
        for transaction in transactions:
            self.service.create_card(transaction.payload)
        return True

    def fetch_transaction_by_id(self, transaction_id: str, *args, **kwargs) -> Transaction:
        transaction = self.service.get_card_by_id(transaction_id)
        return Transaction(id=transaction.get("id"), payload=transaction)
