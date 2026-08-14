import logging
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Union

from ...transaction import Transaction
from ..models import ErgonPlatformClient
from .models import (
    AddressDirection,
    ErgonPlatformChannelsConsumerConfig,
    InboxAttachmentFile,
    ResolvedInboxAddress,
)
from .utils import (
    event_to_transaction,
    extract_items,
    extract_total,
    get_value,
    inbox_attachment_id,
    inbox_attachments,
)

logger = logging.getLogger(__name__)


class _ErgonPlatformChannelsOperations:
    """Private domain helpers wrapping ``ErgonClient.channels`` calls."""

    def __init__(self, config: ErgonPlatformClient, client: Any) -> None:
        self.config = config
        self.client = client
        self._address_cache_by_email: Dict[str, ResolvedInboxAddress] = {}
        self._address_cache_by_id: Dict[str, ResolvedInboxAddress] = {}

    def resolve_address(self, address_email: str) -> ResolvedInboxAddress:
        """Resolve an address by email."""
        cached = self._address_cache_by_email.get(address_email)
        if cached:
            return cached
        self._materialize_address_cache()
        resolved = self._address_cache_by_email.get(address_email)
        if not resolved:
            raise ValueError(
                f"Channel address {address_email!r} not found via channels.addresses "
                "(check the API key scope or the address spelling). "
                "Set consumer_config.config_id (console URL UUID) with the inbox email "
                "so the connector can resolve the address inside that channel config."
            )
        return resolved

    def _cache_resolved(self, resolved: ResolvedInboxAddress) -> ResolvedInboxAddress:
        """Cache a resolved address."""
        resolved = self._enrich_inbox_from_config(resolved)
        self._address_cache_by_email[resolved.address] = resolved
        self._address_cache_by_id[resolved.address_id] = resolved
        return resolved

    def _enrich_inbox_from_config(self, inbox: ResolvedInboxAddress) -> ResolvedInboxAddress:
        """Merge channel config capabilities (verify) onto a resolved inbox."""
        if not inbox.config_id:
            return inbox
        try:
            verified = self.client.channels.configs.verify(inbox.config_id)
        except Exception:
            return inbox
        capabilities = get_value(verified, "capabilities") or {}
        if not isinstance(capabilities, dict):
            capabilities = {}
        inbound_enabled = get_value(verified, "inbound_enabled")
        return inbox.model_copy(
            update={
                "config_name": get_value(verified, "name"),
                "config_can_send": capabilities.get("can_send"),
                "config_can_receive": inbound_enabled if inbound_enabled is not None else None,
            }
        )

    def resolve_address_in_config(
        self,
        config_id: str,
        address_email: str,
        direction_override: Optional[AddressDirection] = None,
        address_id: Optional[str] = None,
    ) -> ResolvedInboxAddress:
        """Resolve ``(address_id, direction)`` from a channel config + inbox email."""
        if address_id:
            return self._cache_resolved(
                ResolvedInboxAddress(
                    address=address_email,
                    address_id=address_id,
                    config_id=config_id,
                    direction=direction_override or "both",
                )
            )

        cached = self._address_cache_by_email.get(address_email)
        if cached and cached.config_id == config_id:
            return cached

        try:
            page = self.client.channels.configs.addresses(config_id).list(limit=100)
            for entry in extract_items(page, keys=["items", "data", "results"]):
                parsed = self._parse_config_address_entry(entry, config_id=config_id)
                if parsed and parsed.address == address_email:
                    return self._cache_resolved(parsed)
        except Exception:
            pass

        return self._cache_resolved(
            self._resolve_address_from_config_activity(
                config_id,
                address_email,
                direction_override=direction_override,
            )
        )

    def _resolve_address_from_config_activity(
        self,
        config_id: str,
        address_email: str,
        direction_override: Optional[AddressDirection] = None,
    ) -> ResolvedInboxAddress:
        """Resolve an address from config activity (address.created fallback)."""
        normalized = address_email.strip().lower()

        created_response = self.client.channels.configs.activity(
            config_id,
            limit=50,
            offset=0,
            event_type="channels.address.created",
            include_acked=True,
        )
        for item in extract_items(created_response, keys=["items", "data", "results"]):
            resolved = self._match_address_in_activity_item(
                config_id,
                address_email,
                normalized,
                item,
                direction_override=direction_override,
            )
            if resolved is not None:
                return resolved

        limit = 50
        offset = 0
        for _ in range(5):
            response = self.client.channels.configs.activity(
                config_id,
                limit=limit,
                offset=offset,
                include_acked=True,
            )
            items = extract_items(response, keys=["items", "data", "results"])
            if not items:
                break
            for item in items:
                resolved = self._match_address_in_activity_item(
                    config_id,
                    address_email,
                    normalized,
                    item,
                    direction_override=direction_override,
                )
                if resolved is not None:
                    return resolved
            if len(items) < limit:
                break
            offset += limit

        raise ValueError(
            f"Channel address {address_email!r} not found under config {config_id!r}. "
            "Check the inbox email and CHANNELS_CONFIG_ID from the console URL."
        )

    def _match_address_in_activity_item(
        self,
        config_id: str,
        address_email: str,
        normalized_email: str,
        item: Any,
        direction_override: Optional[AddressDirection] = None,
    ) -> Optional[ResolvedInboxAddress]:
        """Match an address in an activity item."""
        summary = str(get_value(item, "summary") or "").strip().lower()
        event_type = str(get_value(item, "event_type") or "")
        if event_type != "channels.address.created" and summary != normalized_email:
            return None

        event_id = get_value(item, "id")
        if not event_id:
            return None

        detail = self.client.channels.configs.activity_event(config_id, str(event_id))
        payload = get_value(detail, "payload") or {}
        if not isinstance(payload, dict):
            return None

        payload_address = str(payload.get("address") or summary or "").strip().lower()
        if payload_address and payload_address != normalized_email:
            return None

        address_id = payload.get("channel_address_id") or payload.get("address_id") or payload.get("id")
        if not address_id and summary == normalized_email and "address.created" in event_type:
            address_id = payload.get("channel_address_id")
        if not address_id:
            return None

        direction = direction_override or ResolvedInboxAddress.parse_direction(payload.get("direction"))
        return ResolvedInboxAddress(
            address=address_email,
            address_id=str(address_id),
            config_id=config_id,
            direction=direction,
        )

    def resolve_consumer_inbox(self, config: ErgonPlatformChannelsConsumerConfig) -> ResolvedInboxAddress:
        """Resolve a consumer inbox address."""
        if config.config_id:
            return self.resolve_address_in_config(config.config_id, config.address)
        return self.resolve_address(config.address)

    def resolve_sender_inbox(
        self,
        address: Optional[str],
        address_id: Optional[str],
        direction: Optional[AddressDirection] = None,
        config_id: Optional[str] = None,
    ) -> ResolvedInboxAddress:
        """Resolve a sender inbox address."""
        if address_id:
            try:
                return self.resolve_address_by_id(address_id)
            except ValueError:
                return ResolvedInboxAddress(
                    address=address or address_id,
                    address_id=address_id,
                    config_id=config_id or "",
                    direction=direction or "both",
                )
        if address and config_id:
            try:
                return self.resolve_address_in_config(
                    config_id,
                    address,
                    direction_override=direction,
                )
            except ValueError:
                pass
        if address:
            return self.resolve_address(address)
        raise ValueError(
            "address is required to send a channel message "
            "(set producer_config.address, consumer_config.address, or address_id in a dict payload)"
        )

    def resolve_address_by_id(self, address_id: str) -> ResolvedInboxAddress:
        """Resolve an address by its ID."""
        cached = self._address_cache_by_id.get(address_id)
        if cached:
            return cached
        self._materialize_address_cache()
        resolved = self._address_cache_by_id.get(address_id)
        if not resolved:
            raise ValueError(
                f"Channel address id {address_id!r} not found via channels.addresses "
                "(check the API key scope or the address id)."
            )
        return resolved

    def _materialize_address_cache(self) -> None:
        """Materialize the address cache from the channels.addresses response."""
        if self._address_cache_by_email:
            return
        for entry in extract_items(self.list_addresses(), keys=["items", "data", "results"]):
            parsed = self._parse_address_entry(entry)
            if parsed is None:
                continue
            self._cache_resolved(parsed)

    @staticmethod
    def _parse_config_address_entry(entry: Any, *, config_id: str) -> Optional[ResolvedInboxAddress]:
        """Parse an address entry from the channels.configs.addresses response."""
        entry_email = get_value(entry, "address")
        entry_id = get_value(entry, "id") or get_value(entry, "channel_address_id")
        if not entry_email or not entry_id:
            return None
        return ResolvedInboxAddress(
            address=str(entry_email),
            address_id=str(entry_id),
            config_id=config_id,
            direction=ResolvedInboxAddress.parse_direction(get_value(entry, "direction")),
        )

    @staticmethod
    def _parse_address_entry(entry: Any) -> Optional[ResolvedInboxAddress]:
        """Parse an address entry from the channels.addresses response."""
        entry_email = get_value(entry, "address")
        entry_id = get_value(entry, "id")
        entry_cfg = get_value(entry, "channel_config_id")
        if not entry_email or not entry_id or not entry_cfg:
            return None
        return ResolvedInboxAddress(
            address=str(entry_email),
            address_id=str(entry_id),
            config_id=str(entry_cfg),
            direction=ResolvedInboxAddress.parse_direction(get_value(entry, "direction")),
        )

    def list_addresses(self, **params: Any) -> Any:
        """List all addresses for a company."""
        return self.client.channels.addresses(**params)

    def fetch_thread_messages(
        self,
        thread_id: str,
        limit: Optional[int] = None,
        offset: int = 0,
        **params: Any,
    ) -> List[Transaction]:
        """Fetch messages from a thread."""
        query = dict(params)
        if limit is not None:
            query.setdefault("limit", limit)
        query.setdefault("offset", offset)
        response = self.client.channels.thread_messages(thread_id, **query)
        messages = extract_items(response, keys=["messages", "items", "data"])
        return [event_to_transaction(message, source="thread") for message in messages]

    def fetch_activity_events(
        self,
        company_id: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        **params: Any,
    ) -> List[Transaction]:
        """Fetch activity events for a company."""
        query = dict(params)
        if limit is not None:
            query.setdefault("limit", limit)
        query.setdefault("offset", offset)
        response = self.client.channels.company_activity(company_id=company_id, **query)
        events = extract_items(response, keys=["items", "data", "results"])
        return [event_to_transaction(event, source="activity") for event in events]

    def fetch_inbox_events(
        self,
        config_id: str,
        address_id: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        **params: Any,
    ) -> List[Transaction]:
        """Fetch inbox activity events for a channel config."""
        query = dict(params)
        if address_id:
            query.setdefault("address_id", address_id)
        if limit is not None:
            query.setdefault("limit", limit)
        query.setdefault("offset", offset)
        response = self.client.channels.configs.activity(config_id, **query)
        events = extract_items(response, keys=["items", "data", "results"])
        return [event_to_transaction(event, source="config_activity") for event in events]

    def get_activity_count(self, *, company_id: Optional[str] = None, **params: Any) -> int:
        """Get the total number of activity events for a company."""
        query = {**params, "limit": 1, "offset": 0}
        response = self.client.channels.company_activity(company_id=company_id, **query)
        return extract_total(response)

    def get_inbox_events_count(
        self,
        config_id: str,
        address_id: Optional[str] = None,
        **params: Any,
    ) -> int:
        """Get the total number of inbox activity events for a channel config."""
        query = {**params, "limit": 1, "offset": 0}
        if address_id:
            query["address_id"] = address_id
        response = self.client.channels.configs.activity(config_id, **query)
        return extract_total(response)

    def get_activity_event(
        self,
        event_id: str,
        company_id: Optional[str] = None,
        **params: Any,
    ) -> Transaction:
        """Get an activity event for a company."""
        event = self.client.channels.company_activity_event(
            company_id=company_id,
            event_id=event_id,
            **params,
        )
        return event_to_transaction(event, source="activity")

    def get_inbox_event(self, config_id: str, event_id: str, **params: Any) -> Transaction:
        """Get an inbox activity event for a channel config."""
        event = self.client.channels.configs.activity_event(config_id, event_id, **params)
        return event_to_transaction(event, source="config_activity")

    def download_inbox_attachment(self, config_id: str, event_id: str, attachment_id: str) -> bytes:
        """Download an inbox attachment."""
        return self.client.channels.configs.activity_attachment_file(config_id, event_id, attachment_id)

    def download_inbox_attachments(
        self,
        config_id: str,
        transaction: Transaction,
        dest: Optional[Union[str, Path]] = None,
    ) -> List[InboxAttachmentFile]:
        """Download every inbound attachment on *transaction*, optionally writing to *dest*."""
        event_id = transaction.id
        if not event_id:
            raise ValueError("transaction.id is required to download attachments")

        dest_dir: Optional[Path] = None
        if dest is not None:
            dest_dir = Path(dest) / str(event_id)
            dest_dir.mkdir(parents=True, exist_ok=True)

        downloaded: List[InboxAttachmentFile] = []
        for meta in inbox_attachments(transaction):
            attachment_id = inbox_attachment_id(meta)
            if not attachment_id:
                continue
            filename = PurePosixPath(str(meta.get("filename") or attachment_id)).name or attachment_id
            logger.info("Downloading attachment %s (%s)", filename, attachment_id)
            content = self.download_inbox_attachment(config_id, event_id, attachment_id)
            path: Optional[str] = None
            if dest_dir is not None:
                file_path = dest_dir / filename
                file_path.write_bytes(content)
                path = str(file_path)
            downloaded.append(
                InboxAttachmentFile(
                    attachment_id=attachment_id,
                    filename=filename,
                    content=content,
                    content_type=meta.get("content_type"),
                    path=path,
                )
            )
        return downloaded

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
        """Send a message to a channel."""
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
    def send_response_id(result: Any) -> str:
        """Extract the response ID from a send response."""
        for key in ("log_id", "provider_message_id", "thread_id", "provider_id", "id"):
            value = get_value(result, key)
            if value:
                return str(value)
        return ""
