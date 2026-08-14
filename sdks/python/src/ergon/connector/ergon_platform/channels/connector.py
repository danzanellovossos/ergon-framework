import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ...connector import Connector
from ...transaction import Transaction
from .._client import create_ergon_client
from ..models import ErgonPlatformClient
from ._operations import _ErgonPlatformChannelsOperations
from .models import (
    DEFAULT_CHANNEL,
    ErgonPlatformChannelsConfig,
    ErgonPlatformChannelsConsumerConfig,
    ErgonPlatformChannelsProducerConfig,
    InboxAttachmentFile,
    ResolvedInboxAddress,
    SendMessageAttachment,
    SendMessageInput,
    SendMessagePayload,
)
from .utils import (
    _normalize_recipients,
    deliver_fetched_transactions,
    filter_activity_transactions,
    normalize_send_payload,
)

logger = logging.getLogger(__name__)


class ErgonPlatformChannelsConnector(Connector):
    """Sync connector for one Ergon Platform channels inbox."""

    def __init__(
        self,
        client: ErgonPlatformClient,
        channels_config: Optional[ErgonPlatformChannelsConfig] = None,
        consumer_config: Optional[ErgonPlatformChannelsConsumerConfig] = None,
        producer_config: Optional[ErgonPlatformChannelsProducerConfig] = None,
    ) -> None:
        if channels_config is not None:
            consumer_config = consumer_config or channels_config.to_consumer_config()
            producer_config = producer_config or channels_config.to_producer_config()
        self._consumer_config = consumer_config
        self._producer_config = producer_config or ErgonPlatformChannelsProducerConfig()
        self.client = create_ergon_client(client)
        self._operations = _ErgonPlatformChannelsOperations(client, self.client)
        self._seen_event_ids: set[str] = set()

    def fetch_transactions(
        self,
        batch_size: Optional[int] = None,
        *args,
        **kwargs,
    ) -> List[Transaction]:
        config = self._require_consumer_config("fetch transactions")
        inbox = self._resolve_inbox(config)
        inbox.ensure_can_receive()
        limit = batch_size or config.batch_size
        params: Dict[str, Any] = {**config.activity_query_params(), **kwargs}
        transactions = self._operations.fetch_inbox_events(
            inbox.config_id,
            address_id=inbox.address_id,
            limit=limit,
            offset=config.offset,
            **params,
        )
        return self._finalize_fetched_transactions(config, transactions)

    def _finalize_fetched_transactions(
        self,
        config: ErgonPlatformChannelsConsumerConfig,
        transactions: List[Transaction],
    ) -> List[Transaction]:
        """Finalize fetched transactions by applying activity filters and deduplication."""
        transactions = filter_activity_transactions(transactions, config.effective_activity_filter())
        if config.deduplicate_fetched_events:
            transactions = deliver_fetched_transactions(transactions, self._seen_event_ids)
        return transactions

    def fetch_transaction_by_id(self, transaction_id: str, *args, **kwargs) -> Transaction:
        config = self._require_consumer_config("fetch a transaction by id")
        inbox = self._resolve_inbox(config)
        inbox.ensure_can_receive()
        return self._operations.get_inbox_event(inbox.config_id, transaction_id, **kwargs)

    def get_transactions_count(self, *args, **kwargs) -> int:
        config = self._require_consumer_config("count transactions")
        inbox = self._resolve_inbox(config)
        inbox.ensure_can_receive()
        params: Dict[str, Any] = {**config.activity_query_params(), **kwargs}
        return self._operations.get_inbox_events_count(
            inbox.config_id,
            address_id=inbox.address_id,
            **params,
        )

    def dispatch_transactions(self, transactions: List[Transaction], *args, **kwargs) -> List[str]:
        sent_ids: List[str] = []
        for transaction in transactions:
            result = self._send_from_payload(transaction.payload)
            sent_ids.append(self._operations.send_response_id(result))
        return sent_ids

    def send_message(self, payload: SendMessagePayload) -> Any:
        return self._send_from_payload(payload)

    def send_email(
        self,
        to: Union[str, List[str]],
        subject: str,
        text: Optional[str] = None,
        html: Optional[str] = None,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        reply_to: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        attachments: Optional[List[SendMessageAttachment]] = None,
    ) -> str:
        """Send one email and return the platform log/message id."""
        payload = SendMessageInput(
            to=_normalize_recipients(to),
            subject=subject,
            text=text,
            html=html,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            in_reply_to=in_reply_to,
            attachments=attachments,
        )
        result = self._send_from_payload(payload)
        return self._operations.send_response_id(result)

    def list_thread_messages(self, thread_id: str, **params: Any) -> List[Transaction]:
        """Read messages of a specific thread (outside the normal consumer loop)."""
        return self._operations.fetch_thread_messages(thread_id, **params)

    def list_company_activity(self, **params: Any) -> List[Transaction]:
        """Read the company-wide channels activity feed (admin/reporting)."""
        return self._operations.fetch_activity_events(**params)

    def resolve_inbox(self) -> ResolvedInboxAddress:
        """Resolve ``address`` (+ optional ``config_id``) to platform routing metadata."""
        config = self._require_consumer_config("resolve inbox")
        return self._resolve_inbox(config)

    def resolve_address_info(self, *, address: Optional[str] = None) -> Dict[str, Any]:
        """Return resolved inbox metadata including receive/send capabilities."""
        config = self._require_consumer_config("resolve address info")
        if address and address != config.address:
            config = config.model_copy(update={"address": address})
        return self._resolve_inbox(config).as_info_dict()

    def ack_transaction(self, transaction: Transaction) -> None:
        """Acknowledge a processed inbox event on the platform."""
        config = self._require_consumer_config("ack a transaction")
        inbox = self._resolve_inbox(config)
        self._operations.ack_inbox_event(inbox.config_id, transaction.id)
        if transaction.id:
            self._seen_event_ids.add(transaction.id)

    def nack_transaction(
        self,
        transaction: Transaction,
        requeue: bool = True,
        delay_seconds: Optional[int] = None,
    ) -> None:
        """Requeue or fail an inbox event on the platform."""
        config = self._require_consumer_config("nack a transaction")
        inbox = self._resolve_inbox(config)
        delay = config.nack_delay_seconds if delay_seconds is None else delay_seconds
        self._operations.nack_inbox_event(
            inbox.config_id,
            transaction.id,
            requeue=requeue,
            delay_seconds=delay,
        )
        if requeue and transaction.id:
            self._seen_event_ids.discard(transaction.id)

    def download_attachments(
        self,
        transaction: Transaction,
        dest: Optional[Union[str, Path]] = None,
    ) -> List[InboxAttachmentFile]:
        """Download inbound attachments from a fetched inbox event.

        Attachment ids and filenames come from the transaction payload. Pass
        ``dest`` to write ``{dest}/{event_id}/{filename}`` and fill ``path``.
        """
        config = self._require_consumer_config("download attachments")
        inbox = self._resolve_inbox(config)
        inbox.ensure_can_receive()
        return self._operations.download_inbox_attachments(
            inbox.config_id, transaction, dest=dest
        )

    def close(self) -> None:
        self.client.close()

    def _require_consumer_config(self, action: str) -> ErgonPlatformChannelsConsumerConfig:
        if self._consumer_config is None:
            raise ValueError(f"ErgonPlatformChannelsConnector requires a consumer_config to {action}")
        return self._consumer_config

    def _resolve_inbox(self, config: ErgonPlatformChannelsConsumerConfig) -> ResolvedInboxAddress:
        return self._operations.resolve_consumer_inbox(config)

    def _resolve_send_inbox(
        self,
        *,
        address_id: Optional[str],
        address: Optional[str],
    ) -> ResolvedInboxAddress:
        return self._operations.resolve_sender_inbox(
            address=address,
            address_id=address_id,
            config_id=(
                self._producer_config.config_id
                or (self._consumer_config.config_id if self._consumer_config else None)
            ),
        )

    def _default_inbox_address(self) -> Optional[str]:
        if self._consumer_config and self._consumer_config.address:
            return self._consumer_config.address
        return self._producer_config.address

    def _send_from_payload(self, payload: SendMessagePayload) -> Any:
        parts = normalize_send_payload(payload)
        top = parts["top"]
        config: Dict[str, Any] = parts["config"]
        producer = self._producer_config

        send_address = producer.address or self._default_inbox_address()
        inbox = self._resolve_send_inbox(
            address_id=top.get("address_id"),
            address=send_address if top.get("address_id") is None else None,
        )
        inbox.ensure_can_send()

        channel = top.get("channel") or DEFAULT_CHANNEL
        resource_id = top.get("resource_id")
        service_name = top.get("service_name") or producer.service_name

        if producer.default_reply_to and not config.get("reply_to"):
            config["reply_to"] = producer.default_reply_to

        return self._operations.send_message(
            address_id=inbox.address_id,
            channel=channel,
            config=config,
            resource_id=resource_id,
            service_name=service_name,
        )
