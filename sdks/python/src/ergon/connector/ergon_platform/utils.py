from typing import Any, Dict, List, Optional

from ..transaction import Transaction
from .models import (
    FAILURE_STATUSES,
    PROCESSING_STATUSES,
    SUCCESS_STATUSES,
    CreateItemInput,
    CreateItemPayload,
)


def get_value(obj: Any, key: str, default: Any = None) -> Any:
    """Read a key from a dict or an attribute from an object, tolerating both shapes."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def as_payload(obj: Any) -> Any:
    """Convert SDK models to JSON-friendly payloads while leaving plain data untouched."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


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


def classify_status(raw_status: str) -> str:
    """Map a raw pipeline status string to a coarse state."""
    status = (raw_status or "").lower()
    if status in SUCCESS_STATUSES:
        return "success"
    if status in FAILURE_STATUSES:
        return "failed"
    if status in PROCESSING_STATUSES:
        return "processing"
    return "unknown"


def find_status_entry(statuses: Any, buckets_file_id: str) -> Any:
    for status in extract_status_entries(statuses):
        if extract_status_file_id(status) == str(buckets_file_id):
            return status
    return {}


def extract_status_entries(statuses: Any) -> List[Any]:
    """Normalize SDK attachment status responses to a list of status entries."""
    if statuses is None:
        return []
    if isinstance(statuses, list):
        return statuses
    if isinstance(statuses, tuple):
        return list(statuses)
    if isinstance(statuses, dict):
        for key in ("items", "data", "results", "statuses", "attachments", "files"):
            value = statuses.get(key)
            if isinstance(value, list):
                return value
        if get_value(statuses, "status") is not None or extract_status_file_id(statuses):
            return [statuses]
        return []
    for key in ("items", "data", "results", "statuses", "attachments", "files"):
        value = getattr(statuses, key, None)
        if isinstance(value, (list, tuple)):
            return list(value)
    if get_value(statuses, "status") is not None or extract_status_file_id(statuses):
        return [statuses]
    return []


def extract_status_file_id(status: Any) -> Optional[str]:
    for key in ("buckets_file_id", "bucketsFileId", "file_id", "fileId"):
        value = get_value(status, key)
        if value:
            return str(value)
    return None


def latest_status_entry(statuses: Any) -> Any:
    entries = extract_status_entries(statuses)
    return entries[-1] if entries else {}


def extract_buckets_file_id(item: Any, field_id: str) -> Optional[str]:
    field_values = get_value(item, "field_values", []) or []
    if isinstance(field_values, dict):
        return extract_buckets_file_id_from_attachments(field_values.get(field_id))

    for field_value in field_values:
        if str(get_value(field_value, "field_id")) != str(field_id):
            continue
        buckets_file_id = extract_buckets_file_id_from_attachments(get_value(field_value, "value", []))
        if buckets_file_id:
            return buckets_file_id
    return None


def extract_buckets_file_id_from_attachments(value: Any) -> Optional[str]:
    attachments = value or []
    if isinstance(attachments, dict):
        for key in ("attachments", "files", "items", "data", "results", "value"):
            nested = attachments.get(key)
            if isinstance(nested, list):
                attachments = nested
                break
        else:
            attachments = [attachments]
    if not isinstance(attachments, list):
        return None
    for attachment in reversed(attachments):
        buckets_file_id = get_value(attachment, "buckets_file_id")
        if buckets_file_id:
            return str(buckets_file_id)
    return None


def item_to_transaction(item: Any, workflow_id: str) -> Transaction:
    payload = serialize_object(item)
    payload = payload if isinstance(payload, dict) else {"value": payload}
    return Transaction(
        id=str(payload.get("id", "")),
        payload=payload,
        metadata={
            "workflow_id": workflow_id,
            "phase_id": payload.get("phase_id"),
            "company_id": payload.get("company_id"),
            "title": payload.get("title"),
        },
    )


def normalize_create_payload(payload: CreateItemPayload) -> Dict[str, Any]:
    """Flatten a CreateItemInput/dict into kwargs accepted by the service create_item."""
    if isinstance(payload, CreateItemInput):
        data: Dict[str, Any] = {
            "title": payload.title,
            "workflow_id": payload.workflow_id,
            "phase_id": payload.phase_id,
            "parent_item_id": payload.parent_item_id,
            "field_values": payload.field_values,
            "attachment": payload.attachment,
            "attachment_field_id": payload.attachment_field_id,
            "content_type": payload.content_type,
            "fields": dict(payload.extra_fields),
        }
        return data
    if isinstance(payload, dict):
        data = dict(payload)
        data.setdefault("fields", {})
        return data
    raise TypeError(f"Unsupported create payload type: {type(payload)}")


def extract_items(response: Any) -> List[Any]:
    """Pull the item list out of a Page, dict, or raw list response."""
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        for key in ("items", "data", "results"):
            value = response.get(key)
            if isinstance(value, list):
                return value
        return []
    items = getattr(response, "items", None)
    if isinstance(items, (list, tuple)):
        return list(items)
    return []


def extract_total(response: Any) -> int:
    """Read total count from a paginated Page, dict, or SDK response."""
    for key in ("total", "total_count", "count"):
        value = get_value(response, key)
        if value is not None:
            return int(value)
    return 0
