import html as html_module
from typing import Any, Dict, List, Union

from ._sdk import SdkRecord
from .models import SendMessageInput


class OutboundMessage:
    """Normalizes send payloads into the platform request shape."""

    @staticmethod
    def normalize_recipients(to: Union[str, List[str]]) -> List[str]:
        """Normalize recipients to a list of strings."""
        if isinstance(to, str):
            return [to]
        return list(to)

    @staticmethod
    def _drop_none(mapping: Dict[str, Any]) -> Dict[str, Any]:
        """Drop None values from a dictionary."""
        return {k: v for k, v in mapping.items() if v is not None}

    @classmethod
    def normalize(cls, payload: object) -> Dict[str, Any]:
        """Normalize a send payload."""
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
    def response_id(result: Any) -> str:
        """Get the response id from a send result."""
        record = SdkRecord(result)
        for key in ("log_id", "provider_message_id", "thread_id", "provider_id", "id"):
            value = record.get(key)
            if value:
                return str(value)
        return ""
