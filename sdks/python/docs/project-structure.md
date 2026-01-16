# Project Structure Guide (Python SDK)

So you've decided to build with Ergon—great choice. But where do you put things? How do you keep a growing codebase from turning into spaghetti? This guide walks you through the recommended project layout that keeps your code clean, testable, and scalable.

The structure isn't arbitrary. Every directory and file has a clear purpose, and once you internalize the patterns, adding new tasks or connectors becomes almost mechanical.

---

## The Big Picture

An Ergon Python project has three main areas:

```
my_project/
├── main.py                    # Your single entry point
├── _observability/            # Telemetry infrastructure (Grafana, Prometheus, etc.)
├── connectors/                # Custom connectors when plugins don't cut it
│   └── {connector_name}/      # One folder per connector
├── services/                  # Custom services when plugins don't cut it
│   └── {service_name}/        # One folder per service
└── tasks/                     # Where the magic happens
    ├── {task_name}/           # One folder per task
    └── [shared modules]       # Stuff used across all tasks
```

The philosophy is simple:

- **Tasks** = pure business logic (no I/O, no protocol noise)
- **Connectors** = the bridge to external systems
- **Services** = protocols for external APIs
- **Settings** = wiring (telemetry, connectors, services)
- **Shared modules** = don't repeat yourself

**Separation of concerns:**

- **Tasks** = business logic only. They reference connectors and services from `settings`, but never touch environment variables directly.
- **Connectors** = I/O layer. They handle fetching and dispatching transactions.
- **Services** = protocol layer. They handle HTTP calls, authentication, retries, etc.
- **Settings** = configuration root. All telemetry, connectors, and services are configured here once.

---

## Entry Point: `main.py`

Every Ergon project has exactly one entry point. It is minimal and focused:

```python
from ergon.cli import ergon
from my_project.tasks import TASKS

def main():
    ergon(TASKS)

if __name__ == "__main__":
    main()
```

**Key properties:**

- Single entry point
- CLI invoked via `ergon(TASKS)`
- Task registration is explicit
- No auto-discovery
- No environment loading here—that happens in `tasks/__init__.py`

The CLI does not scan the filesystem. It only has access to tasks that are explicitly registered before commands execute.

---

## Connectors: Your Gateway to the World

### When Do You Need a Custom Connector?

Most of the time, you don't. Ergon ships with **Excel** and **RabbitMQ** connectors built-in—ready to use out of the box.

For other transports, you have two options:

```bash
# Install additional connector plugins
pip install ergon-kafka
pip install ergon-postgres
```

Or build a custom connector directly in your project without publishing. This is perfect for proprietary internal APIs, weird legacy systems, or third-party services with no existing plugin.

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

### Built-in, Plugin, or Custom?

| Situation | What to Do |
|-----------|------------|
| Excel files, RabbitMQ | Use the built-in connectors. They come with Ergon. |
| Kafka, S3, PostgreSQL, etc. | Install a plugin via pip/uv. |
| Internal company API | Build a custom connector directly in your project. |
| Third-party API with no plugin | Build a custom connector directly in your project. |
| Existing plugin doesn't quite fit | Fork it or build custom. |

---

## Services: Protocol Layer for External APIs

### When Do You Need a Custom Service?

Services handle protocol-level interactions with external APIs. They're similar to connectors but are used for enrichment, data transformation, or API calls that don't fit the transaction model.

### The Service Folder Structure

```
services/
├── __init__.py
└── crm_service/               # Name it after the system
    ├── __init__.py
    ├── service.py             # The service interface
    ├── client.py              # The protocol client
    ├── schemas.py             # Pydantic models (optional)
    └── exceptions.py          # Custom errors (optional)
```

### What Goes Where?

| File | What It Does |
|------|--------------|
| `client.py` | Knows *how* to talk to the external API. HTTP calls, authentication, retries—all the protocol mechanics. |
| `service.py` | Wraps the client and exposes business-level methods. This is what your tasks interact with. |
| `schemas.py` | Pydantic models for validating requests and responses. |
| `exceptions.py` | Custom exception classes specific to the service. |

### Built-in, Plugin, or Custom?

| Situation | What to Do |
|-----------|------------|
| OpenAI, common APIs | Install a plugin: `pip install ergon-service-openai` |
| Internal company API | Build a custom service directly in your project. |
| Third-party API with no plugin | Build a custom service directly in your project. |

---

## Tasks: Where Your Logic Lives

The `tasks/` folder is the heart of your project. It contains two things:

