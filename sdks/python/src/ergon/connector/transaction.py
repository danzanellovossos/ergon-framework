from typing import Any, Dict

from pydantic import BaseModel, ConfigDict, Field


class Transaction(BaseModel):
    """
    Generic transaction model.
    """

    id: str
    payload: Any
    metadata: Dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(frozen=True)
