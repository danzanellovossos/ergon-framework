import base64
from unittest.mock import MagicMock, patch

import pytest
import requests

from ergon.connector.outlook.models import (
    OutlookEmailAddress,
    OutlookFlagStatus,
    OutlookGraphClient,
    OutlookMessageQuery,
    OutlookSendMessageInput,
    OutlookWellKnownFolder,
)
from ergon.connector.outlook.service import OutlookGraphService


def _response(status: int, body: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.content = b"" if body is None else b"json"
    response.json.return_value = body or {}
    if status >= 400:
        response.raise_for_status.side_effect = RuntimeError(f"HTTP {status}")
    return response


def _service(session: MagicMock | None = None) -> tuple[OutlookGraphService, MagicMock]:
    session = session or MagicMock()
    client = OutlookGraphClient(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
        user_email="mailbox@example.com",
    )
    return OutlookGraphService(client, session=session), session


def test_list_messages_sends_graph_query_and_tracks_next_link() -> None:
    session = MagicMock()
    service = OutlookGraphService(
        OutlookGraphClient(
            tenant_id="tenant",
            client_id="client",
            client_secret="secret",
            user_email="mailbox@example.com",
        ),
        session=session,
    )
    service._access_token = "token"
    next_link = "https://graph.microsoft.com/v1.0/users/mailbox/messages?$skiptoken=next"
    session.request.return_value = _response(
        200,
        {"value": [{"id": "m1"}], "@odata.nextLink": next_link},
    )

    result = service.list_messages(OutlookMessageQuery(search="invoice"), limit=10)

    assert result == {"data": [{"id": "m1"}], "next_link": next_link}
    call = session.request.call_args
    assert call.args[:2] == (
        "GET",
        "https://graph.microsoft.com/v1.0/users/mailbox%40example.com/mailFolders/Inbox/messages",
    )
    assert call.kwargs["params"]["$search"] == '"invoice"'
    assert call.kwargs["headers"]["ConsistencyLevel"] == "eventual"


def test_request_reauthenticates_once_after_401() -> None:
    session = MagicMock()
    service = OutlookGraphService(
        OutlookGraphClient(
            tenant_id="tenant",
            client_id="client",
            client_secret="secret",
            user_email="mailbox@example.com",
        ),
        session=session,
    )
    service._access_token = "expired"
    session.request.side_effect = [_response(401), _response(200, {"value": []})]

    refreshed_app = MagicMock()
    refreshed_app.acquire_token_for_client.return_value = {"access_token": "fresh"}
    with patch(
        "ergon.connector.outlook.service._get_msal_app_class",
        return_value=MagicMock(return_value=refreshed_app),
    ):
        service.list_mail_folders()

    assert session.request.call_count == 2
    assert session.request.call_args.kwargs["headers"]["Authorization"] == "Bearer fresh"


def test_forbidden_send_explains_mail_send() -> None:
    service, session = _service()
    service._access_token = "token"
    session.request.return_value = _response(
        403,
        {"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}},
    )

    with pytest.raises(requests.HTTPError, match="Mail.Send"):
        service.send_message(
            OutlookSendMessageInput(
                to=[OutlookEmailAddress(email="to@example.com")],
                subject="Hello",
                body="World",
            )
        )


def test_forbidden_mutation_explains_mail_read_write() -> None:
    service, session = _service()
    service._access_token = "token"
    session.request.return_value = _response(
        403,
        {"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}},
    )

    with pytest.raises(requests.HTTPError, match="Mail.ReadWrite"):
        service.mark_as_read("m1")


def test_list_attachments_decodes_only_file_attachments() -> None:
    session = MagicMock()
    service = OutlookGraphService(
        OutlookGraphClient(
            tenant_id="tenant",
            client_id="client",
            client_secret="secret",
            user_email="mailbox@example.com",
        ),
        session=session,
    )
    service._access_token = "token"
    session.request.return_value = _response(
        200,
        {
            "value": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "id": "a1",
                    "name": "invoice.xml",
                    "contentType": "application/xml",
                    "size": 5,
                    "contentBytes": base64.b64encode(b"<x />").decode("ascii"),
                },
                {"@odata.type": "#microsoft.graph.itemAttachment", "id": "ignored"},
            ]
        },
    )

    attachments = service.list_attachments("m1", download_content=True)

    assert attachments == [
        {
            "id": "a1",
            "name": "invoice.xml",
            "content_type": "application/xml",
            "size": 5,
            "is_inline": False,
            "content": b"<x />",
        }
    ]


def test_send_message_uses_send_mail_contract() -> None:
    session = MagicMock()
    service = OutlookGraphService(
        OutlookGraphClient(
            tenant_id="tenant",
            client_id="client",
            client_secret="secret",
            user_email="mailbox@example.com",
        ),
        session=session,
    )
    service._access_token = "token"
    session.request.return_value = _response(202)

    service.send_message(
        OutlookSendMessageInput(
            to=[OutlookEmailAddress(email="recipient@example.com")],
            subject="Hello",
            body="World",
        )
    )

    call = session.request.call_args
    assert call.args[0] == "POST"
    assert call.args[1].endswith("/users/mailbox%40example.com/sendMail")
    assert call.kwargs["json"]["message"]["subject"] == "Hello"
    assert call.kwargs["json"]["saveToSentItems"] is True


def test_reply_reply_all_and_forward_use_graph_actions() -> None:
    service, session = _service()
    service._access_token = "token"
    session.request.return_value = _response(202)
    recipient = OutlookEmailAddress(email="recipient@example.com")

    service.reply("message/1", comment="Reply")
    service.reply_all("message/1", comment="Reply all")
    service.forward("message/1", [recipient], comment="Forward")

    reply_call, reply_all_call, forward_call = session.request.call_args_list
    assert reply_call.args[1].endswith("/messages/message%2F1/reply")
    assert reply_call.kwargs["json"] == {"comment": "Reply"}
    assert reply_all_call.args[1].endswith("/messages/message%2F1/replyAll")
    assert forward_call.args[1].endswith("/messages/message%2F1/forward")
    assert forward_call.kwargs["json"]["toRecipients"] == [{"emailAddress": {"address": "recipient@example.com"}}]


def test_message_state_helpers_build_patch_payloads() -> None:
    service, _ = _service()

    with patch.object(service, "update_message", return_value={"id": "m1"}) as update:
        service.mark_as_read("m1")
        service.mark_as_unread("m1")
        service.set_flag("m1", OutlookFlagStatus.COMPLETE)
        service.set_categories("m1", ["Finance", " Finance ", "", "Urgent"])

    assert [call.args for call in update.call_args_list] == [
        ("m1", {"isRead": True}),
        ("m1", {"isRead": False}),
        ("m1", {"flag": {"flagStatus": "complete"}}),
        ("m1", {"categories": ["Finance", "Urgent"]}),
    ]


def test_move_and_delete_message_use_graph_contracts() -> None:
    service, session = _service()
    service._access_token = "token"
    session.request.side_effect = [
        _response(201, {"id": "moved-message"}),
        _response(204),
    ]

    moved = service.move_message("message/1", OutlookWellKnownFolder.DELETED_ITEMS)
    service.delete_message("message/2")

    move_call, delete_call = session.request.call_args_list
    assert moved == {"id": "moved-message"}
    assert move_call.args[:2] == (
        "POST",
        "https://graph.microsoft.com/v1.0/users/mailbox%40example.com/messages/message%2F1/move",
    )
    assert move_call.kwargs["json"] == {"destinationId": "deleteditems"}
    assert delete_call.args[0] == "DELETE"
    assert delete_call.args[1].endswith("/messages/message%2F2")


def test_resolves_known_folder() -> None:
    service, session = _service()
    service._access_token = "token"
    session.request.return_value = _response(200, {"id": "inbox-id", "displayName": "Inbox"})

    folder = service.get_mail_folder(OutlookWellKnownFolder.INBOX)

    assert folder["id"] == "inbox-id"
    assert session.request.call_args.args[1].endswith("/mailFolders/inbox")


def test_rejects_pagination_url_outside_graph_origin() -> None:
    service, _ = _service()

    with pytest.raises(ValueError, match="outside graph_base_url"):
        service._url("https://example.com/steal-token")