1. **Shared modules** at the root—utilities, schemas, and constants used across all tasks
2. **Per-task folders**—isolated homes for each task's implementation

```
tasks/
├── __init__.py             # Task registration + settings.load_env()
├── settings.py             # All your connectors, services, telemetry configs
├── constants.py            # Enums, magic values, thresholds
├── schemas.py              # Pydantic models shared across tasks
├── exceptions.py           # Exception classes shared across tasks
├── helpers.py              # Utility functions shared across tasks
│
├── document_search/        # One task
│   ├── task.py
│   ├── config.py
│   └── ...
│
└── data_extraction/       # Another task
    ├── task.py
    ├── config.py
    └── ...
```

### `tasks/__init__.py` — Task Registration

This file follows a specific pattern that ensures environment variables are loaded before task configurations are evaluated:

```python
from . import settings
from .document_search.config import TASK_DOCUMENT_SEARCH
from .data_extraction.config import TASK_DATA_EXTRACTION
from .xml_generation.config import TASK_XML_GENERATION
from .initial_routing.config import TASK_INITIAL_ROUTING

settings.load_env()

TASKS = [
    TASK_DOCUMENT_SEARCH,
    TASK_DATA_EXTRACTION,
    TASK_XML_GENERATION,
    TASK_INITIAL_ROUTING,
]

__all__ = [
    "settings",
    "TASK_DOCUMENT_SEARCH",
    "TASK_DATA_EXTRACTION",
    "TASK_XML_GENERATION",
    "TASK_INITIAL_ROUTING",
    "TASKS",
]
```

**Why this order matters:**

1. **Settings imported first** — Makes `settings` module available
2. **Task configs imported** — Each config module is evaluated (may reference `settings`)
3. **Environment loaded** — `settings.load_env()` makes environment variables available
4. **Tasks listed** — Explicit list of registered tasks

This ensures that when task configuration modules are evaluated, all environment variables are already loaded and available via `os.getenv()` in `settings.py`.

---

## Shared Modules (The `tasks/` Root)

These files prevent you from writing the same code in every task.

### `settings.py` — The Control Center

This file is the single control center for:
- Telemetry (logging, tracing, metrics)
- Connectors
- Services
- Environment loading

**Important characteristics:**
- `load_env()` is called once at the top
- Telemetry is env-driven and optional
- No branching on dev / prod
- Uses OpenTelemetry OTLP exporters
- Tasks never read environment variables directly

Here's a complete example based on production patterns:

