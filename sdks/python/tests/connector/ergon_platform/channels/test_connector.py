"""Tests for ErgonPlatformChannelsConnector."""

from unittest.mock import patch

import pytest

from ergon.connector.ergon_platform.channels.adapters import ActivityAdapter
from ergon.connector.ergon_platform.channels.connector import ErgonPlatformChannelsConnector
from ergon.connector.ergon_platform.channels.models import (
    ChannelsActivityFilter,
    ErgonPlatformChannelsConfig,
    ErgonPlatformChannelsConsumerConfig,
    ErgonPlatformChannelsProducerConfig,
    SendMessageInput,
)
from ergon.connector.ergon_platform.models import ErgonPlatformClient
from ergon.connector.transaction import Transaction

event_to_transaction = ActivityAdapter.to_transaction

INBOX = "jsl-xxx@inbox.ergondata.ai"


class _ConfigAddresses:
    def __init__(self, configs: "_Configs"):
        self._configs = configs

    def list(self, **params):
        return self._configs.config_addresses_response


class _Configs:
    def __init__(self):
        self.activity_calls = []
        self.claim_calls = []
        self.claim_responses = []
        self.claim_response = None
        self.event_calls = []
        self.ack_calls = []
        self.nack_calls = []
        self.attachment_calls = []
        self.config_addresses_response: dict = {"items": []}

    def addresses(self, config_id):
        return _ConfigAddresses(self)

    def activity(self, config_id, **params):
        self.activity_calls.append((config_id, params))
        return self.activity_response

    def activity_claim(self, config_id, **params):
        self.claim_calls.append((config_id, params))
        response = (
            self.claim_responses.pop(0) if self.claim_responses else self.claim_response or self.activity_response
        )
        return {
            "items": [
                {
                    "event": {
                        "event_type": "channels.email.received",
                        **event,
                    },
                    "delivery": {
                        "event_id": event.get("id"),
                        "config_id": config_id,
                        "subscription_id": params["subscription_id"],
                        "status": "claimed",
                        "lease_token": f"lease-{event.get('id')}",
                        "lease_expires_at": "2026-08-15T18:00:00Z",
                        "consumer_id": params["consumer_id"],
                        "attempt_count": 1,
                    },
                }
                for event in response.get("items", [])
            ],
            "next_cursor": response.get("next_cursor"),
        }

    def activity_event(self, config_id, event_id, **params):
        self.event_calls.append((config_id, event_id, params))
        return self.event_response

    def activity_ack(self, config_id, event_id, **params):
        self.ack_calls.append((config_id, event_id, params))
        return self.ack_response

    def activity_nack(self, config_id, event_id, **params):
        self.nack_calls.append((config_id, event_id, params))
        return self.nack_response

    def activity_attachment_file(self, config_id, event_id, attachment_id, **params):
        self.attachment_calls.append((config_id, event_id, attachment_id, params))
        return self.attachment_response

    def verify(self, config_id):
        return self.verify_response

    activity_response = {"items": [], "total": 0}
    event_response = {"id": "evt-1", "channel": "email"}
    ack_response = {"status": "acked", "idempotent_replay": False}
    nack_response = {"status": "pending"}
    attachment_response = b"pdf-bytes"
    verify_response = {
        "name": "test-channel",
        "inbound_enabled": True,
        "capabilities": {"can_send": True},
    }


class _Channels:
    def __init__(self):
        self.send_calls = []
        self.thread_calls = []
        self.company_activity_calls = []
        self.addresses_calls = []
        self.configs = _Configs()
        self.send_response = {"log_id": "log-1"}
        self.thread_messages_response = {"messages": []}
        self.company_activity_response = {"items": [], "total": 0}
        self.addresses_response = []

    def send(self, request):
        self.send_calls.append(request)
        return self.send_response

    def thread_messages(self, thread_id, **params):
        self.thread_calls.append((thread_id, params))
        return self.thread_messages_response

    def company_activity(self, **params):
        self.company_activity_calls.append(params)
        return self.company_activity_response

    def addresses(self, **params):
        self.addresses_calls.append(params)
        return self.addresses_response


class _Client:
    def __init__(self):
        self.channels = _Channels()
        self.closed = False

    def close(self):
        self.closed = True


def _client_config() -> ErgonPlatformClient:
    return ErgonPlatformClient(client_id="ek_test", client_secret="eks_test", base_url="https://api.test")


