from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

ConsumerMode = Literal["auto", "list_claim", "pull"]

# Status buckets returned by the Ergon Platform attachment pipeline.
SUCCESS_STATUSES = {"completed", "complete", "succeeded", "success", "processed", "ready"}
FAILURE_STATUSES = {"failed", "error", "errored", "skipped", "oversized"}
PROCESSING_STATUSES = {
    "created",
    "in-progress",
    "in_progress",
    "pending",
    "processing",
    "queued",
    "running",
    "stored",
    "uploaded",
}


class ErgonPlatformConsumerConfig(BaseModel):
    """Parameters used to list workflow items as transactions."""

    workflow_id: str = Field(description="Workflow ID to consume items from")
    phase_id: str = Field(description="Phase ID to list items from")
    batch_size: int = Field(default=50, ge=1, description="Max items per fetch")
    offset: int = Field(default=0, ge=0, description="Pagination offset")
    ack_phase_id: Optional[str] = Field(
        default=None,
        description="Phase to route an item to on ack; ack is a no-op when omitted",
    )
    nack_phase_id: Optional[str] = Field(
        default=None,
        description="Phase to route an item to on nack when requeue=False",
    )
    unassigned: bool = Field(
        default=False,
        description="When true, fetch only unassigned cards and claim them during fetch",
    )
    list_params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Extra query params forwarded to the items listing endpoint",
    )
    mode: ConsumerMode = Field(
        default="auto",
        description=(
            "How items are taken from the phase. 'list_claim' lists and claims one by one "
            "(standard phases). 'pull' uses POST /phases/{id}/items/pull with a consumer id "
            "(Pull only phases): atomic claim with expiry, fail/defer/complete outcomes, "
            "attempt limit handled by the platform. 'auto' (default) reads the phase kind "
            "once, on the first fetch, and picks the matching behaviour."
        ),
    )
    pull_filters: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description=(
            "Pull mode only. Field filters a pulled item must match, same shape as the "
            "'filters' list of POST /items/query (e.g. [{'field_id': ..., 'op': 'eq', 'value': ...}])."
        ),
    )
    pull_wait_seconds: Optional[int] = Field(
        default=None,
        ge=0,
        le=30,
        description="Pull mode only. Long-poll up to this many seconds when the phase has no visible item.",
    )
    consumer_id: Optional[str] = Field(
        default=None,
        description=(
            "Pull mode only. Worker identity sent as X-Ergon-Consumer-Id; claims taken under it "
            "expire and are fenced against other consumers. Defaults to '<hostname>:<pid>'. "
            "Allowed: [A-Za-z0-9._:@-]{1,128}."
        ),
    )


class ErgonPlatformProducerConfig(BaseModel):
    """Defaults applied when creating items via dispatch."""

    workflow_id: Optional[str] = Field(default=None, description="Default workflow for created items")
    phase_id: Optional[str] = Field(default=None, description="Default phase for created items")
    attachment_field_id: Optional[str] = Field(
        default=None,
        description="Default field ID used when an attachment is provided",
    )
    default_content_type: Optional[str] = Field(
        default=None,
        description="Fallback MIME type when it cannot be guessed from the filename",
    )
    parent_item_id: Optional[str] = Field(
        default=None,
        description="Default parent item ID used to create child items via dispatch",
    )


class CreateItemInput(BaseModel):
    """Payload describing an item to create, with optional attachment upload."""

    title: str = Field(description="Item title")
    workflow_id: Optional[str] = Field(default=None, description="Workflow ID; falls back to producer config")
    phase_id: Optional[str] = Field(default=None, description="Target phase ID; falls back to producer config")
    parent_item_id: Optional[str] = Field(
        default=None,
        description="Optional parent item ID to create this item as a child",
    )
    field_values: Optional[Dict[str, Any]] = Field(default=None, description="Field values keyed by field ID")
    attachment: Optional[str] = Field(default=None, description="Local file path to upload after creation")
    attachment_field_id: Optional[str] = Field(
        default=None,
        description="Field ID that receives the attachment; falls back to producer config",
    )
    content_type: Optional[str] = Field(default=None, description="Override MIME type for the attachment")
    extra_fields: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional keyword fields forwarded to create_item",
    )

    model_config = {"populate_by_name": True}


CreateItemPayload = Union[CreateItemInput, Dict[str, Any]]
