from typing import TYPE_CHECKING, Any

from .._lazy import exported_names, load_optional_export
from .models import (
    OutlookAckActionConfig,
    OutlookAttachmentInput,
    OutlookConsumerConfig,
    OutlookEmailAddress,
    OutlookFlagStatus,
    OutlookGraphClient,
    OutlookMessageFilter,
    OutlookMessageQuery,
    OutlookMessageSearch,
    OutlookNackActionConfig,
    OutlookProducerConfig,
    OutlookSendMessageInput,
    OutlookWellKnownFolder,
)

if TYPE_CHECKING:
    from .async_connector import AsyncOutlookGraphConnector
    from .async_service import AsyncOutlookGraphService
    from .connector import OutlookGraphConnector
    from .service import OutlookGraphService

_LAZY_EXPORTS = {
    "AsyncOutlookGraphConnector": "async_connector",
    "AsyncOutlookGraphService": "async_service",
    "OutlookGraphConnector": "connector",
    "OutlookGraphService": "service",
}


def __getattr__(name: str) -> Any:
    return load_optional_export(
        name=name,
        package=__name__,
        exports=_LAZY_EXPORTS,
        namespace=globals(),
        extra="outlook",
        dependencies=("msal", "requests"),
    )


def __dir__() -> list[str]:
    return exported_names(globals(), _LAZY_EXPORTS)


__all__ = [
    "AsyncOutlookGraphConnector",
    "AsyncOutlookGraphService",
    "OutlookAckActionConfig",
    "OutlookAttachmentInput",
    "OutlookConsumerConfig",
    "OutlookEmailAddress",
    "OutlookFlagStatus",
    "OutlookGraphClient",
    "OutlookGraphConnector",
    "OutlookGraphService",
    "OutlookMessageFilter",
    "OutlookMessageQuery",
    "OutlookMessageSearch",
    "OutlookNackActionConfig",
    "OutlookProducerConfig",
    "OutlookSendMessageInput",
    "OutlookWellKnownFolder",
]
