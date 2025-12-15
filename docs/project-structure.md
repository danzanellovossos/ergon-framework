# Project Structure Guide

So you've decided to build with Ergon—great choice. But where do you put things? How do you keep a growing codebase from turning into spaghetti? This guide walks you through the recommended project layout that keeps your code clean, testable, and scalable.

The structure isn't arbitrary. Every directory and file has a clear purpose, and once you internalize the patterns, adding new tasks or connectors becomes almost mechanical.

---

## The Big Picture

An Ergon project has three main areas:

```
my_project/
├── main.py                    # Your single entry point
├── _observability/            # Telemetry infrastructure (Grafana, Prometheus, etc.)
├── connectors/                # Custom connectors when plugins don't cut it
│   └── {connector_name}/      # One folder per connector
└── tasks/                     # Where the magic happens
    ├── {task_name}/           # One folder per task
    └── [shared modules]       # Stuff used across all tasks
```

The philosophy is simple:

- **Tasks** = pure business logic (no I/O, no protocol noise)
- **Connectors** = the bridge to external systems
- **Shared modules** = don't repeat yourself

---

## Entry Point: `main.py`

Every Ergon project has exactly one entry point. It loads your environment and hands control to the CLI:

```python
from ergon_framework.cli import ergon
from ergon_framework.utils.env import load_env

load_env()  # Load .env before anything else

if __name__ == "__main__":
    ergon()
```

That's it. The CLI discovers your registered tasks and runs them. You never touch this file again unless you're doing something exotic.

---

## Connectors: Your Gateway to the World

### When Do You Need a Custom Connector?

Most of the time, you don't. For standard transports like Kafka, RabbitMQ, or PostgreSQL, just install a plugin:

```bash
pip install ergon-kafka
pip install ergon-rabbitmq
```

But sometimes you need to talk to something unique—a proprietary internal API, a weird legacy system, or a third-party service with no existing plugin. That's when you build a custom connector.

### The Connector Folder Structure

```
connectors/
├── __init__.py
└── acme_api/                  # Name it after the system
    ├── __init__.py
    ├── connector.py           # The transaction interface
    ├── service.py             # The protocol mechanics
    ├── schemas.py             # Pydantic models (optional)
    └── exceptions.py          # Custom errors (optional)
```

### What Goes Where?

| File | What It Does |
|------|--------------|
| `service.py` | Knows *how* to talk to the external system. HTTP calls, authentication, retries, pagination—all the messy protocol stuff lives here. |
| `connector.py` | Wraps the service and exposes `fetch_transactions()` and `dispatch_transactions()`. This is what your tasks interact with. |
| `schemas.py` | Pydantic models for validating requests and responses. Keeps your data clean. |
| `exceptions.py` | Custom exception classes like `RateLimitExceeded` or `AuthenticationFailed`. Helps the framework route errors correctly. |

### Plugin or Custom?

| Situation | What to Do |
|-----------|------------|
| Kafka, RabbitMQ, S3, PostgreSQL | Install the plugin. Don't reinvent the wheel. |
| Internal company API | Build a custom connector. |
| Third-party API with no plugin | Build a custom connector. |
| Existing plugin doesn't quite fit | Fork it or build custom. |

---

## Tasks: Where Your Logic Lives

The `tasks/` folder is the heart of your project. It contains two things:

1. **Shared modules** at the root—utilities, schemas, and constants used across all tasks
2. **Per-task folders**—isolated homes for each task's implementation

```
tasks/
├── __init__.py
├── settings.py             # All your connectors, services, telemetry configs
├── constants.py            # Enums, magic values, thresholds
├── schemas.py              # Pydantic models shared across tasks
├── exceptions.py           # Exception classes shared across tasks
├── helpers.py              # Utility functions shared across tasks
│
├── order_ingestion/        # One task
│   ├── task.py
│   ├── config.py
│   └── ...
│
└── order_enrichment/       # Another task
    ├── task.py
    ├── config.py
    └── ...
```

