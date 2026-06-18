"""Tests for Ergon Platform service behavior."""

from unittest.mock import patch

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
