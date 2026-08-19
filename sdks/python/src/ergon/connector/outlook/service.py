import base64
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from ..transaction import Transaction
from .models import (
    OutlookEmailAddress,
    OutlookFlagStatus,
    OutlookGraphClient,
    OutlookMessageQuery,
    OutlookProducerConfig,
    OutlookSendMessagePayload,
    OutlookWellKnownFolder,
)
from .utils import message_to_transaction, normalize_send_payload

logger = logging.getLogger(__name__)

_OUTLOOK_IMPORT_ERROR = "Install with: pip install ergon-framework-python[outlook]"


def _graph_error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or response.reason or "").strip()
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or error.get("code")
        if message:
            return str(message)
    return (response.reason or "").strip()


def _permission_hint(method: str, path: str) -> str:
    target = f"{method} {path}".casefold()
    if any(token in target for token in ("/sendmail", "/reply", "/replyall", "/forward")):
        return "This operation requires the Mail.Send application permission."
    if method.upper() in {"PATCH", "DELETE"} or "/move" in target:
        return "This operation requires the Mail.ReadWrite application permission."
    return "This operation requires the Mail.Read application permission."


def _raise_graph_error(method: str, path: str, response: requests.Response) -> None:
    if response.status_code == 403:
        detail = _graph_error_detail(response)
        suffix = f" Graph: {detail}" if detail else ""
        raise requests.HTTPError(
            f"403 Forbidden from Microsoft Graph. {_permission_hint(method, path)}{suffix}",
            response=response,
        )
    response.raise_for_status()


def _get_msal_app_class():
    try:
        from msal import ConfidentialClientApplication
    except ImportError as exc:
        raise ImportError(_OUTLOOK_IMPORT_ERROR) from exc
    return ConfidentialClientApplication


