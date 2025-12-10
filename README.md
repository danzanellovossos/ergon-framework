# Ergon Framework

<p align="left">
  <img src="assets/logo.png" alt="Ergon Framework Logo" width="400">
</p>

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

**A transaction-first, observability-native execution engine for building high-throughput automation pipelines in Python.**

---

## Overview

Ergon is a framework designed to solve the "maturity gap" in Python background processing. While tools like Celery are great for simple job queues and raw `asyncio` scripts are flexible but unstructured, Ergon provides a **rigorous architectural foundation** for mission-critical workloads.

The framework enforces a strict **layered architecture** where business logic (`Tasks`) is completely isolated from transport mechanics (`Connectors` & `Services`). This ensures your automation code remains deterministic, testable, and portable—whether you're consuming from RabbitMQ, reading from S3, or streaming from Redis.

---

## Architecture

Ergon follows a **Transaction-First** philosophy. The system is organized into concentric layers, ensuring dependencies flow inward and infrastructure concerns never leak into domain logic.

![System Architecture](assets/system-infographic.png)

### Architectural Layers

| Layer                 | Components                              | Responsibility                              |
| --------------------- | --------------------------------------- | ------------------------------------------- |
| **Core**        | `Transaction`                         | The atomic, immutable unit of work          |
| **Domain**      | `Task`, `Mixins`                    | Pure business logic—*what* to do         |
| **Integration** | `Connector`, `Service`              | Transport boundary—*where* and *how*   |
| **Platform**    | `Runner`, `Telemetry`, `Policies` | Orchestration, observability, configuration |

### The Transaction

Everything revolves around the **Transaction**—an immutable, atomic unit of work:

```python
from ergon_framework.connector import Transaction

# Transactions are created by Connectors when fetching data
tx = Transaction(
    id="tx-001",                           # Unique identifier for tracing
    payload={"order_id": 123, "amount": 99.99},  # The actual data
    metadata={"source": "rabbitmq", "queue": "orders"}  # Contextual info
)
```

Transactions are **frozen** (immutable) once created. This guarantees:

- **Thread safety**: Share across workers without locks
- **Auditability**: What you received is what you process
- **Atomicity**: Either the entire transaction is handled, or it is not

---

## Why Ergon?

| Concern                     | Traditional Approach                    | Ergon Approach                               |
| --------------------------- | --------------------------------------- | -------------------------------------------- |
| **Business Logic**    | Mixed with I/O, retries, error handling | Isolated in Tasks—pure and testable         |
| **Retries**           | Ad-hoc, inconsistent across codebase    | Centralized in Policies—consistent behavior |
| **Observability**     | Manual instrumentation, often forgotten | Automatic—every transaction is traced       |
| **Transport Changes** | Requires business logic refactoring     | Swap Connector/Service, logic unchanged      |
| **Testing**           | Requires mocks, integration tests       | Unit tests with plain Transaction objects    |
| **Scaling**           | Custom implementation per project       | Built-in sync/async, single/multi-process    |
| **Error Handling**    | Try/except everywhere                   | Structured lifecycle with dedicated handlers |

---

## Installation

```bash
pip install ergon-framework
```

Or with `uv`:

```bash
uv add ergon-framework
```

---

## Quick Start

### 1. Define a Consumer Task

Create a task by inheriting from `ConsumerTask` and implementing `process_transaction`:

```python
from typing import Any
from ergon_framework.task.mixins import ConsumerTask
from ergon_framework.connector import Transaction

class OrderProcessor(ConsumerTask):
    """Consumes order transactions and processes them."""

    def process_transaction(self, transaction: Transaction) -> Any:
        # Pure business logic—no I/O, no retries, no protocol details
        order = transaction.payload
        total = order["quantity"] * order["unit_price"]
        return {"order_id": order["id"], "total": total, "status": "processed"}

    def handle_process_success(self, transaction: Transaction, result: Any):
        # Called after successful processing
        self.output_connector.dispatch_transactions([
            Transaction(id=f"out-{transaction.id}", payload=result, metadata={})
        ])

    def handle_process_exception(self, transaction: Transaction, exc: Exception):
        # Called when processing fails (after all retries exhausted)
        self.dlq_connector.dispatch_transactions([transaction])
```

### 2. Create a Connector

Connectors bridge external systems to the transaction interface:

```python
from typing import List
from ergon_framework.connector import Connector, Transaction
import uuid

class RabbitMQConnector(Connector):
    def __init__(self, queue_name: str, connection_url: str):
        self.queue_name = queue_name
        # Initialize your RabbitMQ client here
        self.client = create_rabbitmq_client(connection_url)

    def fetch_transactions(self, batch_size: int, **kwargs) -> List[Transaction]:
        messages = self.client.consume(self.queue_name, max_messages=batch_size)
        return [
            Transaction(
                id=str(uuid.uuid4()),
                payload=msg.body,
                metadata={"delivery_tag": msg.delivery_tag, "routing_key": msg.routing_key}
            )
            for msg in messages
        ]

    def dispatch_transactions(self, transactions: List[Transaction], **kwargs):
        for tx in transactions:
            self.client.publish(self.queue_name, tx.payload)
```

