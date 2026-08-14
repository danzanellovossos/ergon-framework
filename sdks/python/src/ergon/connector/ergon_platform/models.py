from typing import Any, Dict, Optional, Union

from pydantic import BaseModel, Field

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


class ErgonPlatformClient(BaseModel):
    """Credentials and connection settings for the Ergon Platform SDK."""

    client_id: str = Field(description="Ergon Platform API key id (ek_...)")
    client_secret: str = Field(description="Ergon Platform API key secret (eks_...)")
    base_url: str = Field(
        default="https://platform.ergondata.ai",
        description="Ergon Platform base URL (gateway origin; auth resolves at /v1/auth/token)",
    )
    company_id: Optional[str] = Field(
        default=None,
        description="Company scope; inferred from the API key token when omitted",
    )
    timeout: float = Field(default=30.0, description="HTTP timeout in seconds")
    max_retries: int = Field(default=2, ge=0, description="Max automatic HTTP retries")
