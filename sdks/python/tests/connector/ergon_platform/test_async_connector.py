"""Tests for AsyncErgonPlatformConnector — async fetch/dispatch/ack mapping."""

from unittest.mock import AsyncMock, patch

import pytest

from ergon.connector.ergon_platform.async_connector import AsyncErgonPlatformConnector
from ergon.connector.ergon_platform.models import (
    CreateItemInput,
    ErgonPlatformClient,
    ErgonPlatformConsumerConfig,
    ErgonPlatformProducerConfig,
)
from ergon.connector.transaction import Transaction


def _make_client() -> ErgonPlatformClient:
    return ErgonPlatformClient(client_id="ek_test", client_secret="eks_test", base_url="https://api.test")


def _make_connector(consumer_config=None, producer_config=None) -> AsyncErgonPlatformConnector:
    with patch("ergon.connector.ergon_platform.service._get_ergon_client"):
        return AsyncErgonPlatformConnector(
            client=_make_client(),
            consumer_config=consumer_config,
            producer_config=producer_config,
        )


class TestFetchTransactions:
    async def test_fetch_delegates_to_service(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf-1", phase_id="ph-1", batch_size=7)
        connector = _make_connector(consumer_config=config)

        expected_tx = Transaction(id="item-1", payload={"id": "item-1"}, metadata={})

        with patch.object(connector.service, "fetch_items", new=AsyncMock(return_value=[expected_tx])) as mock_fetch:
            txns = await connector.fetch_transactions_async()

        assert txns == [expected_tx]
        args, kwargs = mock_fetch.call_args
        assert args == ("wf-1", "ph-1")
        assert kwargs["limit"] == 7

    async def test_fetch_requires_consumer_config(self):
        connector = _make_connector()
        with pytest.raises(ValueError, match="consumer_config"):
            await connector.fetch_transactions_async()


class TestDispatchTransactions:
    async def test_dispatch_creates_item(self):
        producer = ErgonPlatformProducerConfig(workflow_id="wf-1", phase_id="ph-1")
        connector = _make_connector(producer_config=producer)

        tx = Transaction(id="new", payload=CreateItemInput(title="Ticket"))

        with patch.object(connector.service, "create_item", new=AsyncMock(return_value={"id": "item-99"})) as mock_create:
            created = await connector.dispatch_transactions_async([tx])

        assert created == ["item-99"]
        args, _ = mock_create.call_args
        assert args == ("wf-1", "ph-1", "Ticket")

    async def test_dispatch_passes_parent_item_id_from_payload(self):
        producer = ErgonPlatformProducerConfig(workflow_id="wf-1", phase_id="ph-1")
        connector = _make_connector(producer_config=producer)
        tx = Transaction(id="new", payload=CreateItemInput(title="Child", parent_item_id="parent-123"))

        with patch.object(
            connector.service,
            "create_item",
            new=AsyncMock(return_value={"id": "item-100"}),
        ) as mock_create:
            created = await connector.dispatch_transactions_async([tx])

        assert created == ["item-100"]
        _, kwargs = mock_create.call_args
        assert kwargs["parent_item_id"] == "parent-123"


class TestChildItems:
    async def test_fetch_child_transactions_delegates_to_service(self):
        connector = _make_connector()
        expected_tx = Transaction(id="child-1", payload={"id": "child-1"}, metadata={})
        with patch.object(
            connector.service,
            "fetch_child_items",
            new=AsyncMock(return_value=[expected_tx]),
        ) as mock_fetch:
            txns = await connector.fetch_child_transactions_async("parent-1", include_archived=True)

        assert txns == [expected_tx]
        mock_fetch.assert_awaited_once_with("parent-1", include_archived=True)

    async def test_child_surface_methods_delegate_to_service(self):
        connector = _make_connector()

        with (
            patch.object(
                connector.service,
                "list_item_children",
                new=AsyncMock(return_value=[{"child_item_id": "child-1"}]),
            ) as mock_children,
            patch.object(
                connector.service,
                "list_item_child_targets",
                new=AsyncMock(return_value={"folders": []}),
            ) as mock_targets,
            patch.object(
                connector.service,
                "get_item_child_capabilities",
                new=AsyncMock(return_value={"can_create": True, "can_view": True, "can_unlink": False}),
            ) as mock_caps,
            patch.object(connector.service, "unlink_item_child", new=AsyncMock(return_value=None)) as mock_unlink,
        ):
            assert await connector.list_item_children("parent-1") == [{"child_item_id": "child-1"}]
            assert await connector.list_item_child_targets("parent-1") == {"folders": []}
            assert await connector.get_item_child_capabilities("parent-1") == {
                "can_create": True,
                "can_view": True,
                "can_unlink": False,
            }
            await connector.unlink_item_child("parent-1", "child-1")

        mock_children.assert_awaited_once_with("parent-1")
        mock_targets.assert_awaited_once_with("parent-1")
        mock_caps.assert_awaited_once_with("parent-1")
        mock_unlink.assert_awaited_once_with("parent-1", "child-1")


class TestItemOperations:
    async def test_bulk_create_items_delegates(self):
        connector = _make_connector()
        items = [{"title": "A"}, {"title": "B"}]
        with patch.object(
            connector.service,
            "bulk_create_items",
            new=AsyncMock(return_value={"succeeded": ["i1", "i2"]}),
        ) as mock_bulk:
            result = await connector.bulk_create_items("wf-1", items)

        assert result == {"succeeded": ["i1", "i2"]}
        mock_bulk.assert_awaited_once_with("wf-1", items, response_format="full")

    async def test_query_items_delegates(self):
        connector = _make_connector()
        query = {"search": "abc"}
        with patch.object(
            connector.service, "query_items", new=AsyncMock(return_value={"items": []})
        ) as mock_q:
            result = await connector.query_items("wf-1", query)

        assert result == {"items": []}
        mock_q.assert_awaited_once_with("wf-1", query)

    async def test_fetch_items_by_query_delegates(self):
        connector = _make_connector()
        expected_tx = Transaction(id="i1", payload={"id": "i1"}, metadata={})
        with patch.object(
            connector.service,
            "fetch_items_by_query",
            new=AsyncMock(return_value=[expected_tx]),
        ) as mock_fetch:
            txns = await connector.fetch_items_by_query("wf-1", {"search": "abc"})

        assert txns == [expected_tx]
        mock_fetch.assert_awaited_once_with("wf-1", {"search": "abc"})

    async def test_item_action_methods_delegate(self):
        connector = _make_connector()
        with (
            patch.object(
                connector.service, "claim_item", new=AsyncMock(return_value={"ok": True})
            ) as mock_claim,
            patch.object(
                connector.service, "assign_item", new=AsyncMock(return_value={"ok": True})
            ) as mock_assign,
            patch.object(
                connector.service, "assign_item_group", new=AsyncMock(return_value={"ok": True})
            ) as mock_group,
            patch.object(
                connector.service, "release_item", new=AsyncMock(return_value={"ok": True})
            ) as mock_release,
            patch.object(
                connector.service,
                "route_item_to_global_target",
                new=AsyncMock(return_value={"ok": True}),
            ) as mock_route,
            patch.object(
                connector.service, "list_item_comments", new=AsyncMock(return_value=[{"id": "c1"}])
            ) as mock_comments,
            patch.object(
                connector.service, "add_item_comment", new=AsyncMock(return_value={"id": "c2"})
            ) as mock_add,
            patch.object(
                connector.service, "list_item_events", new=AsyncMock(return_value=[{"id": "e1"}])
            ) as mock_events,
        ):
            await connector.claim_item("i1")
            await connector.assign_item("i1", "principal-1")
            await connector.assign_item_group("i1", "group-1")
            await connector.release_item("i1")
            await connector.route_item_to_global_target("i1")
            assert await connector.list_item_comments("i1") == [{"id": "c1"}]
            assert await connector.add_item_comment("i1", {"body": "hi"}) == {"id": "c2"}
            assert await connector.list_item_events("i1") == [{"id": "e1"}]

        mock_claim.assert_awaited_once_with("i1", None)
        mock_assign.assert_awaited_once_with("i1", "principal-1")
        mock_group.assert_awaited_once_with("i1", "group-1")
        mock_release.assert_awaited_once_with("i1", None)
        mock_route.assert_awaited_once_with("i1", None)
        mock_comments.assert_awaited_once_with("i1")
        mock_add.assert_awaited_once_with("i1", {"body": "hi"})
        mock_events.assert_awaited_once_with("i1")


class TestAckTransaction:
    async def test_ack_moves_to_configured_phase(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf", phase_id="ph", ack_phase_id="done")
        connector = _make_connector(consumer_config=config)
        tx = Transaction(id="item-1", payload={})

        with patch.object(connector.service, "move_item_to_phase", new=AsyncMock(return_value={})) as mock_move:
            await connector.ack_transaction(tx)

        mock_move.assert_awaited_once_with("item-1", "done")

    async def test_ack_noop_without_ack_phase(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf", phase_id="ph")
        connector = _make_connector(consumer_config=config)
        tx = Transaction(id="item-1", payload={})

        with patch.object(connector.service, "move_item_to_phase", new=AsyncMock()) as mock_move:
            await connector.ack_transaction(tx)

        mock_move.assert_not_awaited()
