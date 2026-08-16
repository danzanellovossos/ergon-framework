import json
import unicodedata
import uuid
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

from ...transaction import Transaction

ChannelName = Literal["email"]
DEFAULT_CHANNEL: ChannelName = "email"
AddressDirection = Literal["send", "receive", "both"]

INBOUND_RECEIVED_EVENT_TYPE = "channels.email.received"
_SUBSCRIPTION_NAMESPACE = uuid.UUID("a62613cf-d5f8-4d46-b69d-d120766942eb")


class ChannelsActivityFilter(BaseModel):
    """Filter inbox activity events by type and metadata.

    Claim responses contain complete events, so filters are evaluated before a
    transaction is handed to the task. Non-matches are acknowledged only for
    this subscription, preventing them from starving later matching events.
    """

    received_only: bool = Field(
        default=True,
        description=(
            f"When ``True`` (default), fetch inbound emails only (``event_type={INBOUND_RECEIVED_EVENT_TYPE}``)."
        ),
    )
    correlation_id: Optional[str] = Field(
        default=None,
        description="Filter by platform correlation id.",
    )
    thread_id: Optional[str] = Field(
        default=None,
        description="Filter by thread id.",
    )
    from_address: Optional[str] = Field(
        default=None,
        description="Match sender email (client-side, case-insensitive).",
    )
    subject_contains: Optional[str] = Field(
        default=None,
        description="Substring match on subject (client-side; case and accent insensitive).",
    )

    @property
    def has_client_side_filters(self) -> bool:
        return self.from_address is not None or self.subject_contains is not None

    def activity_query_params(self) -> Dict[str, Any]:
        """Build server-side activity query params from this filter."""
        params: Dict[str, Any] = {"channel": DEFAULT_CHANNEL}
        if self.received_only:
            params["event_type"] = INBOUND_RECEIVED_EVENT_TYPE
        if self.correlation_id is not None:
            params["correlation_id"] = self.correlation_id
        if self.thread_id is not None:
            params["thread_id"] = self.thread_id
        return params

    @staticmethod
    def _field(transaction: Transaction, key: str) -> Any:
        metadata = transaction.metadata or {}
        if key in metadata:
            return metadata[key]
        payload = transaction.payload
        if isinstance(payload, dict):
            return payload.get(key)
        return getattr(payload, key, None)

    @staticmethod
    def _fold_text(value: str) -> str:
        """Lowercase and strip combining marks so ``código`` matches ``codigo``."""
        decomposed = unicodedata.normalize("NFKD", value)
        return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()

    def matches(self, transaction: Transaction) -> bool:
        """Return True when *transaction* satisfies this subscription filter."""
        if self.received_only:
            event_type = str(self._field(transaction, "event_type") or "")
            if event_type and event_type != INBOUND_RECEIVED_EVENT_TYPE:
                return False
        if self.correlation_id is not None:
            if str(self._field(transaction, "correlation_id") or "") != self.correlation_id:
                return False
        if self.thread_id is not None:
            if str(self._field(transaction, "thread_id") or "") != self.thread_id:
                return False
        if self.from_address is not None:
            sender = str(self._field(transaction, "from_address") or "").strip().lower()
            if sender != self.from_address.strip().lower():
                return False
        if self.subject_contains is not None:
            subject = str(self._field(transaction, "subject") or "")
            if self._fold_text(self.subject_contains) not in self._fold_text(subject):
                return False
        return True

    def select(self, transactions: List[Transaction]) -> List[Transaction]:
        """Select transactions that satisfy the client-side filter fields."""
        if not self.has_client_side_filters:
            return transactions
        return [tx for tx in transactions if self.matches(tx)]


