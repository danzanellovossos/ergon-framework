from typing import TYPE_CHECKING, Any

from .._lazy import exported_names, load_optional_export
from .models import (
    CreateCardInput,
    FieldFilter,
    FieldFilterOperator,
    PipefyClient,
)

if TYPE_CHECKING:
    from .async_connector import AsyncPipefyConnector
    from .async_service import AsyncPipefyService
    from .connector import PipefyConnector
    from .service import PipefyService

_LAZY_EXPORTS = {
    "AsyncPipefyConnector": "async_connector",
    "AsyncPipefyService": "async_service",
    "PipefyConnector": "connector",
    "PipefyService": "service",
}


def __getattr__(name: str) -> Any:
    return load_optional_export(
        name=name,
        package=__name__,
        exports=_LAZY_EXPORTS,
        namespace=globals(),
        extra="pipefy",
        dependencies=("httpx", "requests"),
    )


def __dir__() -> list[str]:
    return exported_names(globals(), _LAZY_EXPORTS)


__all__ = [
    "AsyncPipefyConnector",
    "AsyncPipefyService",
    "PipefyConnector",
    "PipefyService",
    "PipefyClient",
    "CreateCardInput",
    "FieldFilter",
    "FieldFilterOperator",
]
