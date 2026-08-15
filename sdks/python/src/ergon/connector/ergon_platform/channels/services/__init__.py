from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .activity import ChannelsActivityService
    from .addresses import ChannelsAddressService
    from .attachments import ChannelsAttachmentService
    from .messages import ChannelsMessageService
    from .service import ErgonPlatformChannelsService

_EXPORTS = {
    "ChannelsActivityService": "activity",
    "ChannelsAddressService": "addresses",
    "ChannelsAttachmentService": "attachments",
    "ChannelsMessageService": "messages",
    "ErgonPlatformChannelsService": "service",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})


__all__ = [
    "ChannelsActivityService",
    "ChannelsAddressService",
    "ChannelsAttachmentService",
    "ChannelsMessageService",
    "ErgonPlatformChannelsService",
]
