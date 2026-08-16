"""Tests for Ergon Platform workflows connector utilities."""

import pytest

from ergon.connector.ergon_platform.workflows.models import CreateItemInput
from ergon.connector.ergon_platform.workflows.utils import (
    classify_status,
    extract_buckets_file_id,
    extract_items,
    extract_status_entries,
    extract_status_file_id,
    extract_total,
    find_status_entry,
    get_value,
    item_to_transaction,
    normalize_create_payload,
    requeue,
)
from ergon.connector.transaction import Transaction


class TestGetValue:
    def test_reads_from_dict(self):
        assert get_value({"a": 1}, "a") == 1
        assert get_value({"a": 1}, "b", default=2) == 2

    def test_reads_attribute(self):
        class Obj:
            x = 5

        assert get_value(Obj(), "x") == 5
        assert get_value(Obj(), "missing", default="d") == "d"


class TestClassifyStatus:
    def test_success(self):
        assert classify_status("completed") == "success"
        assert classify_status("READY") == "success"

    def test_failed(self):
        assert classify_status("failed") == "failed"
        assert classify_status("oversized") == "failed"

    def test_processing(self):
        assert classify_status("queued") == "processing"
        assert classify_status("in_progress") == "processing"

    def test_unknown(self):
        assert classify_status("weird") == "unknown"
        assert classify_status("") == "unknown"


class TestFindStatusEntry:
    def test_finds_matching_entry(self):
        statuses = [
            {"buckets_file_id": "a", "status": "queued"},
            {"buckets_file_id": "b", "status": "completed"},
        ]
        entry = find_status_entry(statuses, "b")
        assert entry["status"] == "completed"

    def test_returns_empty_when_missing(self):
        assert find_status_entry([], "x") == {}

    def test_finds_matching_entry_from_dict_response(self):
        statuses = {"items": [{"file_id": "a", "status": "processing"}]}

        entry = find_status_entry(statuses, "a")

        assert entry["status"] == "processing"


class TestExtractStatusEntries:
    def test_from_dict_list_fields(self):
        assert extract_status_entries({"statuses": [{"status": "queued"}]}) == [{"status": "queued"}]

    def test_from_single_status_dict(self):
        assert extract_status_entries({"status": "queued", "file_id": "file-1"}) == [
            {"status": "queued", "file_id": "file-1"}
        ]

    def test_extracts_status_file_id_alias(self):
        assert extract_status_file_id({"fileId": "file-1"}) == "file-1"


class TestExtractBucketsFileId:
    def test_returns_last_buckets_file_id(self):
        item = {
            "field_values": [
                {"field_id": "other", "value": [{"buckets_file_id": "z"}]},
                {
                    "field_id": "f1",
                    "value": [
                        {"buckets_file_id": "first"},
                        {"buckets_file_id": "second"},
                    ],
                },
            ]
        }
        assert extract_buckets_file_id(item, "f1") == "second"

    def test_returns_none_when_absent(self):
        item = {"field_values": [{"field_id": "f1", "value": []}]}
        assert extract_buckets_file_id(item, "f1") is None

    def test_returns_buckets_file_id_from_dict_field_values(self):
        item = {
            "field_values": {
                "f1": {
                    "files": [
                        {"buckets_file_id": "first"},
                        {"buckets_file_id": "second"},
                    ]
                }
            }
        }

        assert extract_buckets_file_id(item, "f1") == "second"


class TestItemToTransaction:
    def test_builds_transaction(self):
        item = {"id": "item-1", "title": "Hello", "phase_id": "p1", "company_id": "c1"}
        tx = item_to_transaction(item, "wf-1")
        assert tx.id == "item-1"
        assert tx.payload == item
        assert tx.metadata["workflow_id"] == "wf-1"
        assert tx.metadata["phase_id"] == "p1"
        assert tx.metadata["title"] == "Hello"


class TestNormalizeCreatePayload:
    def test_from_model(self):
        payload = CreateItemInput(
            title="T",
            workflow_id="wf",
            phase_id="ph",
            parent_item_id="parent-1",
            field_values={"a": 1},
            extra_fields={"priority": "high"},
        )
        data = normalize_create_payload(payload)
        assert data["title"] == "T"
        assert data["workflow_id"] == "wf"
        assert data["phase_id"] == "ph"
        assert data["parent_item_id"] == "parent-1"
        assert data["field_values"] == {"a": 1}
        assert data["fields"] == {"priority": "high"}

    def test_from_dict(self):
        data = normalize_create_payload({"title": "T", "phase_id": "ph"})
        assert data["title"] == "T"
        assert data["phase_id"] == "ph"
        assert data["fields"] == {}


class TestRequeue:
    def test_from_create_item_input_payload(self):
        payload = CreateItemInput(
            title="T",
            workflow_id="wf",
            phase_id="ph",
            parent_item_id="parent-1",
            field_values={"a": 1},
            attachment="/tmp/x.pdf",
            attachment_field_id="f1",
            content_type="application/pdf",
            extra_fields={"priority": "high"},
        )
        tx = Transaction(id="tx-1", payload=payload)

        result = requeue(tx)

        assert result.model_dump() == payload.model_dump()

    def test_from_dict_payload(self):
        tx = Transaction(
            id="tx-2",
            payload={
                "title": "Requeue",
                "workflow_id": "wf",
                "phase_id": "ph",
                "parent_item_id": "parent-2",
                "field_values": {"x": "y"},
                "attachment": "/tmp/a.txt",
                "attachment_field_id": "f2",
                "content_type": "text/plain",
                "fields": {"priority": "low"},
            },
        )

        result = requeue(tx)

        assert result.title == "Requeue"
        assert result.workflow_id == "wf"
        assert result.phase_id == "ph"
        assert result.parent_item_id == "parent-2"
        assert result.field_values == {"x": "y"}
        assert result.attachment == "/tmp/a.txt"
        assert result.attachment_field_id == "f2"
        assert result.content_type == "text/plain"
        assert result.extra_fields == {"priority": "low"}

    def test_raises_value_error_when_missing_title(self):
        tx = Transaction(id="tx-3", payload={"phase_id": "ph"})

        with pytest.raises(ValueError, match="title is required"):
            requeue(tx)

    def test_raises_type_error_for_unsupported_payload(self):
        tx = Transaction(id="tx-4", payload="invalid")

        with pytest.raises(TypeError, match="Unsupported create payload type"):
            requeue(tx)


class TestExtractItems:
    def test_from_page_like(self):
        class Page:
            items = [1, 2, 3]

        assert extract_items(Page()) == [1, 2, 3]

    def test_from_dict(self):
        assert extract_items({"items": [1]}) == [1]
        assert extract_items({"data": [2]}) == [2]

    def test_from_list(self):
        assert extract_items([1, 2]) == [1, 2]

    def test_empty(self):
        assert extract_items(None) == []


class TestExtractTotal:
    def test_from_dict(self):
        assert extract_total({"total": 10, "items": []}) == 10
        assert extract_total({"total_count": 5}) == 5
        assert extract_total({"count": 3}) == 3

    def test_from_page_like(self):
        class Page:
            total = 42
            items = []

        assert extract_total(Page()) == 42

    def test_returns_zero_when_absent(self):
        assert extract_total({"items": [1]}) == 0
        assert extract_total(None) == 0