### 3. Configure and Run

Use `TaskConfig` to wire everything together:

```python
from ergon_framework.task import TaskConfig, runner, policies
from ergon_framework.connector import ConnectorConfig

# Configure the consumer policy
consumer_policy = policies.ConsumerPolicy()
consumer_policy.name = "consumer"
consumer_policy.loop.concurrency.value = 5      # Process 5 transactions in parallel
consumer_policy.loop.batch.size = 10            # Fetch 10 at a time
consumer_policy.loop.streaming = True           # Keep polling for new messages
consumer_policy.process.retry.max_attempts = 3  # Retry failed processing 3 times
consumer_policy.process.retry.backoff = 1.0     # Start with 1s backoff
consumer_policy.process.retry.backoff_multiplier = 2.0  # Double each retry

# Create the task configuration
config = TaskConfig(
    name="order-processor",
    task=OrderProcessor,
    max_workers=1,
    connectors={
        "input": ConnectorConfig(
            connector=RabbitMQConnector,
            kwargs={"queue_name": "orders", "connection_url": "amqp://localhost"}
        ),
        "output": ConnectorConfig(
            connector=RabbitMQConnector,
            kwargs={"queue_name": "processed-orders", "connection_url": "amqp://localhost"}
        ),
    },
    policies=[consumer_policy],
)

# Run the task
if __name__ == "__main__":
    runner.run(config)
```

The framework handles the loop, retry logic, telemetry, and graceful shutdown automatically.

---

## Core Concepts

### Tasks & Mixins

Tasks are **thin** orchestration units containing only business logic. Behavior is added via mixins:

| Mixin             | Purpose                        | Key Methods                                                                             |
| ----------------- | ------------------------------ | --------------------------------------------------------------------------------------- |
| `ConsumerMixin` | Inbound transaction processing | `process_transaction()`, `handle_process_success()`, `handle_process_exception()` |
| `ProducerMixin` | Outbound transaction dispatch  | `prepare_transaction()`, `handle_prepare_success()`, `handle_prepare_exception()` |

**Ready-to-use task classes:**

```python
from ergon_framework.task.mixins import (
    ConsumerTask,      # Sync consumer
    ProducerTask,      # Sync producer
    HybridTask,        # Sync consumer + producer
    AsyncConsumerTask, # Async consumer
    AsyncProducerTask, # Async producer
    AsyncHybridTask,   # Async consumer + producer
)
```

### Connectors & Services

**Services** handle protocol mechanics (HTTP, AMQP, gRPC). They own:

- Connection management, authentication, retries
- Pagination, streaming, batching
- Error translation and resilience

**Connectors** wrap Services to expose the transaction interface:

- `fetch_transactions()` → Pull data, wrap in `Transaction` objects
- `dispatch_transactions()` → Extract payloads, send via Service

```
External System  ←→  Service (protocol)  ←→  Connector (transactions)  ←→  Task (logic)
```

**When to use which:**

- **Service only**: Enrichment APIs (OpenAI, geocoding, database lookups)—inject directly into tasks
- **Connector + Service**: Sources/sinks of your pipeline (queues, files, streams)

### Policies

Policies provide fine-grained control over execution without code changes:

```python
from ergon_framework.task import policies

# Consumer policy with full configuration
policy = policies.ConsumerPolicy()
policy.name = "my-consumer"

# Loop behavior
policy.loop.concurrency.value = 10      # Parallel transaction processing
policy.loop.batch.size = 50             # Transactions per fetch
policy.loop.streaming = True            # Continuous polling mode
policy.loop.timeout = 3600              # Max loop duration (seconds)
policy.loop.limit = 1000                # Max transactions to process

# Empty queue behavior (for streaming mode)
policy.loop.empty_queue.backoff = 1.0
policy.loop.empty_queue.backoff_multiplier = 2.0
policy.loop.empty_queue.backoff_cap = 30.0

# Per-step retry configuration
policy.fetch.retry.max_attempts = 5
policy.fetch.retry.timeout = 30

policy.process.retry.max_attempts = 3
policy.process.retry.backoff = 1.0
policy.process.retry.backoff_multiplier = 2.0
policy.process.retry.backoff_cap = 60.0

policy.success.retry.max_attempts = 2
policy.exception.retry.max_attempts = 2
```

---

## Dependency Injection

Tasks receive all dependencies automatically via the `TaskMeta` metaclass:

```python
class EnrichmentTask(ConsumerTask):
    def process_transaction(self, transaction: Transaction) -> Any:
        # Access connectors as self.{name}_connector
        count = self.input_connector.get_transactions_count()
      
        # Access services as self.{name}_service
        enriched = self.openai_service.complete(transaction.payload["text"])
      
        # Access policies as self.{name}_policy
        timeout = self.consumer_policy.loop.timeout
      
        return {"enriched": enriched, "pending": count}
```

Configure services in `TaskConfig`:

