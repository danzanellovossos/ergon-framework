from typing import TYPE_CHECKING, Any

from .._lazy import exported_names, load_optional_export
from .models import SQSClient, SQSConsumerConfig, SQSProducerConfig

if TYPE_CHECKING:
    from .async_connector import AsyncSQSConnector
    from .async_service import AsyncSQSService
    from .connector import SQSConnector
    from .service import SQSService

_LAZY_EXPORTS = {
    "AsyncSQSConnector": "async_connector",
    "AsyncSQSService": "async_service",
    "SQSConnector": "connector",
    "SQSService": "service",
}


def __getattr__(name: str) -> Any:
    return load_optional_export(
        name=name,
        package=__name__,
        exports=_LAZY_EXPORTS,
        namespace=globals(),
        extra="sqs",
        dependencies=("aiobotocore", "boto3", "botocore"),
    )


def __dir__() -> list[str]:
    return exported_names(globals(), _LAZY_EXPORTS)


__all__ = [
    "AsyncSQSConnector",
    "AsyncSQSService",
    "SQSConnector",
    "SQSService",
    "SQSClient",
    "SQSConsumerConfig",
    "SQSProducerConfig",
]
