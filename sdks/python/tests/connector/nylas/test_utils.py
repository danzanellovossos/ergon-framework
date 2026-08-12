"""Tests for Nylas connector utilities."""

from ergon.connector.nylas.models import (
    AckActionConfig,
    ClientSideFilter,
    EmailAddress,
    MessageQueryFilter,
    NylasConsumerConfig,
    SendMessageInput,
)
from ergon.connector.nylas.utils import (
    apply_client_side_filter,
    build_ack_request_body,
    merge_query_filter,
    normalize_send_payload,
)


class TestMergeQueryFilter:
    def test_merges_consumer_defaults(self):
        config = NylasConsumerConfig(subject="Test", batch_size=20, has_attachment=True)
        query = merge_query_filter(config)
        assert query.subject == "Test"
        assert query.has_attachment is True

    def test_override_takes_precedence(self):
        config = NylasConsumerConfig(subject="Original")
        query = merge_query_filter(config, MessageQueryFilter(subject="Override"))
        assert query.subject == "Override"

    def test_preserves_aliased_fields(self):
        config = NylasConsumerConfig(
            in_="Label_7008109963921275738",
            from_=["sender@example.com"],
            unread=True,
            batch_size=10,
            download_attachments=True,
            ack_config=AckActionConfig(mark_as_read=True),
        )
        merged = merge_query_filter(config)
        assert merged.in_ == "Label_7008109963921275738"
        assert merged.from_ == ["sender@example.com"]
        assert merged.to_query_params(limit=10) == {
            "in": "Label_7008109963921275738",
            "from": ["sender@example.com"],
            "unread": True,
            "limit": 10,
        }

    def test_override_preserves_aliased_fields(self):
        config = NylasConsumerConfig(in_="INBOX", unread=True)
        overrides = MessageQueryFilter(in_="Label_override", from_=["a@b.com"])
        merged = merge_query_filter(config, overrides)
        assert merged.in_ == "Label_override"
        assert merged.from_ == ["a@b.com"]
        assert merged.unread is True


class TestClientSideFilter:
    def test_subject_contains_case_insensitive(self):
        messages = [
            {"id": "1", "subject": "FATURA Março"},
            {"id": "2", "subject": "Outro"},
        ]
        filt = ClientSideFilter(subject_contains="fatura")
        result = apply_client_side_filter(messages, filt)
        assert len(result) == 1
        assert result[0]["id"] == "1"

    def test_attachment_filename_filter(self):
        messages = [
            {"id": "1", "attachments": [{"filename": "report.pdf"}]},
            {"id": "2", "attachments": [{"filename": "image.png"}]},
        ]
        filt = ClientSideFilter(attachment_filename_contains="pdf")
        result = apply_client_side_filter(messages, filt)
        assert len(result) == 1
        assert result[0]["id"] == "1"


class TestAckBody:
    def test_build_ack_request_body(self):
        config = AckActionConfig(mark_as_read=True, move_to_folder_id="folder-1", add_star=True)
        body = build_ack_request_body(config)
        assert body == {"unread": False, "folders": ["folder-1"], "starred": True}


class TestNormalizeSendPayload:
    def test_send_message_input_to_dict(self):
        payload = SendMessageInput(
            to=[EmailAddress(email="user@example.com")],
            subject="Hello",
            body="World",
        )
        body = normalize_send_payload(payload)
        assert body["subject"] == "Hello"
        assert body["to"] == [{"email": "user@example.com"}]
