"""Ergon Platform connectors — Workflows and Channels.

The Ergon Platform SDK exposes several services (workflows, channels, agents,
buckets, ...). This package groups every framework connector against that
platform. Each service lives in its own sub-package (``workflows``,
``channels``, ...) and reuses the shared credentials model
(:class:`ErgonPlatformClient`) so a single API key drives every connector.

Public workflow names are re-exported here for backward compatibility with the
pre-split layout::

    from ergon.connector.ergon_platform import ErgonPlatformConnector  # workflows

New code is encouraged to import directly from the sub-package::

    from ergon.connector.ergon_platform.workflows import ErgonPlatformConnector
    from ergon.connector.ergon_platform.channels import ErgonPlatformChannelsConnector
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .._lazy import exported_names, load_optional_export
from .models import ErgonPlatformClient

if TYPE_CHECKING:
    from .channels import (
        AsyncErgonPlatformChannelsConnector,
        ErgonPlatformChannelsConfig,
        ErgonPlatformChannelsConnector,
        ErgonPlatformChannelsConsumerConfig,
        ErgonPlatformChannelsProducerConfig,
        SendMessageInput,
        SendMessagePayload,
    )
    from .workflows import (
        AsyncErgonPlatformConnector,
        CreateItemInput,
        CreateItemPayload,
        ErgonPlatformConnector,
        ErgonPlatformConsumerConfig,
        ErgonPlatformProducerConfig,
    )

_SUBPACKAGES = ("channels", "workflows")

# Backward-compat aliases: names that used to live under
# ``ergon.connector.ergon_platform`` before the workflows/ split, plus the new
# channels API surface. Every entry is loaded lazily from its sub-package to
# keep the platform SDK out of the core import path.
_LAZY_EXPORTS = {
    # Workflows (pre-split names — kept for backward compatibility)
    "AsyncErgonPlatformConnector": "workflows",
    "CreateItemInput": "workflows",
    "CreateItemPayload": "workflows",
    "ErgonPlatformConnector": "workflows",
    "ErgonPlatformConsumerConfig": "workflows",
    "ErgonPlatformProducerConfig": "workflows",
    # Channels
    "AsyncErgonPlatformChannelsConnector": "channels",
    "ErgonPlatformChannelsConfig": "channels",
    "ErgonPlatformChannelsConnector": "channels",
    "ErgonPlatformChannelsConsumerConfig": "channels",
    "ErgonPlatformChannelsProducerConfig": "channels",
    "SendMessageInput": "channels",
    "SendMessagePayload": "channels",
}


def __getattr__(name: str) -> Any:
    if name in _SUBPACKAGES:
        module = import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    return load_optional_export(
        name=name,
        package=__name__,
        exports=_LAZY_EXPORTS,
        namespace=globals(),
        extra="ergon-platform",
        dependencies=("ergon_platform", "httpx"),
    )


def __dir__() -> list[str]:
    return sorted(set(exported_names(globals(), _LAZY_EXPORTS)) | set(_SUBPACKAGES))


__all__ = [
    "AsyncErgonPlatformChannelsConnector",
    "AsyncErgonPlatformConnector",
    "CreateItemInput",
    "CreateItemPayload",
    "ErgonPlatformChannelsConfig",
    "ErgonPlatformChannelsConnector",
    "ErgonPlatformChannelsConsumerConfig",
    "ErgonPlatformChannelsProducerConfig",
    "ErgonPlatformClient",
    "ErgonPlatformConnector",
    "ErgonPlatformConsumerConfig",
    "ErgonPlatformProducerConfig",
    "SendMessageInput",
    "SendMessagePayload",
]