---

## Shared Modules (The `tasks/` Root)

These files prevent you from writing the same code in every task.

### `settings.py` — The Control Center

This is where all your infrastructure configuration lives. Connectors, services, telemetry—define them once, reference them everywhere.

```python
import os
from ergon_framework.connector import ConnectorConfig, ServiceConfig
from ergon_framework.telemetry import logging, tracing

# Connector configurations
KAFKA_CONNECTOR = ConnectorConfig(
    connector=KafkaConnector,
    kwargs={"bootstrap_servers": os.getenv("KAFKA_BROKERS")}
)

# Service configurations (for enrichment, APIs, etc.)
OPENAI_SERVICE = ServiceConfig(
    service=OpenAIService,
    kwargs={"api_key": os.getenv("OPENAI_API_KEY")}
)

# Telemetry
LOGGING = logging.LoggingConfig(level="INFO", handlers=[...])
TRACING = tracing.TracingConfig(processors=[...])
```

**Why this matters:** Your tasks never touch environment variables directly. They just reference `settings.KAFKA_CONNECTOR`. When you need to swap Kafka for RabbitMQ, you change one file—not twenty.

---

### `constants.py` — No More Magic Numbers

Enums, thresholds, and business constants. Name things properly.

```python
from enum import Enum

class OrderStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

DEFAULT_BATCH_SIZE = 100
MAX_RETRY_ATTEMPTS = 5
PROCESSING_TIMEOUT_SECONDS = 300
```

**Why this matters:** When someone asks "what's the batch size?", they look in one place. When you need to change it, you change it once.

---

### `schemas.py` — Shared Data Models

Pydantic models that multiple tasks use. Define common payloads here.

```python
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class BasePayload(BaseModel):
    """Every transaction payload inherits from this."""
    created_at: datetime
    source: str

class UserPayload(BasePayload):
    user_id: str
    email: str
    name: Optional[str] = None
```

**Why this matters:** If three tasks work with `UserPayload`, you don't want three different definitions that might drift apart.

---

### `exceptions.py` — Shared Error Types

Exception classes that multiple tasks might raise.

```python
from ergon_framework.task.exceptions import TransactionException, ExceptionCategory

class ValidationError(TransactionException):
    """Payload failed validation."""
    category = ExceptionCategory.BUSINESS

class DuplicateRecordError(TransactionException):
    """We've seen this record before."""
    category = ExceptionCategory.BUSINESS
```

**Why this matters:** The framework uses exception categories to decide whether to retry (SYSTEM errors) or fail fast (BUSINESS errors). Consistent exception classes mean consistent behavior.

---

### `helpers.py` — Shared Utilities

Pure functions that you use everywhere.

```python
import hashlib
import json

def generate_idempotency_key(payload: dict) -> str:
    """Deterministic hash for deduplication."""
    content = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()

def normalize_email(email: str) -> str:
    """Lowercase and strip whitespace."""
    return email.strip().lower()
```

**Why this matters:** When the idempotency logic needs to change, you fix it once. Pure functions are trivially testable.

---

## Per-Task Folders

Each task gets its own directory. This keeps task-specific logic from polluting other tasks.

```
tasks/order_processing/
├── __init__.py
├── task.py             # The task class
├── config.py           # TaskConfig and policies
├── schemas.py          # Task-specific Pydantic models
├── exceptions.py       # Task-specific exceptions
└── helpers.py          # Task-specific utilities
```

### `task.py` — Your Business Logic

This is where the actual work happens. Import a ready-to-use task class and implement the required methods:

