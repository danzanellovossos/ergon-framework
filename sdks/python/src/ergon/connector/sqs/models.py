"""SQS connector models for ergon-framework."""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class SQSClient(BaseModel):
    """Configuration for connecting to an AWS SQS queue."""

    region_name: str = Field(description="AWS region (e.g. us-east-1)")
    queue_url: str = Field(description="Full URL of the SQS queue")
    aws_access_key_id: Optional[str] = Field(
        default=None,
        description="AWS access key ID. When None, uses the default credentials chain.",
    )
    aws_secret_access_key: Optional[str] = Field(
        default=None,
        description="AWS secret access key. When None, uses the default credentials chain.",
    )
    aws_session_token: Optional[str] = Field(
        default=None,
        description="AWS session token for temporary credentials.",
    )
    endpoint_url: Optional[str] = Field(
        default=None,
        description="Custom endpoint URL (e.g. LocalStack for local testing).",
    )
    max_number_of_messages: int = Field(
        default=10,
        ge=1,
        le=10,
        description="Max messages per receive call (SQS hard limit is 10).",
    )
    wait_time_seconds: int = Field(
        default=5,
        ge=0,
        le=20,
        description="Long-polling wait time in seconds (0 = short polling).",
    )
    visibility_timeout: int = Field(
        default=30,
        ge=0,
        description="Time in seconds a received message stays invisible to other consumers.",
    )


class SQSProducerMessage(BaseModel):
    """Model representing a message to be sent to an SQS queue."""

    body: str = Field(description="Message body (string; JSON-encode dicts before passing)")
    message_attributes: Dict[str, Any] = Field(
        default_factory=dict,
        description="Custom message attributes in SQS MessageAttribute format.",
    )
    message_group_id: Optional[str] = Field(
        default=None,
        description="Message group ID (required for FIFO queues).",
    )
    message_deduplication_id: Optional[str] = Field(
        default=None,
        description="Deduplication ID (required for FIFO queues without content-based dedup).",
    )
