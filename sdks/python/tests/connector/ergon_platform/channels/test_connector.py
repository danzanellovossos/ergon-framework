"""Tests for ErgonPlatformChannelsConnector."""

from unittest.mock import patch

import pytest

from ergon.connector.ergon_platform.channels.connector import ErgonPlatformChannelsConnector
from ergon.connector.ergon_platform.channels.models import (
    ChannelsActivityFilter,
    ErgonPlatformChannelsConfig,
    ErgonPlatformChannelsConsumerConfig,
    ErgonPlatformChannelsProducerConfig,
    SendMessageInput,
)
from ergon.connector.ergon_platform.channels.utils import event_to_transaction
from ergon.connector.ergon_platform.models import ErgonPlatformClient
from ergon.connector.transaction import Transaction

INBOX = "jsl-xxx@inbox.ergondata.ai"


class _ConfigAddresses:
    def __init__(self, configs: "_Configs"):
        self._configs = configs

    def list(self, **params):
        return self._configs.config_addresses_response


class _Configs:
    def __init__(self):
        self.activity_calls = []
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
        assert sdk_client.channels.configs.activity_calls == [
            (
                "cfg-jsl",
                {
                    "channel": "email",
                    "event_type": "channels.email.received",
                    "address_id": "addr-jsl",
                    "pending_only": True,
                    "limit": 25,
                    "offset": 0,
                },
            ),
        ]
        assert len(sdk_client.channels.addresses_calls) == 1

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

    def test_received_only_false_omits_direction_filter(self):
        config = ErgonPlatformChannelsConsumerConfig(address=INBOX, received_only=False)
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        connector.fetch_transactions()

        assert sdk_client.channels.configs.activity_calls == [
            (
                "cfg-jsl",
                {
                    "channel": "email",
                    "address_id": "addr-jsl",
                    "pending_only": True,
                    "limit": 50,
                    "offset": 0,
                },
            ),
        ]


    def test_activity_filter_passes_server_params_and_filters_client_side(self):
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
                    "from_address": "cliente@x.com",
                    "subject": "Pedido",
                },
                {
                    "id": "evt-2",
                    "channel": "email",
                    "from_address": "outro@x.com",
                    "subject": "Spam",
                },
            ],
            "total": 2,
        }
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        txns = connector.fetch_transactions()

        assert [tx.id for tx in txns] == ["evt-1"]
        assert sdk_client.channels.configs.activity_calls == [
            (
                "cfg-jsl",
                {
                    "channel": "email",
                    "event_type": "channels.email.received",
                    "correlation_id": "order-42",
                    "address_id": "addr-jsl",
                    "pending_only": True,
                    "limit": 50,
                    "offset": 0,
                },
            ),
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
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        txns = connector.fetch_transactions()

        assert [tx.id for tx in txns] == ["evt-created"]
        assert sdk_client.channels.addresses_calls == []
        assert sdk_client.channels.configs.event_calls == [("cfg-jsl", "evt-created", {})]
        assert sdk_client.channels.configs.activity_calls[-1] == (
            "cfg-jsl",
            {
                "channel": "email",
                "event_type": "channels.email.received",
                "address_id": "addr-jsl",
                "pending_only": True,
                "limit": 5,
                "offset": 0,
            },
        )

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
        assert sdk_client.channels.configs.activity_calls[-1][1]["event_type"] == "channels.email.received"

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
                    "channel": "email",
                    "event_type": "channels.email.received",
                    "address_id": "addr-jsl",
                    "pending_only": True,
                    "limit": 1,
                    "offset": 0,
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
        assert sdk_client.channels.thread_calls == [("th-1", {"limit": 5, "offset": 0})]

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

        connector.ack_transaction(Transaction(id="evt-1", payload={}))

        assert sdk_client.channels.configs.ack_calls == [("cfg-jsl", "evt-1", {})]

    def test_nack_calls_platform_activity_nack(self):
        config = ErgonPlatformChannelsConsumerConfig(
            address=INBOX,
            nack_delay_seconds=30,
        )
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        connector.nack_transaction(Transaction(id="evt-1", payload={}), requeue=True)

        assert sdk_client.channels.configs.nack_calls == [
            ("cfg-jsl", "evt-1", {"requeue": True, "delay_seconds": 30}),
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
        assert files[0].filename == "a.pdf"
        assert files[0].content == b"pdf-bytes"
        assert files[0].path == str(tmp_path / "evt-1" / "a.pdf")
        assert (tmp_path / "evt-1" / "a.pdf").read_bytes() == b"pdf-bytes"
        assert sdk_client.channels.configs.attachment_calls == [
            ("cfg-jsl", "evt-1", "att-1", {}),
        ]

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
