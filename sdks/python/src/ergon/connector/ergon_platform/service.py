import logging
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from ..transaction import Transaction
from .models import ErgonPlatformClient
from .utils import (
    as_payload,
    classify_status,
    extract_buckets_file_id,
    extract_items,
    extract_status_file_id,
    find_status_entry,
    get_value,
    item_to_transaction,
    latest_status_entry,
)

logger = logging.getLogger(__name__)

_ERGON_PLATFORM_IMPORT_ERROR = "Install the ergon-platform-sdk package in the same environment"


def _get_ergon_client():
    try:
        from ergon_platform import ErgonClient  # type: ignore[reportMissingImports]
    except ImportError as exc:
        raise ImportError(_ERGON_PLATFORM_IMPORT_ERROR) from exc
    return ErgonClient


class ErgonPlatformService:
    def __init__(self, config: ErgonPlatformClient) -> None:
        logger.info("Initializing ErgonPlatformService")
        self.config = config
        ErgonClient = _get_ergon_client()
        self.client = ErgonClient(
            client_id=config.client_id,
            client_secret=config.client_secret,
            base_url=config.base_url,
            company_id=config.company_id,
            timeout=config.timeout,
            max_retries=config.max_retries,
        )

    def close(self) -> None:
        self.client.close()

    def list_workflows(self, *, limit: int = 50, offset: int = 0, **params: Any) -> Any:
        return self.client.workflows.list(limit=limit, offset=offset, **params)

    def list_workflow_phases(self, workflow_id: str, **params: Any) -> Any:
        return self.client.workflows.workflow(workflow_id).phases(**params)

    def list_phase_fields(self, phase_id: str, **params: Any) -> Any:
        return self.client.workflows.phases.list_fields(phase_id, **params)

    def list_phase_items(self, workflow_id: str, phase_id: str, **params: Any) -> Any:
        return self.client.workflows.workflow(workflow_id).items(phase_id=phase_id, **params)

    def get_item(self, item_id: str, **params: Any) -> Any:
        return self.client.workflows.items.get(item_id, **params)

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
        resolved_content_type = (
            content_type or mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        )
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

    def move_item_to_phase(self, item_id: str, phase_id: str) -> Any:
        return self.client.workflows.items.route(item_id, to_phase_id=phase_id)

    def list_item_children(self, item_id: str, **params: Any) -> Any:
        return self.client.workflows.items.children(item_id, **params)

    def list_item_child_targets(self, item_id: str, **params: Any) -> Any:
        return self.client.workflows.items.child_targets(item_id, **params)

    def get_item_child_capabilities(self, item_id: str, **params: Any) -> Any:
        return self.client.workflows.items.child_capabilities(item_id, **params)

    def unlink_item_child(self, item_id: str, child_item_id: str) -> None:
        self.client.workflows.items.remove_child(item_id, child_item_id)

    def fetch_child_items(self, parent_item_id: str, **params: Any) -> List[Transaction]:
        links = self.list_item_children(parent_item_id, **params) or []
        transactions: List[Transaction] = []
        for link in links:
            child_item_id = str(get_value(link, "child_item_id", ""))
            if not child_item_id:
                continue
            child_item = self.get_item(child_item_id)
            child_workflow_id = str(get_value(child_item, "workflow_id", ""))
            transactions.append(item_to_transaction(child_item, child_workflow_id))
        return transactions

    def bulk_create_items(
        self,
        workflow_id: str,
        items: List[Dict[str, Any]],
        *,
        response_format: str = "full",
        **fields: Any,
    ) -> Any:
        return self.client.workflows.items.bulk_create(
            {
                "workflow_id": workflow_id,
                "items": items,
                "response_format": response_format,
                **fields,
            }
        )

    def query_items(
        self, workflow_id: str, query: Optional[Dict[str, Any]] = None, **fields: Any
    ) -> Any:
        return self.client.workflows.workflow(workflow_id).query_items({**(query or {}), **fields})

    def fetch_items_by_query(
        self, workflow_id: str, query: Optional[Dict[str, Any]] = None, **fields: Any
    ) -> List[Transaction]:
        response = self.query_items(workflow_id, query, **fields)
        items = extract_items(response)
        return [item_to_transaction(item, workflow_id) for item in items]

    def list_item_comments(self, item_id: str, **params: Any) -> Any:
        return self.client.workflows.items.list_comments(item_id, **params)

    def add_item_comment(
        self, item_id: str, data: Optional[Dict[str, Any]] = None, **fields: Any
    ) -> Any:
        return self.client.workflows.items.add_comment(item_id, {**(data or {}), **fields})

    def claim_item(
        self, item_id: str, data: Optional[Dict[str, Any]] = None, **fields: Any
    ) -> Any:
        return self.client.workflows.items.claim(item_id, {**(data or {}), **fields})

    def assign_item(self, item_id: str, principal_id: str) -> Any:
        return self.client.workflows.items.assign(item_id, {"principal_id": principal_id})

    def assign_item_group(self, item_id: str, group_id: str) -> Any:
        return self.client.workflows.items.assign_group(item_id, {"group_id": group_id})

    def release_item(
        self, item_id: str, data: Optional[Dict[str, Any]] = None, **fields: Any
    ) -> Any:
        return self.client.workflows.items.release(item_id, {**(data or {}), **fields})

    def route_item_to_global_target(
        self, item_id: str, data: Optional[Dict[str, Any]] = None, **fields: Any
    ) -> Any:
        return self.client.workflows.items.route_to_global_target(
            item_id, {**(data or {}), **fields}
        )

    def list_item_events(self, item_id: str, **params: Any) -> Any:
        return self.client.workflows.items.events(item_id, **params)

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
            find_status_entry(statuses, resolved_file_id)
            if resolved_file_id
            else latest_status_entry(statuses)
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

    def fetch_items(
        self,
        workflow_id: str,
        phase_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        **params: Any,
    ) -> List[Transaction]:
        response = self.list_phase_items(workflow_id, phase_id, limit=limit, offset=offset, **params)
        items = extract_items(response)
        return [item_to_transaction(item, workflow_id) for item in items]

    def get_item_transaction(self, item_id: str, workflow_id: str = "", **params: Any) -> Transaction:
        item = self.get_item(item_id, **params)
        resolved_workflow = workflow_id or str(get_value(item, "workflow_id", ""))
        return item_to_transaction(item, resolved_workflow)

    def _extract_buckets_file_id(self, item_id: str, field_id: str) -> Optional[str]:
        item = self.get_item(item_id)
        return extract_buckets_file_id(item, field_id)
