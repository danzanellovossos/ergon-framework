"""Pull only phases: mode resolution, fenced client, pull/fail/defer/complete mapping.

The standard-phase behaviour is covered by ``test_connector.py`` /
``test_async_connector.py`` and must stay byte-identical; this file covers what
changes when the consumed phase is ``kind == "pull"``.
"""

import logging
import warnings
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from ergon.connector.ergon_platform.models import ErgonPlatformClient
from ergon.connector.ergon_platform.workflows.async_connector import AsyncErgonPlatformConnector
from ergon.connector.ergon_platform.workflows.connector import ErgonPlatformConnector
from ergon.connector.ergon_platform.workflows.models import ErgonPlatformConsumerConfig
from ergon.connector.transaction import Transaction

from .test_connector import _Client

PULL_PHASES = [{"id": "ph-1", "kind": "pull", "pull_policy": {"max_attempts": 3}}]
STANDARD_PHASES = [{"id": "ph-1", "kind": "standard"}]


class _Factory:
    """Stands in for create_ergon_client: one plain client, one fenced client per consumer id."""

    def __init__(self, phases):
        self.plain = _Client()
        self.plain.workflows.workflow_obj.phases_response = phases
        self.fenced = []

    def __call__(self, config, *, consumer_id=None):
        if consumer_id is None:
            return self.plain
        client = _Client(consumer_id=consumer_id)
        client.workflows.workflow_obj.phases_response = self.plain.workflows.workflow_obj.phases_response
        self.fenced.append(client)
        return client


@contextmanager
def _patched_factory(phases):
    factory = _Factory(phases)
    with (
        patch("ergon.connector.ergon_platform.workflows.connector.create_ergon_client", factory),
        patch("ergon.connector.ergon_platform.workflows.async_connector.create_ergon_client", factory),
        patch("ergon.connector.ergon_platform.workflows._mode.create_ergon_client", factory),
    ):
        yield factory


def _config(**overrides) -> ErgonPlatformConsumerConfig:
    base = dict(workflow_id="wf-1", phase_id="ph-1", batch_size=5)
    base.update(overrides)
    return ErgonPlatformConsumerConfig(**base)


def _client_config() -> ErgonPlatformClient:
    return ErgonPlatformClient(client_id="ek_test", client_secret="eks_test", base_url="https://api.test")


def _tx(item_id="item-1") -> Transaction:
    return Transaction(id=item_id, payload={"id": item_id}, metadata={})


class TestModeResolution:
    def test_auto_reads_phase_kind_once_and_switches_to_fenced_client(self):
        with _patched_factory(PULL_PHASES) as factory:
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config())
            assert connector.client is factory.plain
            assert connector.consumer_id is None

            assert connector.is_pull is True
            assert connector.is_pull is True  # cached

            assert factory.plain.workflows.workflow_obj.phases_calls == 1
            assert len(factory.fenced) == 1
            assert connector.client is factory.fenced[0]
            assert connector.service.client is factory.fenced[0]
            assert connector.consumer_id == factory.fenced[0].consumer_id
            assert factory.plain.closed is True

    def test_default_consumer_id_is_host_and_pid(self):
        with _patched_factory(PULL_PHASES):
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config())
            connector.is_pull
        host, _, pid = connector.consumer_id.rpartition(":")
        assert host and pid.isdigit()

    def test_explicit_consumer_id_is_used(self):
        with _patched_factory(PULL_PHASES) as factory:
            connector = ErgonPlatformConnector(
                client=_client_config(), consumer_config=_config(consumer_id="billing:7")
            )
            connector.is_pull
        assert factory.fenced[0].consumer_id == "billing:7"

    def test_standard_phase_keeps_plain_client_and_never_sends_consumer_id(self):
        with _patched_factory(STANDARD_PHASES) as factory:
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config())
            assert connector.is_pull is False
            assert connector.client is factory.plain
            assert factory.fenced == []
            assert connector.consumer_id is None

    def test_mode_pull_skips_the_phase_read(self):
        with _patched_factory(STANDARD_PHASES) as factory:
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config(mode="pull"))
            assert connector.is_pull is True
            assert factory.plain.workflows.workflow_obj.phases_calls == 0
            assert len(factory.fenced) == 1

    def test_mode_list_claim_skips_the_phase_read(self):
        with _patched_factory(PULL_PHASES) as factory:
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config(mode="list_claim"))
            assert connector.is_pull is False
            assert factory.plain.workflows.workflow_obj.phases_calls == 0

    def test_unreadable_phase_falls_back_to_standard_with_a_warning(self, caplog):
        with _patched_factory(PULL_PHASES) as factory:

            def boom(**params):
                raise RuntimeError("403")

            factory.plain.workflows.workflow_obj.phases = boom
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config())
            with caplog.at_level(logging.WARNING):
                assert connector.is_pull is False
        assert "Could not read the kind of phase ph-1" in caplog.text
        assert factory.fenced == []

    def test_no_consumer_config_is_never_pull(self):
        with _patched_factory(PULL_PHASES):
            connector = ErgonPlatformConnector(client=_client_config())
            assert connector.is_pull is False


