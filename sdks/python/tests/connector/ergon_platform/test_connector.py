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

    def test_child_surface_methods_delegate_to_service(self):
        connector = _make_connector()

        with (
            patch.object(
                connector.service,
                "list_item_children",
                return_value=[{"child_item_id": "child-1"}],
            ) as mock_children,
            patch.object(
                connector.service,
                "list_item_child_targets",
                return_value={"folders": []},
            ) as mock_targets,
            patch.object(
                connector.service,
                "get_item_child_capabilities",
                return_value={"can_create": True, "can_view": True, "can_unlink": False},
            ) as mock_caps,
            patch.object(connector.service, "unlink_item_child", return_value=None) as mock_unlink,
        ):
            assert connector.list_item_children("parent-1") == [{"child_item_id": "child-1"}]
            assert connector.list_item_child_targets("parent-1") == {"folders": []}
            assert connector.get_item_child_capabilities("parent-1") == {
                "can_create": True,
                "can_view": True,
                "can_unlink": False,
            }
            connector.unlink_item_child("parent-1", "child-1")

        mock_children.assert_called_once_with("parent-1")
        mock_targets.assert_called_once_with("parent-1")
        mock_caps.assert_called_once_with("parent-1")
        mock_unlink.assert_called_once_with("parent-1", "child-1")


class TestItemOperations:
    def test_bulk_create_items_delegates(self):
        connector = _make_connector()
        items = [{"title": "A"}, {"title": "B"}]
        with patch.object(
            connector.service, "bulk_create_items", return_value={"succeeded": ["i1", "i2"]}
        ) as mock_bulk:
            result = connector.bulk_create_items("wf-1", items)

        assert result == {"succeeded": ["i1", "i2"]}
        mock_bulk.assert_called_once_with("wf-1", items, response_format="full")

    def test_query_items_delegates(self):
        connector = _make_connector()
        query = {"filters": [{"field_id": "f1", "operator": "eq", "value": "x"}]}
        with patch.object(connector.service, "query_items", return_value={"items": []}) as mock_q:
            result = connector.query_items("wf-1", query)

        assert result == {"items": []}
        mock_q.assert_called_once_with("wf-1", query)

    def test_fetch_items_by_query_delegates(self):
        connector = _make_connector()
        expected_tx = Transaction(id="i1", payload={"id": "i1"}, metadata={})
        with patch.object(
            connector.service, "fetch_items_by_query", return_value=[expected_tx]
        ) as mock_fetch:
            txns = connector.fetch_items_by_query("wf-1", {"search": "abc"})

        assert txns == [expected_tx]
        mock_fetch.assert_called_once_with("wf-1", {"search": "abc"})

    def test_comments_delegate(self):
        connector = _make_connector()
        with (
            patch.object(
                connector.service, "list_item_comments", return_value=[{"id": "c1"}]
            ) as mock_list,
            patch.object(
                connector.service, "add_item_comment", return_value={"id": "c2"}
            ) as mock_add,
        ):
            assert connector.list_item_comments("i1") == [{"id": "c1"}]
            assert connector.add_item_comment("i1", {"body": "hi"}) == {"id": "c2"}

        mock_list.assert_called_once_with("i1")
        mock_add.assert_called_once_with("i1", {"body": "hi"})

    def test_assignment_lifecycle_delegates(self):
        connector = _make_connector()
        with (
            patch.object(connector.service, "claim_item", return_value={"ok": True}) as mock_claim,
            patch.object(connector.service, "assign_item", return_value={"ok": True}) as mock_assign,
            patch.object(
                connector.service, "assign_item_group", return_value={"ok": True}
            ) as mock_group,
            patch.object(
                connector.service, "release_item", return_value={"ok": True}
            ) as mock_release,
        ):
            connector.claim_item("i1")
            connector.assign_item("i1", "principal-1")
            connector.assign_item_group("i1", "group-1")
            connector.release_item("i1")

        mock_claim.assert_called_once_with("i1", None)
        mock_assign.assert_called_once_with("i1", "principal-1")
        mock_group.assert_called_once_with("i1", "group-1")
        mock_release.assert_called_once_with("i1", None)

    def test_route_to_global_target_and_events_delegate(self):
        connector = _make_connector()
        with (
            patch.object(
                connector.service, "route_item_to_global_target", return_value={"ok": True}
            ) as mock_route,
            patch.object(
                connector.service, "list_item_events", return_value=[{"id": "e1"}]
            ) as mock_events,
        ):
            connector.route_item_to_global_target("i1")
            assert connector.list_item_events("i1") == [{"id": "e1"}]

        mock_route.assert_called_once_with("i1", None)
        mock_events.assert_called_once_with("i1")


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
