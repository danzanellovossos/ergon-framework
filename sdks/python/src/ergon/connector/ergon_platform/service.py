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
    find_status_entry,
    get_value,
    item_to_transaction,
)

logger = logging.getLogger(__name__)

_ERGON_PLATFORM_IMPORT_ERROR = "Install with: pip install ergon-framework-python[ergon_platform]"


def _get_ergon_client():
    try:
        from ergon_platform import ErgonClient
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

    def get_pipeline_result(
        self,
        workflow_id: str,
        item_id: str,
        field_id: str,
        buckets_file_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        wf = self.client.workflows.workflow(workflow_id)
        resolved_file_id = buckets_file_id or self._extract_buckets_file_id(item_id, field_id)
        if not resolved_file_id:
            raise ValueError("buckets_file_id not found for the given item and field")

        statuses = wf.item_attachment_status(item_id, field_id=field_id)
        status_entry = find_status_entry(statuses, resolved_file_id)
        raw_status = str(get_value(status_entry, "status", "unknown")).lower()
        state = classify_status(raw_status)

        if state == "success":
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
