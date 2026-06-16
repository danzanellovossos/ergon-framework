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
        return await asyncio.to_thread(
            lambda: self._sync.list_workflows(limit=limit, offset=offset, **params)
        )

    async def list_workflow_phases(self, workflow_id: str, **params: Any) -> Any:
        return await asyncio.to_thread(lambda: self._sync.list_workflow_phases(workflow_id, **params))

    async def list_phase_fields(self, phase_id: str, **params: Any) -> Any:
        return await asyncio.to_thread(lambda: self._sync.list_phase_fields(phase_id, **params))

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
                field_values=field_values,
                attachment=attachment,
                attachment_field_id=attachment_field_id,
                content_type=content_type,
                **fields,
            )
        )

    async def move_item_to_phase(self, item_id: str, phase_id: str) -> Any:
        return await asyncio.to_thread(self._sync.move_item_to_phase, item_id, phase_id)

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
        return await asyncio.to_thread(
            lambda: self._sync.get_item_transaction(item_id, workflow_id, **params)
        )
