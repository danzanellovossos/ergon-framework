from typing import TYPE_CHECKING, Any

from .._lazy import exported_names, load_optional_export
from .models import (
    AckActionConfig,
    AttachmentInput,
    AuthUrlConfig,
    ClientSideFilter,
    CodeExchangeInput,
    EmailAddress,
    GrantAuthResult,
    MessageFields,
    MessageQueryFilter,
    NylasAuthClient,
    NylasClient,
    NylasConsumerConfig,
    NylasProducerConfig,
    SendMessageInput,
)

if TYPE_CHECKING:
    from .async_auth_service import AsyncNylasAuthService
    from .async_connector import AsyncNylasConnector
    from .async_service import AsyncNylasService
    from .auth_service import NylasAuthService
    from .connector import NylasConnector
    from .service import NylasService

_LAZY_EXPORTS = {
    "AsyncNylasAuthService": "async_auth_service",
    "AsyncNylasConnector": "async_connector",
    "AsyncNylasService": "async_service",
    "NylasAuthService": "auth_service",
    "NylasConnector": "connector",
    "NylasService": "service",
}


def __getattr__(name: str) -> Any:
    return load_optional_export(
        name=name,
        package=__name__,
        exports=_LAZY_EXPORTS,
        namespace=globals(),
        extra="nylas",
        dependencies=("nylas",),
    )


def __dir__() -> list[str]:
    return exported_names(globals(), _LAZY_EXPORTS)


__all__ = [
    "AckActionConfig",
    "AsyncNylasAuthService",
    "AsyncNylasConnector",
    "AsyncNylasService",
    "AttachmentInput",
    "AuthUrlConfig",
    "ClientSideFilter",
    "CodeExchangeInput",
    "EmailAddress",
    "GrantAuthResult",
    "MessageFields",
    "MessageQueryFilter",
    "NylasAuthClient",
    "NylasAuthService",
    "NylasClient",
    "NylasConnector",
    "NylasConsumerConfig",
    "NylasProducerConfig",
    "NylasService",
    "SendMessageInput",
]
