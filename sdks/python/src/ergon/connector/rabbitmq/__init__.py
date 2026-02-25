"""
RabbitMQ connector for ergon-framework.
Provides transaction-based access to RabbitMQ queues with support for:
- Message consumption with configurable batching
- Message publishing
- Auto/manual acknowledgement modes
"""

from .connector import RabbitMQConnector
from .models import RabbitmqClient, RabbitmqConsumerMessage, RabbitmqProducerMessage
from .service import RabbitMQService

__all__ = [
    "RabbitMQConnector",
    "RabbitMQService",
    "RabbitmqClient",
    "RabbitmqConsumerMessage",
    "RabbitmqProducerMessage",
]