def _make_connector(
    consumer_config=None,
    producer_config=None,
    channels_config=None,
    sdk_client=None,
    platform_client=None,
) -> ErgonPlatformChannelsConnector:
    sdk_client = sdk_client or _Client()
    with patch(
        "ergon.connector.ergon_platform.channels.connector.create_ergon_client",
        return_value=sdk_client,
    ):
        return ErgonPlatformChannelsConnector(
            client=platform_client or _client_config(),
            channels_config=channels_config,
            consumer_config=consumer_config,
            producer_config=producer_config,
        )


def _seed_addresses(sdk_client: _Client, *, direction: str = "both"):
    sdk_client.channels.addresses_response = [
        {
            "id": "addr-jsl",
            "address": INBOX,
            "channel_config_id": "cfg-jsl",
            "direction": direction,
        }
    ]


def _seed_producer_address(sdk_client: _Client, *, address_id: str, direction: str = "send"):
    sdk_client.channels.addresses_response = [
        {
            "id": address_id,
            "address": "outbound@inbox.ergondata.ai",
            "channel_config_id": "cfg-out",
            "direction": direction,
        }
    ]


def _claimed_transaction(event_id: str = "evt-1") -> Transaction:
    return ActivityAdapter.claimed_transaction(
        {
            "event": {"id": event_id, "event_type": "channels.email.received"},
            "delivery": {
                "subscription_id": "11111111-1111-1111-1111-111111111111",
                "lease_token": "22222222-2222-2222-2222-222222222222",
            },
        }
    )


