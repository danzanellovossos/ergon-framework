import html as html_module
from typing import Any, Dict, List, Optional, Union

from ....transaction import Transaction
from ..adapters import ActivityAdapter
from ..models import SendMessageInput
from .records import SdkRecord


class ChannelsMessageService:
    """Thread reads and outbound message operations."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def fetch_thread_messages(
        self,
        thread_id: str,
        limit: Optional[int] = None,
        offset: int = 0,
        **params: Any,
    ) -> List[Transaction]:
        """Fetch the thread messages."""
        response = self.client.channels.thread_messages(thread_id, **params)
        messages = SdkRecord.items(
            response,
            keys=["messages", "items", "data"],
        )
        if limit is not None:
            messages = messages[offset : offset + limit]
        elif offset:
            messages = messages[offset:]
        return [ActivityAdapter.to_transaction(message, source="thread") for message in messages]

    def send_message(
        self,
        address_id: str,
        channel: str,
        config: Dict[str, Any],
        resource_id: Optional[str] = None,
        service_name: Optional[str] = None,
    ) -> Any:
        """Send the message."""
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
    def normalize_recipients(to: Union[str, List[str]]) -> List[str]:
        """Normalize the recipients."""
        if isinstance(to, str):
            return [to]
        return list(to)

    @staticmethod
    def _drop_none(mapping: Dict[str, Any]) -> Dict[str, Any]:
        """Drop the none values."""
        return {key: value for key, value in mapping.items() if value is not None}

    @classmethod
    def normalize_send_payload(
        cls,
        payload: object,
    ) -> Dict[str, Any]:
        if isinstance(payload, SendMessageInput):
            config: Dict[str, Any] = cls._drop_none(
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
            return {"top": top, "config": cls._drop_none(config)}

        raise TypeError(f"Unsupported send payload type: {type(payload)}")

    @staticmethod
    def send_response_id(result: Any) -> str:
        """Send the response id."""
        record = SdkRecord(result)
        for key in (
            "log_id",
            "provider_message_id",
            "thread_id",
            "provider_id",
            "id",
        ):
            value = record.get(key)
            if value:
                return str(value)
        return ""
