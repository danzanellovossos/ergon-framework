from typing import TYPE_CHECKING, Any

from .._lazy import exported_names, load_optional_export
from .models import (
    AsyncRabbitmqClient,
    AsyncRabbitmqConsumerConfig,
    AsyncRabbitmqExchangeBinding,
    AsyncRabbitmqProducerConfig,
    AsyncRabbitmqQueueSubscription,
    RabbitmqClient,
    RabbitmqConsumerMessage,
    RabbitmqProducerMessage,
)

if TYPE_CHECKING:
    from .async_connector import AsyncRabbitMQConnector
    from .async_service import AsyncRabbitMQService
    from .connector import RabbitMQConnector
    from .service import RabbitMQService

_LAZY_EXPORTS = {
    "AsyncRabbitMQConnector": "async_connector",
    "AsyncRabbitMQService": "async_service",
    "RabbitMQConnector": "connector",
    "RabbitMQService": "service",
}


def __getattr__(name: str) -> Any:
    return load_optional_export(
        name=name,
        package=__name__,
        exports=_LAZY_EXPORTS,
        namespace=globals(),
        extra="rabbitmq",
        dependencies=("aio_pika", "aiormq", "pika"),
    )


def __dir__() -> list[str]:
    return exported_names(globals(), _LAZY_EXPORTS)


__all__ = [
    "AsyncRabbitMQConnector",
    "AsyncRabbitMQService",
    "AsyncRabbitmqClient",
    "AsyncRabbitmqConsumerConfig",
    "AsyncRabbitmqExchangeBinding",
    "AsyncRabbitmqProducerConfig",
    "AsyncRabbitmqQueueSubscription",
    "RabbitMQConnector",
    "RabbitMQService",
    "RabbitmqClient",
    "RabbitmqConsumerMessage",
    "RabbitmqProducerMessage",
]
