from typing import TYPE_CHECKING, Any

from ..._lazy import exported_names, load_optional_export
from .models import (
    FAILURE_STATUSES,
    PROCESSING_STATUSES,
    SUCCESS_STATUSES,
    CreateItemInput,
    CreateItemPayload,
    ErgonPlatformConsumerConfig,
    ErgonPlatformProducerConfig,
)

if TYPE_CHECKING:
    from .async_connector import AsyncErgonPlatformConnector
    from .connector import ErgonPlatformConnector

_LAZY_EXPORTS = {
    "AsyncErgonPlatformConnector": "async_connector",
    "ErgonPlatformConnector": "connector",
}


def __getattr__(name: str) -> Any:
    return load_optional_export(
        name=name,
        package=__name__,
        exports=_LAZY_EXPORTS,
        namespace=globals(),
        extra="ergon-platform",
        dependencies=("ergon_platform", "httpx"),
    )


def __dir__() -> list[str]:
    return exported_names(globals(), _LAZY_EXPORTS)


__all__ = [
    "AsyncErgonPlatformConnector",
    "CreateItemInput",
    "CreateItemPayload",
    "ErgonPlatformConnector",
    "ErgonPlatformConsumerConfig",
    "ErgonPlatformProducerConfig",
    "FAILURE_STATUSES",
    "PROCESSING_STATUSES",
    "SUCCESS_STATUSES",
]