```python
"""
Ergon Task Framework Settings

This module defines global configuration for:
- Logging
- Tracing
- Metrics
- Connectors
- Services

It is used to configure the framework telemetry, connectors, and services.
"""

import os

from ergon import connector, service, telemetry
from ergon.utils import load_env
from ergon.connector.rabbitmq import RabbitMQConnector, RabbitMQClient
from ergon_service_openai.service import OpenAIClient, OpenAIService

# Import custom services from your project
from my_project.services.crm_service.service import CrmClient, CrmService
from my_project.services.billing_service.service import BillingClient, BillingService

load_env()

# ----------------------------------------
# OTEL Resource (applies to all telemetry)
# ----------------------------------------
OTEL_RESOURCE = {
    "service.name": "my-project",
    "service.version": "0.1.0",
}

# ----------------------------------------
# --- LOGGING CONFIGURATION ---
# ----------------------------------------
LOGGING = telemetry.logging.LoggingConfig()
LOGGING.level = "DEBUG"

LOGGING.formatters = [
    telemetry.logging.LogFormatter(
        name="default",
        fmt="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    )
]

LOGGING.handlers.append(
    telemetry.logging.ConsoleLogHandler(
        level="DEBUG",
        stream="stdout",
        formatter="default",
    )
)

if os.getenv("OTEL_LOGS_ENABLED") == "true":
    LOGGING.handlers.append(
        telemetry.logging.OTLPLogHandler(
            level="DEBUG",
            resource=OTEL_RESOURCE,
            formatter="default",
            processors=[
                telemetry.logging.LogProcessor(
                    processor=telemetry.logging.BatchLogRecordProcessor,
                    exporters=[
                        telemetry.logging.OTLPLogExporter(
                            endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
                            insecure=True,
                        ),
                    ],
                ),
            ],
        )
    )

# ----------------------------------------
# --- TRACING CONFIGURATION ---
# ----------------------------------------
if os.getenv("OTEL_TRACES_ENABLED") == "true":
    TRACING = telemetry.tracing.TracingConfig()
    TRACING.resource = OTEL_RESOURCE
    TRACING.processors.append(
        telemetry.tracing.SpanProcessor(
            processor=telemetry.tracing.BatchSpanProcessor,
            exporters=[
                telemetry.tracing.OTLPSpanExporter(
                    endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
                    insecure=True,
                )
            ],
        )
    )
else:
    TRACING = None

# ----------------------------------------
# --- METRICS CONFIGURATION ---
# ----------------------------------------
if os.getenv("OTEL_METRICS_ENABLED") == "true":
    METRICS = telemetry.metrics.MetricsConfig()
    METRICS.resource = OTEL_RESOURCE
    METRICS.readers.append(
        telemetry.metrics.MetricReader(
            reader=telemetry.metrics.PeriodicExportingMetricReader,
            config={"export_interval_millis": 5000},
            exporters=[
                telemetry.metrics.OTLPMetricExporter(
                    endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
                    insecure=True,
                ),
            ],
        ),
    )
else:
    METRICS = None

# ----------------------------------------
# --- CONNECTOR CONFIGURATION ---
# ----------------------------------------
RABBITMQ_CLIENT = RabbitMQClient(
    host=os.getenv("RABBITMQ_HOST"),
    port=int(os.getenv("RABBITMQ_PORT", "5672")),
    username=os.getenv("RABBITMQ_USERNAME"),
    password=os.getenv("RABBITMQ_PASSWORD"),
)

RABBITMQ_CONNECTOR = connector.ConnectorConfig(
    connector=RabbitMQConnector,
    kwargs={"client": RABBITMQ_CLIENT},
)

# ----------------------------------------
# --- SERVICE CONFIGURATION ---
# ----------------------------------------

# OpenAI Service
OPENAI_CLIENT = OpenAIClient(
    api_key=os.getenv("OPENAI_API_KEY"),
    model=os.getenv("OPENAI_MODEL", "gpt-4"),
)

OPENAI_SERVICE = service.ServiceConfig(
    service=OpenAIService,
    kwargs={"client": OPENAI_CLIENT},
)

# CRM Service (custom)
CRM_CLIENT = CrmClient(
    api_key=os.getenv("CRM_API_KEY"),
    base_url=os.getenv("CRM_BASE_URL"),
)

CRM_SERVICE = service.ServiceConfig(
    service=CrmService,
    kwargs={"client": CRM_CLIENT},
)

# Billing Service (custom)
BILLING_CLIENT = BillingClient(
    username=os.getenv("BILLING_USERNAME"),
    password=os.getenv("BILLING_PASSWORD"),
)

BILLING_SERVICE = service.ServiceConfig(
    service=BillingService,
    kwargs={"client": BILLING_CLIENT},
)
```

**Telemetry is controlled via environment variables:**

```bash
OTEL_LOGS_ENABLED=true
OTEL_TRACES_ENABLED=true
OTEL_METRICS_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=localhost:4317
```

**Why this matters:** Your tasks never touch environment variables directly. They just reference `settings.RABBITMQ_CONNECTOR` or `settings.OPENAI_SERVICE`. When you need to swap RabbitMQ for Kafka (or Excel, or a custom connector), you change one file—not twenty. Telemetry is optional and controlled by environment flags, making it easy to enable/disable observability without code changes.

---

### `constants.py` — Configuration and Constants

This file contains retry policies, configuration objects, and business constants. It may reference schemas and environment variables.

```python
import os

from ergon.task.policies import RetryPolicy
from . import schemas

# ----------------------------------------
# --- RETRY POLICY CONFIGURATION ---
# ----------------------------------------
def default_retry_policy():
    return RetryPolicy(
        max_attempts=3,
        backoff=1,
        backoff_multiplier=2,
        backoff_cap=10,
    )

# ----------------------------------------
# --- EXTERNAL SYSTEM CONFIGURATION ---
# ----------------------------------------

# Example: Workflow phases configuration
WORKFLOW_PHASES = schemas.WorkflowPhases(
    initial_phase=schemas.WorkflowPhase(
        name="Initial Phase",
        id=os.getenv("WORKFLOW_INITIAL_PHASE_ID", "123456"),
    ),
    processing_phase=schemas.WorkflowPhase(
        name="Processing Phase",
        id=os.getenv("WORKFLOW_PROCESSING_PHASE_ID", "123457"),
    ),
    completed_phase=schemas.WorkflowPhase(
        name="Completed Phase",
        id=os.getenv("WORKFLOW_COMPLETED_PHASE_ID", "123458"),
    ),
)

# Example: Field mappings
WORKFLOW_FIELDS = schemas.WorkflowFields(
    document_id=schemas.WorkflowField(
        name="Document ID",
        id="document_id",
    ),
    status=schemas.WorkflowField(
        name="Status",
        id="status",
    ),
)

# Organization/System IDs
EXTERNAL_SYSTEM_ORG_ID = os.getenv("EXTERNAL_SYSTEM_ORG_ID")
```

