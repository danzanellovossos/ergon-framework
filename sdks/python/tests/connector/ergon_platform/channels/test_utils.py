"""Tests for Ergon Platform channels connector utilities."""

import pytest

from ergon.connector.ergon_platform.channels._activity import ActivityAdapter
from ergon.connector.ergon_platform.channels._outbound import OutboundMessage
from ergon.connector.ergon_platform.channels._sdk import SdkRecord
from ergon.connector.ergon_platform.channels.models import (
    INBOUND_RECEIVED_EVENT_TYPE,
    ChannelsActivityFilter,
    ResolvedInboxAddress,
    SendMessageAttachment,
    SendMessageInput,
)
from ergon.connector.transaction import Transaction

deliver_fetched_transactions = ActivityAdapter.unseen
event_to_transaction = ActivityAdapter.to_transaction
extract_items = SdkRecord.items
extract_total = SdkRecord.total
inbox_attachment_id = ActivityAdapter.attachment_id
inbox_attachments = ActivityAdapter.attachments
normalize_send_payload = OutboundMessage.normalize


class TestResolvedInboxCapabilities:
    def test_receive_only_inbox_blocks_send_with_friendly_message(self):
        inbox = ResolvedInboxAddress(
            address="inbox@x.ai",
            address_id="addr-1",
            config_id="cfg-1",
            config_name="Recebimento local",
            direction="both",
            config_can_send=False,
            config_can_receive=True,
        )

        with pytest.raises(ValueError, match="receive-only") as exc:
            inbox.ensure_can_send()

        assert "CHANNELS_SEND_CONFIG_ID" in str(exc.value)
        assert "can_send=false" not in str(exc.value)  # uses mode + platform line

    def test_send_only_inbox_blocks_fetch(self):
        inbox = ResolvedInboxAddress(
            address="out@x.ai",
            address_id="addr-2",
            config_id="cfg-2",
            direction="send",
        )

        with pytest.raises(ValueError, match="send-only") as exc:
            inbox.ensure_can_receive()

        assert "consumer_config" in str(exc.value)


class TestChannelsActivityFilter:
    def test_activity_query_params_received_only(self):
        filt = ChannelsActivityFilter()
        assert filt.activity_query_params() == {
            "channel": "email",
            "event_type": INBOUND_RECEIVED_EVENT_TYPE,
        }

    def test_activity_query_params_server_fields(self):
        filt = ChannelsActivityFilter(
            received_only=False,
            correlation_id="order-1",
            thread_id="th-1",
        )
        assert filt.activity_query_params() == {
            "channel": "email",
            "correlation_id": "order-1",
            "thread_id": "th-1",
        }

    def test_matches_from_address_case_insensitive(self):
        filt = ChannelsActivityFilter(from_address="Client@X.com")
        tx = event_to_transaction(
            {"id": "1", "from_address": "client@x.com", "subject": "Hi"},
            source="activity",
        )
        assert filt.matches(tx) is True

    def test_matches_subject_contains(self):
        filt = ChannelsActivityFilter(subject_contains="nf-e")
        tx = event_to_transaction({"id": "1", "subject": "Sua NF-e chegou"}, source="activity")
        assert filt.matches(tx) is True
        assert (
            filt.matches(
                event_to_transaction({"id": "2", "subject": "Outro assunto"}, source="activity"),
            )
            is False
        )

    def test_matches_subject_contains_ignores_accents(self):
        filt = ChannelsActivityFilter(subject_contains="código de acesso")
        tx = event_to_transaction(
            {"id": "1", "subject": "ENC: Seu codigo de acesso | ASSDI25"},
            source="activity",
        )
        assert filt.matches(tx) is True
        assert ChannelsActivityFilter(subject_contains="codigo").matches(tx) is True

    def test_filter_activity_transactions(self):
        filt = ChannelsActivityFilter(from_address="a@x.com")
        txns = [
            event_to_transaction({"id": "1", "from_address": "a@x.com"}, source="activity"),
            event_to_transaction({"id": "2", "from_address": "b@x.com"}, source="activity"),
        ]
        assert [tx.id for tx in filt.select(txns)] == ["1"]


class TestDeliverFetchedTransactions:
    def test_skips_already_delivered_ids(self):
        seen: set[str] = set()
        txns = [
            event_to_transaction({"id": "evt-1"}, source="activity"),
            event_to_transaction({"id": "evt-2"}, source="activity"),
        ]

        assert [tx.id for tx in deliver_fetched_transactions(txns, seen)] == ["evt-1", "evt-2"]
        assert deliver_fetched_transactions(txns, seen) == []
        assert seen == {"evt-1", "evt-2"}

    def test_ignores_transactions_without_id(self):
        seen: set[str] = set()
        txns = [event_to_transaction({}, source="activity")]

        assert deliver_fetched_transactions(txns, seen) == []
        assert seen == set()