class ResolvedInboxAddress(BaseModel):
    """Inbox routing metadata resolved from ``channels.addresses`` or config activity."""

    address: str
    address_id: str
    config_id: str
    direction: AddressDirection = "both"
    config_name: Optional[str] = Field(
        default=None,
        description="Channel config display name from ``configs.verify`` when available.",
    )
    config_can_send: Optional[bool] = Field(
        default=None,
        description="Outbound capability from ``configs.verify`` (``capabilities.can_send``).",
    )
    config_can_receive: Optional[bool] = Field(
        default=None,
        description="Inbound capability from ``configs.verify`` (``inbound_enabled``).",
    )

    @property
    def can_receive(self) -> bool:
        if self.config_can_receive is False:
            return False
        return self.direction in ("receive", "both")

    @property
    def can_send(self) -> bool:
        if self.config_can_send is False:
            return False
        return self.direction in ("send", "both")

    @property
    def mode(self) -> str:
        """Human-readable summary of what this inbox supports."""
        if self.can_receive and self.can_send:
            return "receive and send"
        if self.can_receive:
            return "receive-only"
        if self.can_send:
            return "send-only"
        return "inactive"

    def _channel_label(self) -> str:
        if self.config_name and self.config_id:
            return f"{self.config_name!r} ({self.config_id})"
        if self.config_name:
            return repr(self.config_name)
        if self.config_id:
            return self.config_id
        return "unknown channel"

    def ensure_can_receive(self) -> None:
        """Raise a clear error when this inbox cannot be used as a consumer."""
        if self.can_receive:
            return

        lines = [
            f"Cannot fetch from inbox {self.address!r}: capability is {self.mode}.",
            "  Consumer expects: receive or both.",
        ]
        if self.config_id:
            lines.append(f"  Channel config: {self._channel_label()}")
        lines.append(f"  Address direction: {self.direction}")
        if self.config_can_receive is not None or self.config_can_send is not None:
            lines.append(f"  Platform: inbound_enabled={self.config_can_receive}, can_send={self.config_can_send}")

        if self.config_can_receive is False:
            lines.append(
                "Fix: use ErgonPlatformChannelsConsumerConfig with a channel that has "
                "inbound enabled (receive or both)."
            )
        elif self.direction == "send":
            lines.append("Fix: this address is send-only — point consumer_config to a receive or both inbox.")
        else:
            lines.append("Fix: use ErgonPlatformChannelsConsumerConfig with a receive-capable inbox.")
        raise ValueError("\n".join(lines))

    def ensure_can_send(self) -> None:
        """Raise a clear error when this inbox cannot be used as a producer."""
        if self.can_send:
            return

        lines = [
            f"Cannot send from inbox {self.address!r}: capability is {self.mode}.",
            "  Producer expects: send or both.",
        ]
        if self.config_id:
            lines.append(f"  Channel config: {self._channel_label()}")
        lines.append(f"  Address direction: {self.direction}")
        if self.config_can_receive is not None or self.config_can_send is not None:
            lines.append(f"  Platform: inbound_enabled={self.config_can_receive}, can_send={self.config_can_send}")

        if self.config_can_send is False:
            lines.append(
                "Fix: use ErgonPlatformChannelsProducerConfig with a different channel "
                "config that allows send (can_send=true). "
                "In the example app, set CHANNELS_SEND_CONFIG_ID / CHANNELS_SEND_ADDRESS."
            )
        elif self.direction == "receive":
            lines.append("Fix: this address is receive-only — point producer_config to a send or both inbox.")
        else:
            lines.append("Fix: use ErgonPlatformChannelsProducerConfig with a send-capable inbox.")
        raise ValueError("\n".join(lines))

    def as_info_dict(self) -> Dict[str, Any]:
        return {
            "address": self.address,
            "address_id": self.address_id,
            "config_id": self.config_id,
            "config_name": self.config_name,
            "direction": self.direction,
            "mode": self.mode,
            "config_can_send": self.config_can_send,
            "config_can_receive": self.config_can_receive,
            "can_receive": self.can_receive,
            "can_send": self.can_send,
        }

    @classmethod
    def parse_direction(cls, value: Any, *, default: AddressDirection = "both") -> AddressDirection:
        """Parse an address direction from a string."""
        raw = str(value or default).strip().lower()
        if raw in ("send", "receive", "both"):
            return raw
        raise ValueError(f"Unsupported address direction: {value!r} (expected send, receive or both)")


