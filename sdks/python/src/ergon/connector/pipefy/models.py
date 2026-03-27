from datetime import datetime
from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel, Field


class PipefyClient(BaseModel):
    client_id: str = Field(description="The client ID of the Pipefy API")
    client_secret: str = Field(description="The client secret of the Pipefy API")
    endpoint: str = Field(default="https://api.pipefy.com/graphql", description="The endpoint of the Pipefy API")
    oauth_token_url: str = Field(
        default="https://app.pipefy.com/oauth/token", description="The URL to get the OAuth token"
    )
    timeout_sec: int = Field(default=10, description="The timeout in seconds for the Pipefy API requests")


class CreateCardInput(BaseModel):
    pipe_id: str = Field(description="The ID of the pipe to create the card in")
    title: Optional[str] = Field(default=None, description="The title of the card")
    fields_attributes: Optional[List[dict[str, Any]]] = Field(
        default=None, description="Array of inputs to fill card's fields"
    )
    assignee_ids: Optional[List[str]] = Field(default=None, description="The assignee IDs")
    label_ids: Optional[List[str]] = Field(default=None, description="The label IDs")
    parent_ids: Optional[List[str]] = Field(default=None, description="The parent-card IDs")
    phase_id: Optional[str] = Field(default=None, description="The phase ID")
    due_date: Optional[datetime] = Field(default=None, description="The card due date")
    attachments: Optional[List[str]] = Field(default=None, description="The card attachments")


class FieldFilterOperator(Enum):
    EQUAL = "equal"
    NOT_EQUAL = "not_equal"
    GREATER_THAN = "greater_than"
    LESS_THAN = "less_than"
    GREATER_THAN_OR_EQUAL = "greater_than_or_equal"
    LESS_THAN_OR_EQUAL = "less_than_or_equal"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"


class FieldFilter(BaseModel):
    field: str
    operator: FieldFilterOperator
    value: Any
