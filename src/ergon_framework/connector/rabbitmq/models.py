from typing import Optional
from pydantic import BaseModel, Field
import time
import json


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
    blocked_connection_timeout: Optional[float] = Field(default=None, description="Timeout when connection is blocked by the broker")
    ssl_enabled: bool = Field(default=False, description="Enable SSL/TLS")
    ssl_ca_certs: Optional[str] = Field(default=None, description="Path to CA certificate when using SSL")


class RabbitmqProducerMessage(BaseModel):
    queue_name: str = Field(description="The RabbitMQ queue name")
    durable: bool = Field(default=True, description="True for queue persists if broker restart")
    body: dict = Field(default={"message": "Hello World!"}, description="The body of the message")
    delivery_mode: int = Field(default=2, description="1 for not persistent and 2 for persistent (It needs durable True)")
    content_type : str = Field(default="application/json", description="Content type of the message")
    priority: int = Field(default=0, description="The priority of the message 0 for default")
    timestamp: int = Field(default_factory=lambda: int(time.time()), description="The timestamp of the message")


class RabbitmqConsumerMessage(BaseModel):
    queue_name: str = Field(description="The RabbitMQ queue name")
    auto_ack: bool = Field(default=True, description="True for automatic acknowledgement")