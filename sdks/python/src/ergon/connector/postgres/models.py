from typing import Any, List, Optional

from pydantic import BaseModel, Field


class PostgresClient(BaseModel):
    dsn: Optional[str] = Field(default=None, description="Full DSN (takes precedence over individual params)")
    host: str = Field(default="localhost", description="PostgreSQL host")
    port: int = Field(default=5432, description="PostgreSQL port")
    user: str = Field(default="postgres", description="PostgreSQL user")
    password: str = Field(default="postgres", description="PostgreSQL password")
    database: str = Field(default="postgres", description="PostgreSQL database name")
    min_pool_size: int = Field(default=1, ge=1, description="Minimum connection pool size")
    max_pool_size: int = Field(default=10, ge=1, description="Maximum connection pool size")
    ssl: bool = Field(default=False, description="Enable SSL/TLS connections")

    def get_dsn(self) -> str:
        if self.dsn:
            return self.dsn
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


class PostgresConsumerConfig(BaseModel):
    fetch_query: str = Field(description="SQL query to fetch rows as transactions")
    fetch_params: List[Any] = Field(default_factory=list, description="Bind parameters for the fetch query")
    batch_size: int = Field(default=100, ge=1, description="Max rows to fetch per call")
    listen_channel: Optional[str] = Field(
        default=None, description="Optional PG LISTEN channel for push-based notifications"
    )
    id_column: str = Field(default="id", description="Column name to use as Transaction.id")


class PostgresProducerConfig(BaseModel):
    dispatch_query: str = Field(description="SQL statement to execute per dispatched transaction")