class TestFetchTransactions:
    def test_fetches_inbox_activity_for_configured_address(self):
        config = ErgonPlatformChannelsConsumerConfig(
            address=INBOX,
            batch_size=25,
        )
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        sdk_client.channels.configs.activity_response = {
            "items": [{"id": "evt-1", "channel": "email", "status": "received"}],
            "total": 1,
        }
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        txns = connector.fetch_transactions()

        assert [tx.id for tx in txns] == ["evt-1"]
        assert len(sdk_client.channels.configs.claim_calls) == 1
        claim_config_id, claim = sdk_client.channels.configs.claim_calls[0]
        assert claim_config_id == "cfg-jsl"
        assert claim["limit"] == 25
        assert claim["address_id"] == "addr-jsl"
        assert claim["consumer_id"] == "ergon-framework"
        assert claim["visibility_timeout_seconds"] == 300
        assert claim["cursor"] is None
        assert claim["idempotency_key"]
        assert txns[0].metadata["delivery"]["lease_token"] == "lease-evt-1"
        assert len(sdk_client.channels.addresses_calls) == 1

    def test_fetch_hydrates_attachments_when_enabled(self):
        config = ErgonPlatformChannelsConsumerConfig(
            address=INBOX,
            download_attachments=True,
        )
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        sdk_client.channels.configs.activity_response = {
            "items": [
                {
                    "id": "evt-1",
                    "channel": "email",
                    "payload": {
                        "attachments": [
                            {
                                "resend_attachment_id": "att-1",
                                "filename": "a.pdf",
                                "content_type": "application/pdf",
                            }
                        ]
                    },
                }
            ],
            "total": 1,
        }
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        txns = connector.fetch_transactions()

        assert len(txns) == 1
        assert txns[0].metadata["has_attachment"] is True
        assert txns[0].metadata["attachments"][0]["id"] == "att-1"
        assert txns[0].metadata["attachments"][0]["filename"] == "a.pdf"
        assert txns[0].metadata["attachments"][0]["content"] == b"pdf-bytes"
        assert "resend_attachment_id" not in txns[0].metadata["attachments"][0]
        assert sdk_client.channels.configs.attachment_calls == [
            ("cfg-jsl", "evt-1", "att-1", {}),
        ]

    def test_fetch_skips_attachment_download_by_default(self):
        config = ErgonPlatformChannelsConsumerConfig(address=INBOX)
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        sdk_client.channels.configs.activity_response = {
            "items": [
                {
                    "id": "evt-1",
                    "payload": {"attachments": [{"resend_attachment_id": "att-1", "filename": "a.pdf"}]},
                }
            ],
            "total": 1,
        }
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        txns = connector.fetch_transactions()

        assert txns[0].metadata["attachments"][0]["id"] == "att-1"
        assert txns[0].metadata["attachments"][0]["filename"] == "a.pdf"
        assert "content" not in txns[0].metadata["attachments"][0]
        assert sdk_client.channels.configs.attachment_calls == []

    def test_filter_scans_claim_cursor_until_batch_is_filled(self):
        config = ErgonPlatformChannelsConsumerConfig(
            address=INBOX,
            batch_size=1,
            activity_filter=ChannelsActivityFilter(from_address="wanted@x.com"),
        )
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        sdk_client.channels.configs.claim_responses = [
            {
                "items": [{"id": "evt-skip", "from_address": "other@x.com"}],
                "next_cursor": "cursor-1",
            },
            {
                "items": [{"id": "evt-wanted", "from_address": "wanted@x.com"}],
                "next_cursor": None,
            },
        ]
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        transactions = connector.fetch_transactions()

        assert [transaction.id for transaction in transactions] == ["evt-wanted"]
        assert [call[1]["cursor"] for call in sdk_client.channels.configs.claim_calls] == [
            None,
            "cursor-1",
        ]
        assert sdk_client.channels.configs.ack_calls[0][1] == "evt-skip"

    def test_attachment_failure_requeues_every_claim_in_failed_fetch(self):
        config = ErgonPlatformChannelsConsumerConfig(
            address=INBOX,
            batch_size=2,
            download_attachments=True,
        )
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        sdk_client.channels.configs.activity_response = {
            "items": [
                {
                    "id": event_id,
                    "payload": {"attachments": [{"resend_attachment_id": f"att-{event_id}", "filename": "a.pdf"}]},
                }
                for event_id in ("evt-1", "evt-2")
            ]
        }

        def _boom(*args, **kwargs):
            raise TimeoutError("cdn hung")

        sdk_client.channels.configs.activity_attachment_file = _boom
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        with pytest.raises(TimeoutError, match="cdn hung"):
            connector.fetch_transactions()

        assert [call[1] for call in sdk_client.channels.configs.nack_calls] == [
            "evt-1",
            "evt-2",
        ]

    def test_best_effort_attachment_failure_blocks_ack_by_default(self):
        config = ErgonPlatformChannelsConsumerConfig(
            address=INBOX,
            download_attachments=True,
            attachment_failure_policy="best_effort",
        )
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        sdk_client.channels.configs.activity_response = {
            "items": [
                {
                    "id": "evt-1",
                    "payload": {"attachments": [{"resend_attachment_id": "att-1", "filename": "a.pdf"}]},
                }
            ]
        }

        def _boom(*args, **kwargs):
            raise TimeoutError("cdn hung")

        sdk_client.channels.configs.activity_attachment_file = _boom
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)
        transaction = connector.fetch_transactions()[0]

        assert transaction.metadata["attachment_failures"][0]["attachment_id"] == "att-1"
        with pytest.raises(ValueError, match="failed attachment"):
            connector.ack_transaction(transaction)
        connector.ack_transaction(transaction, allow_attachment_failures=True)

    def test_fetch_requires_consumer_config(self):
        connector = _make_connector()

        with pytest.raises(ValueError, match="consumer_config"):
            connector.fetch_transactions()

    def test_unknown_address_raises(self):
        config = ErgonPlatformChannelsConsumerConfig(address="missing@inbox.ergondata.ai")
        sdk_client = _Client()
        sdk_client.channels.addresses_response = []
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        with pytest.raises(ValueError, match="missing@inbox.ergondata.ai"):
            connector.fetch_transactions()

    def test_received_only_false_still_uses_claim_contract(self):
        config = ErgonPlatformChannelsConsumerConfig(address=INBOX, received_only=False)
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        connector.fetch_transactions()

        assert sdk_client.channels.configs.claim_calls[0][0] == "cfg-jsl"
        assert sdk_client.channels.configs.claim_calls[0][1]["limit"] == 50

    def test_activity_filter_settles_nonmatches_without_starving_later_events(self):
        config = ErgonPlatformChannelsConsumerConfig(
            address=INBOX,
            activity_filter=ChannelsActivityFilter(
                correlation_id="order-42",
                from_address="cliente@x.com",
            ),
        )
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        sdk_client.channels.configs.activity_response = {
            "items": [
                {
                    "id": "evt-1",
                    "channel": "email",
                    "correlation_id": "order-42",
                    "from_address": "cliente@x.com",
                    "subject": "Pedido",
                },
                {
                    "id": "evt-2",
                    "channel": "email",
                    "correlation_id": "order-42",
                    "from_address": "outro@x.com",
                    "subject": "Spam",
                },
            ],
            "total": 2,
        }
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        txns = connector.fetch_transactions()

        assert [tx.id for tx in txns] == ["evt-1"]
        assert sdk_client.channels.configs.ack_calls == [
            (
                "cfg-jsl",
                "evt-2",
                {
                    "subscription_id": sdk_client.channels.configs.claim_calls[0][1]["subscription_id"],
                    "lease_token": "lease-evt-2",
                },
            )
        ]

    def test_fetch_with_config_id_skips_global_addresses_lookup(self):
        config = ErgonPlatformChannelsConsumerConfig(
            address=INBOX,
            config_id="cfg-jsl",
            batch_size=10,
        )
        sdk_client = _Client()
        sdk_client.channels.configs.config_addresses_response = {
            "items": [
                {
                    "id": "addr-jsl",
                    "address": INBOX,
                    "direction": "receive",
                }
            ]
        }
        sdk_client.channels.configs.activity_response = {
            "items": [{"id": "evt-1", "channel": "email", "direction": "in"}],
            "total": 1,
        }
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        txns = connector.fetch_transactions()

        assert [tx.id for tx in txns] == ["evt-1"]
        assert sdk_client.channels.addresses_calls == []

    def test_resolves_address_from_config_id_and_email_via_activity(self):
        config = ErgonPlatformChannelsConsumerConfig(
            address=INBOX,
            config_id="cfg-jsl",
            batch_size=5,
        )
        sdk_client = _Client()
        sdk_client.channels.configs.activity_response = {
            "items": [
                {
                    "id": "evt-created",
                    "event_type": "channels.address.created",
                    "summary": INBOX,
                }
            ],
            "total": 1,
        }
        sdk_client.channels.configs.event_response = {
            "id": "evt-created",
            "payload": {
                "address": INBOX,
                "direction": "receive",
                "channel_address_id": "addr-jsl",
            },
        }
        sdk_client.channels.configs.claim_response = {
            "items": [
                {
                    "id": "evt-inbound",
                    "event_type": "channels.email.received",
                    "payload": {"address_id": "addr-jsl", "text": "complete body"},
                }
            ]
        }
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        txns = connector.fetch_transactions()

        assert [tx.id for tx in txns] == ["evt-inbound"]
        assert txns[0].metadata["message_payload"]["text"] == "complete body"
        assert sdk_client.channels.addresses_calls == []
        assert sdk_client.channels.configs.event_calls == [("cfg-jsl", "evt-created", {})]
        assert sdk_client.channels.configs.claim_calls[-1][0] == "cfg-jsl"
        assert sdk_client.channels.configs.claim_calls[-1][1]["limit"] == 5

    def test_resolve_inbox_from_config_id_and_email(self):
        config = ErgonPlatformChannelsConsumerConfig(
            address=INBOX,
            config_id="cfg-jsl",
        )
        sdk_client = _Client()
        sdk_client.channels.configs.activity_response = {
            "items": [
                {
                    "id": "evt-created",
                    "event_type": "channels.address.created",
                    "summary": INBOX,
                }
            ],
            "total": 1,
        }
        sdk_client.channels.configs.event_response = {
            "id": "evt-created",
            "payload": {
                "address": INBOX,
                "direction": "receive",
                "channel_address_id": "addr-jsl",
            },
        }
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        inbox = connector.resolve_inbox()

        assert inbox.address_id == "addr-jsl"
        assert inbox.config_id == "cfg-jsl"
        assert inbox.direction == "receive"


