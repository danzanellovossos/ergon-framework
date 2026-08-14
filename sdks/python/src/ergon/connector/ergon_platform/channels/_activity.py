from typing import Any, Dict, List, Optional

from ...transaction import Transaction
from ._sdk import SdkRecord
from .models import ErgonPlatformChannelsConsumerConfig


class ActivityAdapter:
    """Maps Channels activity events into the connector ``Transaction`` shape."""

    _META_KEYS = (
        "event_type",
        "channel",
        "direction",
        "status",
        "thread_id",
        "correlation_id",
        "provider_message_id",
        "subject",
        "from_address",
        "to_addresses",
        "consumption_status",
        "acked_at",
        "available_at",
        "consumer_id",
        "attempt_count",
    )

    @classmethod
    def to_transaction(cls, event: Any, *, source: str) -> Transaction:
        """Convert a platform activity event into a ``Transaction``."""
        payload = SdkRecord.serialize(event)
        payload = payload if isinstance(payload, dict) else {"value": payload}
        metadata: Dict[str, Any] = {"source": source}
        for key in cls._META_KEYS:
            value = payload.get(key)
            if value is not None:
                metadata[key] = value
        nested = payload.get("payload")
        if isinstance(nested, dict):
            metadata["message_payload"] = nested
            attachments = cls.public_attachments(nested.get("attachments"))
            if attachments:
                metadata["attachments"] = attachments
                metadata["has_attachment"] = True
        return Transaction(
            id=SdkRecord.first_id(payload, "id", "log_id", "message_id", "provider_message_id"),
            payload=payload,
            metadata=metadata,
        )

    @staticmethod
    def attachment_id(attachment: Any) -> Optional[str]:
        """Extract the attachment id from a platform activity event."""
        if not isinstance(attachment, dict):
            return None
        for key in ("id", "resend_attachment_id", "attachment_id"):
            value = attachment.get(key)
            if value:
                return str(value)
        return None

    @classmethod
    def public_attachment(cls, item: Any) -> Optional[Dict[str, Any]]:
        """Nylas-shaped dict: ``id``, ``filename``, ``content_type``, ``size``, ``content``."""
        if not isinstance(item, dict):
            return None
        filename = str(item.get("filename") or item.get("name") or "").strip()
        if not filename:
            return None
        attachment: Dict[str, Any] = {"filename": filename}
        att_id = cls.attachment_id(item)
        if att_id:
            attachment["id"] = att_id
        content_type = item.get("content_type")
        if content_type:
            attachment["content_type"] = content_type
        size = item.get("size")
        if size is not None:
            attachment["size"] = size
        content = item.get("content")
        if isinstance(content, bytes):
            attachment["content"] = content
        return attachment

    @classmethod
    def public_attachments(cls, items: Any) -> List[Dict[str, Any]]:
        """Normalize a platform attachment list into the public fetch shape."""
        if not isinstance(items, list):
            return []
        attachments: List[Dict[str, Any]] = []
        for item in items:
            attachment = cls.public_attachment(item)
            if attachment is not None:
                attachments.append(attachment)
        return attachments

    @staticmethod
    def attachments(transaction: Transaction) -> List[Dict[str, Any]]:
        """Public attachments on the transaction, falling back to the raw event payload."""
        metadata = transaction.metadata or {}
        items = metadata.get("attachments")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        nested = metadata.get("message_payload")
        if isinstance(nested, dict):
            return ActivityAdapter.public_attachments(nested.get("attachments"))
        payload = transaction.payload
        if isinstance(payload, dict):
            nested = payload.get("payload")
            if isinstance(nested, dict):
                return ActivityAdapter.public_attachments(nested.get("attachments"))
        return []

    @staticmethod
    def with_attachments(transaction: Transaction, attachments: List[Dict[str, Any]]) -> Transaction:
        """Add attachments to a platform activity event."""
        metadata = dict(transaction.metadata or {})
        metadata["attachments"] = attachments
        metadata["has_attachment"] = bool(attachments)
        nested = metadata.get("message_payload")
        if isinstance(nested, dict):
            metadata["message_payload"] = {**nested, "attachments": attachments}
        payload = transaction.payload
        if isinstance(payload, dict):
            payload = dict(payload)
            inner = payload.get("payload")
            if isinstance(inner, dict):
                payload["payload"] = {**inner, "attachments": attachments}
        return transaction.model_copy(update={"payload": payload, "metadata": metadata})

    @staticmethod
    def unseen(transactions: List[Transaction], seen_ids: set[str]) -> List[Transaction]:
        """Filter out seen transactions."""
        fresh: List[Transaction] = []
        for tx in transactions:
            if not tx.id or tx.id in seen_ids:
                continue
            seen_ids.add(tx.id)
            fresh.append(tx)
        return fresh

    @classmethod
    def finalize_fetch(
        cls,
        transactions: List[Transaction],
        config: ErgonPlatformChannelsConsumerConfig,
        seen_ids: Optional[set[str]] = None,
    ) -> List[Transaction]:
        """Finalize the fetched transactions."""
        transactions = config.effective_activity_filter().select(transactions)
        if seen_ids is not None:
            transactions = cls.unseen(transactions, seen_ids)
        return transactions
