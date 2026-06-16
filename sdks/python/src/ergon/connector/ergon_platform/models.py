from typing import Any, Dict, Optional, Union

from pydantic import BaseModel, Field

# Status buckets returned by the Ergon Platform attachment pipeline.
SUCCESS_STATUSES = {"completed", "complete", "succeeded", "success", "processed", "ready"}
FAILURE_STATUSES = {"failed", "error", "errored", "skipped", "oversized"}
PROCESSING_STATUSES = {"pending", "queued", "processing", "running", "stored"}


class ErgonPlatformClient(BaseModel):
    """Credentials and connection settings for the Ergon Platform SDK."""

    client_id: str = Field(description="Ergon Platform API key id (ek_...)")
    client_secret: str = Field(description="Ergon Platform API key secret (eks_...)")
    base_url: str = Field(default="http://localhost", description="Ergon Platform base URL")
    company_id: Optional[str] = Field(
        default=None,
        description="Company scope; inferred from the API key token when omitted",
    )
    timeout: float = Field(default=30.0, description="HTTP timeout in seconds")
    max_retries: int = Field(default=2, ge=0, description="Max automatic HTTP retries")


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
    list_params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Extra query params forwarded to the items listing endpoint",
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


class CreateItemInput(BaseModel):
    """Payload describing an item to create, with optional attachment upload."""

    title: str = Field(description="Item title")
    workflow_id: Optional[str] = Field(default=None, description="Workflow ID; falls back to producer config")
    phase_id: Optional[str] = Field(default=None, description="Target phase ID; falls back to producer config")
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