**Why this matters:** Retry policies, external system configurations, and field mappings are defined once and reused across tasks. When configuration needs to change, you update it in one place.

---

### `schemas.py` — Shared Data Models

Pydantic models that multiple tasks use. This includes configuration schemas (for external systems), shared payload models, and output schemas.

```python
from typing import Any, Optional
from pydantic import BaseModel

# ----------------------------------------
# External System Configuration Schemas
# ----------------------------------------
class WorkflowPhase(BaseModel):
    name: str
    id: str

class WorkflowField(BaseModel):
    name: str
    id: str

class WorkflowPhases(BaseModel):
    initial_phase: WorkflowPhase
    processing_phase: WorkflowPhase
    completed_phase: WorkflowPhase

class WorkflowFields(BaseModel):
    document_id: WorkflowField
    status: WorkflowField

# ----------------------------------------
# Shared Transaction Payloads
# ----------------------------------------
class BasePayload(BaseModel):
    """Every transaction payload inherits from this."""
    source: str
    created_at: Optional[str] = None

class DocumentPayload(BasePayload):
    document_id: str
    document_type: str
    content: dict

# ----------------------------------------
# Process Transaction Output
# ----------------------------------------
class ProcessTransactionOutput(BaseModel):
    destination_phase: WorkflowPhase
    data: Optional[Any] = None
```

**Why this matters:** Configuration schemas (like `WorkflowPhases`, `WorkflowFields`) are used in `constants.py` to define external system mappings. Shared payload models ensure consistency across tasks. Output schemas standardize task return values.

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

This file defines how your task runs: which connectors, which services, what policies. It references configurations from `settings.py`.

```python
from ergon.task import TaskConfig
from .. import settings
from .task import DocumentSearchTask

# Define the task configuration
TASK_DOCUMENT_SEARCH = TaskConfig(
    task=DocumentSearchTask,
    name="document-search",
    connectors={"input": settings.RABBITMQ_CONNECTOR},
    services={"openai": settings.OPENAI_SERVICE},
    policies=[...],  # Policy configuration
    logging=settings.LOGGING,
    tracing=settings.TRACING,
    metrics=settings.METRICS,
)
```

**Why this is separate from `task.py`:**

- **Configuration vs. Logic** — Configuration changes shouldn't require touching business logic
- **Single Source of Truth** — Connectors and services are defined once in `settings.py`
- **Environment-Agnostic Tasks** — Tasks don't know about environment variables
- **Easy Testing** — Mock `settings` instead of mocking environment variables
- **Clear Boundaries** — Need to bump concurrency? Edit `config.py`. Business rules changed? Edit `task.py`

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
├── services/
│   └── crm_service/                  # Custom service for CRM API
│       ├── __init__.py
│       ├── service.py
│       └── client.py
│
└── tasks/
    ├── __init__.py
    ├── settings.py                   # Telemetry, RABBITMQ_CONNECTOR, OPENAI_SERVICE
    ├── constants.py                  # Retry policies, workflow phases
    ├── schemas.py                    # WorkflowPhases, BasePayload, ProcessTransactionOutput
    ├── exceptions.py                 # ValidationError
    ├── helpers.py                    # generate_idempotency_key
    │
    ├── order_ingestion/              # Consumes raw orders from RabbitMQ
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
5. Register with `manager.register(TASK_CONFIG)`

### Adding a Custom Connector

1. Create the folder: `connectors/my_connector/`
2. Create `service.py` with your protocol implementation
3. Create `connector.py` wrapping the service with `fetch_transactions()` / `dispatch_transactions()`
4. Add a `ConnectorConfig` in `tasks/settings.py`

### Using a Plugin Connector

1. Install: `pip install ergon-kafka` (or use uv)
2. Import and configure in `tasks/settings.py`
3. Reference in your task's `config.py`

### Using Built-in Connectors

1. Import directly: `from ergon_framework.connector import RabbitMQConnector, ExcelConnector`
2. Configure in `tasks/settings.py`
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

<br/>