class ErgonPlatformChannelsConfig(BaseModel):
    """Connect to one channels inbox."""

    address: str = Field(min_length=3, description="Inbox email from the console.")
    config_id: str = Field(min_length=1, description="Channel UUID from the console URL.")
    send_address: Optional[str] = Field(
        default=None,
        description="Separate sender inbox; defaults to ``address``.",
    )
    batch_size: int = Field(default=50, ge=1, description="Max emails per fetch.")
    subscription_id: Optional[str] = Field(
        default=None,
        description="Optional stable inbox subscription UUID.",
    )
    consumer_id: str = Field(default="ergon-framework", min_length=1, max_length=255)
    visibility_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    nack_delay_seconds: int = Field(default=0, ge=0)
    received_only: bool = Field(
        default=True,
        description=f"Fetch inbound emails only (``{INBOUND_RECEIVED_EVENT_TYPE}``).",
    )
    activity_filter: Optional[ChannelsActivityFilter] = Field(
        default=None,
        description="Advanced activity filter; overrides ``received_only`` when set.",
    )
    download_attachments: bool = Field(
        default=False,
        description="Download attachment bytes onto each fetched Transaction.",
    )
    attachment_failure_policy: Literal["raise", "best_effort"] = "raise"
    attachment_download_timeout: float = Field(
        default=20.0,
        ge=1.0,
        le=120.0,
        description="HTTP timeout in seconds for each inbound attachment download.",
    )

    def to_consumer_config(self) -> "ErgonPlatformChannelsConsumerConfig":
        return ErgonPlatformChannelsConsumerConfig(
            address=self.address,
            config_id=self.config_id,
            batch_size=self.batch_size,
            subscription_id=self.subscription_id,
            consumer_id=self.consumer_id,
            visibility_timeout_seconds=self.visibility_timeout_seconds,
            nack_delay_seconds=self.nack_delay_seconds,
            received_only=self.received_only,
            activity_filter=self.activity_filter,
            download_attachments=self.download_attachments,
            attachment_failure_policy=self.attachment_failure_policy,
            attachment_download_timeout=self.attachment_download_timeout,
        )

    def to_producer_config(self) -> "ErgonPlatformChannelsProducerConfig":
        return ErgonPlatformChannelsProducerConfig(
            address=self.send_address or self.address,
            config_id=self.config_id,
        )