class TestPullFetch:
    def test_fetch_uses_pull_endpoint_with_filters_and_returns_full_items(self):
        with _patched_factory(PULL_PHASES) as factory:
            filters = [{"field_id": "f-cliente", "op": "eq", "value": "suzano"}]
            connector = ErgonPlatformConnector(
                client=_client_config(),
                consumer_config=_config(pull_filters=filters, pull_wait_seconds=5, unassigned=True),
            )
            connector.is_pull
            fenced = factory.fenced[0]
            fenced.workflows.phases.pull_response = {
                "items": [{"id": "item-1", "phase_id": "ph-1", "title": "T1", "claim": {"owner": "x"}}],
                "total": 1,
                "claim_expires_at": "2026-01-01T00:05:00Z",
                "consumer_id": fenced.consumer_id,
            }

            txns = connector.fetch_transactions(batch_size=7, assigned_to="ignored")

        assert [tx.id for tx in txns] == ["item-1"]
        assert txns[0].payload["claim"] == {"owner": "x"}
        assert fenced.workflows.phases.pull_calls == [
            ("ph-1", {"max": 7, "filter": {"filters": filters}, "wait_seconds": 5})
        ]
        # no list, no per-item claim, no refetch
        assert fenced.workflows.workflow_obj.last_items_kwargs is None
        assert fenced.workflows.items.claim_calls == []

    def test_fetch_caps_max_at_100(self):
        with _patched_factory(PULL_PHASES) as factory:
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config(batch_size=500))
            connector.fetch_transactions()
        assert factory.fenced[0].workflows.phases.pull_calls == [("ph-1", {"max": 100})]


class TestPullOutcomes:
    def test_ack_without_target_completes_the_stay(self):
        with _patched_factory(PULL_PHASES) as factory:
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config())
            connector.ack_transaction(_tx())
        fenced = factory.fenced[0]
        assert fenced.workflows.items.complete_calls == [("item-1", {})]
        assert fenced.workflows.items.route_calls == []

    def test_ack_with_target_still_routes(self):
        with _patched_factory(PULL_PHASES) as factory:
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config(ack_phase_id="ph-next"))
            connector.is_pull
            connector.ack_transaction(_tx())
        fenced = factory.fenced[0]
        assert fenced.workflows.items.route_calls == [("item-1", "ph-next")]
        assert fenced.workflows.items.complete_calls == []

    def test_nack_requeue_fails_the_attempt_with_default_reason(self):
        with _patched_factory(PULL_PHASES) as factory:
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config())
            with warnings.catch_warnings():
                warnings.simplefilter("error")  # no deprecation on the pull path
                connector.nack_transaction(_tx())
        fenced = factory.fenced[0]
        assert fenced.workflows.items.fail_calls == [("item-1", {"reason": "nack_transaction"})]
        assert fenced.workflows.items.release_calls == []
        assert fenced.workflows.items.update_calls == []

    def test_nack_requeue_forwards_delay_reason_and_details(self):
        with _patched_factory(PULL_PHASES) as factory:
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config())
            connector.nack_transaction(_tx(), delay_seconds=300, reason="portal down", details="trace")
        assert factory.fenced[0].workflows.items.fail_calls == [
            ("item-1", {"reason": "portal down", "delay_seconds": 300, "details": "trace"})
        ]

    def test_nack_without_requeue_routes_to_nack_phase(self):
        with _patched_factory(PULL_PHASES) as factory:
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config(nack_phase_id="ph-exc"))
            connector.is_pull
            connector.nack_transaction(_tx(), requeue=False)
        assert factory.fenced[0].workflows.items.route_calls == [("item-1", "ph-exc")]

    def test_release_with_delay_is_a_defer(self):
        with _patched_factory(PULL_PHASES) as factory:
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config())
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                connector.release_item("item-1", delay_seconds=90)
        fenced = factory.fenced[0]
        assert fenced.workflows.items.defer_calls == [("item-1", {"delay_seconds": 90})]
        assert fenced.workflows.items.update_calls == []
        assert fenced.workflows.items.release_calls == []

    def test_release_without_delay_is_a_release(self):
        with _patched_factory(PULL_PHASES) as factory:
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config())
            connector.release_item("item-1")
        assert factory.fenced[0].workflows.items.release_calls == [("item-1", {})]

    def test_explicit_outcomes(self):
        with _patched_factory(PULL_PHASES) as factory:
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config())
            connector.complete_transaction(_tx(), reason="done")
            connector.fail_transaction(_tx(), reason="bad row", delay_seconds=0)
            connector.defer_transaction(_tx(), delay_seconds=3600, reason="wait for TMS")
            connector.extend_claim(_tx(), seconds=600)
            connector.extend_claim(_tx())
        items = factory.fenced[0].workflows.items
        assert items.complete_calls == [("item-1", {"reason": "done"})]
        assert items.fail_calls == [("item-1", {"reason": "bad row", "delay_seconds": 0})]
        assert items.defer_calls == [("item-1", {"delay_seconds": 3600, "reason": "wait for TMS"})]
        assert items.extend_calls == [("item-1", {"seconds": 600}), ("item-1", {})]

    def test_defer_rejects_zero_delay(self):
        with _patched_factory(PULL_PHASES):
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config())
            with pytest.raises(ValueError, match="delay_seconds >= 1"):
                connector.defer_transaction(_tx(), delay_seconds=0)


