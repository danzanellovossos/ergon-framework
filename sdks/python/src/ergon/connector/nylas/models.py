from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


class EmailAddress(BaseModel):
    name: Optional[str] = None
    email: str


class MessageFields(str, Enum):
    STANDARD = "standard"
    INCLUDE_HEADERS = "include_headers"
    INCLUDE_TRACKING_OPTIONS = "include_tracking_options"
    RAW_MIME = "raw_mime"


class MessageQueryFilter(BaseModel):
    """Query parameters for Nylas messages.list / threads.list."""

    subject: Optional[str] = Field(default=None, description="Case-sensitive partial subject match")
    any_email: Optional[List[str]] = Field(default=None, description="Match any participant email")
    from_: Optional[List[str]] = Field(default=None, alias="from", description="Sender email addresses")
    to: Optional[List[str]] = Field(default=None, description="Recipient email addresses")
    cc: Optional[List[str]] = Field(default=None, description="CC email addresses")
    bcc: Optional[List[str]] = Field(default=None, description="BCC email addresses")
    in_: Optional[str] = Field(default=None, alias="in", description="Folder or label ID")
    unread: Optional[bool] = Field(default=None, description="Filter by unread status")
    starred: Optional[bool] = Field(default=None, description="Filter by starred status")
    thread_id: Optional[str] = Field(default=None, description="Filter by thread ID")
    received_before: Optional[int] = Field(default=None, description="Unix timestamp upper bound")
    received_after: Optional[int] = Field(default=None, description="Unix timestamp lower bound")
    has_attachment: Optional[bool] = Field(default=None, description="Filter messages with attachments")
    fields: Optional[MessageFields] = Field(default=None, description="Fields to include in response")
    search_query_native: Optional[str] = Field(
        default=None, description="Provider-specific search query (URL-encoded)"
    )
    metadata_pair: Optional[str] = Field(default=None, description="Metadata key-value filter")
    select: Optional[str] = Field(default=None, description="Comma-separated fields to return")

    model_config = {"populate_by_name": True}

    def to_query_params(self, limit: Optional[int] = None, page_token: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        field_map = {
            "subject": self.subject,
            "any_email": self.any_email,
            "from": self.from_,
            "to": self.to,
            "cc": self.cc,
            "bcc": self.bcc,
            "in": self.in_,
            "unread": self.unread,
            "starred": self.starred,
            "thread_id": self.thread_id,
            "received_before": self.received_before,
            "received_after": self.received_after,
            "has_attachment": self.has_attachment,
            "search_query_native": self.search_query_native,
            "metadata_pair": self.metadata_pair,
            "select": self.select,
        }
        for key, value in field_map.items():
            if value is not None:
                params[key] = value
        if self.fields is not None:
            params["fields"] = self.fields.value
        if limit is not None:
            params["limit"] = limit
        if page_token is not None:
            params["page_token"] = page_token
        return params


class ClientSideFilter(BaseModel):
    subject_contains: Optional[str] = Field(default=None, description="Case-insensitive subject substring")
    attachment_filename_contains: Optional[str] = Field(default=None, description="Attachment filename substring")
    attachment_content_type: Optional[str] = Field(default=None, description="Attachment MIME type")


class AckActionConfig(BaseModel):
    mark_as_read: bool = Field(default=True, description="Mark message as read on ack")
    move_to_folder_id: Optional[str] = Field(default=None, description="Folder ID to move message to")
    add_star: bool = Field(default=False, description="Star message on ack")
    archive: bool = Field(default=False, description="Archive message on ack (provider-specific)")


class NylasAuthClient(BaseModel):
    api_key: str = Field(description="Nylas API key")
    client_id: Optional[str] = Field(
        default=None,
        description="Nylas Application/Client ID from dashboard; defaults to api_key when omitted",
    )
    api_uri: str = Field(default="https://api.us.nylas.com", description="Nylas API base URI")

    def get_client_id(self) -> str:
        return self.client_id or self.api_key


class AuthUrlConfig(BaseModel):
    redirect_uri: str = Field(description="OAuth callback URI registered in the Nylas Dashboard")
    provider: Optional[str] = Field(
        default=None,
        description="Provider slug (google, microsoft, imap, ...); omit to let user choose",
    )
    login_hint: Optional[str] = Field(default=None, description="Suggested email address for login")
    scopes: Optional[List[str]] = Field(default=None, description="OAuth scopes to request")
    state: Optional[str] = Field(default=None, description="CSRF state parameter")
    access_type: Optional[Literal["online", "offline"]] = Field(default=None, description="OAuth access type")

    def to_auth_config(self, client_id: str) -> Dict[str, Any]:
        config: Dict[str, Any] = {
            "client_id": client_id,
            "redirect_uri": self.redirect_uri,
        }
        optional = {
            "provider": self.provider,
            "login_hint": self.login_hint,
            "scopes": self.scopes,
            "state": self.state,
            "access_type": self.access_type,
        }
        for key, value in optional.items():
            if value is not None:
                config[key] = value
        return config


class CodeExchangeInput(BaseModel):
    code: str = Field(description="Authorization code from OAuth redirect")
    redirect_uri: str = Field(description="Same redirect URI used when generating the auth URL")


class GrantAuthResult(BaseModel):
    grant_id: str = Field(description="Grant ID for the authenticated mailbox")
    email: Optional[str] = Field(default=None, description="Authenticated email address")
    provider: Optional[str] = Field(default=None, description="Email provider slug")
    scope: Optional[str] = Field(default=None, description="Granted OAuth scopes")


class NylasClient(BaseModel):
    api_key: str = Field(description="Nylas API key")
    api_uri: str = Field(default="https://api.us.nylas.com", description="Nylas API base URI")
    grant_id: str = Field(description="Grant ID for the connected mailbox")
    timeout_sec: int = Field(default=30, description="HTTP timeout in seconds")


class NylasConsumerConfig(MessageQueryFilter):
    batch_size: int = Field(default=10, ge=1, description="Max messages per fetch")
    download_attachments: bool = Field(default=False, description="Download attachment bytes on fetch")
    client_side_filter: Optional[ClientSideFilter] = Field(default=None, description="Post-API client-side filters")
    ack_config: Optional[AckActionConfig] = Field(default=None, description="Actions applied on ack_transaction")
    fetch_unit: Literal["message", "thread"] = Field(default="message", description="Fetch messages or threads")


class NylasProducerConfig(BaseModel):
    send_mode: Literal["send", "draft"] = Field(default="send", description="Send directly or via draft")
    default_from: Optional[EmailAddress] = Field(default=None, description="Default sender when payload omits from")


class AttachmentInput(BaseModel):
    filename: str
    content_type: Optional[str] = None
    content: Optional[str] = Field(default=None, description="Base64-encoded content")
    file_path: Optional[str] = Field(default=None, description="Local file path to attach")


class SendMessageInput(BaseModel):
    to: List[EmailAddress] = Field(description="Recipients")
    subject: str = Field(description="Message subject")
    body: str = Field(description="Message body (HTML or plain)")
    cc: Optional[List[EmailAddress]] = Field(default=None)
    bcc: Optional[List[EmailAddress]] = Field(default=None)
    reply_to: Optional[List[EmailAddress]] = Field(default=None)
    reply_to_message_id: Optional[str] = Field(default=None, description="Message ID to reply to")
    from_: Optional[EmailAddress] = Field(default=None, alias="from")
    attachments: Optional[List[AttachmentInput]] = Field(default=None)
    send_at: Optional[int] = Field(default=None, description="Scheduled send unix timestamp")
    tracking_options: Optional[Dict[str, Any]] = Field(default=None)

    model_config = {"populate_by_name": True}

    def to_request_body(self, default_from: Optional[EmailAddress] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "to": [addr.model_dump(exclude_none=True) for addr in self.to],
            "subject": self.subject,
            "body": self.body,
        }
        optional_lists = {
            "cc": self.cc,
            "bcc": self.bcc,
            "reply_to": self.reply_to,
        }
        for key, addrs in optional_lists.items():
            if addrs:
                body[key] = [addr.model_dump(exclude_none=True) for addr in addrs]
        sender = self.from_ or default_from
        if sender:
            body["from"] = [sender.model_dump(exclude_none=True)]
        if self.reply_to_message_id:
            body["reply_to_message_id"] = self.reply_to_message_id
        if self.send_at is not None:
            body["send_at"] = self.send_at
        if self.tracking_options:
            body["tracking_options"] = self.tracking_options
        if self.attachments:
            body["attachments"] = [
                att.model_dump(exclude_none=True, exclude={"file_path"}) for att in self.attachments
            ]
        return body


SendMessagePayload = Union[SendMessageInput, Dict[str, Any]]
