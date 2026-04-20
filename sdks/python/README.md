# Ergon Framework — Python SDK

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

**The official Python implementation of the Ergon Framework—a transaction-first, observability-native execution engine for building high-throughput automation pipelines.**

---

## Installation

```bash
pip install ergon-framework-python
```

For local development, install in editable mode:

```bash
pip install -e /path/to/ergon-framework/sdks/python
```

📖 **[Full Getting Started Guide](https://github.com/danzanellovossos/ergon-framework/blob/main/sdks/python/docs/getting-started.md)** — Complete setup, project configuration, and task registration

---

## Quick Start

### 1. Define a Consumer Task

Create a task by inheriting from `ConsumerTask` and implementing `process_transaction`:

```python
from typing import Any
from ergon.task.mixins import ConsumerTask
from ergon.connector import Transaction

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
from ergon.connector import Connector, Transaction
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
from ergon.task import TaskConfig, runner, policies
from ergon.connector import ConnectorConfig

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

## The Transaction Object

In the Python SDK, transactions are implemented as immutable Pydantic models:

```python
from ergon.connector import Transaction

# Transactions are created by Connectors when fetching data
tx = Transaction(
    id="tx-001",                           # Unique identifier for tracing
    payload={"order_id": 123, "amount": 99.99},  # The actual data
    metadata={"source": "rabbitmq", "queue": "orders"}  # Contextual info
)
```

Transactions are **frozen** (immutable) once created via Pydantic's `frozen=True` configuration.

---

## Task Classes

The Python SDK provides ready-to-use task classes:

```python
from ergon.task.mixins import (
    ConsumerTask,      # Sync consumer
    ProducerTask,      # Sync producer
    HybridTask,        # Sync consumer + producer
    AsyncConsumerTask, # Async consumer
    AsyncProducerTask, # Async producer
    AsyncHybridTask,   # Async consumer + producer
)
```

### Synchronous Tasks

Best for CPU-bound processing or legacy libraries:

```python
from ergon.task.mixins import ConsumerTask

class SyncProcessor(ConsumerTask):
    def process_transaction(self, transaction):
        # Runs in ThreadPoolExecutor
        return heavy_computation(transaction.payload)
```

- Concurrency controlled by `policy.loop.concurrency.value`
- Each transaction processed in its own thread
- Multi-process scaling via `max_workers > 1`

### Asynchronous Tasks

Best for I/O-bound workloads with high concurrency:

```python
from ergon.task.mixins import AsyncConsumerTask

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
from ergon.connector import ServiceConfig

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

## Policy Configuration

Policies are created as instances and configured by setting properties directly:

```python
from ergon.task import policies

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

## Observability

The Python SDK integrates **OpenTelemetry** natively:

```python
from ergon.telemetry import logging, tracing, metrics

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
├── main.py                  # Application entry point
├── _observability/          # Telemetry infrastructure configs
├── connectors/              # Custom connectors and services
│   └── {connector_name}/    # One submodule per connector
│       ├── connector.py     # Transaction interface
│       └── service.py       # Protocol mechanics
└── tasks/                   # Task definitions and shared modules
    ├── settings.py          # Global configs (connectors, services, telemetry)
    ├── constants.py         # Global constants and enums
    ├── schemas.py           # Shared Pydantic models
    ├── exceptions.py        # Shared exception classes
    ├── helpers.py           # Shared utility functions
    └── {task_name}/         # Per-task submodule
        ├── task.py          # Task implementation
        ├── config.py        # TaskConfig definition
        ├── schemas.py       # Task-specific models
        ├── exceptions.py    # Task-specific exceptions
        └── helpers.py       # Task-specific utilities
```

📖 **[Full Project Structure Guide](https://github.com/danzanellovossos/ergon-framework/blob/main/sdks/python/docs/project-structure.md)** — Detailed documentation on organizing connectors, tasks, and shared modules.

---

## Documentation

### Python SDK Guides

- **[Getting Started](https://github.com/danzanellovossos/ergon-framework/blob/main/sdks/python/docs/getting-started.md)** — Installation, project setup, and running your first task
- **[CLI Reference](https://github.com/danzanellovossos/ergon-framework/blob/main/sdks/python/docs/cli.md)** — Commands, options, and exit codes
- **[Project Structure Guide](https://github.com/danzanellovossos/ergon-framework/blob/main/sdks/python/docs/project-structure.md)** — How to organize connectors, tasks, and shared modules

### Framework Concepts

- **[Framework Architecture](https://github.com/danzanellovossos/ergon-framework/blob/main/docs/architecture.md)** — Full system specification and design philosophy
- **[Transaction Abstraction](https://github.com/danzanellovossos/ergon-framework/blob/main/docs/modules/1.transaction.md)** — Understanding atomicity rules
- **[Task Module](https://github.com/danzanellovossos/ergon-framework/blob/main/docs/modules/2.task.md)** — Mixins, lifecycles, and execution modes
- **[Connector Module](https://github.com/danzanellovossos/ergon-framework/blob/main/docs/modules/3.connector.md)** — Building integration boundaries
- **[Service Module](https://github.com/danzanellovossos/ergon-framework/blob/main/docs/modules/4.service.md)** — Protocol engineering and reliability
- **[Telemetry Module](https://github.com/danzanellovossos/ergon-framework/blob/main/docs/modules/5.telemetry.md)** — Configuring OTel logs, metrics, and traces

---

## License

This project is licensed under the [MIT License](https://github.com/danzanellovossos/ergon-framework/blob/main/LICENSE).

<br/>
