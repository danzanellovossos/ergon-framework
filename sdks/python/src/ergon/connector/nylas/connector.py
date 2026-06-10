import logging
from typing import List, Optional

from ..connector import Connector
from ..transaction import Transaction
from .models import (
    AckActionConfig,
    MessageQueryFilter,
    NylasClient,
    NylasConsumerConfig,
    NylasProducerConfig,
)
from .service import NylasService
from .utils import (
    apply_client_side_filter,
    build_ack_request_body,
    merge_query_filter,
    message_to_transaction,
)

logger = logging.getLogger(__name__)


class NylasConnector(Connector):
    service: NylasService

    def __init__(
        self,
        client: NylasClient,
        consumer_config: Optional[NylasConsumerConfig] = None,
        producer_config: Optional[NylasProducerConfig] = None,
    ) -> None:
        self.service = NylasService(client)
        self._consumer_config = consumer_config
        self._producer_config = producer_config or NylasProducerConfig()

    def _resolve_attachments(self, message: dict) -> list:
        config = self._consumer_config
        if config is None or not config.download_attachments:
            return message.get("attachments") or []

        enriched = []
        for att in message.get("attachments") or []:
            att_id = att.get("id")
            if not att_id:
                enriched.append(att)
                continue
            content = self.service.download_attachment(str(message.get("id", "")), str(att_id))
            enriched.append({**att, "content": content})
        return enriched

    def _fetch_items(
        self,
        query: MessageQueryFilter,
        limit: int,
        client_side_filter=None,
        fetch_unit: str = "message",
    ) -> List[Transaction]:
        if fetch_unit == "thread":
            result = self.service.list_threads(query, limit)
            items = result.get("data") or []
            items = apply_client_side_filter(items, client_side_filter)
            return [
                Transaction(
                    id=str(item.get("id", "")),
                    payload=item,
                    metadata={
                        "grant_id": self.service.grant_id,
                        "thread_id": item.get("id"),
                        "fetch_unit": "thread",
                        "has_attachment": bool(item.get("attachments")),
                        "unread": item.get("unread"),
                    },
                )
                for item in items
            ]

        result = self.service.list_messages(query, limit)
        messages = result.get("data") or []
        messages = apply_client_side_filter(messages, client_side_filter)

        transactions: List[Transaction] = []
        for message in messages:
            attachments_meta = self._resolve_attachments(message)
            transactions.append(
                message_to_transaction(message, self.service.grant_id, attachments_meta=attachments_meta)
            )
        return transactions

    def fetch_transactions(
        self,
        batch_size: Optional[int] = None,
        query_overrides: Optional[MessageQueryFilter] = None,
        *args,
        **kwargs,
    ) -> List[Transaction]:
        if self._consumer_config is None:
            raise ValueError("NylasConnector requires a consumer_config to fetch transactions")

        config = self._consumer_config
        limit = batch_size or config.batch_size
        query = merge_query_filter(config, query_overrides, **kwargs)

        return self._fetch_items(
            query=query,
            limit=limit,
            client_side_filter=config.client_side_filter,
            fetch_unit=config.fetch_unit,
        )

    def dispatch_transactions(self, transactions: List[Transaction], *args, **kwargs) -> List[str]:
        if self._producer_config is None:
            raise ValueError("NylasConnector requires a producer_config to dispatch transactions")

        producer = self._producer_config
        sent_ids: List[str] = []

        for transaction in transactions:
            if producer.send_mode == "draft":
                draft = self.service.create_draft(
                    transaction.payload,
                    default_from=producer.default_from,
                )
                draft_id = str(draft.get("id", ""))
                if not draft_id:
                    raise ValueError("Draft creation did not return an id")
                sent = self.service.send_draft(draft_id)
                sent_ids.append(str(sent.get("id", draft_id)))
            else:
                sent = self.service.send_message(
                    transaction.payload,
                    default_from=producer.default_from,
                )
                sent_ids.append(str(sent.get("id", "")))

        return sent_ids

    def fetch_transaction_by_id(self, transaction_id: str, *args, **kwargs) -> Transaction:
        fields = kwargs.get("fields")
        message = self.service.find_message(transaction_id, fields=fields)
        attachments_meta = self._resolve_attachments(message)
        return message_to_transaction(message, self.service.grant_id, attachments_meta=attachments_meta)

    def get_transactions_count(self, *args, **kwargs) -> int:
        if self._consumer_config is None:
            raise ValueError("NylasConnector requires a consumer_config to count transactions")

        query = merge_query_filter(self._consumer_config, **kwargs)
        max_pages = kwargs.get("max_pages")
        return self.service.get_messages_count(query, max_pages=max_pages)

    def ack_transaction(self, transaction: Transaction, ack_config: Optional[AckActionConfig] = None) -> None:
        config = ack_config
        if config is None and self._consumer_config is not None:
            config = self._consumer_config.ack_config
        if config is None:
            return

        body = build_ack_request_body(config)
        if not body:
            return

        self.service.update_message(transaction.id, body)

    def nack_transaction(self, transaction: Transaction, requeue: bool = True) -> None:
        if not requeue:
            return
        logger.debug("nack_transaction is a no-op for Nylas; message %s remains available for refetch", transaction.id)

    def close(self) -> None:
        self.service.close()