class TestUnifiedChannelsConfig:
    def test_fetch_via_channels_config(self):
        channels_config = ErgonPlatformChannelsConfig(
            address=INBOX,
            config_id="cfg-jsl",
            batch_size=10,
        )
        sdk_client = _Client()
        sdk_client.channels.configs.config_addresses_response = {
            "items": [{"id": "addr-jsl", "address": INBOX, "direction": "both"}]
        }
        sdk_client.channels.configs.activity_response = {
            "items": [{"id": "evt-1", "event_type": "channels.email.received"}],
            "total": 1,
        }
        connector = _make_connector(channels_config=channels_config, sdk_client=sdk_client)

        txns = connector.fetch_transactions()

        assert [tx.id for tx in txns] == ["evt-1"]
        assert sdk_client.channels.configs.claim_calls[-1][1]["limit"] == 10

    def test_send_email(self):
        channels_config = ErgonPlatformChannelsConfig(
            address=INBOX,
            config_id="cfg-jsl",
        )
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        connector = _make_connector(channels_config=channels_config, sdk_client=sdk_client)

        sent_id = connector.send_email(to="a@x.com", subject="Hi", html="<p>hi</p>")

        assert sent_id == "log-1"
        request = sdk_client.channels.send_calls[0]
        assert request["channel"] == "email"
        assert request["address_id"] == "addr-jsl"
        assert request["config"]["to"] == ["a@x.com"]


