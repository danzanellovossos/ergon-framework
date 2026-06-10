"""Tests for NylasService — query params and SDK delegation."""

from unittest.mock import MagicMock, patch

import pytest

from ergon.connector.nylas.models import MessageQueryFilter, NylasClient
from ergon.connector.nylas.service import NylasService


def _make_mock_nylas():
    mock = MagicMock()
    mock.messages = MagicMock()
    mock.threads = MagicMock()
    mock.drafts = MagicMock()
    mock.attachments = MagicMock()
    mock.folders = MagicMock()
    return mock


def _make_service() -> NylasService:
    client = NylasClient(api_key="key", grant_id="grant-1")
    mock_nylas = _make_mock_nylas()
    with patch("ergon.connector.nylas.service._get_nylas_client", return_value=mock_nylas):
        service = NylasService(client)
    return service


class TestListMessages:
    def test_builds_query_params(self):
        service = _make_service()
        query = MessageQueryFilter(subject="Fatura", has_attachment=True, unread=True)

        response = MagicMock()
        response.data = [{"id": "m1", "subject": "Fatura Junho"}]
        response.next_cursor = "next-1"
        service._nylas.messages.list.return_value = response

        result = service.list_messages(query, limit=10, use_internal_pagination=False)

        assert len(result["data"]) == 1
        assert result["next_cursor"] == "next-1"

        call_kwargs = service._nylas.messages.list.call_args
        assert call_kwargs.args[0] == "grant-1"
        query_params = call_kwargs.kwargs["query_params"]
        assert query_params["subject"] == "Fatura"
        assert query_params["has_attachment"] is True
        assert query_params["unread"] is True
        assert query_params["limit"] == 10

    def test_pagination_state(self):
        service = _make_service()
        query = MessageQueryFilter()

        first = MagicMock()
        first.data = [{"id": "m1"}]
        first.next_cursor = "page-2"

        second = MagicMock()
        second.data = []
        second.next_cursor = None

        service._nylas.messages.list.side_effect = [first, second]

        result1 = service.list_messages(query, limit=1)
        assert len(result1["data"]) == 1
        assert service._page_token == "page-2"

        result2 = service.list_messages(query, limit=1)
        assert result2["data"] == []
        assert service._exhausted is True


class TestSendMessage:
    def test_send_message_delegates_to_sdk(self):
        service = _make_service()
        response = MagicMock()
        response.data = {"id": "sent-1"}
        service._nylas.messages.send.return_value = response

        payload = {
            "to": [{"email": "a@b.com"}],
            "subject": "Hi",
            "body": "Body",
        }
        result = service.send_message(payload)

        assert result["id"] == "sent-1"
        service._nylas.messages.send.assert_called_once()


class TestGetMessagesCount:
    def test_counts_across_pages(self):
        service = _make_service()
        query = MessageQueryFilter(unread=True)

        with patch.object(service, "list_messages") as mock_list:
            mock_list.side_effect = [
                {"data": [{"id": "1"}, {"id": "2"}], "next_cursor": "p2"},
                {"data": [{"id": "3"}], "next_cursor": None},
            ]
            count = service.get_messages_count(query)

        assert count == 3
