"""Tests for Ergon Platform service behavior."""

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
