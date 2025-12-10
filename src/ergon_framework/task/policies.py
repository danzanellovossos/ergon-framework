from typing import Optional, Union

from pydantic import BaseModel, Field

# =====================================================================
#   SHARED MODELS
# =====================================================================


class ConcurrencyPolicy(BaseModel):
    value: int = Field(
        default=1, ge=1, description="The default number of concurrent transactions to process or produce."
    )
    min: int = Field(
        default=1, ge=1, description="The minimum number of concurrent transactions to process or produce."
    )
    max: int = Field(
        default=1, ge=1, description="The maximum number of concurrent transactions to process or produce."
    )


class BatchPolicy(BaseModel):
    size: int = Field(default=1, ge=1, description="The default number of transactions to process per batch.")
    min_size: int = Field(default=1, ge=1, description="The minimum number of transactions to process per batch.")
    max_size: int = Field(default=1, ge=1, description="The maximum number of transactions to process per batch.")
    interval: float = Field(default=0.0, ge=0.0, description="The interval in seconds to wait between batches.")
    # backoff: float = Field(default=1.0, ge=0.0, description="The backoff in seconds to wait before retrying.")
    # backoff_multiplier: float = Field(default=2.0, ge=1.0, description="The backoff multiplier to the next attempt.")
    # backoff_cap: float = Field(default=0, ge=0.0, description="The maximum backoff in seconds.")


class RetryPolicy(BaseModel):
    max_attempts: int = Field(default=1, ge=1, description="The maximum number of attempts to finish a step.")
    timeout: Optional[float] = Field(default=None, ge=0, description="The timeout in seconds for the step.")
    backoff: float = Field(default=0.0, ge=0.0, description="The backoff in seconds for the next attempt.")
    backoff_multiplier: float = Field(default=0.0, ge=1.0, description="The backoff multiplier to the next attempt.")
    backoff_cap: float = Field(default=0, ge=0.0, description="The maximum backoff in seconds.")


# =====================================================================
#   CONSUMER STEP POLICIES
# =====================================================================


class FetchPolicy(BaseModel):
    retry: RetryPolicy = Field(default_factory=RetryPolicy, description="The retry policy for fetching transactions.")
    connector_name: str = Field(default=None, description="The name of the connector to use for fetching transactions.")
    extra: dict = Field(default_factory=dict)


class ProcessPolicy(BaseModel):
    retry: RetryPolicy = Field(default_factory=RetryPolicy, description="The retry policy for processing transactions.")


class SuccessPolicy(BaseModel):
    retry: RetryPolicy = Field(
        default_factory=RetryPolicy, description="The retry policy for handling successful transactions."
    )


class ExceptionPolicy(BaseModel):
    retry: RetryPolicy = Field(default_factory=RetryPolicy, description="The retry policy for handling exceptions.")


# =====================================================================
#   CONSUMER LOOP POLICIES
# =====================================================================


class EmptyQueuePolicy(BaseModel):
    backoff: float = Field(default=0.0, ge=0.0, description="The backoff in seconds to wait before retrying.")
    backoff_multiplier: float = Field(default=0.0, ge=1.0, description="The backoff multiplier to the next attempt.")
    backoff_cap: float = Field(default=0.0, ge=0.0, description="The maximum backoff in seconds.")
    interval: float = Field(default=0.0, ge=0.0, description="The interval in seconds to wait between attempts.")


class ConsumerLoopPolicy(BaseModel):
    batch: BatchPolicy = Field(default_factory=BatchPolicy, description="The batch policy for the consumer loop.")
    concurrency: ConcurrencyPolicy = Field(
        default_factory=ConcurrencyPolicy, description="The concurrency policy for the consumer loop."
    )
    timeout: Optional[float] = Field(default=None, ge=0, description="The timeout in seconds for the loop.")
    limit: Optional[int] = Field(default=None, ge=0, description="The maximum number of transactions to process.")
    transaction_timeout: Optional[float] = Field(
        default=None, ge=0, description="The timeout in seconds for the transaction."
    )
    streaming: bool = Field(default=False, description="Whether to stream transactions.")
    empty_queue: EmptyQueuePolicy = Field(
        default_factory=EmptyQueuePolicy, description="The empty queue policy for the consumer loop."
    )


# =====================================================================
#   CONSUMER STEP POLICIES
# =====================================================================
class ConsumerPolicy(BaseModel):
    name: str = Field(default=None, description="The name of the policy.")
    loop: ConsumerLoopPolicy = Field(default_factory=ConsumerLoopPolicy, description="The consumer loop policy.")
    fetch: FetchPolicy = Field(default_factory=FetchPolicy, description="The fetch policy for the consumer.")
    process: ProcessPolicy = Field(default_factory=ProcessPolicy, description="The process policy for the consumer.")
    success: SuccessPolicy = Field(default_factory=SuccessPolicy, description="The success policy for the consumer.")
    exception: ExceptionPolicy = Field(
        default_factory=ExceptionPolicy, description="The exception policy for the consumer."
    )


# =====================================================================
#   PRODUCER POLICIES
# =====================================================================


class PreparePolicy(BaseModel):
    retry: RetryPolicy = Field(default_factory=RetryPolicy, description="The retry policy for preparing transactions.")


class ProducerLoopPolicy(BaseModel):
    concurrency: ConcurrencyPolicy = Field(
        default_factory=ConcurrencyPolicy, description="The concurrency policy for the producer loop."
    )
    batch: BatchPolicy = Field(default_factory=BatchPolicy, description="The batch policy for the producer loop.")
    timeout: Optional[float] = Field(default=None, ge=0, description="The timeout in seconds for the loop.")
    limit: Optional[int] = Field(default=None, ge=0, description="The maximum number of transactions to produce.")
    transaction_timeout: Optional[float] = Field(
        default=None, ge=0, description="The timeout in seconds for the transaction."
    )


class ProducerPolicy(BaseModel):
    name: str = Field(default=None, description="The name of the policy.")
    loop: ProducerLoopPolicy = Field(default_factory=ProducerLoopPolicy, description="The producer loop policy.")
    prepare: PreparePolicy = Field(default_factory=PreparePolicy, description="The prepare policy for the producer.")
    success: SuccessPolicy = Field(default_factory=SuccessPolicy, description="The success policy for the producer.")
    exception: ExceptionPolicy = Field(
        default_factory=ExceptionPolicy, description="The exception policy for the producer."
    )


ExecutionPolicy = Union[ConsumerPolicy, ProducerPolicy]
