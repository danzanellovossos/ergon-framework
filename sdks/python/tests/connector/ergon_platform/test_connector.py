"""Tests for ErgonPlatformConnector — fetch/dispatch mapping, ack, pipeline."""

from unittest.mock import patch

import pytest

from ergon.connector.ergon_platform.connector import ErgonPlatformConnector
from ergon.connector.ergon_platform.models import (
    CreateItemInput,
    ErgonPlatformClient,
    ErgonPlatformConsumerConfig,
    ErgonPlatformProducerConfig,
)
from ergon.connector.transaction import Transaction


def _make_client() -> ErgonPlatformClient:
    return ErgonPlatformClient(client_id="ek_test", client_secret="eks_test", base_url="https://api.test")


def _make_connector(consumer_config=None, producer_config=None) -> ErgonPlatformConnector:
    with patch("ergon.connector.ergon_platform.service._get_ergon_client"):
        return ErgonPlatformConnector(
            client=_make_client(),
            consumer_config=consumer_config,
            producer_config=producer_config,
        )


class TestFetchTransactions:
    def test_fetch_delegates_to_service(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf-1", phase_id="ph-1", batch_size=5)
        connector = _make_connector(consumer_config=config)

        expected_tx = Transaction(id="item-1", payload={"id": "item-1"}, metadata={"workflow_id": "wf-1"})

        with patch.object(connector.service, "fetch_items", return_value=[expected_tx]) as mock_fetch:
            txns = connector.fetch_transactions(batch_size=5)

        assert txns == [expected_tx]
        args, kwargs = mock_fetch.call_args
        assert args == ("wf-1", "ph-1")
        assert kwargs["limit"] == 5
        assert kwargs["offset"] == 0

    def test_fetch_requires_consumer_config(self):
        connector = _make_connector()
        with pytest.raises(ValueError, match="consumer_config"):
            connector.fetch_transactions()


class TestDispatchTransactions:
    def test_dispatch_creates_item(self):
        producer = ErgonPlatformProducerConfig(workflow_id="wf-1", phase_id="ph-1")
        connector = _make_connector(producer_config=producer)

        tx = Transaction(id="new", payload=CreateItemInput(title="Ticket"))

        with patch.object(connector.service, "create_item", return_value={"id": "item-99"}) as mock_create:
            created = connector.dispatch_transactions([tx])

        assert created == ["item-99"]
        args, kwargs = mock_create.call_args
        assert args == ("wf-1", "ph-1", "Ticket")
        assert kwargs["attachment"] is None

    def test_dispatch_with_attachment_result_shape(self):
        producer = ErgonPlatformProducerConfig(workflow_id="wf-1", phase_id="ph-1", attachment_field_id="f1")
        connector = _make_connector(producer_config=producer)

        tx = Transaction(
            id="new",
            payload=CreateItemInput(title="WithFile", attachment="/tmp/x.pdf"),
        )

        result = {"item": {"id": "item-7"}, "upload": {}, "attachments": []}
        with patch.object(connector.service, "create_item", return_value=result) as mock_create:
            created = connector.dispatch_transactions([tx])

        assert created == ["item-7"]
        _, kwargs = mock_create.call_args
        assert kwargs["attachment"] == "/tmp/x.pdf"
        assert kwargs["attachment_field_id"] == "f1"

    def test_dispatch_requires_workflow_id(self):
        connector = _make_connector(producer_config=ErgonPlatformProducerConfig())
        tx = Transaction(id="new", payload=CreateItemInput(title="NoWf"))
        with pytest.raises(ValueError, match="workflow_id"):
            connector.dispatch_transactions([tx])


class TestAckTransaction:
    def test_ack_moves_to_configured_phase(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf", phase_id="ph", ack_phase_id="done")
        connector = _make_connector(consumer_config=config)
        tx = Transaction(id="item-1", payload={})

        with patch.object(connector.service, "move_item_to_phase", return_value={}) as mock_move:
            connector.ack_transaction(tx)

        mock_move.assert_called_once_with("item-1", "done")

    def test_ack_noop_without_ack_phase(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf", phase_id="ph")
        connector = _make_connector(consumer_config=config)
        tx = Transaction(id="item-1", payload={})

        with patch.object(connector.service, "move_item_to_phase") as mock_move:
            connector.ack_transaction(tx)

        mock_move.assert_not_called()


class TestPipelineResult:
    def test_get_pipeline_result_delegates(self):
        connector = _make_connector()
        expected = {"state": "success", "results": {}}
        with patch.object(connector.service, "get_pipeline_result", return_value=expected) as mock_pr:
            result = connector.get_pipeline_result("wf", "item", "field", "bfid")

        assert result == expected
        mock_pr.assert_called_once_with("wf", "item", "field", "bfid")
