"""
SQS connector for ergon-framework.

Provides transaction-based access to AWS SQS queues with support for:
- Batch receive (long-polling)
- Message send (standard and FIFO queues)
- Message delete (ack equivalent)
"""

from .connector import SQSConnector
from .models import SQSClient, SQSProducerMessage
from .service import SQSService

__all__ = [
    "SQSConnector",
    "SQSClient",
    "SQSProducerMessage",
    "SQSService",
]
