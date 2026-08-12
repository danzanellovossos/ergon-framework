"""Tests for AsyncNylasService — async delegation to the sync service."""

from unittest.mock import patch

import pytest

from ergon.connector.nylas.async_service import AsyncNylasService
from ergon.connector.nylas.models import NylasClient

pytestmark = pytest.mark.asyncio(loop_scope="function")


def _make_service() -> AsyncNylasService:
    client = NylasClient(api_key="key", grant_id="grant-1")
    with patch("ergon.connector.nylas.service._get_nylas_client"):
        return AsyncNylasService(client)


class TestDeleteMessage:
    async def test_delegates_to_sync_service(self):
        service = _make_service()

        with patch.object(service._sync, "delete_message") as mock_delete:
            await service.delete_message("msg-1")

        mock_delete.assert_called_once_with("msg-1")