class OutlookGraphService:
    """Low-level Microsoft Graph mail service using application credentials."""

    def __init__(
        self,
        client: OutlookGraphClient,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.client = client
        self._session = session or requests.Session()
        self._msal_app: Any = None
        self._access_token: Optional[str] = None
        self._next_link: Optional[str] = None
        self._exhausted = False

    @property
    def user_email(self) -> str:
        return self.client.user_email

    def close(self) -> None:
        self._session.close()

    def reset_pagination(self) -> None:
        self._next_link = None
        self._exhausted = False

    def _authenticate(self, *, force: bool = False) -> str:
        if self._access_token and not force:
            return self._access_token
        if self._msal_app is None or force:
            app_class = _get_msal_app_class()
            self._msal_app = app_class(
                client_id=self.client.client_id,
                client_credential=self.client.client_secret,
                authority=self.client.authority,
            )
        token_response = self._msal_app.acquire_token_for_client(scopes=self.client.scopes)
        access_token = token_response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            description = token_response.get("error_description") or token_response.get("error") or "unknown error"
            raise RuntimeError(f"Microsoft Graph authentication failed: {description}")
        self._access_token = access_token
        return access_token

    def _headers(self, *, eventual_consistency: bool = False) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._authenticate()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if eventual_consistency:
            headers["ConsistencyLevel"] = "eventual"
        return headers

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            base = self.client.graph_base_url.rstrip("/")
            if path != base and not path.startswith(f"{base}/"):
                raise ValueError("Microsoft Graph pagination URL is outside graph_base_url")
            return path
        return f"{self.client.graph_base_url.rstrip('/')}/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        eventual_consistency: bool = False,
    ) -> requests.Response:
        response = self._session.request(
            method,
            self._url(path),
            headers=self._headers(eventual_consistency=eventual_consistency),
            params=params,
            json=json,
            timeout=self.client.timeout_sec,
        )
        if response.status_code == 401:
            self._authenticate(force=True)
            response = self._session.request(
                method,
                self._url(path),
                headers=self._headers(eventual_consistency=eventual_consistency),
                params=params,
                json=json,
                timeout=self.client.timeout_sec,
            )
        if response.status_code >= 400:
            _raise_graph_error(method, path, response)
        return response

    def _user_path(self) -> str:
        return f"users/{quote(self.user_email, safe='')}"

    @staticmethod
    def _response_data(response: requests.Response) -> Dict[str, Any]:
        if response.status_code == 204 or not response.content:
            return {}
        data = response.json()
        return data if isinstance(data, dict) else {}

    def list_messages(
        self,
        query: OutlookMessageQuery,
        limit: int,
        next_link: Optional[str] = None,
        use_internal_pagination: bool = True,
    ) -> Dict[str, Any]:
        if use_internal_pagination and self._exhausted:
            return {"data": [], "next_link": None}

        cursor = next_link if next_link is not None else (self._next_link if use_internal_pagination else None)
        if cursor:
            response = self._request("GET", cursor, eventual_consistency=query.requires_eventual_consistency)
        else:
            folder = quote(query.folder_id, safe="")
            response = self._request(
                "GET",
                f"{self._user_path()}/mailFolders/{folder}/messages",
                params=query.to_query_params(top=limit),
                eventual_consistency=query.requires_eventual_consistency,
            )
        body = self._response_data(response)
        messages = body.get("value") or []
        if not isinstance(messages, list):
            messages = []
        cursor = body.get("@odata.nextLink")
        cursor = cursor if isinstance(cursor, str) and cursor else None
        if use_internal_pagination:
            self._next_link = cursor
            self._exhausted = cursor is None
        return {"data": messages, "next_link": cursor}

    def find_message(self, message_id: str, *, select: Optional[List[str]] = None) -> Dict[str, Any]:
        params = {"$select": ",".join(select)} if select else None
        response = self._request(
            "GET",
            f"{self._user_path()}/messages/{quote(message_id, safe='')}",
            params=params,
        )
        return self._response_data(response)

    def list_attachments(self, message_id: str, *, download_content: bool = False) -> List[Dict[str, Any]]:
        path: Optional[str] = f"{self._user_path()}/messages/{quote(message_id, safe='')}/attachments"
        attachments: List[Dict[str, Any]] = []
        while path:
            response = self._request("GET", path)
            body = self._response_data(response)
            for item in body.get("value") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("@odata.type") != "#microsoft.graph.fileAttachment":
                    continue
                attachment = {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "content_type": item.get("contentType"),
                    "size": item.get("size"),
                    "is_inline": item.get("isInline", False),
                }
                if download_content and item.get("contentBytes"):
                    attachment["content"] = base64.b64decode(item["contentBytes"])
                attachments.append(attachment)
            next_link = body.get("@odata.nextLink")
            path = next_link if isinstance(next_link, str) and next_link else None
        return attachments

    def save_attachment(
        self,
        attachment: Dict[str, Any],
        destination: str | Path,
        overwrite: bool = False,
    ) -> Path:
        name = Path(str(attachment.get("name") or "")).name
        content = attachment.get("content")
        if not name or not isinstance(content, bytes):
            raise ValueError("Attachment requires a safe name and downloaded byte content")

        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        if path.exists() and not overwrite:
            stem, suffix = path.stem, path.suffix
            counter = 1
            while path.exists():
                path = directory / f"{stem}_{counter}{suffix}"
                counter += 1
        path.write_bytes(content)
        return path

    def fetch_items(
        self,
        query: OutlookMessageQuery,
        limit: int,
        download_attachments: bool = False,
    ) -> List[Transaction]:
        result = self.list_messages(query, limit)
        transactions: List[Transaction] = []
        for message in result["data"]:
            attachments: List[Dict[str, Any]] = []
            if message.get("hasAttachments"):
                attachments = self.list_attachments(
                    str(message.get("id", "")),
                    download_content=download_attachments,
                )
            transactions.append(message_to_transaction(message, self.user_email, attachments))
        return transactions

    def find_message_transaction(
        self,
        message_id: str,
        select: Optional[List[str]] = None,
        download_attachments: bool = False,
    ) -> Transaction:
        message = self.find_message(message_id, select=select)
        attachments: List[Dict[str, Any]] = []
        if message.get("hasAttachments"):
            attachments = self.list_attachments(message_id, download_content=download_attachments)
        return message_to_transaction(message, self.user_email, attachments)

    def get_messages_count(self, query: OutlookMessageQuery, *, max_pages: Optional[int] = None) -> int:
        count = 0
        pages = 0
        next_link: Optional[str] = None
        while True:
            result = self.list_messages(
                query,
                limit=1000,
                next_link=next_link,
                use_internal_pagination=False,
            )
            batch = result["data"]
            count += len(batch)
            pages += 1
            next_link = result["next_link"]
            if not batch or not next_link:
                break
            if max_pages is not None and pages >= max_pages:
                break
        return count

    def send_message(
        self,
        payload: OutlookSendMessagePayload,
        producer_config: Optional[OutlookProducerConfig] = None,
    ) -> None:
        config = producer_config or OutlookProducerConfig()
        self._request(
            "POST",
            f"{self._user_path()}/sendMail",
            json={
                "message": normalize_send_payload(payload),
                "saveToSentItems": config.save_to_sent_items,
            },
        )

    def update_message(self, message_id: str, request_body: Dict[str, Any]) -> Dict[str, Any]:
        response = self._request(
            "PATCH",
            f"{self._user_path()}/messages/{quote(message_id, safe='')}",
            json=request_body,
        )
        return self._response_data(response)

    def mark_as_read(self, message_id: str) -> Dict[str, Any]:
        return self.update_message(message_id, {"isRead": True})

    def mark_as_unread(self, message_id: str) -> Dict[str, Any]:
        return self.update_message(message_id, {"isRead": False})

    def set_flag(
        self,
        message_id: str,
        status: OutlookFlagStatus | str = OutlookFlagStatus.FLAGGED,
    ) -> Dict[str, Any]:
        flag_status = OutlookFlagStatus(status)
        return self.update_message(message_id, {"flag": {"flagStatus": flag_status.value}})

    def set_categories(self, message_id: str, categories: List[str]) -> Dict[str, Any]:
        normalized = list(dict.fromkeys(category.strip() for category in categories if category.strip()))
        return self.update_message(message_id, {"categories": normalized})

    def move_message(self, message_id: str, folder_id: OutlookWellKnownFolder | str) -> Dict[str, Any]:
        destination = folder_id.value if isinstance(folder_id, OutlookWellKnownFolder) else folder_id
        response = self._request(
            "POST",
            f"{self._user_path()}/messages/{quote(message_id, safe='')}/move",
            json={"destinationId": destination},
        )
        return self._response_data(response)

    def delete_message(self, message_id: str) -> None:
        self._request(
            "DELETE",
            f"{self._user_path()}/messages/{quote(message_id, safe='')}",
        )

    def reply(self, message_id: str, *, comment: str = "") -> None:
        self._request(
            "POST",
            f"{self._user_path()}/messages/{quote(message_id, safe='')}/reply",
            json={"comment": comment},
        )

    def reply_all(self, message_id: str, *, comment: str = "") -> None:
        self._request(
            "POST",
            f"{self._user_path()}/messages/{quote(message_id, safe='')}/replyAll",
            json={"comment": comment},
        )

    def forward(
        self,
        message_id: str,
        to: List[OutlookEmailAddress],
        comment: str = "",
    ) -> None:
        self._request(
            "POST",
            f"{self._user_path()}/messages/{quote(message_id, safe='')}/forward",
            json={
                "comment": comment,
                "toRecipients": [address.to_graph() for address in to],
            },
        )

    def list_mail_folders(self, *, include_hidden: bool = False) -> List[Dict[str, Any]]:
        response = self._request(
            "GET",
            f"{self._user_path()}/mailFolders",
            params={"includeHiddenFolders": str(include_hidden).lower()},
        )
        body = self._response_data(response)
        folders = body.get("value") or []
        return folders if isinstance(folders, list) else []

    def get_mail_folder(self, folder_id: OutlookWellKnownFolder | str) -> Dict[str, Any]:
        value = folder_id.value if isinstance(folder_id, OutlookWellKnownFolder) else folder_id
        response = self._request(
            "GET",
            f"{self._user_path()}/mailFolders/{quote(value, safe='')}",
        )
        return self._response_data(response)
