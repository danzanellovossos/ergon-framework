import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..transaction import Transaction
from .models import ErgonPlatformClient
from .service import ErgonPlatformService

logger = logging.getLogger(__name__)


class AsyncErgonPlatformService:
    def __init__(self, config: ErgonPlatformClient) -> None:
        self._sync = ErgonPlatformService(config)

    @property
    def config(self) -> ErgonPlatformClient:
        return self._sync.config

    async def close(self) -> None:
        await asyncio.to_thread(self._sync.close)

    async def list_workflows(self, *, limit: int = 50, offset: int = 0, **params: Any) -> Any:
        return await asyncio.to_thread(lambda: self._sync.list_workflows(limit=limit, offset=offset, **params))

    async def list_workflow_phases(self, workflow_id: str, **params: Any) -> Any:
        return await asyncio.to_thread(lambda: self._sync.list_workflow_phases(workflow_id, **params))

    async def list_phase_fields(
        self,
        phase_id: str,
        *,
        workflow_id: Optional[str] = None,
        include_workflow_fields: bool = True,
        **params: Any,
    ) -> Any:
        return await asyncio.to_thread(
            lambda: self._sync.list_phase_fields(
                phase_id,
                workflow_id=workflow_id,
                include_workflow_fields=include_workflow_fields,
                **params,
            )
        )

    async def list_phase_items(self, workflow_id: str, phase_id: str, **params: Any) -> Any:
        return await asyncio.to_thread(lambda: self._sync.list_phase_items(workflow_id, phase_id, **params))

    async def get_item(self, item_id: str, **params: Any) -> Any:
        return await asyncio.to_thread(lambda: self._sync.get_item(item_id, **params))

    async def create_item(
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
        return await asyncio.to_thread(
            lambda: self._sync.create_item(
                workflow_id,
                phase_id,
                title,
                parent_item_id=parent_item_id,
                field_values=field_values,
                attachment=attachment,
                attachment_field_id=attachment_field_id,
                content_type=content_type,
                **fields,
            )
        )

    async def move_item_to_phase(self, item_id: str, phase_id: str) -> Any:
        return await asyncio.to_thread(self._sync.move_item_to_phase, item_id, phase_id)

    async def list_item_children(self, item_id: str, **params: Any) -> Any:
        return await asyncio.to_thread(lambda: self._sync.list_item_children(item_id, **params))

    async def list_item_child_targets(self, item_id: str, **params: Any) -> Any:
        return await asyncio.to_thread(lambda: self._sync.list_item_child_targets(item_id, **params))

    async def get_item_child_capabilities(self, item_id: str, **params: Any) -> Any:
        return await asyncio.to_thread(lambda: self._sync.get_item_child_capabilities(item_id, **params))

    async def unlink_item_child(self, item_id: str, child_item_id: str) -> None:
        await asyncio.to_thread(self._sync.unlink_item_child, item_id, child_item_id)

    async def fetch_child_items(self, parent_item_id: str, **params: Any) -> List[Transaction]:
        return await asyncio.to_thread(lambda: self._sync.fetch_child_items(parent_item_id, **params))

    async def bulk_create_items(
        self,
        workflow_id: str,
        items: List[Dict[str, Any]],
        *,
        response_format: str = "full",
        **fields: Any,
    ) -> Any:
        return await asyncio.to_thread(
            lambda: self._sync.bulk_create_items(workflow_id, items, response_format=response_format, **fields)
        )

    async def query_items(self, workflow_id: str, query: Optional[Dict[str, Any]] = None, **fields: Any) -> Any:
        return await asyncio.to_thread(lambda: self._sync.query_items(workflow_id, query, **fields))

    async def fetch_items_by_query(
        self, workflow_id: str, query: Optional[Dict[str, Any]] = None, **fields: Any
    ) -> List[Transaction]:
        return await asyncio.to_thread(lambda: self._sync.fetch_items_by_query(workflow_id, query, **fields))

    async def list_item_comments(self, item_id: str, **params: Any) -> Any:
        return await asyncio.to_thread(lambda: self._sync.list_item_comments(item_id, **params))

    async def add_item_comment(self, item_id: str, data: Optional[Dict[str, Any]] = None, **fields: Any) -> Any:
        return await asyncio.to_thread(lambda: self._sync.add_item_comment(item_id, data, **fields))

    async def claim_item(self, item_id: str, data: Optional[Dict[str, Any]] = None, **fields: Any) -> Any:
        return await asyncio.to_thread(lambda: self._sync.claim_item(item_id, data, **fields))

    async def assign_item(self, item_id: str, principal_id: str) -> Any:
        return await asyncio.to_thread(self._sync.assign_item, item_id, principal_id)

    async def assign_item_group(self, item_id: str, group_id: str) -> Any:
        return await asyncio.to_thread(self._sync.assign_item_group, item_id, group_id)

    async def release_item(
        self,
        item_id: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        delay_seconds: Optional[int] = None,
        **fields: Any,
    ) -> Any:
        return await asyncio.to_thread(
            lambda: self._sync.release_item(
                item_id,
                data,
                delay_seconds=delay_seconds,
                **fields,
            )
        )

    async def route_item_to_global_target(
        self, item_id: str, data: Optional[Dict[str, Any]] = None, **fields: Any
    ) -> Any:
        return await asyncio.to_thread(lambda: self._sync.route_item_to_global_target(item_id, data, **fields))

    async def list_item_events(self, item_id: str, **params: Any) -> Any:
        return await asyncio.to_thread(lambda: self._sync.list_item_events(item_id, **params))

    async def get_pipeline_result(
        self,
        workflow_id: str,
        item_id: str,
        field_id: str,
        buckets_file_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self._sync.get_pipeline_result,
            workflow_id,
            item_id,
            field_id,
            buckets_file_id,
        )

    async def get_phase_items_count(self, workflow_id: str, phase_id: str, **params: Any) -> int:
        return await asyncio.to_thread(lambda: self._sync.get_phase_items_count(workflow_id, phase_id, **params))

    async def fetch_items(
        self,
        workflow_id: str,
        phase_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        **params: Any,
    ) -> List[Transaction]:
        return await asyncio.to_thread(
            lambda: self._sync.fetch_items(workflow_id, phase_id, limit=limit, offset=offset, **params)
        )

    async def get_item_transaction(self, item_id: str, workflow_id: str = "", **params: Any) -> Transaction:
        return await asyncio.to_thread(lambda: self._sync.get_item_transaction(item_id, workflow_id, **params))
