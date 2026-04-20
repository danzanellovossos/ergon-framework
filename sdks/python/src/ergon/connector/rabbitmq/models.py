import time
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class RabbitmqClient(BaseModel):
    host: str = Field(default="localhost", description="The RabbitMQ host")
    port: int = Field(default=5672, description="The RabbitMQ port")
    username: str = Field(description="The RabbitMQ username")
    password: str = Field(description="The RabbitMQ password")
    queue_name: str = Field(default="minha fila", description="The RabbitMQ queue name")
    prefetch_count: int = Field(default=10, description="The number of prefetched messages")
    virtual_host: Optional[str] = Field(default="/", description="The RabbitMQ vhost")
    connection_attempts: int = Field(default=3, description="The RabbitMQ connection retry attempts")
    socket_timeout: float = Field(default=999, description="Socket timeout in seconds")
    heartbeat: Optional[float] = Field(default=600, description="Heartbeat interval in seconds")
    blocked_connection_timeout: Optional[float] = Field(
        default=None, description="Timeout when connection is blocked by the broker"
    )
    ssl_enabled: bool = Field(default=False, description="Enable SSL/TLS")
    ssl_ca_certs: Optional[str] = Field(default=None, description="Path to CA certificate when using SSL")


class RabbitmqProducerMessage(BaseModel):
    queue_name: str = Field(description="The RabbitMQ queue name")
    durable: bool = Field(default=True, description="True for queue persists if broker restart")
    body: dict = Field(default={"message": "Hello World!"}, description="The body of the message")
    delivery_mode: Literal[1, 2] = Field(
        default=2, description="1 for not persistent and 2 for persistent (It needs durable True)"
    )
    content_type: str = Field(default="application/json", description="Content type of the message")
    priority: int = Field(default=0, description="The priority of the message 0 for default")
    timestamp: int = Field(default_factory=lambda: int(time.time()), description="The timestamp of the message")


class RabbitmqConsumerMessage(BaseModel):
    queue_name: str = Field(description="The RabbitMQ queue name")
    auto_ack: bool = Field(default=True, description="True for automatic acknowledgement")


# ---------------------------------------------------------
# ASYNC MODELS (aio-pika)
# ---------------------------------------------------------


class AsyncRabbitmqClient(BaseModel):
    url: Optional[str] = Field(default=None, description="Full AMQP URL (takes precedence over individual params)")
    host: str = Field(default="localhost", description="RabbitMQ host")
    port: int = Field(default=5672, description="RabbitMQ port")
    username: str = Field(default="guest", description="RabbitMQ username")
    password: str = Field(default="guest", description="RabbitMQ password")
    virtual_host: str = Field(default="/", description="RabbitMQ virtual host")
    heartbeat: int = Field(default=600, description="Heartbeat interval in seconds")
    connection_attempts: int = Field(default=3, description="Connection retry attempts")
    ssl_enabled: bool = Field(default=False, description="Enable SSL/TLS")
    ssl_ca_certs: Optional[str] = Field(default=None, description="Path to CA certificate when using SSL")

    def get_url(self) -> str:
        if self.url:
            return self.url
        return f"amqp://{self.username}:{self.password}@{self.host}:{self.port}/{self.virtual_host}"


class AsyncRabbitmqConsumerConfig(BaseModel):
    queue_name: str = Field(description="Queue to consume from")
    exchange_name: str = Field(default="", description="Exchange name (empty for default exchange)")
    exchange_type: str = Field(default="topic", description="Exchange type: topic, direct, fanout, headers")
    binding_keys: list[str] = Field(default=["#"], description="Routing key patterns for queue binding")
    prefetch_count: int = Field(default=10, description="Number of unacknowledged messages allowed")
    durable: bool = Field(default=True, description="Durable exchange and queue declarations")
    auto_ack: bool = Field(default=False, description="Automatically acknowledge messages on delivery")
    consume_timeout: float = Field(default=2.0, description="Max seconds to wait per fetch call")
    queue_arguments: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Extra AMQP arguments forwarded to queue.declare (the AMQP `x-arguments` table). "
            "Use for dead-lettering (`x-dead-letter-exchange`, `x-dead-letter-routing-key`), "
            "TTL (`x-message-ttl`), max length, etc. Note: changing these on an existing queue "
            "will cause RabbitMQ to reject re-declaration with PRECONDITION_FAILED; the queue "
            "must be recreated or the args set via a broker-side Policy."
        ),
    )


class AsyncRabbitmqProducerConfig(BaseModel):
    exchange_name: str = Field(default="", description="Exchange to publish to (empty for default exchange)")
    exchange_type: str = Field(default="topic", description="Exchange type: topic, direct, fanout, headers")
    routing_key: str = Field(default="", description="Default routing key for published messages")
    durable: bool = Field(default=True, description="Durable exchange declaration")
    delivery_mode: Literal[1, 2] = Field(default=2, description="1 for transient, 2 for persistent")
    content_type: str = Field(default="application/json", description="Content type of published messages")
