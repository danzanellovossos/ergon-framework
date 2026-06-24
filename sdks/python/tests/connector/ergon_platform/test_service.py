"""Tests for private Ergon Platform domain operations."""

import base64
import json
from unittest.mock import patch

import pytest

from ergon.connector.ergon_platform._operations import _ErgonPlatformOperations
from ergon.connector.ergon_platform.models import ErgonPlatformClient


def _make_client_config() -> ErgonPlatformClient:
    return ErgonPlatformClient(client_id="ek_test", client_secret="eks_test", base_url="https://api.test")


class _Items:
    def __init__(self):
        self.calls = []
        self.items_by_id = {}

    def get(self, item_id, **params):
        return self.items_by_id[item_id]

    def update(self, item_id, **fields):
        self.calls.append(("update", item_id, fields))
        return {"id": item_id, **fields}

    def release(self, item_id, payload):
        self.calls.append(("release", item_id, payload))
        return {"id": item_id, "released": True}


class _PhaseCollection:
    def __init__(self):
        self.phase_fields = [{"id": "phase-1", "name": "Phase field"}]

    def list_fields(self, phase_id, **params):
        return self.phase_fields


class _Workflow:
    def __init__(self):
        self.workflow_fields = [{"id": "workflow-1", "name": "Workflow field"}]
        self.items_response = {"items": []}
        self.last_items_kwargs = None
        self.status_response = [{"status": "processing"}]

    def fields(self, **params):
        return self.workflow_fields

    def items(self, **params):
        self.last_items_kwargs = params
        return self.items_response

    def item_attachment_status(self, item_id, *, field_id):
        return self.status_response

    def item_attachment_results(self, item_id, *, buckets_file_id):
        raise AssertionError("results should not be fetched while the file is processing")


class _Workflows:
    def __init__(self):
        self.items = _Items()
        self.phases = _PhaseCollection()
        self.workflow_obj = _Workflow()

    def workflow(self, workflow_id):
        return self.workflow_obj


class _Client:
    def __init__(self):
        self.workflows = _Workflows()


def _make_operations(client=None) -> _ErgonPlatformOperations:
    return _ErgonPlatformOperations(_make_client_config(), client or _Client())


class TestPipelineResult:
    def test_returns_processing_when_buckets_file_id_is_not_available_yet(self):
        operations = _make_operations()

        with patch.object(operations, "_extract_buckets_file_id", return_value=None):
            result = operations.get_pipeline_result("wf", "item", "field")

        assert result == {
            "status": "processing",
            "state": "processing",
            "buckets_file_id": None,
            "process_skip_reason": None,
            "results": None,
        }


class TestListPhaseFields:
    def test_merges_phase_and_workflow_fields(self):
        fields = _make_operations().list_phase_fields("ph-1", workflow_id="wf-1")

        assert fields == [
            {"id": "phase-1", "name": "Phase field"},
            {"id": "workflow-1", "name": "Workflow field"},
        ]

    def test_deduplicates_by_field_id_when_merging(self):
        client = _Client()
        client.workflows.workflow_obj.workflow_fields = [
            {"id": "phase-1", "name": "Duplicated"},
            {"id": "workflow-2", "name": "Another"},
        ]
        fields = _make_operations(client).list_phase_fields("ph-1", workflow_id="wf-1")

        assert fields == [
            {"id": "phase-1", "name": "Phase field"},
            {"id": "workflow-2", "name": "Another"},
        ]

    def test_requires_workflow_id_when_merging(self):
        with pytest.raises(ValueError, match="workflow_id is required"):
            _make_operations().list_phase_fields("ph-1")

    def test_can_disable_workflow_fields_merge(self):
        fields = _make_operations().list_phase_fields("ph-1", include_workflow_fields=False)

        assert fields == [{"id": "phase-1", "name": "Phase field"}]


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
        operations = _make_operations()
        operations.client.workflows.workflow_obj.items_response = {
            "items": [{"id": "item-1", "phase_id": "ph-1", "company_id": "co-1", "title": "T1"}]
        }
        token = _make_jwt_with_claims({"sub": "11111111-1111-1111-1111-111111111111"})

        with patch(
            "ergon.connector.ergon_platform._operations.httpx.post",
            return_value=_TokenExchangeResponse(token),
        ):
            txs = operations.fetch_items("wf-1", "ph-1")

        assert len(txs) == 1
        assert (
            operations.client.workflows.workflow_obj.last_items_kwargs["assigned_to"]
            == "11111111-1111-1111-1111-111111111111"
        )

    def test_fetch_items_keeps_explicit_assigned_to(self):
        operations = _make_operations()

        with patch("ergon.connector.ergon_platform._operations.httpx.post") as mock_post:
            operations.fetch_items("wf-1", "ph-1", assigned_to="22222222-2222-2222-2222-222222222222")

        assert (
            operations.client.workflows.workflow_obj.last_items_kwargs["assigned_to"]
            == "22222222-2222-2222-2222-222222222222"
        )
        mock_post.assert_not_called()

    def test_fetch_items_reuses_cached_m2m_principal(self):
        operations = _make_operations()
        token = _make_jwt_with_claims({"sub": "33333333-3333-3333-3333-333333333333"})

        with patch(
            "ergon.connector.ergon_platform._operations.httpx.post",
            return_value=_TokenExchangeResponse(token),
        ) as mock_post:
            operations.fetch_items("wf-1", "ph-1")
            operations.fetch_items("wf-1", "ph-1")

        assert mock_post.call_count == 1


class TestReleaseWithDelaySeconds:
    def test_release_with_delay_seconds_updates_minutes_then_releases(self):
        operations = _make_operations()

        result = operations.release_item("item-1", delay_seconds=90)

        assert result == {"id": "item-1", "released": True}
        assert operations.client.workflows.items.calls == [
            ("update", "item-1", {"visibility_timeout_on_release_minutes": 2}),
            ("release", "item-1", {}),
        ]

    def test_release_without_delay_does_not_update_visibility_timeout(self):
        operations = _make_operations()

        operations.release_item("item-1")

        assert operations.client.workflows.items.calls == [
            ("release", "item-1", {}),
        ]

    def test_release_rejects_negative_delay_seconds(self):
        operations = _make_operations()

        with pytest.raises(ValueError, match="delay_seconds must be a non-negative integer"):
            operations.release_item("item-1", delay_seconds=-1)

        assert operations.client.workflows.items.calls == []
