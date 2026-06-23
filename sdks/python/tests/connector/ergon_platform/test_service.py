"""Tests for Ergon Platform service behavior."""

import base64
import json
from unittest.mock import patch

import pytest

from ergon.connector.ergon_platform.models import ErgonPlatformClient
from ergon.connector.ergon_platform.service import ErgonPlatformService


def _make_client_config() -> ErgonPlatformClient:
    return ErgonPlatformClient(client_id="ek_test", client_secret="eks_test", base_url="https://api.test")


class _Workflow:
    def item_attachment_status(self, item_id, *, field_id):
        return [{"status": "processing"}]

    def item_attachment_results(self, item_id, *, buckets_file_id):
        raise AssertionError("results should not be fetched while the file is processing")


class _Workflows:
    def workflow(self, workflow_id):
        return _Workflow()


class _ErgonClient:
    def __init__(self, **kwargs):
        self.workflows = _Workflows()

    def close(self):
        pass


class TestPipelineResult:
    def test_returns_processing_when_buckets_file_id_is_not_available_yet(self):
        with patch(
            "ergon.connector.ergon_platform.service._get_ergon_client",
            return_value=_ErgonClient,
        ):
            service = ErgonPlatformService(_make_client_config())

        with patch.object(service, "_extract_buckets_file_id", return_value=None):
            result = service.get_pipeline_result("wf", "item", "field")

        assert result == {
            "status": "processing",
            "state": "processing",
            "buckets_file_id": None,
            "process_skip_reason": None,
            "results": None,
        }


class _PhaseCollection:
    def list_fields(self, phase_id, **params):
        return [{"id": "phase-1", "name": "Phase field"}]

    def get(self, phase_id):
        return {"id": phase_id, "workflow_id": "wf-1"}


class _WorkflowWithFields:
    def fields(self, **params):
        return [{"id": "workflow-1", "name": "Workflow field"}]


class _WorkflowsWithFields:
    def __init__(self):
        self.phases = _PhaseCollection()

    def workflow(self, workflow_id):
        return _WorkflowWithFields()


class _ErgonClientWithFields:
    def __init__(self, **kwargs):
        self.workflows = _WorkflowsWithFields()

    def close(self):
        pass


class _DuplicateWorkflowWithFields:
    def fields(self, **params):
        return [{"id": "phase-1", "name": "Duplicated"}, {"id": "workflow-2", "name": "Another"}]


class _WorkflowsWithDuplicateFields:
    def __init__(self):
        self.phases = _PhaseCollection()

    def workflow(self, workflow_id):
        return _DuplicateWorkflowWithFields()


class _ErgonClientWithDuplicateFields:
    def __init__(self, **kwargs):
        self.workflows = _WorkflowsWithDuplicateFields()

    def close(self):
        pass


class TestListPhaseFields:
    def test_merges_phase_and_workflow_fields(self):
        with patch(
            "ergon.connector.ergon_platform.service._get_ergon_client",
            return_value=_ErgonClientWithFields,
        ):
            service = ErgonPlatformService(_make_client_config())

        fields = service.list_phase_fields("ph-1", workflow_id="wf-1")

        assert fields == [
            {"id": "phase-1", "name": "Phase field"},
            {"id": "workflow-1", "name": "Workflow field"},
        ]

    def test_deduplicates_by_field_id_when_merging(self):
        with patch(
            "ergon.connector.ergon_platform.service._get_ergon_client",
            return_value=_ErgonClientWithDuplicateFields,
        ):
            service = ErgonPlatformService(_make_client_config())

        fields = service.list_phase_fields("ph-1", workflow_id="wf-1")

        assert fields == [
            {"id": "phase-1", "name": "Phase field"},
            {"id": "workflow-2", "name": "Another"},
        ]

    def test_requires_workflow_id_when_merging(self):
        with patch(
            "ergon.connector.ergon_platform.service._get_ergon_client",
            return_value=_ErgonClientWithFields,
        ):
            service = ErgonPlatformService(_make_client_config())

        with pytest.raises(ValueError, match="workflow_id is required"):
            service.list_phase_fields("ph-1")

    def test_can_disable_workflow_fields_merge(self):
        with patch(
            "ergon.connector.ergon_platform.service._get_ergon_client",
            return_value=_ErgonClientWithFields,
        ):
            service = ErgonPlatformService(_make_client_config())

        fields = service.list_phase_fields("ph-1", include_workflow_fields=False)

        assert fields == [{"id": "phase-1", "name": "Phase field"}]


class _WorkflowWithItems:
    def __init__(self):
        self.last_items_kwargs = None

    def items(self, **params):
        self.last_items_kwargs = params
        return {"items": [{"id": "item-1", "phase_id": "ph-1", "company_id": "co-1", "title": "T1"}]}


