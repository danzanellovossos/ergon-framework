"""Tests for AsyncErgonPlatformConnector with direct SDK client usage."""

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


class _Items:
    def __init__(self):
        self.claim_calls = []
        self.route_calls = []
        self.release_calls = []
        self.update_calls = []
        self.children_response = []
        self.items_by_id = {}

    def claim(self, item_id, payload):
        self.claim_calls.append((item_id, payload))
        return {"id": item_id}

    def route(self, item_id, *, to_phase_id):
        self.route_calls.append((item_id, to_phase_id))
        return {"id": item_id, "phase_id": to_phase_id}

    def release(self, item_id, payload):
        self.release_calls.append((item_id, payload))
        return {"id": item_id, "released": True}

    def update(self, item_id, **fields):
        self.update_calls.append((item_id, fields))
        return {"id": item_id, **fields}

    def children(self, item_id, **params):
        return self.children_response

    def get(self, item_id, **params):
        return self.items_by_id[item_id]


class _Workflow:
    def __init__(self):
        self.items_response = {"items": []}
        self.query_response = {"items": []}
        self.create_response = {"id": "item-99"}
        self.last_items_kwargs = None
        self.last_query_payload = None
        self.last_create_kwargs = None

    def items(self, **params):
        self.last_items_kwargs = params
        return self.items_response

    def query_items(self, payload):
        self.last_query_payload = payload
        return self.query_response

    def create_item(self, **kwargs):
        self.last_create_kwargs = kwargs
        return self.create_response


class _Workflows:
    def __init__(self):
        self.items = _Items()
        self.workflow_obj = _Workflow()
        self.workflow_calls = []

    def workflow(self, workflow_id):
        self.workflow_calls.append(workflow_id)
        return self.workflow_obj


class _Client:
    def __init__(self):
        self.workflows = _Workflows()
        self.closed = False

    def close(self):
        self.closed = True


def _make_client_config() -> ErgonPlatformClient:
    return ErgonPlatformClient(client_id="ek_test", client_secret="eks_test", base_url="https://api.test")


def _make_connector(consumer_config=None, producer_config=None, sdk_client=None) -> AsyncErgonPlatformConnector:
    sdk_client = sdk_client or _Client()
    with patch("ergon.connector.ergon_platform.async_connector.create_ergon_client", return_value=sdk_client):
        return AsyncErgonPlatformConnector(
            client=_make_client_config(),
            consumer_config=consumer_config,
            producer_config=producer_config,
        )


