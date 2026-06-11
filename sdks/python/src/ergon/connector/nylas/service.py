import logging
from typing import Any, Dict, List, Optional

from ..transaction import Transaction
from .models import (
    ClientSideFilter,
    MessageFields,
    MessageQueryFilter,
    NylasClient,
    SendMessagePayload,
)
from .utils import (
    apply_client_side_filter,
    extract_next_cursor,
    extract_response_data,
    message_to_transaction,
    normalize_send_payload,
    serialize_nylas_object,
)

logger = logging.getLogger(__name__)

_NYLAS_IMPORT_ERROR = "Install with: pip install ergon-framework-python[nylas]"


def _get_nylas_client():
    try:
        from nylas import Client
    except ImportError as exc:
        raise ImportError(_NYLAS_IMPORT_ERROR) from exc
    return Client


def _get_attach_builder():
    try:
        from nylas.utils.file_utils import attach_file_request_builder
    except ImportError as exc:
        raise ImportError(_NYLAS_IMPORT_ERROR) from exc
    return attach_file_request_builder


class NylasService:
    def __init__(self, client: NylasClient) -> None:
        self.client = client
        Client = _get_nylas_client()
        self._nylas = Client(client.api_key, client.api_uri)
        self._page_token: Optional[str] = None
        self._exhausted: bool = False

    @property
    def grant_id(self) -> str:
        return self.client.grant_id

    def reset_pagination(self) -> None:
        self._page_token = None
        self._exhausted = False

    def close(self) -> None:
        pass

    def _build_attachments(self, request_body: Dict[str, Any]) -> Dict[str, Any]:
        body = dict(request_body)
        raw_attachments = body.pop("attachments", None) or []
        if not raw_attachments:
            return body

        attach_builder = _get_attach_builder()
        built: List[Any] = []
        for att in raw_attachments:
            if isinstance(att, dict) and att.get("file_path"):
                built.append(attach_builder(att["file_path"]))
            else:
                built.append(att)
        body["attachments"] = built
        return body

    def list_messages(
        self,
        query: MessageQueryFilter,
        limit: int,
        page_token: Optional[str] = None,
        use_internal_pagination: bool = True,
    ) -> Dict[str, Any]:
        if use_internal_pagination and self._exhausted:
            return {"data": [], "next_cursor": None}

        token = page_token if page_token is not None else (self._page_token if use_internal_pagination else None)
        query_params = query.to_query_params(limit=limit, page_token=token)

        response = self._nylas.messages.list(self.grant_id, query_params=query_params)  # type: ignore[reportArgumentType]
        data = extract_response_data(response) or []
        if not isinstance(data, list):
            data = [data]

        next_cursor = extract_next_cursor(response)
        if use_internal_pagination:
            self._page_token = next_cursor
            self._exhausted = next_cursor is None

        return {"data": data, "next_cursor": next_cursor}

    def list_threads(
        self,
        query: MessageQueryFilter,
        limit: int,
        page_token: Optional[str] = None,
        use_internal_pagination: bool = True,
    ) -> Dict[str, Any]:
        if use_internal_pagination and self._exhausted:
            return {"data": [], "next_cursor": None}

        token = page_token if page_token is not None else (self._page_token if use_internal_pagination else None)
        query_params = query.to_query_params(limit=limit, page_token=token)

        response = self._nylas.threads.list(self.grant_id, query_params=query_params)  # type: ignore[reportArgumentType]
        data = extract_response_data(response) or []
        if not isinstance(data, list):
            data = [data]

        next_cursor = extract_next_cursor(response)
        if use_internal_pagination:
            self._page_token = next_cursor
            self._exhausted = next_cursor is None

        return {"data": data, "next_cursor": next_cursor}

    def find_message(
        self,
        message_id: str,
        fields: Optional[MessageFields] = None,
    ) -> Dict[str, Any]:
        query_params: Dict[str, Any] = {}
        if fields is not None:
            query_params["fields"] = fields.value
        response = self._nylas.messages.find(self.grant_id, message_id, query_params=query_params or None)  # type: ignore[reportArgumentType]
        data = extract_response_data(response)
        return data if isinstance(data, dict) else serialize_nylas_object(data)

    def resolve_attachments(self, message: dict, *, download: bool) -> list:
        if not download:
            return message.get("attachments") or []

        enriched = []
        for att in message.get("attachments") or []:
            att_id = att.get("id")
            if not att_id:
                enriched.append(att)
                continue
            content = self.download_attachment(str(message.get("id", "")), str(att_id))
            enriched.append({**att, "content": content})
        return enriched

    def fetch_items(
        self,
        query: MessageQueryFilter,
        limit: int,
        *,
        client_side_filter: Optional[ClientSideFilter] = None,
        fetch_unit: str = "message",
        download_attachments: bool = False,
    ) -> List[Transaction]:
        if fetch_unit == "thread":
            result = self.list_threads(query, limit)
            items = result.get("data") or []
            items = apply_client_side_filter(items, client_side_filter)
            return [
                Transaction(
                    id=str(item.get("id", "")),
                    payload=item,
                    metadata={
                        "grant_id": self.grant_id,
                        "thread_id": item.get("id"),
                        "fetch_unit": "thread",
                        "has_attachment": bool(item.get("attachments")),
                        "unread": item.get("unread"),
                    },
                )
                for item in items
            ]

        result = self.list_messages(query, limit)
        messages = result.get("data") or []
        messages = apply_client_side_filter(messages, client_side_filter)

        transactions: List[Transaction] = []
        for message in messages:
            attachments_meta = self.resolve_attachments(message, download=download_attachments)
            transactions.append(message_to_transaction(message, self.grant_id, attachments_meta=attachments_meta))
        return transactions

    def find_message_transaction(
        self,
        message_id: str,
        *,
        fields: Optional[MessageFields] = None,
        download_attachments: bool = False,
    ) -> Transaction:
        message = self.find_message(message_id, fields=fields)
        attachments_meta = self.resolve_attachments(message, download=download_attachments)
        return message_to_transaction(message, self.grant_id, attachments_meta=attachments_meta)

    def get_messages_count(self, query: MessageQueryFilter, max_pages: Optional[int] = None) -> int:
        count = 0
        page_token: Optional[str] = None
        pages = 0
        while True:
            result = self.list_messages(query, limit=100, page_token=page_token, use_internal_pagination=False)
            batch = result.get("data") or []
            count += len(batch)
            page_token = result.get("next_cursor")
            pages += 1
            if not page_token or not batch:
                break
            if max_pages is not None and pages >= max_pages:
                break
        return count

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        response = self._nylas.attachments.download_bytes(
            self.grant_id,
            attachment_id,
            query_params={"message_id": message_id},
        )
        if isinstance(response, bytes):
            return response
        content = getattr(response, "content", None)
        if content is not None:
            return content
        return bytes(response)

    def get_attachment_metadata(self, message_id: str, attachment_id: str) -> Dict[str, Any]:
        response = self._nylas.attachments.find(
            self.grant_id,
            attachment_id,
            query_params={"message_id": message_id},
        )
        data = extract_response_data(response)
        return data if isinstance(data, dict) else serialize_nylas_object(data)

    def send_message(self, payload: SendMessagePayload, default_from: Optional[Any] = None) -> Dict[str, Any]:
        request_body = self._build_attachments(normalize_send_payload(payload, default_from=default_from))
        response = self._nylas.messages.send(self.grant_id, request_body=request_body)  # type: ignore[reportArgumentType]
        data = extract_response_data(response)
        return data if isinstance(data, dict) else serialize_nylas_object(data)

    def create_draft(self, payload: SendMessagePayload, default_from: Optional[Any] = None) -> Dict[str, Any]:
        request_body = self._build_attachments(normalize_send_payload(payload, default_from=default_from))
        response = self._nylas.drafts.create(self.grant_id, request_body=request_body)  # type: ignore[reportArgumentType]
        data = extract_response_data(response)
        return data if isinstance(data, dict) else serialize_nylas_object(data)

    def send_draft(self, draft_id: str) -> Dict[str, Any]:
        response = self._nylas.drafts.send(self.grant_id, draft_id)
        data = extract_response_data(response)
        return data if isinstance(data, dict) else serialize_nylas_object(data)

    def update_message(self, message_id: str, request_body: Dict[str, Any]) -> Dict[str, Any]:
        response = self._nylas.messages.update(self.grant_id, message_id, request_body=request_body)  # type: ignore[reportArgumentType]
        data = extract_response_data(response)
        return data if isinstance(data, dict) else serialize_nylas_object(data)

    def list_folders(self) -> List[Dict[str, Any]]:
        response = self._nylas.folders.list(self.grant_id)
        data = extract_response_data(response) or []
        if not isinstance(data, list):
            data = [data]
        return data

    def create_folder(self, name: str, parent: Optional[str] = None) -> Dict[str, Any]:
        request_body: Dict[str, Any] = {"name": name}
        if parent is not None:
            request_body["parent"] = parent
        response = self._nylas.folders.create(self.grant_id, request_body=request_body)  # type: ignore[reportArgumentType]
        data = extract_response_data(response)
        return data if isinstance(data, dict) else serialize_nylas_object(data)

    def update_folder(self, folder_id: str, request_body: Dict[str, Any]) -> Dict[str, Any]:
        response = self._nylas.folders.update(self.grant_id, folder_id, request_body=request_body)  # type: ignore[reportArgumentType]
        data = extract_response_data(response)
        return data if isinstance(data, dict) else serialize_nylas_object(data)