class ErgonPlatformChannelsConsumerConfig(BaseModel):
    """Read inbound email from one inbox."""

    address: str = Field(
        min_length=3,
        description="Inbox email to read from.",
    )
    config_id: Optional[str] = Field(
        default=None,
        description="Channel UUID from the console URL. Set together with ``address`` (recommended).",
    )
    batch_size: int = Field(default=50, ge=1, description="Max events per fetch.")
    subscription_id: Optional[str] = Field(
        default=None,
        description=(
            "Stable inbox subscription UUID. When omitted, the connector derives one "
            "from the channel, address, and filter contract so credential rotation "
            "does not reset delivery state."
        ),
    )
    consumer_id: str = Field(
        default="ergon-framework",
        min_length=1,
        max_length=255,
        description="Worker/replica label recorded on the current delivery lease.",
    )
    visibility_timeout_seconds: int = Field(
        default=300,
        ge=1,
        le=3600,
        description="Duration of each atomic claim before another replica may retry it.",
    )
    claim_page_size: int = Field(
        default=100,
        ge=1,
        le=100,
        description="Claim page size used while scanning past filtered events.",
    )
    nack_delay_seconds: int = Field(
        default=0,
        ge=0,
        description="Default delay applied by ``nack_transaction(requeue=True)``.",
    )
    deduplicate_fetched_events: bool = Field(
        default=False,
        description=(
            "Optional in-process skip of already-fetched ids. Prefer the platform "
            "subscription claim state — this is only a local fallback."
        ),
    )
    received_only: bool = Field(
        default=True,
        description=(
            f"When ``True`` (default), fetch inbound emails only (``event_type={INBOUND_RECEIVED_EVENT_TYPE}``)."
        ),
    )
    activity_filter: Optional[ChannelsActivityFilter] = Field(
        default=None,
        description="Advanced activity filter; overrides ``received_only`` when set.",
    )
    download_attachments: bool = Field(
        default=False,
        description="Download attachment bytes onto each fetched Transaction (like Nylas).",
    )
    attachment_failure_policy: Literal["raise", "best_effort"] = Field(
        default="raise",
        description=(
            "Attachment failure policy. 'raise' nacks every claim in the failed fetch; "
            "'best_effort' exposes attachment_failures and blocks ack unless explicitly overridden."
        ),
    )
    attachment_download_timeout: float = Field(
        default=20.0,
        ge=1.0,
        le=120.0,
        description=(
            "Per-file HTTP timeout for attachment downloads. Uses a dedicated client "
            "(no retries) so a stuck Resend CDN cannot block ack/nack."
        ),
    )

    def effective_activity_filter(self) -> ChannelsActivityFilter:
        if self.activity_filter is not None:
            return self.activity_filter
        return ChannelsActivityFilter(received_only=self.received_only)

    def activity_query_params(self) -> Dict[str, Any]:
        """Build the immutable filter identity for this subscription."""
        params = self.effective_activity_filter().activity_query_params()
        return params

    def resolved_subscription_id(self, config_id: str, address_id: str) -> str:
        """Return an explicit or deterministic subscription UUID."""

        if self.subscription_id is not None:
            return str(uuid.UUID(self.subscription_id))
        identity = json.dumps(
            {
                "config_id": config_id,
                "address_id": address_id,
                "filter": self.effective_activity_filter().model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return str(uuid.uuid5(_SUBSCRIPTION_NAMESPACE, identity))


class ErgonPlatformChannelsProducerConfig(BaseModel):
    """Defaults for outbound send."""

    address: Optional[str] = Field(
        default=None,
        description="Sender inbox email. Falls back to the consumer inbox when unset.",
    )
    config_id: Optional[str] = Field(
        default=None,
        description="Channel UUID for resolving the sender. Falls back to the consumer config.",
    )
    service_name: Optional[str] = Field(default=None, description="Optional tag on send.")
    default_reply_to: Optional[str] = Field(default=None, description="Default Reply-To header on send.")


class SendMessageAttachment(BaseModel):
    """File attachment for a channels send payload."""

    filename: str = Field(description="Displayed filename")
    content_type: str = Field(description="MIME type")
    content: str = Field(description="Base64-encoded content (SDK-friendly transport)")

    model_config = {"populate_by_name": True}


class InboxAttachmentFile(BaseModel):
    """Inbound attachment downloaded from a fetched inbox event."""

    attachment_id: str
    filename: str
    content: bytes
    content_type: Optional[str] = None
    path: Optional[str] = Field(
        default=None,
        description="Local path when ``download_attachments(..., dest=...)`` wrote the file.",
    )


class SendMessageInput(BaseModel):
    """Email content for ``send_email`` / ``dispatch_transactions``."""

    to: List[str] = Field(default_factory=list, description="Recipients")
    subject: Optional[str] = Field(default=None, description="Subject")
    html: Optional[str] = Field(default=None, description="HTML body")
    text: Optional[str] = Field(default=None, description="Plain-text body")
    cc: Optional[List[str]] = Field(default=None, description="CC")
    bcc: Optional[List[str]] = Field(default=None, description="BCC")
    reply_to: Optional[str] = Field(default=None, description="Reply-To header")
    in_reply_to: Optional[str] = Field(default=None, description="Message-Id being replied to")
    attachments: Optional[List[SendMessageAttachment]] = Field(default=None, description="Attachments")

    model_config = {"populate_by_name": True}


SendMessagePayload = Union[SendMessageInput, Dict[str, Any]]