class TestExtractItems:
    def test_from_messages_key(self):
        assert extract_items({"messages": [1, 2]}, keys=["messages"]) == [1, 2]

    def test_from_items_key(self):
        assert extract_items({"items": [1]}) == [1]

    def test_from_list(self):
        assert extract_items([1, 2, 3]) == [1, 2, 3]

    def test_from_page_like(self):
        class Page:
            items = [1, 2]

        assert extract_items(Page()) == [1, 2]

    def test_empty(self):
        assert extract_items(None) == []


class TestExtractTotal:
    def test_from_dict(self):
        assert extract_total({"total": 42}) == 42
        assert extract_total({"count": 3}) == 3

    def test_from_page_like(self):
        class Page:
            total = 10

        assert extract_total(Page()) == 10

    def test_missing(self):
        assert extract_total({}) == 0


class TestEventToTransaction:
    def test_wraps_dict_event_with_metadata(self):
        event = {
            "id": "evt-1",
            "channel": "email",
            "direction": "in",
            "status": "received",
            "thread_id": "th-1",
            "provider_message_id": "<abc@x>",
            "subject": "Hi",
            "from_address": "a@x.com",
            "to_addresses": ["b@x.com"],
        }

        tx = event_to_transaction(event, source="activity")

        assert tx.id == "evt-1"
        assert tx.payload == event
        assert tx.metadata["source"] == "activity"
        assert tx.metadata["channel"] == "email"
        assert tx.metadata["direction"] == "in"
        assert tx.metadata["thread_id"] == "th-1"
        assert tx.metadata["from_address"] == "a@x.com"
        assert tx.metadata["to_addresses"] == ["b@x.com"]

    def test_exposes_nested_message_payload_in_metadata(self):
        event = {
            "id": "evt-1",
            "channel": "email",
            "subject": "Hi",
            "payload": {
                "text": "hello",
                "html": "<p>hello</p>",
                "attachments": [{"filename": "a.pdf"}],
            },
        }

        tx = event_to_transaction(event, source="activity")

        assert tx.metadata["message_payload"] == event["payload"]
        assert tx.metadata["attachments"] == [{"filename": "a.pdf"}]
        assert tx.metadata["has_attachment"] is True

    def test_normalizes_platform_attachments_to_nylas_shape(self):
        event = {
            "id": "evt-1",
            "payload": {
                "attachments": [
                    {
                        "resend_attachment_id": "att-1",
                        "filename": "a.pdf",
                        "content_type": "application/pdf",
                        "size": 12,
                    },
                    {"name": "b.txt"},
                    {"resend_attachment_id": "skip-me"},
                ]
            },
        }

        tx = event_to_transaction(event, source="config_activity")

        assert tx.metadata["attachments"] == [
            {
                "id": "att-1",
                "filename": "a.pdf",
                "content_type": "application/pdf",
                "size": 12,
            },
            {"filename": "b.txt"},
        ]
        assert "content" not in tx.metadata["attachments"][0]
        assert tx.metadata["message_payload"]["attachments"][0]["resend_attachment_id"] == "att-1"

    def test_falls_back_to_log_id_when_no_id(self):
        event = {"log_id": "log-1", "channel": "email"}
        tx = event_to_transaction(event, source="activity")
        assert tx.id == "log-1"

    def test_falls_back_to_provider_message_id_when_no_id(self):
        event = {"provider_message_id": "<xyz@srv>", "channel": "email"}
        tx = event_to_transaction(event, source="thread")
        assert tx.id == "<xyz@srv>"

    def test_wraps_non_dict_payloads(self):
        tx = event_to_transaction("literal", source="activity")
        assert tx.id == ""
        assert tx.payload == {"value": "literal"}