class _WorkflowsWithItems:
    def __init__(self):
        self.workflow_obj = _WorkflowWithItems()

    def workflow(self, workflow_id):
        return self.workflow_obj


class _ErgonClientWithItems:
    def __init__(self, **kwargs):
        self.workflows = _WorkflowsWithItems()

    def close(self):
        pass


def _make_jwt_with_claims(claims: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).decode()
    header = header.rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    payload = payload.rstrip("=")
    return f"{header}.{payload}.signature"


class _TokenExchangeResponse:
    def __init__(self, token: str):
        self._token = token

    def raise_for_status(self):
        return None

    def json(self):
        return {"access_token": self._token}


class TestFetchItemsAssignedToDefault:
    def test_fetch_items_defaults_assigned_to_to_m2m_principal(self):
        with patch(
            "ergon.connector.ergon_platform.service._get_ergon_client",
            return_value=_ErgonClientWithItems,
        ):
            service = ErgonPlatformService(_make_client_config())

        token = _make_jwt_with_claims({"sub": "11111111-1111-1111-1111-111111111111"})
        with patch(
            "ergon.connector.ergon_platform.service.httpx.post",
            return_value=_TokenExchangeResponse(token),
        ):
            txs = service.fetch_items("wf-1", "ph-1")

        assert len(txs) == 1
        assert (
            service.client.workflows.workflow_obj.last_items_kwargs["assigned_to"]
            == "11111111-1111-1111-1111-111111111111"
        )

    def test_fetch_items_keeps_explicit_assigned_to(self):
        with patch(
            "ergon.connector.ergon_platform.service._get_ergon_client",
            return_value=_ErgonClientWithItems,
        ):
            service = ErgonPlatformService(_make_client_config())

        with patch("ergon.connector.ergon_platform.service.httpx.post") as mock_post:
            service.fetch_items("wf-1", "ph-1", assigned_to="22222222-2222-2222-2222-222222222222")

        assert (
            service.client.workflows.workflow_obj.last_items_kwargs["assigned_to"]
            == "22222222-2222-2222-2222-222222222222"
        )
        mock_post.assert_not_called()

    def test_fetch_items_reuses_cached_m2m_principal(self):
        with patch(
            "ergon.connector.ergon_platform.service._get_ergon_client",
            return_value=_ErgonClientWithItems,
        ):
            service = ErgonPlatformService(_make_client_config())

        token = _make_jwt_with_claims({"sub": "33333333-3333-3333-3333-333333333333"})
        with patch(
            "ergon.connector.ergon_platform.service.httpx.post",
            return_value=_TokenExchangeResponse(token),
        ) as mock_post:
            service.fetch_items("wf-1", "ph-1")
            service.fetch_items("wf-1", "ph-1")

        assert mock_post.call_count == 1


class _ItemsWithRelease:
    def __init__(self):
        self.calls = []

    def update(self, item_id, **fields):
        self.calls.append(("update", item_id, fields))
        return {"id": item_id, **fields}

    def release(self, item_id, payload):
        self.calls.append(("release", item_id, payload))
        return {"id": item_id, "released": True}


class _WorkflowsForRelease:
    def __init__(self):
        self.items = _ItemsWithRelease()


class _ErgonClientForRelease:
    def __init__(self, **kwargs):
        self.workflows = _WorkflowsForRelease()

    def close(self):
        pass


class TestReleaseWithDelaySeconds:
    def test_release_with_delay_seconds_updates_minutes_then_releases(self):
        with patch(
            "ergon.connector.ergon_platform.service._get_ergon_client",
            return_value=_ErgonClientForRelease,
        ):
            service = ErgonPlatformService(_make_client_config())

        result = service.release_item("item-1", delay_seconds=90)

        assert result == {"id": "item-1", "released": True}
        assert service.client.workflows.items.calls == [
            ("update", "item-1", {"visibility_timeout_on_release_minutes": 2}),
            ("release", "item-1", {}),
        ]

    def test_release_without_delay_does_not_update_visibility_timeout(self):
        with patch(
            "ergon.connector.ergon_platform.service._get_ergon_client",
            return_value=_ErgonClientForRelease,
        ):
            service = ErgonPlatformService(_make_client_config())

        service.release_item("item-1")

        assert service.client.workflows.items.calls == [
            ("release", "item-1", {}),
        ]

    def test_release_rejects_negative_delay_seconds(self):
        with patch(
            "ergon.connector.ergon_platform.service._get_ergon_client",
            return_value=_ErgonClientForRelease,
        ):
            service = ErgonPlatformService(_make_client_config())

        with pytest.raises(ValueError, match="delay_seconds must be a non-negative integer"):
            service.release_item("item-1", delay_seconds=-1)
