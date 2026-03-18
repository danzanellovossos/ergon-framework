"""Tests for ergon.connector.sqs.service — SQSService."""

import json
from unittest.mock import MagicMock, patch

import pytest

from ergon.connector.sqs.models import SQSClient
from ergon.connector.sqs.service import SQSService


def _make_client(**overrides) -> SQSClient:
    defaults = {
        "region_name": "us-east-1",
        "queue_url": "https://sqs.us-east-1.amazonaws.com/123456789/test-queue",
    }
    defaults.update(overrides)
    return SQSClient(**defaults)


@pytest.fixture
def mock_boto3():
    with patch("ergon.connector.sqs.service.boto3") as m:
        yield m


# =====================================================================
#   __init__ / _create_client
# =====================================================================


class TestInit:
    def test_creates_boto3_client_with_region(self, mock_boto3):
        client = _make_client()
        SQSService(client)

        mock_boto3.client.assert_called_once()
        call_kwargs = mock_boto3.client.call_args
        assert call_kwargs[0][0] == "sqs"
        assert call_kwargs[1]["region_name"] == "us-east-1"

    def test_passes_credentials_when_provided(self, mock_boto3):
        client = _make_client(
            aws_access_key_id="AKID",
            aws_secret_access_key="SECRET",
            aws_session_token="TOKEN",
        )
        SQSService(client)

        call_kwargs = mock_boto3.client.call_args[1]
        assert call_kwargs["aws_access_key_id"] == "AKID"
        assert call_kwargs["aws_secret_access_key"] == "SECRET"
        assert call_kwargs["aws_session_token"] == "TOKEN"

    def test_passes_endpoint_url_when_provided(self, mock_boto3):
        client = _make_client(endpoint_url="http://localhost:4566")
        SQSService(client)

        call_kwargs = mock_boto3.client.call_args[1]
        assert call_kwargs["endpoint_url"] == "http://localhost:4566"

    def test_omits_optional_fields_when_none(self, mock_boto3):
        client = _make_client()
        SQSService(client)

        call_kwargs = mock_boto3.client.call_args[1]
        assert "aws_access_key_id" not in call_kwargs
        assert "endpoint_url" not in call_kwargs


# =====================================================================
#   receive
# =====================================================================


class TestReceive:
    def test_returns_empty_list_when_no_messages(self, mock_boto3):
        mock_boto3.client.return_value.receive_message.return_value = {}

        svc = SQSService(_make_client())
        result = svc.receive()

        assert result == []

    def test_returns_parsed_messages(self, mock_boto3):
        body_dict = {"key": "value"}
        mock_boto3.client.return_value.receive_message.return_value = {
            "Messages": [
                {
                    "MessageId": "msg-1",
                    "ReceiptHandle": "rh-1",
                    "Body": json.dumps(body_dict),
                    "Attributes": {"ApproximateReceiveCount": "1"},
                    "MessageAttributes": {},
                }
            ]
        }

        svc = SQSService(_make_client())
        result = svc.receive()

        assert len(result) == 1
        assert result[0]["data"] == body_dict
        assert result[0]["receipt_handle"] == "rh-1"
        assert result[0]["message_id"] == "msg-1"

    def test_returns_raw_body_when_not_json(self, mock_boto3):
        mock_boto3.client.return_value.receive_message.return_value = {
            "Messages": [
                {
                    "MessageId": "msg-2",
                    "ReceiptHandle": "rh-2",
                    "Body": "plain text",
                }
            ]
        }

        svc = SQSService(_make_client())
        result = svc.receive()

        assert result[0]["data"] == "plain text"

    def test_uses_custom_queue_url(self, mock_boto3):
        mock_boto3.client.return_value.receive_message.return_value = {}

        svc = SQSService(_make_client())
        svc.receive(queue_url="https://custom-queue")

        call_kwargs = mock_boto3.client.return_value.receive_message.call_args[1]
        assert call_kwargs["QueueUrl"] == "https://custom-queue"

    def test_respects_max_messages_cap_of_10(self, mock_boto3):
        mock_boto3.client.return_value.receive_message.return_value = {}

        svc = SQSService(_make_client(max_number_of_messages=10))
        svc.receive(max_messages=15)

        call_kwargs = mock_boto3.client.return_value.receive_message.call_args[1]
        assert call_kwargs["MaxNumberOfMessages"] == 10


# =====================================================================
#   send
# =====================================================================


class TestSend:
    def test_sends_message_with_body(self, mock_boto3):
        mock_boto3.client.return_value.send_message.return_value = {"MessageId": "new-1"}

        svc = SQSService(_make_client())
        resp = svc.send(body='{"hello": "world"}')

        call_kwargs = mock_boto3.client.return_value.send_message.call_args[1]
        assert call_kwargs["MessageBody"] == '{"hello": "world"}'
        assert resp["MessageId"] == "new-1"

    def test_sends_fifo_params(self, mock_boto3):
        mock_boto3.client.return_value.send_message.return_value = {"MessageId": "fifo-1"}

        svc = SQSService(_make_client())
        svc.send(
            body="msg",
            message_group_id="group-A",
            message_deduplication_id="dedup-1",
        )

        call_kwargs = mock_boto3.client.return_value.send_message.call_args[1]
        assert call_kwargs["MessageGroupId"] == "group-A"
        assert call_kwargs["MessageDeduplicationId"] == "dedup-1"

    def test_omits_fifo_params_when_none(self, mock_boto3):
        mock_boto3.client.return_value.send_message.return_value = {"MessageId": "std-1"}

        svc = SQSService(_make_client())
        svc.send(body="msg")

        call_kwargs = mock_boto3.client.return_value.send_message.call_args[1]
        assert "MessageGroupId" not in call_kwargs
        assert "MessageDeduplicationId" not in call_kwargs


# =====================================================================
#   delete
# =====================================================================


class TestDelete:
    def test_calls_delete_message(self, mock_boto3):
        svc = SQSService(_make_client())
        svc.delete(receipt_handle="rh-99")

        mock_boto3.client.return_value.delete_message.assert_called_once_with(
            QueueUrl=_make_client().queue_url,
            ReceiptHandle="rh-99",
        )

    def test_uses_custom_queue_url(self, mock_boto3):
        svc = SQSService(_make_client())
        svc.delete(receipt_handle="rh-1", queue_url="https://other-queue")

        call_kwargs = mock_boto3.client.return_value.delete_message.call_args[1]
        assert call_kwargs["QueueUrl"] == "https://other-queue"
