import math
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from ...transaction import Transaction
from ..models import ErgonPlatformClient
from .utils import (
    as_payload,
    classify_status,
    decode_jwt_claims,
    extract_buckets_file_id,
    extract_items,
    extract_status_file_id,
    extract_total,
    find_status_entry,
    get_value,
    item_to_transaction,
    latest_status_entry,
)


class _ErgonPlatformOperations:
    def __init__(self, config: ErgonPlatformClient, client: Any) -> None:
        self.config = config
        self.client = client
        self._m2m_principal_id: Optional[str] = None

    def list_phase_fields(
        self,
        phase_id: str,
        *,
        workflow_id: Optional[str] = None,
        include_workflow_fields: bool = True,
        **params: Any,
    ) -> Any:
        phase_fields = self.client.workflows.phases.list_fields(phase_id, **params)
        if not include_workflow_fields:
            return phase_fields

        if not workflow_id:
            raise ValueError(
                "workflow_id is required when include_workflow_fields=True "
                "(the platform API does not support GET /workflows/phases/{id})"
            )

        workflow_fields = self.client.workflows.workflow(workflow_id).fields(**params)
        return self._merge_unique_fields(phase_fields, workflow_fields)

    def create_item(
        self,
        workflow_id: str,
        phase_id: str,
        title: str,
        *,
        parent_item_id: Optional[str] = None,
        field_values: Optional[Dict[str, Any]] = None,
        attachment: Optional[Any] = None,
        attachment_field_id: Optional[str] = None,
        content_type: Optional[str] = None,
        **fields: Any,
    ) -> Any:
        wf = self.client.workflows.workflow(workflow_id)
        item = wf.create_item(
            title=title,
            phase_id=phase_id,
            parent_item_id=parent_item_id,
            field_values=field_values,
            **fields,
        )

        if attachment is None:
            return item

        if not attachment_field_id:
            raise ValueError("attachment_field_id is required when attachment is provided")

        item_id = str(get_value(item, "id"))
        file_path = Path(attachment)
        resolved_content_type = content_type or mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        file_size = file_path.stat().st_size

        upload = wf.item_attachment_upload_url(
            item_id=item_id,
            field_id=attachment_field_id,
            filename=file_path.name,
            content_type=resolved_content_type,
            size=file_size,
        )

        with file_path.open("rb") as file:
            response = httpx.put(
                get_value(upload, "upload_url"),
                content=file.read(),
                headers={"Content-Type": resolved_content_type},
            )
        response.raise_for_status()

        attachments = wf.confirm_item_attachment(
            item_id=item_id,
            field_id=attachment_field_id,
            object_key=get_value(upload, "object_key"),
            filename=file_path.name,
            content_type=resolved_content_type,
            size=file_size,
        )

        return {
            "item": as_payload(item),
            "upload": as_payload(upload),
            "attachments": as_payload(attachments),
        }

    def fetch_child_items(self, parent_item_id: str, **params: Any) -> List[Transaction]:
        links = self.client.workflows.items.children(parent_item_id, **params) or []
        transactions: List[Transaction] = []
        for link in links:
            child_item_id = str(get_value(link, "child_item_id", ""))
            if not child_item_id:
                continue
            child_item = self.client.workflows.items.get(child_item_id)
            child_workflow_id = str(get_value(child_item, "workflow_id", ""))
            transactions.append(item_to_transaction(child_item, child_workflow_id))
        return transactions

    def fetch_items_by_query(
        self, workflow_id: str, query: Optional[Dict[str, Any]] = None, **fields: Any
    ) -> List[Transaction]:
        response = self.client.workflows.workflow(workflow_id).query_items({**(query or {}), **fields})
        items = extract_items(response)
        return [item_to_transaction(item, workflow_id) for item in items]

    def release_item(
        self,
        item_id: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        delay_seconds: Optional[int] = None,
        **fields: Any,
    ) -> Any:
        if delay_seconds is not None:
            if delay_seconds < 0:
                raise ValueError("delay_seconds must be a non-negative integer")
            if delay_seconds > 0:
                release_minutes = math.ceil(delay_seconds / 60)
                self.client.workflows.items.update(
                    item_id,
                    visibility_timeout_on_release_minutes=release_minutes,
                )
        return self.client.workflows.items.release(item_id, {**(data or {}), **fields})

    def get_pipeline_result(
        self,
        workflow_id: str,
        item_id: str,
        field_id: str,
        buckets_file_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        wf = self.client.workflows.workflow(workflow_id)
        resolved_file_id = buckets_file_id or self._extract_buckets_file_id(item_id, field_id)
        statuses = wf.item_attachment_status(item_id, field_id=field_id)

        status_entry = (
            find_status_entry(statuses, resolved_file_id) if resolved_file_id else latest_status_entry(statuses)
        )
        resolved_file_id = resolved_file_id or extract_status_file_id(status_entry)
        raw_status = str(get_value(status_entry, "status", "unknown")).lower()
        state = classify_status(raw_status)

        if state == "success":
            if not resolved_file_id:
                raise ValueError("buckets_file_id not found for the given item and field")
            return {
                "status": raw_status,
                "state": "success",
                "buckets_file_id": resolved_file_id,
                "results": wf.item_attachment_results(item_id, buckets_file_id=resolved_file_id),
            }

        return {
            "status": raw_status,
            "state": state,
            "buckets_file_id": resolved_file_id,
            "process_skip_reason": get_value(status_entry, "process_skip_reason"),
            "results": None,
        }

    def get_phase_items_count(self, workflow_id: str, phase_id: str, **params: Any) -> int:
        response = self.client.workflows.workflow(workflow_id).items(phase_id=phase_id, limit=1, offset=0, **params)
        return extract_total(response)

    def fetch_items(
        self,
        workflow_id: str,
        phase_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        **params: Any,
    ) -> List[Transaction]:
        query_params = dict(params)
        if "assigned" not in query_params and not query_params.get("assigned_to"):
            query_params["assigned_to"] = self._get_m2m_principal_id()
        response = self.client.workflows.workflow(workflow_id).items(
            phase_id=phase_id,
            limit=limit,
            offset=offset,
            **query_params,
        )
        items = extract_items(response)
        return [item_to_transaction(item, workflow_id) for item in items]

    def get_item_transaction(self, item_id: str, workflow_id: str = "", **params: Any) -> Transaction:
        item = self.client.workflows.items.get(item_id, **params)
        resolved_workflow = workflow_id or str(get_value(item, "workflow_id", ""))
        return item_to_transaction(item, resolved_workflow)

    def _extract_buckets_file_id(self, item_id: str, field_id: str) -> Optional[str]:
        item = self.client.workflows.items.get(item_id)
        return extract_buckets_file_id(item, field_id)

    def _get_m2m_principal_id(self) -> str:
        if self._m2m_principal_id:
            return self._m2m_principal_id

        response = httpx.post(
            f"{self.config.base_url.rstrip('/')}/v1/auth/token",
            json={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
            },
            timeout=self.config.timeout,
        )
        response.raise_for_status()
        token = response.json().get("access_token")
        if not token:
            raise ValueError("IAM token exchange did not return access_token")

        claims = decode_jwt_claims(str(token))
        principal_id = claims.get("sub")
        if not isinstance(principal_id, str) or not principal_id.strip():
            raise ValueError("M2M token does not contain principal id in 'sub' claim")

        self._m2m_principal_id = principal_id
        return principal_id

    @staticmethod
    def _merge_unique_fields(phase_fields: Any, workflow_fields: Any) -> Any:
        if not isinstance(phase_fields, list) or not isinstance(workflow_fields, list):
            return phase_fields

        merged: list[Any] = []
        seen_ids: set[str] = set()

        for field in [*phase_fields, *workflow_fields]:
            field_id = str(get_value(field, "id", ""))
            if field_id and field_id in seen_ids:
                continue
            if field_id:
                seen_ids.add(field_id)
            merged.append(field)
        return merged
