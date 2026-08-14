from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ...transaction import Transaction
from ..models import ErgonPlatformClient
from ._activity import ActivityAdapter
from ._addresses import InboxAddressBook
from ._attachments import InboxAttachments
from ._outbound import OutboundMessage
from ._sdk import SdkRecord
from .models import (
    AddressDirection,
    ErgonPlatformChannelsConsumerConfig,
    InboxAttachmentFile,
    ResolvedInboxAddress,
    SendMessagePayload,
)


class _ErgonPlatformChannelsOperations:
    """Thin facade: HTTP to the Channels SDK, delegated to domain objects."""

    def __init__(
        self,
        config: ErgonPlatformClient,
        client: Any,
        *,
        download_client: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.client = client
        self._download_client = download_client
        self._addresses = InboxAddressBook(client)
        self._attachments = InboxAttachments(client, download_client=download_client)

    def close(self) -> None:
        download = self._download_client
        if download is not None and download is not self.client and hasattr(download, "close"):
            download.close()

    def resolve_consumer_inbox(self, config: ErgonPlatformChannelsConsumerConfig) -> ResolvedInboxAddress:
        """Resolve a consumer inbox."""
        return self._addresses.resolve_consumer(config)

    def resolve_sender_inbox(
        self,
        address: Optional[str],
        address_id: Optional[str],
        direction: Optional[AddressDirection] = None,
        config_id: Optional[str] = None,
    ) -> ResolvedInboxAddress:
        """Resolve a sender inbox."""
        return self._addresses.resolve_sender(
            address=address,
            address_id=address_id,
            direction=direction,
            config_id=config_id,
        )

    def fetch_thread_messages(
        self,
        thread_id: str,
        limit: Optional[int] = None,
        offset: int = 0,
        **params: Any,
    ) -> List[Transaction]:
        """Fetch thread messages."""
        response = self.client.channels.thread_messages(
            thread_id, **self._page_query(params, limit=limit, offset=offset)
        )
        messages = SdkRecord.items(response, keys=["messages", "items", "data"])
        return [ActivityAdapter.to_transaction(message, source="thread") for message in messages]

    def fetch_activity_events(
        self,
        company_id: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        **params: Any,
    ) -> List[Transaction]:
        """Fetch activity events."""
        response = self.client.channels.company_activity(
            company_id=company_id, **self._page_query(params, limit=limit, offset=offset)
        )
        events = SdkRecord.items(response, keys=["items", "data", "results"])
        return [ActivityAdapter.to_transaction(event, source="activity") for event in events]

    def fetch_inbox_events(
        self,
        config_id: str,
        address_id: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        **params: Any,
    ) -> List[Transaction]:
        response = self.client.channels.configs.activity(
            config_id,
            **self._page_query(params, limit=limit, offset=offset, address_id=address_id),
        )
        """Fetch inbox events."""
        events = SdkRecord.items(response, keys=["items", "data", "results"])
        return [ActivityAdapter.to_transaction(event, source="config_activity") for event in events]

    def get_activity_count(self, *, company_id: Optional[str] = None, **params: Any) -> int:
        """Get the number of activity events."""
        query = {**params, "limit": 1, "offset": 0}
        response = self.client.channels.company_activity(company_id=company_id, **query)
        return SdkRecord.total(response)

    def get_inbox_events_count(
        self,
        config_id: str,
        address_id: Optional[str] = None,
        **params: Any,
    ) -> int:
        """Get the number of inbox events."""
        query = {**params, "limit": 1, "offset": 0}
        if address_id:
            query["address_id"] = address_id
        response = self.client.channels.configs.activity(config_id, **query)
        return SdkRecord.total(response)

    def get_activity_event(
        self,
        event_id: str,
        company_id: Optional[str] = None,
        **params: Any,
    ) -> Transaction:
        """Get an activity event."""
        event = self.client.channels.company_activity_event(
            company_id=company_id,
            event_id=event_id,
            **params,
        )
        return ActivityAdapter.to_transaction(event, source="activity")

    def get_inbox_event(self, config_id: str, event_id: str, **params: Any) -> Transaction:
        """Get an inbox event."""
        event = self.client.channels.configs.activity_event(config_id, event_id, **params)
        return ActivityAdapter.to_transaction(event, source="config_activity")

    def finalize_fetched_transactions(
        self,
        transactions: List[Transaction],
        config: ErgonPlatformChannelsConsumerConfig,
        seen_ids: Optional[set[str]] = None,
    ) -> List[Transaction]:
        """Finalize the fetched transactions."""
        return ActivityAdapter.finalize_fetch(transactions, config, seen_ids=seen_ids)

    def hydrate_inbox_attachments(self, config_id: str, transaction: Transaction) -> Transaction:
        """Hydrate a transaction with attachment bytes."""
        return self._attachments.hydrate(config_id, transaction)

    def download_inbox_attachments(
        self,
        config_id: str,
        transaction: Transaction,
        dest: Optional[Union[str, Path]] = None,
    ) -> List[InboxAttachmentFile]:
        """Download all attachments from a transaction."""
        return self._attachments.download_all(config_id, transaction, dest=dest)

    def ack_inbox_event(self, config_id: str, event_id: str) -> Any:
        """Acknowledge an inbox event."""
        return self.client.channels.configs.activity_ack(config_id, event_id)

    def nack_inbox_event(
        self,
        config_id: str,
        event_id: str,
        requeue: bool = True,
        delay_seconds: int = 0,
    ) -> Any:
        """Nack an inbox event."""
        return self.client.channels.configs.activity_nack(
            config_id,
            event_id,
            requeue=requeue,
            delay_seconds=delay_seconds,
        )

    def send_message(
        self,
        address_id: str,
        channel: str,
        config: Dict[str, Any],
        resource_id: Optional[str] = None,
        service_name: Optional[str] = None,
    ) -> Any:
        """Send a channel message."""
        if not address_id:
            raise ValueError("address_id is required to send a channel message")
        if not channel:
            raise ValueError("channel is required to send a channel message")
        request: Dict[str, Any] = {
            "address_id": address_id,
            "channel": channel,
            "config": config,
        }
        if resource_id is not None:
            request["resource_id"] = resource_id
        if service_name is not None:
            request["service_name"] = service_name
        return self.client.channels.send(request)

    @staticmethod
    def _page_query(
        params: Dict[str, Any],
        limit: Optional[int],
        offset: int,
        **extra: Any,
    ) -> Dict[str, Any]:
        query = {k: v for k, v in extra.items() if v is not None}
        query.update(params)
        if limit is not None:
            query.setdefault("limit", limit)
        query.setdefault("offset", offset)
        return query

    @staticmethod
    def normalize_recipients(to: Union[str, List[str]]) -> List[str]:
        """Normalize recipients."""
        return OutboundMessage.normalize_recipients(to)

    @staticmethod
    def normalize_send_payload(payload: SendMessagePayload) -> Dict[str, Any]:
        """Normalize a send payload."""
        return OutboundMessage.normalize(payload)

    @staticmethod
    def send_response_id(result: Any) -> str:
        """Get the response id from a send result."""
        return OutboundMessage.response_id(result)
