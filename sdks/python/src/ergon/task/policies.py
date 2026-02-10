from typing import Optional, Union

from pydantic import BaseModel, Field, field_validator

# =====================================================================
#   INTERNAL NORMALIZERS (framework-level helpers)
# =====================================================================


def _normalize_optional(v):
    """
    Normalize optional env-driven values.

    Accepts:
      - None
      - ""
      - "none" / "null"
      - numeric strings

    Lets Pydantic handle final coercion.
    """
    if v is None:
        return None

    if isinstance(v, str):
        v = v.strip()
        if v == "" or v.lower() in {"none", "null"}:
            return None

    return v


def _normalize_bool(v):
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return v


# =====================================================================
#   SHARED MODELS
# =====================================================================


class ConcurrencyPolicy(BaseModel):
    value: int = Field(default=1, ge=1)
    headroom: int = Field(default=0, ge=0)
    min: int = Field(default=1, ge=1)
    max: int = Field(default=1, ge=1)

    @field_validator("value", "headroom", "min", "max", mode="before")
    @classmethod
    def _normalize_ints(cls, v):
        return _normalize_optional(v)


class BatchPolicy(BaseModel):
    size: int = Field(default=1, ge=1)
    min_size: int = Field(default=1, ge=1)
    max_size: int = Field(default=1, ge=1)
    interval: float = Field(default=0.0, ge=0.0)

    @field_validator("size", "min_size", "max_size", "interval", mode="before")
    @classmethod
    def _normalize_numbers(cls, v):
        return _normalize_optional(v)


class RetryPolicy(BaseModel):
    max_attempts: int = Field(default=1, ge=1)
    timeout: Optional[float] = Field(default=None, ge=0)
    backoff: float = Field(default=0.0, ge=0.0)
    backoff_multiplier: float = Field(default=0.0, ge=1.0)
    backoff_cap: float = Field(default=0.0, ge=0.0)

    @field_validator(
        "max_attempts",
        "timeout",
        "backoff",
        "backoff_multiplier",
        "backoff_cap",
        mode="before",
    )
    @classmethod
    def _normalize_numbers(cls, v):
        return _normalize_optional(v)


class TransactionRuntimePolicy(BaseModel):
    timeout: Optional[float] = Field(default=60.0, ge=0)

    @field_validator("timeout", mode="before")
    @classmethod
    def _normalize_optional_numbers(cls, v):
        return _normalize_optional(v)


# =====================================================================
#   CONSUMER STEP POLICIES
# =====================================================================


class EmptyFetchPolicy(BaseModel):
    backoff: float = Field(default=0.0, ge=0.0)
    backoff_multiplier: float = Field(default=1.0, ge=0.0)
    backoff_cap: float = Field(default=0.0, ge=0.0)
    interval: float = Field(default=0.0, ge=0.0)

    @field_validator(
        "backoff",
        "backoff_multiplier",
        "backoff_cap",
        "interval",
        mode="before",
    )
    @classmethod
    def _normalize_numbers(cls, v):
        return _normalize_optional(v)


class FetchPolicy(BaseModel):
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    batch: BatchPolicy = Field(default_factory=BatchPolicy)
    empty: EmptyFetchPolicy = Field(default_factory=EmptyFetchPolicy)
    connector_name: Optional[str] = None
    extra: dict = Field(default_factory=dict)


class ProcessPolicy(BaseModel):
    retry: RetryPolicy = Field(default_factory=RetryPolicy)


class SuccessPolicy(BaseModel):
    retry: RetryPolicy = Field(default_factory=RetryPolicy)


class ExceptionPolicy(BaseModel):
    retry: RetryPolicy = Field(default_factory=RetryPolicy)


# =====================================================================
#   CONSUMER LOOP POLICIES
# =====================================================================


class ConsumerLoopPolicy(BaseModel):
    concurrency: ConcurrencyPolicy = Field(default_factory=ConcurrencyPolicy)
    timeout: Optional[float] = Field(default=None, ge=0)
    limit: Optional[int] = Field(default=None, ge=0)
    streaming: bool = Field(default=False)

    @field_validator("timeout", "limit", mode="before")
    @classmethod
    def _normalize_optional_numbers(cls, v):
        return _normalize_optional(v)

    @field_validator("streaming", mode="before")
    @classmethod
    def _normalize_streaming(cls, v):
        return _normalize_bool(v)


# =====================================================================
#   CONSUMER POLICY
# =====================================================================


class ConsumerPolicy(BaseModel):
    name: Optional[str] = None
    loop: ConsumerLoopPolicy = Field(default_factory=ConsumerLoopPolicy)
    fetch: FetchPolicy = Field(default_factory=FetchPolicy)
    transaction_runtime: TransactionRuntimePolicy = Field(default_factory=TransactionRuntimePolicy)
    process: ProcessPolicy = Field(default_factory=ProcessPolicy)
    success: SuccessPolicy = Field(default_factory=SuccessPolicy)
    exception: ExceptionPolicy = Field(default_factory=ExceptionPolicy)


# =====================================================================
#   PRODUCER POLICIES
# =====================================================================


class PreparePolicy(BaseModel):
    retry: RetryPolicy = Field(default_factory=RetryPolicy)


class ProducerLoopPolicy(BaseModel):
    concurrency: ConcurrencyPolicy = Field(default_factory=ConcurrencyPolicy)
    batch: BatchPolicy = Field(default_factory=BatchPolicy)
    timeout: Optional[float] = Field(default=None, ge=0)
    limit: Optional[int] = Field(default=None, ge=0)

    @field_validator("timeout", "limit", mode="before")
    @classmethod
    def _normalize_optional_numbers(cls, v):
        return _normalize_optional(v)


class ProducerPolicy(BaseModel):
    name: Optional[str] = None
    loop: ProducerLoopPolicy = Field(default_factory=ProducerLoopPolicy)
    transaction_runtime: TransactionRuntimePolicy = Field(default_factory=TransactionRuntimePolicy)
    prepare: PreparePolicy = Field(default_factory=PreparePolicy)
    success: SuccessPolicy = Field(default_factory=SuccessPolicy)
    exception: ExceptionPolicy = Field(default_factory=ExceptionPolicy)


# =====================================================================
#   UNION
# =====================================================================

ExecutionPolicy = Union[ConsumerPolicy, ProducerPolicy]
