"""Tests for AsyncErgonPlatformConnector — async fetch/dispatch/ack/count mapping."""

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

    async def test_fetch_unassigned_forces_assigned_no_and_claims_items(self):
        config = ErgonPlatformConsumerConfig(
            workflow_id="wf-1",
            phase_id="ph-1",
            unassigned=True,
            list_params={"assigned_to": "44444444-4444-4444-4444-444444444444"},
        )
        connector = _make_connector(consumer_config=config)
        listed_tx = Transaction(id="item-1", payload={"id": "item-1"}, metadata={})
        claimed_tx = Transaction(id="item-1", payload={"id": "item-1", "assigned_to": "worker"}, metadata={})

        with (
            patch.object(connector.service, "fetch_items", new=AsyncMock(return_value=[listed_tx])) as mock_fetch,
            patch.object(connector.service, "claim_item", new=AsyncMock(return_value={})) as mock_claim,
            patch.object(
                connector, "fetch_transaction_by_id_async", new=AsyncMock(return_value=claimed_tx)
            ) as mock_refresh,
        ):
            txns = await connector.fetch_transactions_async()

        assert txns == [claimed_tx]
        _, kwargs = mock_fetch.call_args
        assert kwargs["assigned"] == "no"
        assert "assigned_to" not in kwargs
        mock_claim.assert_awaited_once_with("item-1")
        mock_refresh.assert_awaited_once_with("item-1")

    async def test_fetch_unassigned_skips_item_when_claim_fails(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf-1", phase_id="ph-1", unassigned=True)
        connector = _make_connector(consumer_config=config)
        listed_tx = Transaction(id="item-1", payload={"id": "item-1"}, metadata={})

        with (
            patch.object(connector.service, "fetch_items", new=AsyncMock(return_value=[listed_tx])),
            patch.object(connector.service, "claim_item", new=AsyncMock(side_effect=RuntimeError("race"))),
            patch.object(connector, "fetch_transaction_by_id_async", new=AsyncMock()) as mock_refresh,
        ):
            txns = await connector.fetch_transactions_async()

        assert txns == []
        mock_refresh.assert_not_awaited()


class TestDispatchTransactions:
    async def test_dispatch_creates_item(self):
        producer = ErgonPlatformProducerConfig(workflow_id="wf-1", phase_id="ph-1")
        connector = _make_connector(producer_config=producer)

        tx = Transaction(id="new", payload=CreateItemInput(title="Ticket"))

        with patch.object(
            connector.service,
            "create_item",
            new=AsyncMock(return_value={"id": "item-99"}),
        ) as mock_create:
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


class TestFetchItemsByQuery:
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


class TestGetTransactionsCount:
    async def test_count_delegates_to_service(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf-1", phase_id="ph-1")
        connector = _make_connector(consumer_config=config)

        with patch.object(
            connector.service,
            "get_phase_items_count",
            new=AsyncMock(return_value=42),
        ) as mock_count:
            count = await connector.get_transactions_count_async()

        assert count == 42
        mock_count.assert_awaited_once_with("wf-1", "ph-1")

    async def test_count_requires_consumer_config(self):
        connector = _make_connector()
        with pytest.raises(ValueError, match="consumer_config"):
            await connector.get_transactions_count_async()


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


class TestReleaseItem:
    async def test_release_forwards_delay_seconds(self):
        connector = _make_connector()

        with patch.object(
            connector.service,
            "release_item",
            new=AsyncMock(return_value={"id": "item-1"}),
        ) as mock_release:
            result = await connector.release_item("item-1", delay_seconds=75)

        assert result == {"id": "item-1"}
        mock_release.assert_awaited_once_with(
            "item-1",
            None,
            delay_seconds=75,
        )


class TestNackTransaction:
    async def test_nack_requeue_releases_item_with_delay_seconds(self):
        connector = _make_connector()
        tx = Transaction(id="item-1", payload={})

        with patch.object(connector.service, "release_item", new=AsyncMock(return_value={})) as mock_release:
            await connector.nack_transaction(tx, requeue=True, delay_seconds=0)

        mock_release.assert_awaited_once_with("item-1", delay_seconds=0)

    async def test_nack_requeue_forwards_positive_delay_seconds_to_release(self):
        connector = _make_connector()
        tx = Transaction(id="item-1", payload={})

        with patch.object(connector.service, "release_item", new=AsyncMock(return_value={})) as mock_release:
            await connector.nack_transaction(tx, requeue=True, delay_seconds=2)

        mock_release.assert_awaited_once_with("item-1", delay_seconds=2)

    async def test_nack_without_requeue_moves_to_nack_phase(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf", phase_id="ph", nack_phase_id="err")
        connector = _make_connector(consumer_config=config)
        tx = Transaction(id="item-1", payload={})

        with (
            patch.object(connector.service, "release_item", new=AsyncMock(return_value={})) as mock_release,
            patch.object(connector.service, "move_item_to_phase", new=AsyncMock(return_value={})) as mock_move,
        ):
            await connector.nack_transaction(tx, requeue=False)

        mock_release.assert_not_awaited()
        mock_move.assert_awaited_once_with("item-1", "err")

    async def test_nack_without_requeue_requires_nack_phase(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf", phase_id="ph")
        connector = _make_connector(consumer_config=config)
        tx = Transaction(id="item-1", payload={})

        with pytest.raises(ValueError, match="nack_phase_id"):
            await connector.nack_transaction(tx, requeue=False)
