from importlib import import_module
from typing import TYPE_CHECKING, Any

from ._lazy import exported_names, load_export
from .connector import AsyncConnector, Connector, ConnectorConfig
from .transaction import Transaction

if TYPE_CHECKING:
    from .ergon_platform import (
        AsyncErgonPlatformChannelsConnector,
        AsyncErgonPlatformConnector,
        CreateItemInput,
        ErgonPlatformChannelsConfig,
        ErgonPlatformChannelsConsumerConfig,
        ErgonPlatformChannelsConnector,
        ErgonPlatformChannelsProducerConfig,
        ErgonPlatformClient,
        ErgonPlatformConnector,
        ErgonPlatformConsumerConfig,
        ErgonPlatformProducerConfig,
    )

    # NOTE: ``SendMessageInput`` is defined by both Nylas and Channels with
    # different shapes. The top-level alias here points to the Nylas type
    # (matches the pre-split lazy-export map); channels users must import it
    # from ``ergon.connector.ergon_platform.channels``.
    from .excel import ExcelConnector, ExcelFetchConfig, ExcelRow, ExcelService
    from .nylas import (
        AckActionConfig,
        AsyncNylasAuthService,
        AsyncNylasConnector,
        AsyncNylasService,
        AuthUrlConfig,
        CodeExchangeInput,
        GrantAuthResult,
        MessageQueryFilter,
        NylasAuthClient,
        NylasAuthService,
        NylasClient,
        NylasConnector,
        NylasConsumerConfig,
        NylasProducerConfig,
        NylasService,
        SendMessageInput,
    )
    from .postgres import (
        AsyncPostgresConnector,
        AsyncPostgresService,
        PostgresClient,
        PostgresConsumerConfig,
        PostgresProducerConfig,
    )
    from .rabbitmq import (
        AsyncRabbitmqClient,
        AsyncRabbitMQConnector,
        AsyncRabbitmqConsumerConfig,
        AsyncRabbitmqExchangeBinding,
        AsyncRabbitmqProducerConfig,
        AsyncRabbitmqQueueSubscription,
        AsyncRabbitMQService,
        RabbitmqClient,
        RabbitMQConnector,
        RabbitmqConsumerMessage,
        RabbitmqProducerMessage,
        RabbitMQService,
    )
    from .sqs import (
        AsyncSQSConnector,
        AsyncSQSService,
        SQSClient,
        SQSConnector,
        SQSConsumerConfig,
        SQSProducerConfig,
        SQSService,
    )

_CONNECTOR_SUBPACKAGES = (
    "ergon_platform",
    "excel",
    "nylas",
    "pipefy",
    "postgres",
    "rabbitmq",
    "sqs",
)