class TestStandardPhaseIsUnchanged:
    def test_nack_default_delay_is_still_ten_seconds_with_deprecation(self):
        with _patched_factory(STANDARD_PHASES) as factory:
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config())
            with pytest.warns(DeprecationWarning, match="2026-11-27"):
                connector.nack_transaction(_tx())
        items = factory.plain.workflows.items
        assert items.update_calls == [("item-1", {"visibility_timeout_on_release_minutes": 1})]
        assert items.release_calls == [("item-1", {})]
        assert items.fail_calls == []

    def test_ack_without_target_is_still_a_noop(self):
        with _patched_factory(STANDARD_PHASES) as factory:
            connector = ErgonPlatformConnector(client=_client_config(), consumer_config=_config())
            connector.ack_transaction(_tx())
        items = factory.plain.workflows.items
        assert items.complete_calls == [] and items.route_calls == []


class TestAsyncConnector:
    async def test_pull_fetch_and_outcomes(self):
        with _patched_factory(PULL_PHASES) as factory:
            connector = AsyncErgonPlatformConnector(client=_client_config(), consumer_config=_config())
            assert await connector.is_pull_async() is True
            fenced = factory.fenced[0]
            fenced.workflows.phases.pull_response = {"items": [{"id": "item-1", "phase_id": "ph-1"}], "total": 1}

            txns = await connector.fetch_transactions_async()
            await connector.ack_transaction(txns[0])
            await connector.nack_transaction(txns[0], reason="retry")
            await connector.release_item("item-1", delay_seconds=30)
            await connector.defer_transaction(txns[0], delay_seconds=60)
            await connector.extend_claim(txns[0])

        assert [tx.id for tx in txns] == ["item-1"]
        assert fenced.workflows.phases.pull_calls == [("ph-1", {"max": 5})]
        items = fenced.workflows.items
        assert items.complete_calls == [("item-1", {})]
        assert items.fail_calls == [("item-1", {"reason": "retry"})]
        assert items.defer_calls == [("item-1", {"delay_seconds": 30}), ("item-1", {"delay_seconds": 60})]
        assert items.extend_calls == [("item-1", {})]
        assert connector.client is fenced

    async def test_standard_phase_unchanged(self):
        with _patched_factory(STANDARD_PHASES) as factory:
            connector = AsyncErgonPlatformConnector(
                client=_client_config(), consumer_config=_config(list_params={"assigned_to": "p-1"})
            )
            factory.plain.workflows.workflow_obj.items_response = {"items": [{"id": "item-1"}]}
            txns = await connector.fetch_transactions_async()
            with pytest.warns(DeprecationWarning):
                await connector.nack_transaction(txns[0])
        assert factory.fenced == []
        items = factory.plain.workflows.items
        assert items.update_calls == [("item-1", {"visibility_timeout_on_release_minutes": 1})]
        assert items.release_calls == [("item-1", {})]