class TestDispatchTransactions:
    def test_dispatch_sends_message_with_producer_defaults(self):
        producer = ErgonPlatformChannelsProducerConfig(
            address="outbound@inbox.ergondata.ai",
            service_name="svc",
            default_reply_to="ops@x.com",
        )
        sdk_client = _Client()
        _seed_producer_address(sdk_client, address_id="addr-default", direction="send")
        connector = _make_connector(producer_config=producer, sdk_client=sdk_client)
        tx = Transaction(
            id="tx-1",
            payload=SendMessageInput(to=["a@x.com"], subject="Hi", html="<p>hi</p>"),
        )

        result = connector.dispatch_transactions([tx])

        assert result == ["log-1"]
        request = sdk_client.channels.send_calls[0]
        assert request["address_id"] == "addr-default"
        assert request["service_name"] == "svc"
        assert request["config"]["reply_to"] == "ops@x.com"

    def test_producer_inherits_consumer_address(self):
        consumer = ErgonPlatformChannelsConsumerConfig(address=INBOX)
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        connector = _make_connector(consumer_config=consumer, sdk_client=sdk_client)
        tx = Transaction(id="tx-1", payload=SendMessageInput(to=["a@x.com"], subject="s", html="h"))

        connector.dispatch_transactions([tx])

        assert sdk_client.channels.send_calls[0]["address_id"] == "addr-jsl"

    def test_dispatch_requires_address(self):
        connector = _make_connector()
        tx = Transaction(id="tx-1", payload=SendMessageInput(to=["a@x.com"], subject="s", html="h"))

        with pytest.raises(ValueError, match="address"):
            connector.dispatch_transactions([tx])


class TestFetchTransactionById:
    def test_fetches_inbox_event(self):
        config = ErgonPlatformChannelsConsumerConfig(address=INBOX)
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        tx = connector.fetch_transaction_by_id("evt-1")

        assert tx.id == "evt-1"
        assert sdk_client.channels.configs.event_calls == [("cfg-jsl", "evt-1", {})]
        assert sdk_client.channels.configs.attachment_calls == []

    def test_hydrates_attachments_when_enabled(self):
        config = ErgonPlatformChannelsConsumerConfig(
            address=INBOX,
            download_attachments=True,
        )
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        sdk_client.channels.configs.event_response = {
            "id": "evt-1",
            "channel": "email",
            "payload": {
                "attachments": [
                    {
                        "resend_attachment_id": "att-1",
                        "filename": "a.pdf",
                        "content_type": "application/pdf",
                    }
                ]
            },
        }
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        tx = connector.fetch_transaction_by_id("evt-1")

        assert tx.metadata["attachments"][0]["id"] == "att-1"
        assert tx.metadata["attachments"][0]["content"] == b"pdf-bytes"
        assert sdk_client.channels.configs.attachment_calls == [
            ("cfg-jsl", "evt-1", "att-1", {}),
        ]

    def test_raises_when_attachment_download_fails(self):
        config = ErgonPlatformChannelsConsumerConfig(
            address=INBOX,
            download_attachments=True,
        )
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        sdk_client.channels.configs.event_response = {
            "id": "evt-1",
            "channel": "email",
            "payload": {
                "attachments": [
                    {
                        "resend_attachment_id": "att-1",
                        "filename": "a.pdf",
                        "content_type": "application/pdf",
                    }
                ]
            },
        }

        def _boom(*args, **kwargs):
            raise TimeoutError("cdn hung")

        sdk_client.channels.configs.activity_attachment_file = _boom
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        with pytest.raises(TimeoutError, match="cdn hung"):
            connector.fetch_transaction_by_id("evt-1")