class TestFetchTransactions:
    async def test_fetch_uses_sdk_items_endpoint(self):
        config = ErgonPlatformConsumerConfig(
            workflow_id="wf-1",
            phase_id="ph-1",
            batch_size=7,
            list_params={"assigned_to": "44444444-4444-4444-4444-444444444444"},
        )
        sdk_client = _Client()
        sdk_client.workflows.workflow_obj.items_response = {"items": [{"id": "item-1", "phase_id": "ph-1"}]}
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        txns = await connector.fetch_transactions_async()

        assert [tx.id for tx in txns] == ["item-1"]
        assert sdk_client.workflows.workflow_obj.last_items_kwargs == {
            "phase_id": "ph-1",
            "limit": 7,
            "offset": 0,
            "assigned_to": "44444444-4444-4444-4444-444444444444",
        }

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
        sdk_client = _Client()
        sdk_client.workflows.workflow_obj.items_response = {"items": [{"id": "item-1", "phase_id": "ph-1"}]}
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)
        claimed_tx = Transaction(id="item-1", payload={"id": "item-1", "assigned_to": "worker"}, metadata={})

        with patch.object(
            connector, "fetch_transaction_by_id_async", new=AsyncMock(return_value=claimed_tx)
        ) as mock_refresh:
            txns = await connector.fetch_transactions_async()

        assert txns == [claimed_tx]
        assert sdk_client.workflows.workflow_obj.last_items_kwargs["assigned"] == "no"
        assert "assigned_to" not in sdk_client.workflows.workflow_obj.last_items_kwargs
        assert sdk_client.workflows.items.claim_calls == [("item-1", {})]
        mock_refresh.assert_awaited_once_with("item-1")

    async def test_fetch_unassigned_skips_item_when_claim_fails(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf-1", phase_id="ph-1", unassigned=True)
        sdk_client = _Client()
        sdk_client.workflows.workflow_obj.items_response = {"items": [{"id": "item-1", "phase_id": "ph-1"}]}
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        with (
            patch.object(sdk_client.workflows.items, "claim", side_effect=RuntimeError("race")),
            patch.object(connector, "fetch_transaction_by_id_async", new=AsyncMock()) as mock_refresh,
        ):
            txns = await connector.fetch_transactions_async()

        assert txns == []
        mock_refresh.assert_not_awaited()


class TestDispatchTransactions:
    async def test_dispatch_creates_item_with_sdk_workflow(self):
        producer = ErgonPlatformProducerConfig(workflow_id="wf-1", phase_id="ph-1")
        sdk_client = _Client()
        connector = _make_connector(producer_config=producer, sdk_client=sdk_client)
        tx = Transaction(id="new", payload=CreateItemInput(title="Ticket"))

        created = await connector.dispatch_transactions_async([tx])

        assert created == ["item-99"]
        assert sdk_client.workflows.workflow_obj.last_create_kwargs == {
            "title": "Ticket",
            "phase_id": "ph-1",
            "parent_item_id": None,
            "field_values": None,
        }

    async def test_dispatch_passes_parent_item_id_from_payload(self):
        producer = ErgonPlatformProducerConfig(workflow_id="wf-1", phase_id="ph-1")
        sdk_client = _Client()
        connector = _make_connector(producer_config=producer, sdk_client=sdk_client)
        tx = Transaction(id="new", payload=CreateItemInput(title="Child", parent_item_id="parent-123"))

        created = await connector.dispatch_transactions_async([tx])

        assert created == ["item-99"]
        assert sdk_client.workflows.workflow_obj.last_create_kwargs["parent_item_id"] == "parent-123"


class TestChildItems:
    async def test_fetch_child_transactions_uses_sdk_children_and_get(self):
        sdk_client = _Client()
        sdk_client.workflows.items.children_response = [{"child_item_id": "child-1"}]
        sdk_client.workflows.items.items_by_id = {
            "child-1": {"id": "child-1", "workflow_id": "wf-child", "title": "Child"}
        }
        connector = _make_connector(sdk_client=sdk_client)

        txns = await connector.fetch_child_transactions_async("parent-1", include_archived=True)

        assert [tx.id for tx in txns] == ["child-1"]
        assert txns[0].metadata["workflow_id"] == "wf-child"


class TestFetchItemsByQuery:
    async def test_fetch_items_by_query_uses_sdk_query_items(self):
        sdk_client = _Client()
        sdk_client.workflows.workflow_obj.query_response = {"items": [{"id": "i1", "title": "Found"}]}
        connector = _make_connector(sdk_client=sdk_client)

        txns = await connector.fetch_items_by_query("wf-1", {"search": "abc"})

        assert [tx.id for tx in txns] == ["i1"]
        assert sdk_client.workflows.workflow_obj.last_query_payload == {"search": "abc"}


class TestGetTransactionsCount:
    async def test_count_reads_total_from_sdk_page(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf-1", phase_id="ph-1")
        sdk_client = _Client()
        sdk_client.workflows.workflow_obj.items_response = {"items": [], "total": 42}
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        assert await connector.get_transactions_count_async() == 42
        assert sdk_client.workflows.workflow_obj.last_items_kwargs == {"phase_id": "ph-1", "limit": 1, "offset": 0}

    async def test_count_requires_consumer_config(self):
        connector = _make_connector()
        with pytest.raises(ValueError, match="consumer_config"):
            await connector.get_transactions_count_async()


class TestAckTransaction:
    async def test_ack_routes_to_configured_phase_with_sdk(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf", phase_id="ph", ack_phase_id="done")
        sdk_client = _Client()
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        await connector.ack_transaction(Transaction(id="item-1", payload={}))

        assert sdk_client.workflows.items.route_calls == [("item-1", "done")]

    async def test_ack_noop_without_ack_phase(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf", phase_id="ph")
        sdk_client = _Client()
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        await connector.ack_transaction(Transaction(id="item-1", payload={}))

        assert sdk_client.workflows.items.route_calls == []

    async def test_ack_does_not_set_release_visibility_cooldown(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf", phase_id="ph", ack_phase_id="done")
        sdk_client = _Client()
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        await connector.ack_transaction(Transaction(id="item-1", payload={}))

        assert sdk_client.workflows.items.update_calls == []
        assert sdk_client.workflows.items.release_calls == []
        assert sdk_client.workflows.items.route_calls == [("item-1", "done")]


class TestReleaseItem:
    async def test_release_forwards_delay_seconds_to_domain_operation(self):
        sdk_client = _Client()
        connector = _make_connector(sdk_client=sdk_client)

        result = await connector.release_item("item-1", delay_seconds=75)

        assert result == {"id": "item-1", "released": True}
        assert sdk_client.workflows.items.update_calls == [("item-1", {"visibility_timeout_on_release_minutes": 2})]
        assert sdk_client.workflows.items.release_calls == [("item-1", {})]

    async def test_release_with_delay_seconds_zero_skips_visibility_override(self):
        sdk_client = _Client()
        connector = _make_connector(sdk_client=sdk_client)

        await connector.release_item("item-1", delay_seconds=0)

        assert sdk_client.workflows.items.update_calls == []
        assert sdk_client.workflows.items.release_calls == [("item-1", {})]


class TestNackTransaction:
    async def test_nack_requeue_releases_item_with_delay_seconds(self):
        sdk_client = _Client()
        connector = _make_connector(sdk_client=sdk_client)

        await connector.nack_transaction(Transaction(id="item-1", payload={}), requeue=True, delay_seconds=0)

        assert sdk_client.workflows.items.release_calls == [("item-1", {})]
        assert sdk_client.workflows.items.update_calls == []

    async def test_nack_default_delay_does_not_set_visibility_override(self):
        sdk_client = _Client()
        connector = _make_connector(sdk_client=sdk_client)

        await connector.nack_transaction(Transaction(id="item-1", payload={}), requeue=True)

        assert sdk_client.workflows.items.release_calls == [("item-1", {})]
        assert sdk_client.workflows.items.update_calls == []

    async def test_nack_requeue_updates_release_delay_before_release(self):
        sdk_client = _Client()
        connector = _make_connector(sdk_client=sdk_client)

        await connector.nack_transaction(Transaction(id="item-1", payload={}), requeue=True, delay_seconds=75)

        assert sdk_client.workflows.items.update_calls == [("item-1", {"visibility_timeout_on_release_minutes": 2})]
        assert sdk_client.workflows.items.release_calls == [("item-1", {})]

    async def test_nack_sub_minute_delay_rounds_up_to_one_minute(self):
        sdk_client = _Client()
        connector = _make_connector(sdk_client=sdk_client)

        await connector.nack_transaction(Transaction(id="item-1", payload={}), requeue=True, delay_seconds=10)

        assert sdk_client.workflows.items.update_calls == [("item-1", {"visibility_timeout_on_release_minutes": 1})]
        assert sdk_client.workflows.items.release_calls == [("item-1", {})]

    async def test_nack_without_requeue_moves_to_nack_phase(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf", phase_id="ph", nack_phase_id="err")
        sdk_client = _Client()
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        await connector.nack_transaction(Transaction(id="item-1", payload={}), requeue=False)

        assert sdk_client.workflows.items.route_calls == [("item-1", "err")]

    async def test_nack_without_requeue_requires_nack_phase(self):
        config = ErgonPlatformConsumerConfig(workflow_id="wf", phase_id="ph")
        connector = _make_connector(consumer_config=config)

        with pytest.raises(ValueError, match="nack_phase_id"):
            await connector.nack_transaction(Transaction(id="item-1", payload={}), requeue=False)
