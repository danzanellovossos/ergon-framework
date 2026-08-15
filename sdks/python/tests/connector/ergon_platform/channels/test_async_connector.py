"""Tests for AsyncErgonPlatformChannelsConnector."""

from unittest.mock import patch

import pytest

from ergon.connector.ergon_platform.channels.adapters import ActivityAdapter
from ergon.connector.ergon_platform.channels.async_connector import (
    AsyncErgonPlatformChannelsConnector,
)
from ergon.connector.ergon_platform.channels.models import (
    ErgonPlatformChannelsConfig,
    ErgonPlatformChannelsConsumerConfig,
    ErgonPlatformChannelsProducerConfig,
    SendMessageInput,
)
from ergon.connector.ergon_platform.models import ErgonPlatformClient
from ergon.connector.transaction import Transaction

event_to_transaction = ActivityAdapter.to_transaction

INBOX = "jsl-xxx@inbox.ergondata.ai"


class _Configs:
    def __init__(self):
        self.activity_calls = []
        self.claim_calls = []
        self.event_calls = []
        self.ack_calls = []
        self.nack_calls = []
        self.attachment_calls = []

    def activity(self, config_id, **params):
        self.activity_calls.append((config_id, params))
        return self.activity_response

    def activity_claim(self, config_id, **params):
        self.claim_calls.append((config_id, params))
        return {
            "items": [
                {
                    "event": {
                        "event_type": "channels.email.received",
                        **event,
                    },
                    "delivery": {
                        "subscription_id": params["subscription_id"],
                        "lease_token": f"lease-{event.get('id')}",
                    },
                }
                for event in self.activity_response.get("items", [])
            ],
            "next_cursor": None,
        }

    def activity_event(self, config_id, event_id, **params):
        self.event_calls.append((config_id, event_id, params))
        return self.event_response

    def activity_ack(self, config_id, event_id, **params):
        self.ack_calls.append((config_id, event_id, params))
        return {"status": "acked"}

    def activity_nack(self, config_id, event_id, **params):
        self.nack_calls.append((config_id, event_id, params))
        return {"status": "pending"}

    def activity_attachment_file(self, config_id, event_id, attachment_id, **params):
        self.attachment_calls.append((config_id, event_id, attachment_id, params))
        return b"pdf-bytes"

    activity_response = {"items": [], "total": 0}
    event_response = {"id": "evt-1", "channel": "email"}


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
) -> AsyncErgonPlatformChannelsConnector:
    sdk_client = sdk_client or _Client()
    with patch(
        "ergon.connector.ergon_platform.channels.async_connector.create_ergon_client",
        return_value=sdk_client,
    ):
        return AsyncErgonPlatformChannelsConnector(
            client=_client_config(),
            channels_config=channels_config,
            consumer_config=consumer_config,
            producer_config=producer_config,
        )


def _seed_addresses(sdk_client: _Client, *, direction: str = "both"):
    sdk_client.channels.addresses_response = [
        {"id": "addr-jsl", "address": INBOX, "channel_config_id": "cfg-jsl", "direction": direction}
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


class TestAsyncFetchTransactions:
    async def test_fetches_inbox_activity(self):
        config = ErgonPlatformChannelsConsumerConfig(address=INBOX, batch_size=25)
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        sdk_client.channels.configs.activity_response = {"items": [{"id": "evt-1"}], "total": 1}
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        txns = await connector.fetch_transactions_async()

        assert [tx.id for tx in txns] == ["evt-1"]
        assert sdk_client.channels.configs.claim_calls[0][0] == "cfg-jsl"
        assert sdk_client.channels.configs.claim_calls[0][1]["limit"] == 25
        assert txns[0].metadata["delivery"]["lease_token"] == "lease-evt-1"

    async def test_requires_consumer_config(self):
        connector = _make_connector()

        with pytest.raises(ValueError, match="consumer_config"):
            await connector.fetch_transactions_async()


class TestAsyncDispatchTransactions:
    async def test_dispatch_sends_message(self):
        producer = ErgonPlatformChannelsProducerConfig(address="outbound@inbox.ergondata.ai")
        sdk_client = _Client()
        _seed_producer_address(sdk_client, address_id="addr-1", direction="send")
        connector = _make_connector(producer_config=producer, sdk_client=sdk_client)
        tx = Transaction(
            id="tx-1",
            payload=SendMessageInput(to=["a@x.com"], subject="Hi", html="<p>hi</p>"),
        )

        result = await connector.dispatch_transactions_async([tx])

        assert result == ["log-1"]

    async def test_send_email_async_via_channels_config(self):
        channels_config = ErgonPlatformChannelsConfig(address=INBOX, config_id="cfg-jsl")
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        connector = _make_connector(channels_config=channels_config, sdk_client=sdk_client)

        sent_id = await connector.send_email_async(to="a@x.com", subject="Hi", text="hello")

        assert sent_id == "log-1"
        assert sdk_client.channels.send_calls[0]["config"]["text"] == "hello"


class TestAsyncLifecycle:
    async def test_close_closes_client_in_thread(self):
        sdk_client = _Client()
        connector = _make_connector(sdk_client=sdk_client)

        await connector.close()

        assert sdk_client.closed

    async def test_ack_calls_platform_activity_ack(self):
        config = ErgonPlatformChannelsConsumerConfig(address=INBOX)
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        await connector.ack_transaction(_claimed_transaction())

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

    async def test_nack_calls_platform_activity_nack(self):
        config = ErgonPlatformChannelsConsumerConfig(address=INBOX)
        sdk_client = _Client()
        _seed_addresses(sdk_client)
        connector = _make_connector(consumer_config=config, sdk_client=sdk_client)

        await connector.nack_transaction(_claimed_transaction(), requeue=False)

        assert sdk_client.channels.configs.nack_calls == [
            (
                "cfg-jsl",
                "evt-1",
                {
                    "subscription_id": "11111111-1111-1111-1111-111111111111",
                    "lease_token": "22222222-2222-2222-2222-222222222222",
                    "requeue": False,
                    "delay_seconds": 0,
                },
            ),
        ]

    async def test_download_attachments_uses_transaction_metadata(self):
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

        files = await connector.download_attachments(tx)

        assert len(files) == 1
        assert files[0].content == b"pdf-bytes"
        assert files[0].path is None
        assert sdk_client.channels.configs.attachment_calls == [
            ("cfg-jsl", "evt-1", "att-1", {}),
        ]
