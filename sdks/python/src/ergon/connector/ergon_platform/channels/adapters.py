from typing import Any, Dict, List, Optional

from ...transaction import Transaction
from .models import ErgonPlatformChannelsConsumerConfig
from .services.records import SdkRecord


class ActivityAdapter:
    """Map Channels activity events into the connector Transaction shape."""

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
        "address_id",
        "consumption_status",
        "acked_at",
        "available_at",
        "consumer_id",
        "attempt_count",
    )
    _DELIVERY_KEYS = (
        "subscription_id",
        "lease_token",
        "lease_expires_at",
        "consumer_id",
        "attempt_count",
    )

    @classmethod
    def to_transaction(cls, event: Any, *, source: str) -> Transaction:
        """To transaction."""
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
            id=SdkRecord.first_id(
                payload,
                "id",
                "log_id",
                "message_id",
                "provider_message_id",
            ),
            payload=payload,
            metadata=metadata,
        )

    @classmethod
    def claimed_transaction(cls, item: Any) -> Transaction:
        """Claimed transaction."""
        claimed = SdkRecord.serialize(item)
        claimed = claimed if isinstance(claimed, dict) else {}
        transaction = cls.to_transaction(
            claimed.get("event"),
            source="config_activity_claim",
        )
        metadata = dict(transaction.metadata or {})
        delivery = claimed.get("delivery")
        if isinstance(delivery, dict):
            metadata["delivery"] = {key: delivery[key] for key in cls._DELIVERY_KEYS if delivery.get(key) is not None}
        return transaction.model_copy(update={"metadata": metadata})

    @staticmethod
    def delivery(transaction: Transaction) -> Dict[str, Any]:
        """Delivery."""
        metadata = transaction.metadata or {}
        delivery = metadata.get("delivery")
        if not isinstance(delivery, dict):
            raise ValueError("Transaction was not claimed and cannot be acknowledged or nacked")
        if not delivery.get("subscription_id") or not delivery.get("lease_token"):
            raise ValueError("Transaction claim is missing subscription_id or lease_token")
        return delivery

    @staticmethod
    def belongs_to_address(
        transaction: Transaction,
        address_id: str,
    ) -> bool:
        """Belongs to address."""
        metadata = transaction.metadata or {}
        nested = metadata.get("message_payload")
        if isinstance(nested, dict):
            candidate = nested.get("address_id") or nested.get("channel_address_id")
            if candidate is not None:
                return str(candidate) == address_id
        event = transaction.payload if isinstance(transaction.payload, dict) else {}
        candidate = event.get("address_id") or event.get("channel_address_id")
        if candidate is not None:
            return str(candidate) == address_id
        return True

    @staticmethod
    def attachment_id(attachment: Any) -> Optional[str]:
        """Attachment id."""
        if not isinstance(attachment, dict):
            return None
        for key in ("id", "resend_attachment_id", "attachment_id"):
            value = attachment.get(key)
            if value:
                return str(value)
        return None

    @classmethod
    def public_attachment(
        cls,
        item: Any,
    ) -> Optional[Dict[str, Any]]:
        """Public attachment."""
        if not isinstance(item, dict):
            return None
        filename = str(item.get("filename") or item.get("name") or "").strip()
        if not filename:
            return None
        attachment: Dict[str, Any] = {"filename": filename}
        attachment_id = cls.attachment_id(item)
        if attachment_id:
            attachment["id"] = attachment_id
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
        """Public attachments."""
        if not isinstance(items, list):
            return []
        attachments: List[Dict[str, Any]] = []
        for item in items:
            attachment = cls.public_attachment(item)
            if attachment is not None:
                attachments.append(attachment)
        return attachments

    @classmethod
    def attachments(
        cls,
        transaction: Transaction,
    ) -> List[Dict[str, Any]]:
        """Attachments."""
        metadata = transaction.metadata or {}
        items = metadata.get("attachments")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        nested = metadata.get("message_payload")
        if isinstance(nested, dict):
            return cls.public_attachments(nested.get("attachments"))
        payload = transaction.payload
        if isinstance(payload, dict):
            nested = payload.get("payload")
            if isinstance(nested, dict):
                return cls.public_attachments(nested.get("attachments"))
        return []

    @staticmethod
    def with_attachments(
        transaction: Transaction,
        attachments: List[Dict[str, Any]],
    ) -> Transaction:
        """With the attachments."""
        metadata = dict(transaction.metadata or {})
        metadata["attachments"] = attachments
        metadata["has_attachment"] = bool(attachments)
        nested = metadata.get("message_payload")
        if isinstance(nested, dict):
            metadata["message_payload"] = {
                **nested,
                "attachments": attachments,
            }
        payload = transaction.payload
        if isinstance(payload, dict):
            payload = dict(payload)
            inner = payload.get("payload")
            if isinstance(inner, dict):
                payload["payload"] = {
                    **inner,
                    "attachments": attachments,
                }
        return transaction.model_copy(update={"payload": payload, "metadata": metadata})

    @staticmethod
    def unseen(
        transactions: List[Transaction],
        seen_ids: set[str],
    ) -> List[Transaction]:
        fresh: List[Transaction] = []
        """Unseen transactions."""
        for transaction in transactions:
            if not transaction.id or transaction.id in seen_ids:
                continue
            seen_ids.add(transaction.id)
            fresh.append(transaction)
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
