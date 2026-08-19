from typing import Any, Dict, Optional

from ..transaction import Transaction
from .models import (
    OutlookAckActionConfig,
    OutlookConsumerConfig,
    OutlookMessageQuery,
    OutlookNackActionConfig,
    OutlookSendMessageInput,
    OutlookSendMessagePayload,
)


def merge_query(
    base: Optional[OutlookConsumerConfig],
    overrides: Optional[OutlookMessageQuery] = None,
    **kwargs: Any,
) -> OutlookMessageQuery:
    query_fields = set(OutlookMessageQuery.model_fields)
    merged: Dict[str, Any] = {}
    if base is not None:
        merged.update({key: value for key, value in base.model_dump(exclude_none=True).items() if key in query_fields})
    if overrides is not None:
        dumped = {
            key: value
            for key, value in overrides.model_dump(exclude_none=True, exclude_unset=True).items()
            if key in query_fields
        }
        if "search" in dumped:
            merged.pop("filter", None)
        if "filter" in dumped:
            merged.pop("search", None)
        merged.update(dumped)
    for key, value in kwargs.items():
        if key not in query_fields or value is None:
            continue
        if key == "search":
            merged.pop("filter", None)
        if key == "filter":
            merged.pop("search", None)
        merged[key] = value
    return OutlookMessageQuery.model_validate(merged)


def _graph_email(value: Any) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    address = value.get("emailAddress")
    if not isinstance(address, dict):
        return None
    email = address.get("address")
    return email if isinstance(email, str) and email else None


def _graph_emails(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    emails: list[str] = []
    for item in values:
        email = _graph_email(item)
        if email:
            emails.append(email)
    return emails


def message_to_transaction(
    message: Dict[str, Any],
    user_email: str,
    attachments: Optional[list[Dict[str, Any]]] = None,
) -> Transaction:
    attachment_items = attachments or message.get("attachments") or []
    raw_body = message.get("body")
    body = raw_body if isinstance(raw_body, dict) else {}
    return Transaction(
        id=str(message.get("id", "")),
        payload=message,
        metadata={
            "user_email": user_email,
            "subject": message.get("subject"),
            "from_email": _graph_email(message.get("from")),
            "to_emails": _graph_emails(message.get("toRecipients")),
            "received_at": message.get("receivedDateTime"),
            "body_preview": message.get("bodyPreview"),
            "body": body.get("content"),
            "body_type": body.get("contentType"),
            "conversation_id": message.get("conversationId"),
            "internet_message_id": message.get("internetMessageId"),
            "parent_folder_id": message.get("parentFolderId"),
            "has_attachment": bool(message.get("hasAttachments") or attachment_items),
            "attachments": attachment_items,
            "is_read": message.get("isRead"),
        },
    )


def normalize_send_payload(payload: OutlookSendMessagePayload) -> Dict[str, Any]:
    if isinstance(payload, OutlookSendMessageInput):
        return payload.to_graph_message()
    if isinstance(payload, dict):
        if "message" in payload and isinstance(payload["message"], dict):
            return dict(payload["message"])
        return dict(payload)
    raise TypeError(f"Unsupported Outlook dispatch payload type: {type(payload)}")


def build_ack_patch(config: OutlookAckActionConfig) -> Dict[str, Any]:
    return {"isRead": True} if config.mark_as_read else {}


def build_nack_patch(config: OutlookNackActionConfig) -> Dict[str, Any]:
    patch: Dict[str, Any] = {}
    if config.mark_as_unread:
        patch["isRead"] = False
    if config.categories:
        patch["categories"] = list(
            dict.fromkeys(category.strip() for category in config.categories if category.strip())
        )
    return patch
