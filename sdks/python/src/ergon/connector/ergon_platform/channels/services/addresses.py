from typing import Any, Dict, Optional

from ..models import (
    AddressDirection,
    ErgonPlatformChannelsConsumerConfig,
    ResolvedInboxAddress,
)
from .records import SdkRecord


class ChannelsAddressService:
    """Resolve consumer and sender addresses through Channels."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._by_email: Dict[str, ResolvedInboxAddress] = {}
        self._by_id: Dict[str, ResolvedInboxAddress] = {}

    def resolve_consumer_inbox(
        self,
        config: ErgonPlatformChannelsConsumerConfig,
    ) -> ResolvedInboxAddress:
        """Resolve the consumer inbox address."""
        if config.config_id:
            return self.resolve_in_config(config.config_id, config.address)
        return self.resolve_email(config.address)

    def resolve_sender_inbox(
        self,
        address: Optional[str],
        address_id: Optional[str],
        direction: Optional[AddressDirection] = None,
        config_id: Optional[str] = None,
    ) -> ResolvedInboxAddress:
        """Resolve the sender inbox address."""
        if address_id:
            try:
                return self.resolve_id(address_id)
            except ValueError:
                return ResolvedInboxAddress(
                    address=address or address_id,
                    address_id=address_id,
                    config_id=config_id or "",
                    direction=direction or "both",
                )
        if address and config_id:
            try:
                return self.resolve_in_config(
                    config_id,
                    address,
                    direction_override=direction,
                )
            except ValueError:
                pass
        if address:
            return self.resolve_email(address)
        raise ValueError(
            "address is required to send a channel message "
            "(set producer_config.address, consumer_config.address, "
            "or address_id in a dict payload)"
        )

    def resolve_email(self, address_email: str) -> ResolvedInboxAddress:
        """Resolve the email address."""
        cached = self._by_email.get(address_email)
        if cached:
            return cached
        self._load_granted_addresses()
        resolved = self._by_email.get(address_email)
        if not resolved:
            raise ValueError(
                f"Channel address {address_email!r} not found via "
                "channels.addresses (check the API key scope or the address "
                "spelling). Set consumer_config.config_id (console URL UUID) "
                "with the inbox email so the connector can resolve the "
                "address inside that channel config."
            )
        return resolved

    def resolve_id(self, address_id: str) -> ResolvedInboxAddress:
        """Resolve the address id."""
        cached = self._by_id.get(address_id)
        if cached:
            return cached
        self._load_granted_addresses()
        resolved = self._by_id.get(address_id)
        if not resolved:
            raise ValueError(
                f"Channel address id {address_id!r} not found via "
                "channels.addresses (check the API key scope or the address id)."
            )
        return resolved

    def resolve_in_config(
        self,
        config_id: str,
        address_email: str,
        direction_override: Optional[AddressDirection] = None,
        address_id: Optional[str] = None,
    ) -> ResolvedInboxAddress:
        """Resolve the address in the config."""
        if address_id:
            return self._remember(
                ResolvedInboxAddress(
                    address=address_email,
                    address_id=address_id,
                    config_id=config_id,
                    direction=direction_override or "both",
                )
            )

        cached = self._by_email.get(address_email)
        if cached and cached.config_id == config_id:
            return cached

        try:
            page = self._client.channels.configs.addresses(config_id).list(limit=100)
            for entry in SdkRecord.items(
                page,
                keys=["items", "data", "results"],
            ):
                parsed = self._parse_config_entry(
                    entry,
                    config_id=config_id,
                )
                if parsed and parsed.address == address_email:
                    return self._remember(parsed)
        except Exception:
            pass

        return self._remember(
            self._from_config_activity(
                config_id,
                address_email,
                direction_override=direction_override,
            )
        )

    def _remember(
        self,
        resolved: ResolvedInboxAddress,
    ) -> ResolvedInboxAddress:
        """Remember the resolved address."""
        resolved = self._with_config_capabilities(resolved)
        self._by_email[resolved.address] = resolved
        self._by_id[resolved.address_id] = resolved
        return resolved

    def _with_config_capabilities(
        self,
        inbox: ResolvedInboxAddress,
    ) -> ResolvedInboxAddress:
        """With the config capabilities."""
        if not inbox.config_id:
            return inbox
        try:
            verified = self._client.channels.configs.verify(inbox.config_id)
        except Exception:
            return inbox
        record = SdkRecord(verified)
        capabilities = record.get("capabilities") or {}
        if not isinstance(capabilities, dict):
            capabilities = {}
        inbound_enabled = record.get("inbound_enabled")
        return inbox.model_copy(
            update={
                "config_name": record.get("name"),
                "config_can_send": capabilities.get("can_send"),
                "config_can_receive": (inbound_enabled if inbound_enabled is not None else None),
            }
        )

    def _from_config_activity(
        self,
        config_id: str,
        address_email: str,
        direction_override: Optional[AddressDirection] = None,
    ) -> ResolvedInboxAddress:
        """From the config activity."""
        normalized = address_email.strip().lower()
        created_response = self._client.channels.configs.activity(
            config_id,
            limit=50,
            page=1,
            event_type="channels.address.created",
        )
        for item in SdkRecord.items(
            created_response,
            keys=["items", "data", "results"],
        ):
            resolved = self._match_activity_item(
                config_id,
                address_email,
                normalized,
                item,
                direction_override=direction_override,
            )
            if resolved is not None:
                return resolved

        limit = 50
        for page in range(1, 6):
            response = self._client.channels.configs.activity(
                config_id,
                limit=limit,
                page=page,
            )
            items = SdkRecord.items(
                response,
                keys=["items", "data", "results"],
            )
            if not items:
                break
            for item in items:
                resolved = self._match_activity_item(
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

        raise ValueError(
            f"Channel address {address_email!r} not found under config "
            f"{config_id!r}. Check the inbox email and CHANNELS_CONFIG_ID "
            "from the console URL."
        )

    def _match_activity_item(
        self,
        config_id: str,
        address_email: str,
        normalized_email: str,
        item: Any,
        direction_override: Optional[AddressDirection] = None,
    ) -> Optional[ResolvedInboxAddress]:
        """Match the activity item."""
        record = SdkRecord(item)
        summary = str(record.get("summary") or "").strip().lower()
        event_type = str(record.get("event_type") or "")
        if event_type != "channels.address.created" and summary != normalized_email:
            return None

        event_id = record.get("id")
        if not event_id:
            return None

        detail = self._client.channels.configs.activity_event(
            config_id,
            str(event_id),
        )
        payload = SdkRecord(detail).get("payload") or {}
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

    def _load_granted_addresses(self) -> None:
        """Load the granted addresses."""
        if self._by_email:
            return
        for entry in SdkRecord.items(
            self._client.channels.addresses(),
            keys=["items", "data", "results"],
        ):
            parsed = self._parse_granted_entry(entry)
            if parsed is not None:
                self._remember(parsed)

    @classmethod
    def _parse_config_entry(
        cls,
        entry: Any,
        config_id: str,
    ) -> Optional[ResolvedInboxAddress]:
        """Parse the config entry."""
        record = SdkRecord(entry)
        entry_email = record.get("address")
        entry_id = record.get("id") or record.get("channel_address_id")
        if not entry_email or not entry_id:
            return None
        return ResolvedInboxAddress(
            address=str(entry_email),
            address_id=str(entry_id),
            config_id=config_id,
            direction=ResolvedInboxAddress.parse_direction(record.get("direction")),
        )

    @staticmethod
    def _parse_granted_entry(
        entry: Any,
    ) -> Optional[ResolvedInboxAddress]:
        """Parse the granted entry."""
        record = SdkRecord(entry)
        entry_email = record.get("address")
        entry_id = record.get("id")
        entry_config = record.get("channel_config_id")
        if not entry_email or not entry_id or not entry_config:
            return None
        return ResolvedInboxAddress(
            address=str(entry_email),
            address_id=str(entry_id),
            config_id=str(entry_config),
            direction=ResolvedInboxAddress.parse_direction(record.get("direction")),
        )
