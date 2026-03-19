from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class SQSClient(BaseModel):
    region_name: str = Field(description="AWS region name (e.g. us-east-1)")
    aws_access_key_id: Optional[str] = Field(default=None, description="AWS access key ID. None = use env/IAM role")
    aws_secret_access_key: Optional[str] = Field(
        default=None, description="AWS secret access key. None = use env/IAM role"
    )
    aws_session_token: Optional[str] = Field(default=None, description="AWS session token for temporary credentials")
    endpoint_url: Optional[str] = Field(default=None, description="Custom endpoint URL (e.g. LocalStack)")
    queue_url: Optional[str] = Field(default=None, description="Default SQS queue URL, can be overridden per-call")


class SQSConsumerConfig(BaseModel):
    queue_url: Optional[str] = Field(default=None, description="Override default queue URL for consuming")
    wait_time_seconds: int = Field(default=20, description="Long polling wait time in seconds (0-20)")
    visibility_timeout: Optional[int] = Field(
        default=None, description="Override queue's default visibility timeout in seconds"
    )
    attribute_names: List[str] = Field(default=["All"], description="System attribute names to include in response")
    message_attribute_names: List[str] = Field(
        default=["All"], description="Message attribute names to include in response"
    )


class SQSProducerConfig(BaseModel):
    queue_url: Optional[str] = Field(default=None, description="Override default queue URL for producing")
    delay_seconds: int = Field(default=0, description="Delay in seconds before message becomes visible (0-900)")
    message_group_id: Optional[str] = Field(default=None, description="Message group ID for FIFO queues")
    message_deduplication_id: Optional[str] = Field(default=None, description="Deduplication ID for FIFO queues")
    message_attributes: Optional[Dict[str, Dict]] = Field(
        default=None, description="Custom message attributes to attach to sent messages"
    )