```python
from ergon_framework.task.mixins import AsyncConsumerTask
from ergon_framework.connector import Transaction
from .schemas import OrderSchema

class OrderProcessorTask(AsyncConsumerTask):
    """Consumes orders and processes them."""

    async def process_transaction(self, transaction: Transaction):
        # Parse and validate
        order = OrderSchema(**transaction.payload)
        
        # Use an injected service for enrichment
        customer = await self.crm_service.get_customer(order.customer_id)
        
        # Pure business logic
        total = sum(item.quantity * item.price for item in order.items)
        
        return {
            "order_id": order.id,
            "customer_name": customer.name,
            "total": total,
            "status": "processed"
        }

    async def handle_process_success(self, transaction: Transaction, result):
        # Dispatch to output connector after success
        self.logger.info(f"Processed order {result['order_id']}")

    async def handle_process_exception(self, transaction: Transaction, exception):
        # Log failures, maybe send to DLQ
        self.logger.error(f"Failed to process {transaction.id}", exc_info=exception)
```

**Available task classes:**

| Class | Use Case |
|-------|----------|
| `ConsumerTask` | Sync task that consumes transactions |
| `AsyncConsumerTask` | Async task that consumes transactions |
| `ProducerTask` | Sync task that produces transactions |
| `AsyncProducerTask` | Async task that produces transactions |
| `HybridTask` | Sync task that consumes and produces |
| `AsyncHybridTask` | Async task that consumes and produces |

All of these are pre-configured with the right mixins. Just pick the one that fits your use case.

---

### `config.py` — Wiring It All Together

This file defines how your task runs: which connectors, which services, what policies.

```python
from ergon_framework.task import TaskConfig, task_manager, policies
from .. import settings
from .task import OrderProcessorTask

# Define the consumer policy
CONSUMER_POLICY = policies.ConsumerPolicy()
CONSUMER_POLICY.name = "order-consumer"
CONSUMER_POLICY.loop.concurrency.value = 10      # Process 10 at a time
CONSUMER_POLICY.loop.batch.size = 50             # Fetch 50 per batch
CONSUMER_POLICY.loop.streaming = True            # Keep polling
CONSUMER_POLICY.process.retry.max_attempts = 3   # Retry failures 3 times
CONSUMER_POLICY.process.retry.backoff = 1        # Start with 1 second
CONSUMER_POLICY.process.retry.backoff_multiplier = 2  # Double each retry

# Define the task configuration
TASK_CONFIG = TaskConfig(
    task=OrderProcessorTask,
    name="order-processor",
    connectors={"input": settings.KAFKA_CONNECTOR},
    services={"crm": settings.CRM_SERVICE},
    policies=[CONSUMER_POLICY],
    logging=settings.LOGGING,
    tracing=settings.TRACING,
)

# Register it
task_manager.register(TASK_CONFIG)
```

**Why this is separate from `task.py`:** Configuration changes shouldn't require touching business logic. Need to bump concurrency? Edit `config.py`. Business rules changed? Edit `task.py`. Clear boundaries.

---

### `schemas.py` (Per-Task)

Pydantic models specific to this task. Import shared schemas when needed.

```python
from pydantic import BaseModel, Field
from typing import List
from .. import schemas as shared

class OrderLineItem(BaseModel):
    sku: str
    quantity: int
    price: float

class OrderSchema(shared.BasePayload):
    """Order-specific payload."""
    order_id: str
    customer_id: str
    items: List[OrderLineItem]
    total: float = Field(ge=0)
```

---

### `exceptions.py` (Per-Task)

Exceptions that only make sense for this task.

```python
from ergon_framework.task.exceptions import TransactionException, ExceptionCategory

class InvalidOrderError(TransactionException):
    """Order failed validation."""
    category = ExceptionCategory.BUSINESS

class InventoryShortageError(TransactionException):
    """Not enough stock."""
    category = ExceptionCategory.BUSINESS
```

---

### `helpers.py` (Per-Task)

Utility functions used only by this task.

```python
from .schemas import OrderLineItem

def calculate_order_total(items: list[OrderLineItem]) -> float:
    """Sum up the line items."""
    return sum(item.quantity * item.price for item in items)

def generate_order_reference(order_id: str) -> str:
    """Human-readable reference number."""
    return f"ORD-{order_id[:8].upper()}"
```

