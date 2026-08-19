import base64
import mimetypes
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


class OutlookWellKnownFolder(str, Enum):
    INBOX = "inbox"
    SENT_ITEMS = "sentitems"
    DELETED_ITEMS = "deleteditems"


class OutlookFlagStatus(str, Enum):
    NOT_FLAGGED = "notFlagged"
    COMPLETE = "complete"
    FLAGGED = "flagged"


class OutlookMessageSearch(BaseModel):
    """Discoverable Microsoft Graph message search fields."""

    text: Optional[str] = None
    subject: Optional[str] = None
    sender: Optional[str] = None
    recipient: Optional[str] = None

    def to_graph(self) -> str:
        terms = []
        if self.text and self.text.strip():
            terms.append(self.text.strip())
        for field, value in (
            ("subject", self.subject),
            ("from", self.sender),
            ("to", self.recipient),
        ):
            if value and value.strip():
                terms.append(f"{field}:{self._escape(value)}")
        return " AND ".join(terms)

    @staticmethod
    def _escape(value: str) -> str:
        """Escape a string for use in an OData filter."""
        escaped = value.strip().replace("\\", "\\\\").replace('"', '\\"')
        return f'\\"{escaped}\\"' if any(character.isspace() for character in escaped) else escaped


class OutlookMessageFilter(BaseModel):
    """Common message filters without requiring OData syntax."""

    unread_only: bool = False
    has_attachments: Optional[bool] = None
    sender: Optional[str] = None
    subject_starts_with: Optional[str] = None
    received_after: Optional[datetime] = None
    received_before: Optional[datetime] = None

    def to_graph(self) -> List[str]:
        """Convert the filter to a list of OData filters."""
        filters: List[str] = []
        if self.unread_only:
            filters.append("isRead eq false")
        if self.has_attachments is not None:
            filters.append(f"hasAttachments eq {str(self.has_attachments).lower()}")
        if self.sender and self.sender.strip():
            filters.append(f"from/emailAddress/address eq {self._odata_string(self.sender)}")
        if self.subject_starts_with and self.subject_starts_with.strip():
            value = self._odata_string(self.subject_starts_with)
            filters.append(f"startswith(subject,{value})")
        if self.received_after is not None:
            filters.append(f"receivedDateTime ge {self._format_datetime(self.received_after)}")
        if self.received_before is not None:
            filters.append(f"receivedDateTime le {self._format_datetime(self.received_before)}")
        return filters

    @staticmethod
    def _odata_string(value: str) -> str:
        """Escape a string for use in an OData filter."""
        return "'" + value.strip().replace("'", "''") + "'"

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        """Format a datetime for use in an OData filter."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class OutlookGraphClient(BaseModel):
    """Microsoft Entra application credentials and target mailbox."""

    tenant_id: str
    client_id: str
    client_secret: str
    user_email: str
    scopes: List[str] = Field(default_factory=lambda: ["https://graph.microsoft.com/.default"])
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    authority_base_url: str = "https://login.microsoftonline.com"
    timeout_sec: int = Field(default=30, ge=1)

    @property
    def authority(self) -> str:
        return f"{self.authority_base_url.rstrip('/')}/{self.tenant_id}"


class OutlookMessageQuery(BaseModel):
    """Supported Microsoft Graph query options for a mailbox folder."""

    DEFAULT_MESSAGE_SELECT: ClassVar[List[str]] = [
        "id",
        "subject",
        "receivedDateTime",
        "hasAttachments",
        "from",
        "toRecipients",
        "ccRecipients",
        "bccRecipients",
        "isRead",
        "importance",
        "bodyPreview",
        "body",
        "conversationId",
        "internetMessageId",
        "parentFolderId",
        "categories",
    ]
    _SEARCH_AND_FILTER_ERROR: ClassVar[str] = "Cannot combine search and filter in the same query."

    folder_id: str = "Inbox"
    search: Optional[Union[OutlookMessageSearch, str]] = None
    filter: Optional[Union[OutlookMessageFilter, str]] = None
    select: List[str] = Field(default_factory=lambda: list(OutlookMessageQuery.DEFAULT_MESSAGE_SELECT))
    order_by: Optional[str] = "receivedDateTime asc"

    @field_validator("folder_id", mode="before")
    @classmethod
    def normalize_folder_id(cls, value: Any) -> str:
        if isinstance(value, OutlookWellKnownFolder):
            return value.value
        if isinstance(value, str) and value.strip():
            return value.strip()
        raise ValueError("folder_id is required")

    def search_expression(self) -> Optional[str]:
        if self.search is None:
            return None
        value = self.search.to_graph() if isinstance(self.search, OutlookMessageSearch) else self.search.strip()
        return value or None

    def filter_expressions(self) -> List[str]:
        if self.filter is None:
            return []
        if isinstance(self.filter, OutlookMessageFilter):
            return self.filter.to_graph()
        value = self.filter.strip()
        return [value] if value else []

    @model_validator(mode="after")
    def reject_search_with_filter(self) -> "OutlookMessageQuery":
        if self.search_expression() and self.filter_expressions():
            raise ValueError(self._SEARCH_AND_FILTER_ERROR)
        return self

    def to_query_params(self, *, top: Optional[int] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if top is not None:
            params["$top"] = top
        search = self.search_expression()
        if search:
            params["$search"] = search if search.startswith('"') and search.endswith('"') else f'"{search}"'
        filters = self.filter_expressions()
        order_by = self.order_by
        if filters and order_by:
            order_field = order_by.split(maxsplit=1)[0]
            if order_field.casefold() != "receiveddatetime":
                raise ValueError("Filtered Outlook queries only support ordering by receivedDateTime")
            filters.insert(0, "receivedDateTime ge 1900-01-01T00:00:00Z")
        if filters:
            params["$filter"] = " and ".join(filters)
        if self.select:
            params["$select"] = ",".join(self.select)
        if order_by and not search:
            params["$orderby"] = order_by
        return params

    @property
    def requires_eventual_consistency(self) -> bool:
        return bool(self.search_expression())


class OutlookAckActionConfig(BaseModel):
    """Actions applied after a message is processed successfully."""

    mark_as_read: bool = True
    move_to_folder_id: Optional[str] = None
    delete: bool = False

    @model_validator(mode="after")
    def normalize_delete_action(self) -> "OutlookAckActionConfig":
        if self.delete and self.move_to_folder_id:
            raise ValueError("Ack cannot move and delete the same message")
        if self.delete:
            self.mark_as_read = False
        return self


class OutlookNackActionConfig(BaseModel):
    """Actions applied after message processing fails."""

    mark_as_unread: bool = True
    move_to_folder_id: Optional[str] = None
    categories: List[str] = Field(default_factory=list)


class OutlookConsumerConfig(OutlookMessageQuery):
    """Configuration for consuming Outlook messages."""

    batch_size: int = Field(default=10, ge=1, le=1000)
    download_attachments: bool = False
    ack_config: Optional[OutlookAckActionConfig] = None
    nack_config: Optional[OutlookNackActionConfig] = None


class OutlookProducerConfig(BaseModel):
    """Configuration for producing Outlook messages."""

    save_to_sent_items: bool = True


class OutlookEmailAddress(BaseModel):
    email: str
    name: Optional[str] = None

    def to_graph(self) -> Dict[str, Dict[str, str]]:
        value = {"address": self.email}
        if self.name:
            value["name"] = self.name
        return {"emailAddress": value}


class OutlookAttachmentInput(BaseModel):
    """Input for an Outlook attachment."""

    filename: Optional[str] = None
    content_type: Optional[str] = None
    content: Optional[bytes] = None
    content_base64: Optional[str] = None
    file_path: Optional[Path] = None

    def to_graph(self) -> Dict[str, Any]:
        filename = self.filename
        content_type = self.content_type
        raw = self.content

        if self.file_path is not None:
            path = Path(self.file_path)
            if not path.is_file():
                raise FileNotFoundError(f"Outlook attachment not found: {path}")
            filename = filename or path.name
            content_type = content_type or mimetypes.guess_type(path.name)[0]
            raw = path.read_bytes()

        if self.content_base64 is not None:
            encoded = self.content_base64
        elif raw is not None:
            encoded = base64.b64encode(raw).decode("ascii")
        else:
            raise ValueError("Outlook attachment requires content, content_base64, or file_path")

        if not filename:
            raise ValueError("Outlook attachment requires a filename")

        return {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": filename,
            "contentType": content_type or "application/octet-stream",
            "contentBytes": encoded,
        }


class OutlookSendMessageInput(BaseModel):
    """Input for sending an Outlook message."""

    to: List[OutlookEmailAddress]
    subject: str
    body: str
    body_type: Literal["HTML", "Text"] = "HTML"
    cc: List[OutlookEmailAddress] = Field(default_factory=list)
    bcc: List[OutlookEmailAddress] = Field(default_factory=list)
    reply_to: List[OutlookEmailAddress] = Field(default_factory=list)
    attachments: List[OutlookAttachmentInput] = Field(default_factory=list)

    def to_graph_message(self) -> Dict[str, Any]:
        message: Dict[str, Any] = {
            "subject": self.subject,
            "body": {"contentType": self.body_type, "content": self.body},
            "toRecipients": [address.to_graph() for address in self.to],
        }
        optional_recipients = {
            "ccRecipients": self.cc,
            "bccRecipients": self.bcc,
            "replyTo": self.reply_to,
        }
        for key, values in optional_recipients.items():
            if values:
                message[key] = [address.to_graph() for address in values]
        if self.attachments:
            message["attachments"] = [attachment.to_graph() for attachment in self.attachments]
        return message


OutlookSendMessagePayload = Union[OutlookSendMessageInput, Dict[str, Any]]
