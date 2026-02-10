from typing import Dict, Generator, List, Optional

from ergon_framework import Connector, Transaction

from src.connectors.rabbitmq_connector.service import RabbitMQService

from .models import RabbitmqClient


class RabbitMQConnector(Connector):
    service = RabbitMQService

    def __init__(
        self,
        client: RabbitmqClient,
        default_queue: Optional[str] = None,
        auto_ack: bool = False,
    ) -> None:
        self.service = RabbitMQService(client)

        self.default_queue = default_queue or client.queue_name
        self.auto_ack = auto_ack

        # Mantém generators por (queue_name, auto_ack)
        self._iterators: Dict[tuple, Generator[dict, None, None]] = {}

    def fetch_transactions(
        self,
        batch_size: int = 1,
        queue_name: Optional[str] = None,
        auto_ack: bool = False,
        metadata: Optional[dict] = None,
        *args,
        **kwargs,
    ) -> List[Transaction]:
        """
        Busca até `batch_size` mensagens da fila indicada, transformando
        cada uma em um Transaction do ergon_framework.
        """
        queue_items = self.service.consume(queue_name=queue_name, auto_ack=auto_ack, batch_size=batch_size)

        if not queue_items:
            return []

        return [Transaction(id=str(queue_item["delivery_tag"]), payload=queue_item) for queue_item in queue_items]

    def dispatch_transactions(
        self,
        transaction: List[Transaction],
        *args,
        **kwargs,
    ) -> None:
        """
        Publica um Transaction como mensagem no RabbitMQ.
        """
        for transaction_item in transaction:
            self.service.publish(transaction_item.payload)