---

## Observability: `_observability/`

This folder holds infrastructure configs for your local telemetry stack.

```
_observability/
├── docker-compose.telemetry.yml   # Spin up Grafana, Prometheus, etc.
├── otel-collector-config.yaml     # OpenTelemetry Collector
├── prometheus.yaml                # Scrape configs
├── grafana.yaml                   # Datasources and dashboards
├── loki.yaml                      # Log aggregation
└── tempo.yaml                     # Trace storage
```

Run `docker compose -f _observability/docker-compose.telemetry.yml up` and you've got a full observability stack locally. In production, you'll likely use managed services, but this gives you the same visibility during development.

---

## Putting It All Together

Here's a complete project with three tasks:

```
my_order_pipeline/
├── main.py
├── _observability/
│   ├── docker-compose.telemetry.yml
│   └── otel-collector-config.yaml
│
├── connectors/
│   └── acme_api/                     # Custom connector for ACME Corp API
│       ├── __init__.py
│       ├── connector.py
│       ├── service.py
│       └── schemas.py
│
└── tasks/
    ├── __init__.py
    ├── settings.py                   # KAFKA_CONNECTOR, ACME_CONNECTOR, CRM_SERVICE
    ├── constants.py                  # OrderStatus, BATCH_SIZE
    ├── schemas.py                    # BasePayload, UserPayload
    ├── exceptions.py                 # ValidationError
    ├── helpers.py                    # generate_idempotency_key
    │
    ├── order_ingestion/              # Consumes raw orders from Kafka
    │   ├── __init__.py
    │   ├── task.py                   # AsyncConsumerTask
    │   ├── config.py
    │   ├── schemas.py
    │   ├── exceptions.py
    │   └── helpers.py
    │
    ├── order_enrichment/             # Enriches orders with CRM data
    │   ├── __init__.py
    │   ├── task.py                   # AsyncHybridTask (consumes + produces)
    │   ├── config.py
    │   ├── schemas.py
    │   ├── exceptions.py
    │   └── helpers.py
    │
    └── order_dispatch/               # Sends orders to ACME API
        ├── __init__.py
        ├── task.py                   # AsyncProducerTask
        ├── config.py
        ├── schemas.py
        ├── exceptions.py
        └── helpers.py
```

---

## Quick Reference

### Adding a New Task

1. Create the folder: `tasks/my_new_task/`
2. Create the files: `__init__.py`, `task.py`, `config.py`, `schemas.py`, `exceptions.py`, `helpers.py`
3. Pick your task class (`AsyncConsumerTask`, `HybridTask`, etc.) and implement the required methods
4. Wire it up in `config.py` with connectors, services, and policies
5. Register with `task_manager.register(TASK_CONFIG)`

### Adding a Custom Connector

1. Create the folder: `connectors/my_connector/`
2. Create `service.py` with your protocol implementation
3. Create `connector.py` wrapping the service with `fetch_transactions()` / `dispatch_transactions()`
4. Add a `ConnectorConfig` in `tasks/settings.py`

### Using a Plugin Connector

1. Install: `pip install ergon-kafka`
2. Import and configure in `tasks/settings.py`
3. Reference in your task's `config.py`

---

## Why This Structure Works

| Principle | How It's Enforced |
|-----------|-------------------|
| **Separation of Concerns** | Tasks, connectors, and shared modules live in separate directories |
| **DRY** | Global modules prevent copy-paste across tasks |
| **Single Responsibility** | Each file does one thing (schemas, exceptions, helpers) |
| **Testability** | Pure functions in helpers, isolated business logic in tasks |
| **Discoverability** | Consistent naming—you always know where to look |
| **Scalability** | Adding a new task is just copying the pattern |

The structure might feel rigid at first, but that's the point. When your project grows from 3 tasks to 30, you'll be glad everything has a home.
