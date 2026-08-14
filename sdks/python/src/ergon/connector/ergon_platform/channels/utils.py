import html as html_module
from typing import Any, Dict, List, Optional, Union

from ...transaction import Transaction
from .models import (
    ChannelsActivityFilter,
    SendMessageInput,
    SendMessagePayload,
)


def get_value(obj: Any, key: str, default: Any = None) -> Any:
    """Read a key from a dict or an attribute from an object, tolerating both shapes."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def serialize_object(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool, bytes)):
        return obj
    if isinstance(obj, dict):
        return {k: serialize_object(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [serialize_object(item) for item in obj]
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return {k: serialize_object(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
    return obj


def extract_items(response: Any, *, keys: Optional[List[str]] = None) -> List[Any]:
    """Pull the item list out of a Page, dict, or raw list response."""
    candidate_keys = keys or ["items", "messages", "data", "results"]
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        for key in candidate_keys:
            value = response.get(key)
            if isinstance(value, list):
                return value
        return []
    for key in candidate_keys:
        value = getattr(response, key, None)
        if isinstance(value, (list, tuple)):
            return list(value)
    return []


def extract_total(response: Any) -> int:
    """Read total count from a paginated Page, dict, or SDK response."""
    for key in ("total", "total_count", "count"):
        value = get_value(response, key)
        if value is not None:
            return int(value)
    return 0


def _transaction_id(payload: Dict[str, Any]) -> str:
    for key in ("id", "log_id", "message_id", "provider_message_id"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def event_to_transaction(event: Any, *, source: str) -> Transaction:
    """Wrap a channels activity event / thread message as a ``Transaction``."""
    payload = serialize_object(event)
    payload = payload if isinstance(payload, dict) else {"value": payload}
    metadata: Dict[str, Any] = {
        "source": source,
        "event_type": payload.get("event_type"),
        "channel": payload.get("channel"),
        "direction": payload.get("direction"),
        "status": payload.get("status"),
        "thread_id": payload.get("thread_id"),
        "correlation_id": payload.get("correlation_id"),
        "provider_message_id": payload.get("provider_message_id"),
        "subject": payload.get("subject"),
        "from_address": payload.get("from_address"),
        "to_addresses": payload.get("to_addresses"),
        "consumption_status": payload.get("consumption_status"),
        "acked_at": payload.get("acked_at"),
        "available_at": payload.get("available_at"),
        "consumer_id": payload.get("consumer_id"),
        "attempt_count": payload.get("attempt_count"),
    }
    nested = payload.get("payload")
    if isinstance(nested, dict):
        metadata["message_payload"] = nested
    return Transaction(
        id=_transaction_id(payload),
        payload=payload,
        metadata={k: v for k, v in metadata.items() if v is not None},
    )


def _transaction_field(transaction: Transaction, key: str) -> Any:
    metadata = transaction.metadata or {}
    if key in metadata:
        return metadata[key]
    payload = transaction.payload
    if isinstance(payload, dict):
        return payload.get(key)
    return getattr(payload, key, None)


def matches_activity_filter(transaction: Transaction, filt: ChannelsActivityFilter) -> bool:
    """Return True when *transaction* satisfies client-side filter fields."""
    if filt.from_address is not None:
        sender = str(_transaction_field(transaction, "from_address") or "").strip().lower()
        if sender != filt.from_address.strip().lower():
            return False
    if filt.subject_contains is not None:
        subject = str(_transaction_field(transaction, "subject") or "")
        if filt.subject_contains.lower() not in subject.lower():
            return False
    return True


def filter_activity_transactions(
    transactions: List[Transaction],
    filt: Optional[ChannelsActivityFilter],
) -> List[Transaction]:
    """Apply client-side activity filters to fetched transactions."""
    if filt is None or not filt.has_client_side_filters:
        return transactions
    return [tx for tx in transactions if matches_activity_filter(tx, filt)]


def deliver_fetched_transactions(
    transactions: List[Transaction],
    seen_ids: set[str],
) -> List[Transaction]:
    """Return only events not yet delivered by ``fetch_transactions`` on this connector."""
    fresh: List[Transaction] = []
    for tx in transactions:
        if not tx.id or tx.id in seen_ids:
            continue
        seen_ids.add(tx.id)
        fresh.append(tx)
    return fresh


def _drop_none(mapping: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in mapping.items() if v is not None}


def _normalize_recipients(to: Union[str, List[str]]) -> List[str]:
    if isinstance(to, str):
        return [to]
    return list(to)


def normalize_send_payload(payload: SendMessagePayload) -> Dict[str, Any]:
    """Flatten a ``SendMessageInput``/``dict`` into request kwargs."""
    if isinstance(payload, SendMessageInput):
        config: Dict[str, Any] = _drop_none(
            {
                "to": list(payload.to) if payload.to else None,
                "subject": payload.subject,
                "html": payload.html,
                "text": payload.text,
                "cc": list(payload.cc) if payload.cc else None,
                "bcc": list(payload.bcc) if payload.bcc else None,
                "reply_to": payload.reply_to,
                "in_reply_to": payload.in_reply_to,
                "attachments": (
                    [attachment.model_dump(mode="json") for attachment in payload.attachments]
                    if payload.attachments
                    else None
                ),
            }
        )
        if config.get("text") and not config.get("html"):
            config["html"] = f"<p>{html_module.escape(config['text'])}</p>"
        return {"top": {}, "config": config}

    if isinstance(payload, dict):
        data = dict(payload)
        config = dict(data.pop("config", {}) or {})
        top = {
            "address_id": data.pop("address_id", None),
            "channel": data.pop("channel", None),
            "resource_id": data.pop("resource_id", None),
            "service_name": data.pop("service_name", None),
        }
        for key, value in data.items():
            config.setdefault(key, value)
        return {"top": top, "config": _drop_none(config)}

    raise TypeError(f"Unsupported send payload type: {type(payload)}")


def inbox_attachment_id(attachment: Any) -> Optional[str]:
    """Return the provider attachment id from activity payload metadata."""
    if not isinstance(attachment, dict):
        return None
    for key in ("resend_attachment_id", "id", "attachment_id"):
        value = attachment.get(key)
        if value:
            return str(value)
    return None


def inbox_attachments(transaction: Transaction) -> List[Dict[str, Any]]:
    """Attachment metadata from a fetched inbox ``Transaction``."""
    metadata = transaction.metadata or {}
    nested = metadata.get("message_payload")
    if isinstance(nested, dict):
        items = nested.get("attachments") or []
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    payload = transaction.payload
    if isinstance(payload, dict):
        nested = payload.get("payload")
        if isinstance(nested, dict):
            items = nested.get("attachments") or []
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return []
