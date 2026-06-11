import logging
from typing import List, Optional

from ..connector import AsyncConnector
from ..transaction import Transaction
from .async_service import AsyncNylasService
from .models import (
    AckActionConfig,
    MessageQueryFilter,
    NylasClient,
    NylasConsumerConfig,
    NylasProducerConfig,
)
from .utils import build_ack_request_body, merge_query_filter

logger = logging.getLogger(__name__)


class AsyncNylasConnector(AsyncConnector):
    service: AsyncNylasService

    def __init__(
        self,
        client: NylasClient,
        consumer_config: Optional[NylasConsumerConfig] = None,
        producer_config: Optional[NylasProducerConfig] = None,
    ) -> None:
        self.service = AsyncNylasService(client)
        self._consumer_config = consumer_config
        self._producer_config = producer_config or NylasProducerConfig()

    async def fetch_transactions_async(
        self,
        batch_size: Optional[int] = None,
        query_overrides: Optional[MessageQueryFilter] = None,
        *args,
        **kwargs,
    ) -> List[Transaction]:
        if self._consumer_config is None:
            raise ValueError("AsyncNylasConnector requires a consumer_config to fetch transactions")

        config = self._consumer_config
        limit = batch_size or config.batch_size
        query = merge_query_filter(config, query_overrides, **kwargs)

        return await self.service.fetch_items(
            query=query,
            limit=limit,
            client_side_filter=config.client_side_filter,
            fetch_unit=config.fetch_unit,
            download_attachments=config.download_attachments,
        )

    async def dispatch_transactions_async(
        self,
        transactions: List[Transaction],
        *args,
        **kwargs,
    ) -> List[str]:
        if self._producer_config is None:
            raise ValueError("AsyncNylasConnector requires a producer_config to dispatch transactions")

        producer = self._producer_config
        sent_ids: List[str] = []

        for transaction in transactions:
            if producer.send_mode == "draft":
                draft = await self.service.create_draft(
                    transaction.payload,
                    default_from=producer.default_from,
                )
                draft_id = str(draft.get("id", ""))
                if not draft_id:
                    raise ValueError("Draft creation did not return an id")
                sent = await self.service.send_draft(draft_id)
                sent_ids.append(str(sent.get("id", draft_id)))
            else:
                sent = await self.service.send_message(
                    transaction.payload,
                    default_from=producer.default_from,
                )
                sent_ids.append(str(sent.get("id", "")))

        return sent_ids

    async def fetch_transaction_by_id_async(
        self,
        transaction_id: str,
        *args,
        **kwargs,
    ) -> Transaction:
        fields = kwargs.get("fields")
        download = self._consumer_config.download_attachments if self._consumer_config else False
        return await self.service.find_message_transaction(
            transaction_id,
            fields=fields,
            download_attachments=download,
        )

    async def get_transactions_count_async(self, *args, **kwargs) -> int:
        if self._consumer_config is None:
            raise ValueError("AsyncNylasConnector requires a consumer_config to count transactions")

        query = merge_query_filter(self._consumer_config, **kwargs)
        max_pages = kwargs.get("max_pages")
        return await self.service.get_messages_count(query, max_pages=max_pages)

    async def ack_transaction(self, transaction: Transaction, ack_config: Optional[AckActionConfig] = None) -> None:
        config = ack_config
        if config is None and self._consumer_config is not None:
            config = self._consumer_config.ack_config
        if config is None:
            return

        body = build_ack_request_body(config)
        if not body:
            return

        await self.service.update_message(transaction.id, body)

    async def nack_transaction(self, transaction: Transaction, requeue: bool = True) -> None:
        if not requeue:
            return
        logger.debug("nack_transaction is a no-op for Nylas; message %s remains available for refetch", transaction.id)

    async def close(self) -> None:
        await self.service.close()