class TestNormalizeSendPayload:
    def test_send_message_input_splits_top_and_config(self):
        payload = SendMessageInput(
            to=["a@x.com"],
            subject="Hello",
            html="<p>hi</p>",
            cc=["c@x.com"],
        )

        parts = normalize_send_payload(payload)

        assert parts["top"] == {}
        assert parts["config"]["to"] == ["a@x.com"]
        assert parts["config"]["subject"] == "Hello"
        assert parts["config"]["html"] == "<p>hi</p>"
        assert parts["config"]["cc"] == ["c@x.com"]
        # dropped-None fields
        assert "text" not in parts["config"]
        assert "bcc" not in parts["config"]
        assert "attachments" not in parts["config"]

    def test_text_only_generates_html_body(self):
        payload = SendMessageInput(to=["a@x.com"], subject="Hello", text="plain body")

        parts = normalize_send_payload(payload)

        assert parts["config"]["text"] == "plain body"
        assert parts["config"]["html"] == "<p>plain body</p>"

    def test_send_message_input_serializes_attachments(self):
        payload = SendMessageInput(
            to=["a@x.com"],
            subject="s",
            html="h",
            attachments=[
                SendMessageAttachment(filename="a.txt", content_type="text/plain", content="Zm9v"),
            ],
        )

        parts = normalize_send_payload(payload)
        attachments = parts["config"]["attachments"]

        assert attachments == [{"filename": "a.txt", "content_type": "text/plain", "content": "Zm9v"}]

    def test_dict_payload_with_nested_config_block(self):
        payload = {
            "address_id": "addr-1",
            "channel": "email",
            "config": {"to": ["a@x.com"], "subject": "S", "html": "H"},
            "resource_id": "res-1",
        }

        parts = normalize_send_payload(payload)

        assert parts["top"] == {
            "address_id": "addr-1",
            "channel": "email",
            "resource_id": "res-1",
            "service_name": None,
        }
        assert parts["config"] == {"to": ["a@x.com"], "subject": "S", "html": "H"}

    def test_dict_payload_flat_shape_promotes_to_config(self):
        payload = {
            "address_id": "addr-1",
            "channel": "email",
            "to": ["a@x.com"],
            "subject": "S",
            "html": "H",
        }

        parts = normalize_send_payload(payload)

        assert parts["config"] == {"to": ["a@x.com"], "subject": "S", "html": "H"}

    def test_raises_for_unsupported_payload_type(self):
        with pytest.raises(TypeError, match="Unsupported send payload type"):
            normalize_send_payload("bad")


class TestBackwardCompatImports:
    def test_channels_names_are_reexported_at_root(self):
        from ergon.connector.ergon_platform import (
            ErgonPlatformChannelsConnector as RootConnector,
        )
        from ergon.connector.ergon_platform import (
            ErgonPlatformChannelsConsumerConfig as RootConsumer,
        )
        from ergon.connector.ergon_platform import (
            ErgonPlatformChannelsProducerConfig as RootProducer,
        )
        from ergon.connector.ergon_platform.channels.connector import (
            ErgonPlatformChannelsConnector,
        )
        from ergon.connector.ergon_platform.channels.models import (
            ErgonPlatformChannelsConsumerConfig,
            ErgonPlatformChannelsProducerConfig,
        )

        assert RootConnector is ErgonPlatformChannelsConnector
        assert RootConsumer is ErgonPlatformChannelsConsumerConfig
        assert RootProducer is ErgonPlatformChannelsProducerConfig

    def test_transaction_helper_smoke_test(self):
        """Round-trip: dispatch payload -> serialized transaction survives Pydantic frozen."""
        payload = SendMessageInput(to=["a@x.com"], subject="s", html="h")
        tx = Transaction(id="tx-1", payload=payload)
        assert tx.payload.to == ["a@x.com"]


class TestInboxAttachments:
    def test_reads_message_payload_metadata(self):
        tx = event_to_transaction(
            {
                "id": "evt-1",
                "payload": {
                    "text": "hi",
                    "attachments": [
                        {
                            "resend_attachment_id": "att-1",
                            "filename": "a.pdf",
                            "content_type": "application/pdf",
                        }
                    ],
                },
            },
            source="config_activity",
        )
        atts = inbox_attachments(tx)
        assert [inbox_attachment_id(att) for att in atts] == ["att-1"]
        assert atts[0]["filename"] == "a.pdf"

    def test_empty_when_missing(self):
        tx = event_to_transaction({"id": "evt-2", "payload": {"text": "hi"}}, source="config_activity")
        assert inbox_attachments(tx) == []
        assert inbox_attachment_id({"filename": "x"}) is None


class TestHydrateSkipsFailedDownloads:
    def test_keeps_metadata_when_download_raises(self):
        from ergon.connector.ergon_platform.channels._attachments import InboxAttachments

        class _Configs:
            def activity_attachment_file(self, *args, **kwargs):
                raise TimeoutError("cdn hung")

        class _Client:
            def __init__(self):
                self.channels = type("Channels", (), {})()
                self.channels.configs = _Configs()

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

        out = InboxAttachments(_Client()).hydrate("cfg-1", tx)

        assert out.metadata["attachments"][0]["id"] == "att-1"
        assert out.metadata["attachments"][0]["filename"] == "a.pdf"
        assert "content" not in out.metadata["attachments"][0]