_LAZY_EXPORTS = {
    "AckActionConfig": "nylas",
    "AsyncErgonPlatformChannelsConnector": "ergon_platform",
    "AsyncErgonPlatformConnector": "ergon_platform",
    "AsyncNylasAuthService": "nylas",
    "AsyncNylasConnector": "nylas",
    "AsyncNylasService": "nylas",
    "AsyncPostgresConnector": "postgres",
    "AsyncPostgresService": "postgres",
    "AsyncRabbitMQConnector": "rabbitmq",
    "AsyncRabbitMQService": "rabbitmq",
    "AsyncRabbitmqClient": "rabbitmq",
    "AsyncRabbitmqConsumerConfig": "rabbitmq",
    "AsyncRabbitmqExchangeBinding": "rabbitmq",
    "AsyncRabbitmqProducerConfig": "rabbitmq",
    "AsyncRabbitmqQueueSubscription": "rabbitmq",
    "AsyncSQSConnector": "sqs",
    "AsyncSQSService": "sqs",
    "AuthUrlConfig": "nylas",
    "CodeExchangeInput": "nylas",
    "CreateItemInput": "ergon_platform",
    "ErgonPlatformChannelsConfig": "ergon_platform",
    "ErgonPlatformChannelsConnector": "ergon_platform",
    "ErgonPlatformChannelsConsumerConfig": "ergon_platform",
    "ErgonPlatformChannelsProducerConfig": "ergon_platform",
    "ErgonPlatformClient": "ergon_platform",
    "ErgonPlatformConnector": "ergon_platform",
    "ErgonPlatformConsumerConfig": "ergon_platform",
    "ErgonPlatformProducerConfig": "ergon_platform",
    "ExcelConnector": "excel",
    "ExcelFetchConfig": "excel",
    "ExcelRow": "excel",
    "ExcelService": "excel",
    "GrantAuthResult": "nylas",
    "MessageQueryFilter": "nylas",
    "NylasAuthClient": "nylas",
    "NylasAuthService": "nylas",
    "NylasClient": "nylas",
    "NylasConnector": "nylas",
    "NylasConsumerConfig": "nylas",
    "NylasProducerConfig": "nylas",
    "NylasService": "nylas",
    "PostgresClient": "postgres",
    "PostgresConsumerConfig": "postgres",
    "PostgresProducerConfig": "postgres",
    "RabbitMQConnector": "rabbitmq",
    "RabbitMQService": "rabbitmq",
    "RabbitmqClient": "rabbitmq",
    "RabbitmqConsumerMessage": "rabbitmq",
    "RabbitmqProducerMessage": "rabbitmq",
    "SQSClient": "sqs",
    "SQSConnector": "sqs",
    "SQSConsumerConfig": "sqs",
    "SQSProducerConfig": "sqs",
    "SQSService": "sqs",
    "SendMessageInput": "nylas",
}


def __getattr__(name: str) -> Any:
    if name in _CONNECTOR_SUBPACKAGES:
        module = import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    return load_export(
        name=name,
        package=__name__,
        exports=_LAZY_EXPORTS,
        namespace=globals(),
    )


def __dir__() -> list[str]:
    return sorted(set(exported_names(globals(), _LAZY_EXPORTS)) | set(_CONNECTOR_SUBPACKAGES))


__all__ = [
    "AckActionConfig",
    "AsyncConnector",
    "AsyncErgonPlatformChannelsConnector",
    "AsyncErgonPlatformConnector",
    "AsyncNylasAuthService",
    "AsyncNylasConnector",
    "AsyncNylasService",
    "AuthUrlConfig",
    "CodeExchangeInput",
    "GrantAuthResult",
    "AsyncPostgresConnector",
    "AsyncPostgresService",
    "AsyncRabbitMQConnector",
    "AsyncRabbitMQService",
    "AsyncRabbitmqClient",
    "AsyncRabbitmqConsumerConfig",
    "AsyncRabbitmqExchangeBinding",
    "AsyncRabbitmqProducerConfig",
    "AsyncRabbitmqQueueSubscription",
    "AsyncSQSConnector",
    "AsyncSQSService",
    "Connector",
    "ConnectorConfig",
    "CreateItemInput",
    "ErgonPlatformChannelsConfig",
    "ErgonPlatformChannelsConnector",
    "ErgonPlatformChannelsConsumerConfig",
    "ErgonPlatformChannelsProducerConfig",
    "ErgonPlatformClient",
    "ErgonPlatformConnector",
    "ErgonPlatformConsumerConfig",
    "ErgonPlatformProducerConfig",
    "ExcelConnector",
    "ExcelFetchConfig",
    "ExcelRow",
    "ExcelService",
    "MessageQueryFilter",
    "NylasAuthClient",
    "NylasAuthService",
    "NylasClient",
    "NylasConnector",
    "NylasConsumerConfig",
    "NylasProducerConfig",
    "NylasService",
    "PostgresClient",
    "SendMessageInput",
    "PostgresConsumerConfig",
    "PostgresProducerConfig",
    "RabbitMQConnector",
    "RabbitMQService",
    "RabbitmqClient",
    "RabbitmqConsumerMessage",
    "RabbitmqProducerMessage",
    "SQSClient",
    "SQSConnector",
    "SQSConsumerConfig",
    "SQSProducerConfig",
    "SQSService",
    "Transaction",
]
