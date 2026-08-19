import base64

import pytest
from pydantic import ValidationError

from ergon.connector.outlook.models import (
    OutlookAckActionConfig,
    OutlookAttachmentInput,
    OutlookConsumerConfig,
    OutlookEmailAddress,
    OutlookMessageFilter,
    OutlookMessageQuery,
    OutlookMessageSearch,
    OutlookNackActionConfig,
    OutlookSendMessageInput,
    OutlookWellKnownFolder,
)
from ergon.connector.outlook.utils import build_ack_patch, build_nack_patch, merge_query, message_to_transaction


def test_message_query_builds_search_params() -> None:
    query = OutlookMessageQuery(
        search="invoice",
        select=["id", "subject"],
    )

    assert query.to_query_params(top=25) == {
        "$top": 25,
        "$search": '"invoice"',
        "$select": "id,subject",
    }
    assert query.requires_eventual_consistency is True


def test_message_query_rejects_search_combined_with_filter() -> None:
    with pytest.raises(ValidationError, match="Cannot combine search and filter"):
        OutlookMessageQuery(search="invoice", filter="isRead eq false")


def test_empty_search_does_not_conflict_with_filter() -> None:
    query = OutlookMessageQuery(
        search=OutlookMessageSearch(),
        filter=OutlookMessageFilter(unread_only=True),
        select=["id"],
    )

    params = query.to_query_params()
    assert "$search" not in params
    assert "isRead eq false" in params["$filter"]


def test_message_query_orders_non_search_folder_results() -> None:
    query = OutlookMessageQuery(select=["id"])

    assert query.to_query_params(top=10)["$orderby"] == "receivedDateTime asc"


def test_message_query_includes_body_by_default() -> None:
    assert "body" in OutlookMessageQuery.DEFAULT_MESSAGE_SELECT
    assert "body" in OutlookMessageQuery().to_query_params()["$select"]


def test_message_query_accepts_well_known_folder() -> None:
    query = OutlookMessageQuery(folder_id=OutlookWellKnownFolder.INBOX)

    assert query.folder_id == "inbox"


def test_ack_config_rejects_move_and_delete_together() -> None:
    with pytest.raises(ValidationError, match="cannot move and delete"):
        OutlookAckActionConfig(move_to_folder_id="processed", delete=True)


def test_ack_config_delete_disables_mark_as_read() -> None:
    config = OutlookAckActionConfig(mark_as_read=True, delete=True)

    assert config.delete is True
    assert config.mark_as_read is False


def test_ack_and_nack_configs_build_message_patches() -> None:
    assert build_ack_patch(OutlookAckActionConfig()) == {"isRead": True}
    assert build_nack_patch(
        OutlookNackActionConfig(categories=["Processing failed", " Processing failed ", "", "Retry"])
    ) == {
        "isRead": False,
        "categories": ["Processing failed", "Retry"],
    }


def test_message_query_builds_safe_typed_filters() -> None:
    query = OutlookMessageQuery(
        filter=OutlookMessageFilter(
            unread_only=True,
            has_attachments=True,
            sender="billing@example.com",
            subject_starts_with="Invoice",
        ),
        select=["id"],
    )

    params = query.to_query_params(top=10)

    assert params["$filter"] == (
        "receivedDateTime ge 1900-01-01T00:00:00Z and isRead eq false and "
        "hasAttachments eq true and from/emailAddress/address eq 'billing@example.com' "
        "and startswith(subject,'Invoice')"
    )
    assert params["$orderby"] == "receivedDateTime asc"


def test_message_filter_escapes_odata_strings() -> None:
    query = OutlookMessageQuery(
        filter=OutlookMessageFilter(subject_starts_with="O'Reilly"),
        select=["id"],
    )

    assert "startswith(subject,'O''Reilly')" in query.to_query_params()["$filter"]


def test_message_query_builds_typed_search() -> None:
    query = OutlookMessageQuery(
        search=OutlookMessageSearch(
            subject="Monthly invoice",
            sender="billing@example.com",
        ),
        select=["id"],
    )

    assert query.to_query_params()["$search"] == ('"subject:\\"Monthly invoice\\" AND from:billing@example.com"')


def test_query_override_replaces_filter_when_search_is_set() -> None:
    base = OutlookConsumerConfig(
        folder_id="custom-folder",
        filter=OutlookMessageFilter(unread_only=True),
        batch_size=50,
    )

    merged = merge_query(base, OutlookMessageQuery(search="invoice"))

    assert merged.folder_id == "custom-folder"
    assert merged.filter is None
    assert merged.search == "invoice"


def test_query_override_preserves_unspecified_consumer_fields() -> None:
    base = OutlookConsumerConfig(
        folder_id="custom-folder",
        filter=OutlookMessageFilter(unread_only=True),
        batch_size=50,
    )

    merged = merge_query(base, OutlookMessageQuery(folder_id="other-folder"))

    assert merged.folder_id == "other-folder"
    assert merged.filter == OutlookMessageFilter(unread_only=True)


def test_transaction_metadata_exposes_usable_mail_fields() -> None:
    transaction = message_to_transaction(
        {
            "id": "m1",
            "subject": "Invoice",
            "receivedDateTime": "2026-08-19T12:00:00Z",
            "hasAttachments": True,
            "from": {"emailAddress": {"address": "billing@example.com"}},
            "toRecipients": [{"emailAddress": {"address": "mailbox@example.com"}}],
            "bodyPreview": "Please see attached",
            "body": {"contentType": "HTML", "content": "<p>Please see attached</p>"},
            "conversationId": "c1",
            "internetMessageId": "<id@example.com>",
            "parentFolderId": "inbox",
            "isRead": False,
        },
        "mailbox@example.com",
        attachments=[{"name": "invoice.pdf", "content": b"pdf"}],
    )

    assert transaction.metadata["subject"] == "Invoice"
    assert transaction.metadata["from_email"] == "billing@example.com"
    assert transaction.metadata["to_emails"] == ["mailbox@example.com"]
    assert transaction.metadata["body"] == "<p>Please see attached</p>"
    assert transaction.metadata["body_type"] == "HTML"
    assert transaction.metadata["has_attachment"] is True


def test_send_message_builds_recipients_and_attachment() -> None:
    payload = OutlookSendMessageInput(
        to=[OutlookEmailAddress(email="to@example.com", name="Recipient")],
        cc=[OutlookEmailAddress(email="copy@example.com")],
        subject="Invoice",
        body="<p>Attached</p>",
        attachments=[
            OutlookAttachmentInput(
                filename="invoice.xml",
                content_type="application/xml",
                content=b"<xml />",
            )
        ],
    )

    message = payload.to_graph_message()

    assert message["toRecipients"] == [{"emailAddress": {"address": "to@example.com", "name": "Recipient"}}]
    assert message["ccRecipients"] == [{"emailAddress": {"address": "copy@example.com"}}]
    assert message["attachments"][0]["contentBytes"] == base64.b64encode(b"<xml />").decode("ascii")


def test_attachment_reads_file_path(tmp_path) -> None:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"pdf")

    attachment = OutlookAttachmentInput(file_path=path).to_graph()

    assert attachment["name"] == "report.pdf"
    assert attachment["contentType"] == "application/pdf"
    assert base64.b64decode(attachment["contentBytes"]) == b"pdf"