class TestGetTransactionsCount:
    def test_returns_total_for_inbox(self):
        config = ErgonPlatformChannelsConsumerConfig(address=INBOX)
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        sdk_client.channels.configs.activity_response = {"items": [], "total": 15}
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        assert connector.get_transactions_count() == 15
        assert sdk_client.channels.configs.activity_calls == [
            (
                "cfg-jsl",
                {
                    "address_id": "addr-jsl",
                    "limit": 1,
                    "page": 1,
                },
            ),
        ]


class TestAddressCapabilities:
    def test_fetch_rejects_send_only_address(self):
        config = ErgonPlatformChannelsConsumerConfig(address=INBOX)
        sdk_client = _Client()
        _seed_addresses(sdk_client, direction="send")
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        with pytest.raises(ValueError, match="send-only"):
            connector.fetch_transactions()

    def test_dispatch_rejects_receive_only_address(self):
        consumer = ErgonPlatformChannelsConsumerConfig(address=INBOX)
        sdk_client = _Client()
        _seed_addresses(sdk_client, direction="receive")
        connector = _make_connector(consumer_config=consumer, sdk_client=sdk_client)
        tx = Transaction(id="tx-1", payload=SendMessageInput(to=["a@x.com"], subject="s", html="h"))

        with pytest.raises(ValueError, match="receive-only"):
            connector.dispatch_transactions([tx])

    def test_dispatch_rejects_config_with_can_send_false(self):
        consumer = ErgonPlatformChannelsConsumerConfig(address=INBOX, config_id="cfg-jsl")
        sdk_client = _Client()
        sdk_client.channels.configs.verify_response = {
            "name": "Recebimento local",
            "inbound_enabled": True,
            "capabilities": {"can_send": False},
        }
        sdk_client.channels.configs.activity_response = {
            "items": [
                {
                    "id": "evt-created",
                    "event_type": "channels.address.created",
                    "summary": INBOX,
                }
            ],
            "total": 1,
        }
        sdk_client.channels.configs.event_response = {
            "payload": {
                "address": INBOX,
                "direction": "both",
                "channel_address_id": "addr-jsl",
            }
        }
        connector = _make_connector(consumer_config=consumer, sdk_client=sdk_client)
        tx = Transaction(id="tx-1", payload=SendMessageInput(to=["a@x.com"], subject="s", html="h"))

        with pytest.raises(ValueError, match="can_send=true"):
            connector.dispatch_transactions([tx])

    def test_resolve_address_info_includes_capabilities(self):
        config = ErgonPlatformChannelsConsumerConfig(address=INBOX)
        sdk_client = _Client()
        _seed_addresses(sdk_client, direction="both")
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        info = connector.resolve_address_info()

        assert info == {
            "address": INBOX,
            "address_id": "addr-jsl",
            "config_id": "cfg-jsl",
            "config_name": "test-channel",
            "direction": "both",
            "mode": "receive and send",
            "config_can_send": True,
            "config_can_receive": True,
            "can_receive": True,
            "can_send": True,
        }


class TestAdvancedHelpers:
    def test_list_thread_messages(self):
        sdk_client = _Client()
        sdk_client.channels.thread_messages_response = {"messages": [{"id": "msg-1"}]}
        connector = _make_connector(sdk_client=sdk_client)

        txns = connector.list_thread_messages("th-1", limit=5)

        assert [tx.id for tx in txns] == ["msg-1"]
        assert sdk_client.channels.thread_calls == [("th-1", {})]

    def test_list_company_activity(self):
        sdk_client = _Client()
        sdk_client.channels.company_activity_response = {"items": [{"id": "evt-co"}]}
        connector = _make_connector(sdk_client=sdk_client)

        txns = connector.list_company_activity(limit=10)

        assert [tx.id for tx in txns] == ["evt-co"]


