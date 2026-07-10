import time
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class TaskLivenessSnapshot(BaseModel):
    """Transport-agnostic task progress evaluated by a task/mixin."""

    healthy: bool
    state: str
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    observed_at: float = Field(default_factory=time.time)


@runtime_checkable
class LivenessProvider(Protocol):
    def liveness_snapshot(self) -> TaskLivenessSnapshot:
        """Return a non-blocking, thread-safe snapshot of task progress."""
        ...


class TaskSupervisionPolicy(BaseModel):
    """Process-level reaction policy for tasks exposing liveness."""

    enabled: bool = True
    check_interval: float = Field(default=10.0, gt=0)
    startup_grace: float = Field(default=60.0, ge=0)
    unhealthy_grace: float = Field(default=30.0, ge=0)
    shutdown_grace: float = Field(default=15.0, ge=0)
    fetch_stale_after: float | None = Field(
        default=None,
        gt=0,
        description="Optional override for maximum fetch duration before it is unhealthy",
    )
    processing_stale_after: float | None = Field(
        default=None,
        gt=0,
        description="Optional override for maximum time without in-flight transaction progress",
    )
