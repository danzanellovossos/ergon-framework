"""Tests for ErgonPlatformConnector — fetch/dispatch mapping, ack, count."""

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

    def test_fetch_forwards_assigned_to_from_list_params(self):
        config = ErgonPlatformConsumerConfig(
            workflow_id="wf-1",
            phase_id="ph-1",
            list_params={"assigned_to": "44444444-4444-4444-4444-444444444444"},
        )
        connector = _make_connector(consumer_config=config)
        expected_tx = Transaction(id="item-1", payload={"id": "item-1"}, metadata={"workflow_id": "wf-1"})

        with patch.object(connector.service, "fetch_items", return_value=[expected_tx]) as mock_fetch:
            connector.fetch_transactions()

        _, kwargs = mock_fetch.call_args
        assert kwargs["assigned_to"] == "44444444-4444-4444-4444-444444444444"

    def test_fetch_unassigned_forces_assigned_no_and_claims_items(self):
        config = ErgonPlatformConsumerConfig(
            workflow_id="wf-1",
            phase_id="ph-1",
            unassigned=True,
            list_params={"assigned_to": "44444444-4444-4444-4444-444444444444"},
        )
        connector = _make_connector(consumer_config=config)
        listed_tx = Transaction(id="item-1", payload={"id": "item-1"}, metadata={"workflow_id": "wf-1"})
        claimed_tx = Transaction(id="item-1", payload={"id": "item-1", "assigned_to": "worker"}, metadata={})

        with patch.object(connector.service, "fetch_items", return_value=[listed_tx]) as mock_fetch, patch.object(
            connector.service, "claim_item", return_value={}
        ) as mock_claim, patch.object(
            connector, "fetch_transaction_by_id", return_value=claimed_tx
        ) as mock_refresh:
            txns = connector.fetch_transactions()

        assert txns == [claimed_tx]
        _, kwargs = mock_fetch.call_args
        assert kwargs["assigned"] == "no"
        assert "assigned_to" not in kwargs
        mock_claim.assert_called_once_with("item-1")
        mock_refresh.assert_called_once_with("item-1")

    def test_fetch_unassigned_skips_item_when_claim_fails(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf-1", phase_id="ph-1", unassigned=True)
        connector = _make_connector(consumer_config=config)
        listed_tx = Transaction(id="item-1", payload={"id": "item-1"}, metadata={"workflow_id": "wf-1"})

        with patch.object(connector.service, "fetch_items", return_value=[listed_tx]), patch.object(
            connector.service, "claim_item", side_effect=RuntimeError("race")
        ), patch.object(connector, "fetch_transaction_by_id") as mock_refresh:
            txns = connector.fetch_transactions()

        assert txns == []
        mock_refresh.assert_not_called()


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

    def test_dispatch_passes_parent_item_id_from_payload(self):
        producer = ErgonPlatformProducerConfig(workflow_id="wf-1", phase_id="ph-1")
        connector = _make_connector(producer_config=producer)
        tx = Transaction(id="new", payload=CreateItemInput(title="Child", parent_item_id="parent-123"))

        with patch.object(connector.service, "create_item", return_value={"id": "item-77"}) as mock_create:
            created = connector.dispatch_transactions([tx])

        assert created == ["item-77"]
        _, kwargs = mock_create.call_args
        assert kwargs["parent_item_id"] == "parent-123"

    def test_dispatch_passes_parent_item_id_from_producer_default(self):
        producer = ErgonPlatformProducerConfig(
            workflow_id="wf-1",
            phase_id="ph-1",
            parent_item_id="parent-default",
        )
        connector = _make_connector(producer_config=producer)
        tx = Transaction(id="new", payload=CreateItemInput(title="Child"))

        with patch.object(connector.service, "create_item", return_value={"id": "item-88"}) as mock_create:
            created = connector.dispatch_transactions([tx])

        assert created == ["item-88"]
        _, kwargs = mock_create.call_args
        assert kwargs["parent_item_id"] == "parent-default"


class TestChildItems:
    def test_fetch_child_transactions_delegates_to_service(self):
        connector = _make_connector()
        expected_tx = Transaction(id="child-1", payload={"id": "child-1"}, metadata={})
        with patch.object(
            connector.service,
            "fetch_child_items",
            return_value=[expected_tx],
        ) as mock_fetch:
            txns = connector.fetch_child_transactions("parent-1", include_archived=True)

        assert txns == [expected_tx]
        mock_fetch.assert_called_once_with("parent-1", include_archived=True)


class TestFetchItemsByQuery:
    def test_fetch_items_by_query_delegates(self):
        connector = _make_connector()
        expected_tx = Transaction(id="i1", payload={"id": "i1"}, metadata={})
        with patch.object(connector.service, "fetch_items_by_query", return_value=[expected_tx]) as mock_fetch:
            txns = connector.fetch_items_by_query("wf-1", {"search": "abc"})

        assert txns == [expected_tx]
        mock_fetch.assert_called_once_with("wf-1", {"search": "abc"})


class TestGetTransactionsCount:
    def test_count_delegates_to_service(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf-1", phase_id="ph-1")
        connector = _make_connector(consumer_config=config)

        with patch.object(connector.service, "get_phase_items_count", return_value=42) as mock_count:
            count = connector.get_transactions_count()

        assert count == 42
        mock_count.assert_called_once_with("wf-1", "ph-1")

    def test_count_requires_consumer_config(self):
        connector = _make_connector()
        with pytest.raises(ValueError, match="consumer_config"):
            connector.get_transactions_count()


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


class TestReleaseItem:
    def test_release_forwards_delay_seconds(self):
        connector = _make_connector()

        with patch.object(connector.service, "release_item", return_value={"id": "item-1"}) as mock_release:
            result = connector.release_item("item-1", delay_seconds=75)

        assert result == {"id": "item-1"}
        mock_release.assert_called_once_with(
            "item-1",
            None,
            delay_seconds=75,
        )


class TestNackTransaction:
    def test_nack_requeue_releases_item_with_delay_seconds(self):
        connector = _make_connector()
        tx = Transaction(id="item-1", payload={})

        with patch.object(connector.service, "release_item", return_value={}) as mock_release:
            connector.nack_transaction(tx, requeue=True, delay_seconds=0)

        mock_release.assert_called_once_with("item-1", delay_seconds=0)

    def test_nack_requeue_forwards_positive_delay_seconds_to_release(self):
        connector = _make_connector()
        tx = Transaction(id="item-1", payload={})

        with patch.object(connector.service, "release_item", return_value={}) as mock_release:
            connector.nack_transaction(tx, requeue=True, delay_seconds=2)

        mock_release.assert_called_once_with("item-1", delay_seconds=2)

    def test_nack_without_requeue_moves_to_nack_phase(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf", phase_id="ph", nack_phase_id="err")
        connector = _make_connector(consumer_config=config)
        tx = Transaction(id="item-1", payload={})

        with patch.object(connector.service, "release_item", return_value={}) as mock_release, patch.object(
            connector.service, "move_item_to_phase", return_value={}
        ) as mock_move:
            connector.nack_transaction(tx, requeue=False)

        mock_release.assert_not_called()
        mock_move.assert_called_once_with("item-1", "err")

    def test_nack_without_requeue_requires_nack_phase(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf", phase_id="ph")
        connector = _make_connector(consumer_config=config)
        tx = Transaction(id="item-1", payload={})

        with pytest.raises(ValueError, match="nack_phase_id"):
            connector.nack_transaction(tx, requeue=False)