class TestLifecycle:
    def test_close_closes_client(self):
        sdk_client = _Client()
        connector = _make_connector(sdk_client=sdk_client)

        connector.close()

        assert sdk_client.closed

    def test_ack_calls_platform_activity_ack(self):
        config = ErgonPlatformChannelsConsumerConfig(address=INBOX)
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        connector.ack_transaction(_claimed_transaction())

        assert sdk_client.channels.configs.ack_calls == [
            (
                "cfg-jsl",
                "evt-1",
                {
                    "subscription_id": "11111111-1111-1111-1111-111111111111",
                    "lease_token": "22222222-2222-2222-2222-222222222222",
                },
            )
        ]

    def test_ack_rejects_unclaimed_detail_transaction(self):
        config = ErgonPlatformChannelsConsumerConfig(address=INBOX)
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        with pytest.raises(ValueError, match="not claimed"):
            connector.ack_transaction(Transaction(id="evt-1", payload={}))

    def test_nack_calls_platform_activity_nack(self):
        config = ErgonPlatformChannelsConsumerConfig(
            address=INBOX,
            nack_delay_seconds=30,
        )
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        connector.nack_transaction(_claimed_transaction(), requeue=True)

        assert sdk_client.channels.configs.nack_calls == [
            (
                "cfg-jsl",
                "evt-1",
                {
                    "subscription_id": "11111111-1111-1111-1111-111111111111",
                    "lease_token": "22222222-2222-2222-2222-222222222222",
                    "requeue": True,
                    "delay_seconds": 30,
                },
            ),
        ]

    def test_download_attachments_uses_transaction_metadata(self, tmp_path):
        config = ErgonPlatformChannelsConsumerConfig(address=INBOX)
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)
        tx = event_to_transaction(
            {
                "id": "evt-1",
                "payload": {
                    "attachments": [
                        {
                            "resend_attachment_id": "att-1",
                            "filename": "a.pdf",
                            "content_type": "application/pdf",
                        }
                    ]
                },
            },
            source="config_activity",
        )

        files = connector.download_attachments(tx, dest=tmp_path)

        assert len(files) == 1
        assert files[0].attachment_id == "att-1"
        assert files[0].filename == "a--att-1.pdf"
        assert files[0].content == b"pdf-bytes"
        assert files[0].path == str(tmp_path / "evt-1" / "a--att-1.pdf")
        assert (tmp_path / "evt-1" / "a--att-1.pdf").read_bytes() == b"pdf-bytes"
        assert sdk_client.channels.configs.attachment_calls == [
            ("cfg-jsl", "evt-1", "att-1", {}),
        ]

    def test_download_rejects_windows_absolute_and_unc_filenames(self, tmp_path):
        config = ErgonPlatformChannelsConsumerConfig(address=INBOX)
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        for filename in (r"C:\Windows\system.ini", r"\\server\share\file.txt"):
            transaction = event_to_transaction(
                {
                    "id": "evt-unsafe",
                    "payload": {
                        "attachments": [
                            {
                                "resend_attachment_id": "att-unsafe",
                                "filename": filename,
                            }
                        ]
                    },
                },
                source="config_activity",
            )
            with pytest.raises(ValueError, match="Unsafe absolute"):
                connector.download_attachments(transaction, dest=tmp_path)

        assert not (tmp_path / "evt-unsafe").exists()

    def test_download_neutralizes_windows_traversal_and_disambiguates(self, tmp_path):
        config = ErgonPlatformChannelsConsumerConfig(address=INBOX)
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)
        transaction = event_to_transaction(
            {
                "id": "evt-safe",
                "payload": {
                    "attachments": [
                        {
                            "resend_attachment_id": "att-safe",
                            "filename": r"..\..\invoice.pdf",
                        }
                    ]
                },
            },
            source="config_activity",
        )

        files = connector.download_attachments(transaction, dest=tmp_path)

        expected = tmp_path / "evt-safe" / "invoice--att-safe.pdf"
        assert files[0].path == str(expected)
        assert expected.read_bytes() == b"pdf-bytes"
        assert not (tmp_path.parent / "invoice.pdf").exists()

    def test_download_attachments_skips_dest_when_empty(self, tmp_path):
        config = ErgonPlatformChannelsConsumerConfig(address=INBOX)
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)
        tx = event_to_transaction(
            {"id": "evt-empty", "payload": {"attachments": []}},
            source="config_activity",
        )

        files = connector.download_attachments(tx, dest=tmp_path)

        assert files == []
        assert not (tmp_path / "evt-empty").exists()
        assert sdk_client.channels.configs.attachment_calls == []

    def test_optional_client_side_dedup_still_skips_seen_ids(self):
        config = ErgonPlatformChannelsConsumerConfig(
            address=INBOX,
            deduplicate_fetched_events=True,
        )
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        sdk_client.channels.configs.activity_response = {
            "items": [{"id": "evt-1", "channel": "email", "status": "received"}],
            "total": 1,
        }
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        assert [tx.id for tx in connector.fetch_transactions()] == ["evt-1"]
        assert connector.fetch_transactions() == []
