from typing import TYPE_CHECKING, Any

from .._lazy import exported_names, load_optional_export
from .models import PostgresClient, PostgresConsumerConfig, PostgresProducerConfig

if TYPE_CHECKING:
    from .async_connector import AsyncPostgresConnector
    from .async_service import AsyncPostgresService

_LAZY_EXPORTS = {
    "AsyncPostgresConnector": "async_connector",
    "AsyncPostgresService": "async_service",
}


def __getattr__(name: str) -> Any:
    return load_optional_export(
        name=name,
        package=__name__,
        exports=_LAZY_EXPORTS,
        namespace=globals(),
        extra="postgres",
        dependencies=("asyncpg",),
    )


def __dir__() -> list[str]:
    return exported_names(globals(), _LAZY_EXPORTS)


__all__ = [
    "AsyncPostgresConnector",
    "AsyncPostgresService",
    "PostgresClient",
    "PostgresConsumerConfig",
    "PostgresProducerConfig",
]
