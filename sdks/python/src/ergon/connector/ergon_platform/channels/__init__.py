from typing import TYPE_CHECKING, Any

from ..._lazy import exported_names, load_optional_export
from .models import (
    ChannelsActivityFilter,
    ErgonPlatformChannelsConfig,
    ErgonPlatformChannelsConsumerConfig,
    ErgonPlatformChannelsProducerConfig,
    InboxAttachmentFile,
    SendMessageAttachment,
    SendMessageInput,
    SendMessagePayload,
)

if TYPE_CHECKING:
    from .async_connector import AsyncErgonPlatformChannelsConnector
    from .connector import ErgonPlatformChannelsConnector

_LAZY_EXPORTS = {
    "AsyncErgonPlatformChannelsConnector": "async_connector",
    "ErgonPlatformChannelsConnector": "connector",
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
    "AsyncErgonPlatformChannelsConnector",
    "ChannelsActivityFilter",
    "ErgonPlatformChannelsConnector",
    "ErgonPlatformChannelsConfig",
    "ErgonPlatformChannelsConsumerConfig",
    "ErgonPlatformChannelsProducerConfig",
    "InboxAttachmentFile",
    "SendMessageAttachment",
    "SendMessageInput",
    "SendMessagePayload",
]
