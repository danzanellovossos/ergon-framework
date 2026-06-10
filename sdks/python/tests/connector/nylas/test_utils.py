"""Tests for Nylas connector utilities."""

from ergon.connector.nylas.models import (
    AckActionConfig,
    ClientSideFilter,
    MessageQueryFilter,
    NylasConsumerConfig,
    SendMessageInput,
    EmailAddress,
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
