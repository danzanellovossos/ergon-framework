from typing import Any, Dict, List, Optional

from ..transaction import Transaction
from .models import (
    AckActionConfig,
    ClientSideFilter,
    MessageQueryFilter,
    NylasConsumerConfig,
    SendMessageInput,
    SendMessagePayload,
)


def serialize_nylas_object(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, bytes):
        return obj
    if isinstance(obj, dict):
        return {k: serialize_nylas_object(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [serialize_nylas_object(item) for item in obj]
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return {k: serialize_nylas_object(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
    return obj


def extract_response_data(response: Any) -> Any:
    if response is None:
        return None
    if hasattr(response, "data"):
        return serialize_nylas_object(response.data)
    return serialize_nylas_object(response)


def extract_next_cursor(response: Any) -> Optional[str]:
    if response is None:
        return None
    for attr in ("next_cursor", "page_token", "next_page_token"):
        value = getattr(response, attr, None)
        if isinstance(value, str) and value:
            return value
    if isinstance(response, dict):
        for key in ("next_cursor", "page_token", "next_page_token"):
            token = response.get(key)
            if isinstance(token, str) and token:
                return token
    return None


def merge_query_filter(
    base: Optional[NylasConsumerConfig],
    overrides: Optional[MessageQueryFilter] = None,
    **kwargs: Any,
) -> MessageQueryFilter:
    query_field_names = set(MessageQueryFilter.model_fields.keys())
    merged: Dict[str, Any] = {}
    if base is not None:
        merged.update(
            {k: v for k, v in base.model_dump(exclude_none=True, by_alias=True).items() if k in query_field_names}
        )
    if overrides is not None:
        merged.update(overrides.model_dump(exclude_none=True, by_alias=True))
    for key, value in kwargs.items():
        if value is not None and key in query_field_names:
            merged[key] = value
    return MessageQueryFilter.model_validate(merged)


def apply_client_side_filter(messages: List[Dict[str, Any]], filt: Optional[ClientSideFilter]) -> List[Dict[str, Any]]:
    if filt is None:
        return messages

    result: List[Dict[str, Any]] = []
    for message in messages:
        if filt.subject_contains:
            subject = message.get("subject") or ""
            if filt.subject_contains.lower() not in subject.lower():
                continue
        attachments = message.get("attachments") or []
        if filt.attachment_filename_contains:
            if not any(
                filt.attachment_filename_contains.lower() in (att.get("filename") or "").lower() for att in attachments
            ):
                continue
        if filt.attachment_content_type:
            if not any(att.get("content_type") == filt.attachment_content_type for att in attachments):
                continue
        result.append(message)
    return result


def message_to_transaction(
    message: Dict[str, Any],
    grant_id: str,
    attachments_meta: Optional[List[Dict[str, Any]]] = None,
) -> Transaction:
    return Transaction(
        id=str(message.get("id", "")),
        payload=message,
        metadata={
            "grant_id": grant_id,
            "thread_id": message.get("thread_id"),
            "folder_ids": message.get("folders", []),
            "has_attachment": bool(message.get("attachments")),
            "attachments": attachments_meta or message.get("attachments") or [],
            "unread": message.get("unread"),
        },
    )


def build_ack_request_body(ack_config: AckActionConfig) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    if ack_config.mark_as_read:
        body["unread"] = False
    if ack_config.move_to_folder_id:
        body["folders"] = [ack_config.move_to_folder_id]
    if ack_config.add_star:
        body["starred"] = True
    if ack_config.archive:
        body["folders"] = body.get("folders", [])
    return body


def normalize_send_payload(payload: SendMessagePayload, default_from: Optional[Any] = None) -> Dict[str, Any]:
    if isinstance(payload, SendMessageInput):
        return payload.to_request_body(default_from=default_from)
    if isinstance(payload, dict):
        return dict(payload)
    raise TypeError(f"Unsupported dispatch payload type: {type(payload)}")