```python
from ergon_framework.connector import ServiceConfig

config = TaskConfig(
    name="enrichment-task",
    task=EnrichmentTask,
    connectors={"input": input_connector_config},
    services={
        "openai": ServiceConfig(
            service=OpenAIService,
            kwargs={"api_key": "sk-...", "model": "gpt-4"}
        )
    },
    policies=[consumer_policy],
)
```

---

## Observability

Ergon integrates **OpenTelemetry** natively. Every task execution automatically generates:

### Logging

- Structured JSON logging via `python-json-logger`
- Automatic `trace_id` and `span_id` injection
- Multiple handlers: Console, File, RotatingFile, OTLP

### Tracing

- Hierarchical spans: Task → Batch → Transaction → Attempt
- Full context propagation across async boundaries
- Export to Jaeger, Tempo, or any OTLP-compatible backend

### Metrics

- Push-based via `PeriodicExportingMetricReader`
- Automatic resource attributes (task name, host, PID, execution ID)
- Export to Prometheus, OTLP, or console

```python
from ergon_framework.telemetry import logging, tracing, metrics

config = TaskConfig(
    name="observed-task",
    task=MyTask,
    connectors={...},
    logging=logging.LoggingConfig(
        level="INFO",
        handlers=[
            logging.ConsoleLogHandler(),
            logging.OTLPLogHandler(endpoint="http://collector:4317"),
        ]
    ),
    tracing=tracing.TracingConfig(
        processors=[
            tracing.SpanProcessor(
                processor=tracing.BatchSpanProcessor,
                exporters=[tracing.OTLPSpanExporter(endpoint="http://collector:4317")]
            )
        ]
    ),
    metrics=metrics.MetricsConfig(
        readers=[
            metrics.MetricReader(
                reader=metrics.PeriodicExportingMetricReader,
                exporters=[metrics.OTLPMetricExporter(endpoint="http://collector:4317")]
            )
        ]
    ),
)
```

---

## Concurrency Models

### Synchronous Execution

Best for CPU-bound processing or legacy libraries:

```python
from ergon_framework.task.mixins import ConsumerTask

class SyncProcessor(ConsumerTask):
    def process_transaction(self, transaction):
        # Runs in ThreadPoolExecutor
        return heavy_computation(transaction.payload)
```

- Concurrency controlled by `policy.loop.concurrency.value`
- Each transaction processed in its own thread
- Multi-process scaling via `max_workers > 1`

### Asynchronous Execution

Best for I/O-bound workloads with high concurrency:

```python
from ergon_framework.task.mixins import AsyncConsumerTask

class AsyncProcessor(AsyncConsumerTask):
    async def process_transaction(self, transaction):
        # Runs in asyncio event loop
        result = await self.http_client.post(transaction.payload)
        return result
```

- Concurrency controlled by `asyncio.Semaphore`
- Thousands of concurrent operations with minimal overhead
- Perfect for API calls, database queries, network I/O

---

## Scaling

| Axis                | Mechanism                    | Configuration                              |
| ------------------- | ---------------------------- | ------------------------------------------ |
| **Process**   | `ProcessPoolExecutor`      | `TaskConfig.max_workers`                 |
| **Thread**    | `ThreadPoolExecutor`       | `policy.loop.concurrency.value`          |
| **Async**     | `asyncio.Semaphore`        | `policy.loop.concurrency.value`          |
| **Connector** | External system partitioning | Service-specific (consumer groups, shards) |

```python
# Multi-process scaling (sync tasks only)
config = TaskConfig(
    name="scaled-task",
    task=MyTask,
    max_workers=4,  # Spawn 4 isolated worker processes
    connectors={...},
)
```

---

## Project Structure

After scaffolding with `ergon init`:

```
my-project/
├── connectors/
│   └── __init__.py
├── tasks/
│   ├── __init__.py
│   ├── settings.py
│   └── my_task/
│       ├── __init__.py
│       ├── config.py        # TaskConfig definition
│       ├── task.py          # Task implementation
│       ├── schemas.py       # Pydantic models
│       ├── helper.py        # Task-specific utilities
│       └── exceptions.py    # Custom exceptions
├── _observability/
│   ├── docker-compose.telemetry.yml
│   ├── grafana.yaml
│   ├── loki.yaml
│   ├── otel-collector-config.yaml
│   ├── prometheus.yaml
│   └── tempo.yaml
└── main.py
```

---

## Documentation

Deep dive into the core modules:

- **[Transaction Abstraction](docs/modules/1.transaction.md)** — Understanding atomicity rules
- **[Task Module](docs/modules/2.task.md)** — Mixins, lifecycles, and execution modes
- **[Connector Module](docs/modules/3.connector.md)** — Building integration boundaries
- **[Service Module](docs/modules/4.service.md)** — Protocol engineering and reliability
- **[Telemetry Module](docs/modules/5.telemetry.md)** — Configuring OTel logs, metrics, and traces
- **[Architecture Guide](docs/architecture.md)** — Full system specification

---

## License

This project is licensed under the [MIT License](LICENSE).
