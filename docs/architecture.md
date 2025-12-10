# Ergon Framework Architecture

Ergon is a transaction-centric framework for building reliable, observable, and scalable data processing pipelines. The architecture enforces strict separation between business logic and infrastructure concerns, enabling teams to write testable domain code while the framework handles retries, timeouts, observability, and transport mechanics. The diagram below illustrates how the four architectural layers—Core, Domain, Integration, and Platform—work together to process atomic units of work called Transactions.

![System Architecture](../assets/system-infographic.png)

The diagram illustrates the layered architecture of the Ergon Framework. At its heart lies the **Transaction**—an immutable, atomic unit of work that flows through the system. Surrounding it, the **Task and Mixins** layer contains the business logic. The **Integration Layer** (Connectors and Services) bridges the gap between your domain code and the outside world. Finally, the **Platform and Infrastructure Layer** orchestrates execution, manages configuration, and provides observability across all components. This document explains each layer in depth, how they interact, and why this design leads to robust, maintainable, and scalable data pipelines.

---

## 1. Introduction

Building reliable data processing systems is hard. Developers often find themselves tangled in retry logic, connection management, error handling, and observability concerns—all mixed into the same codebase as their business logic. The result is code that is difficult to test, hard to maintain, and nearly impossible to extend without introducing regressions.

Ergon was designed to solve this problem by enforcing a strict architectural separation between **what** your code does (business logic) and **how** it interacts with the world (protocols, transports, retries, observability). This separation is not just a guideline—it is enforced by the framework's structure itself.

The framework is built around a single, powerful abstraction: the **Transaction**. Everything else—Tasks, Connectors, Services, Runners, Telemetry—exists to support the lifecycle of transactions. By centering the architecture on this atomic unit of work, Ergon achieves consistency, predictability, and extreme flexibility.

---

## 2. Core Layer: The Transaction

### What is a Transaction?

A **Transaction** is the fundamental unit of work in Ergon. It represents a single, atomic piece of data that must be processed indivisibly. This could be:

- A message from a queue (RabbitMQ, SQS, Kafka)
- A record from a file (CSV, JSON, Parquet)
- An event from a stream
- A payload from an API
- A batch of records that must be treated as a single unit

The key insight is that "atomic" does not mean "small." A transaction is whatever the external system considers a single, cohesive unit. If Kafka delivers a batch of 1,000 records as one message, that entire batch becomes one Transaction. The framework never splits or merges transactions—it respects the boundaries defined by the source system.

### Transaction Structure

Every Transaction in Ergon is a Pydantic model with three fields:

```python
class Transaction(BaseModel):
    id: str                          # Unique identifier for tracing
    payload: Any                     # The actual data content
    metadata: Dict[str, Any] = {}    # Contextual information
    model_config = ConfigDict(frozen=True)
```

- **`id`**: A unique identifier assigned when the transaction is created. This ID is used for distributed tracing, logging correlation, and debugging. Every log entry, metric, and span generated while processing this transaction will include this ID.
- **`payload`**: The actual data. This is intentionally typed as `Any` because different connectors produce different data types. A RabbitMQ connector might produce a dictionary, while a file connector might produce a string or bytes.
- **`metadata`**: Additional context that does not belong in the payload itself. This might include routing keys, headers, timestamps, file paths, line numbers, or any other information that helps with processing or debugging.

### Immutability Guarantee

The Transaction model is **frozen** (`frozen=True`). Once a transaction is created, it cannot be modified. This immutability provides several critical benefits:

1. **Thread Safety**: Immutable objects can be safely shared across threads without locks.
2. **Debugging**: You can always trust that the transaction you are looking at is exactly what was received.
3. **Atomicity Enforcement**: Since transactions cannot be modified, they cannot be partially processed. Either the entire transaction is handled, or it is not.

### Why Transaction-First Architecture Matters

By placing the Transaction at the center of the architecture, Ergon achieves:

- **Consistency**: Every component in the system speaks the same language—transactions. Tasks process transactions. Connectors fetch and dispatch transactions. Telemetry tracks transactions.
- **Testability**: Business logic can be tested by simply creating Transaction objects and passing them to your task. No mocks for HTTP clients, no fake queues—just data.
- **Scalability**: Scaling decisions are made at the transaction level. More workers mean more transactions processed in parallel. The framework handles the rest.
- **Observability**: Since every unit of work has a unique ID, tracing a request through a distributed system becomes trivial.

---

## 3. Domain Layer: Tasks and Mixins

### The Philosophy of "Thin" Tasks

In Ergon, a **Task** is where your business logic lives. But unlike traditional frameworks, Ergon tasks are deliberately "thin." They contain:

- **Business logic**: Transformations, validations, enrichments, decisions.

They do NOT contain:

- **I/O operations**: No HTTP calls, no database queries, no file reads.
- **Retry logic**: No `while` loops with exponential backoff.
- **Connection management**: No connection pools, no session handling.
- **Protocol details**: No message acknowledgments, no commit offsets.

All of these concerns are handled by other layers (Connectors, Services, Policies). This constraint might feel limiting at first, but it provides enormous benefits:

1. **Testability**: Your business logic can be unit tested with simple function calls. No mocks needed.
2. **Reusability**: The same task can work with different transports (switch from RabbitMQ to Kafka without changing business logic).
3. **Maintainability**: When something breaks, you know exactly where to look. Business logic bugs are in the Task. Transport bugs are in the Connector/Service.

### Base Task Classes

Ergon provides two base classes for tasks:

**`BaseTask`** (Synchronous):

```python
class BaseTask(ABC, metaclass=TaskMeta):
    def __init__(self, connectors, services, policies): ...
  
    @abstractmethod
    def execute(self) -> Any:
        """Main entry point for the task."""
  
    def exit(self):
        """Cleanup hook called after execution."""
```

**`BaseAsyncTask`** (Asynchronous):

```python
class BaseAsyncTask(ABC, metaclass=TaskMeta):
    def __init__(self, connectors, services, policies): ...
  
    @abstractmethod
    async def execute(self) -> Any:
        """Async entry point for the task."""
  
    async def exit(self):
        """Async cleanup hook."""
```

Both classes use the `TaskMeta` metaclass, which automatically injects dependencies (connectors, services, policies) as instance attributes. This means you can access them as `self.input_connector`, `self.openai_service`, etc.

### Mixins: Adding Behavior

Raw base tasks are just shells. The real power comes from **Mixins** that add specific behaviors:

#### ConsumerMixin

The `ConsumerMixin` transforms a task into a transaction consumer. It provides:

- **`process_transaction(transaction)`**: The method you implement. Receives a single transaction, returns a result.
- **`handle_process_success(transaction, result)`**: Hook called after successful processing.
- **`handle_process_exception(transaction, exception)`**: Hook called when processing fails.
- **`consume_transactions(policy)`**: The main loop that fetches and processes transactions.

The consumer engine handles all the complexity:

- Fetching batches from connectors
- Concurrent processing with configurable parallelism
- Retry logic with exponential backoff
- Timeout enforcement
- Error routing to exception handlers
- Observability instrumentation

#### ProducerMixin

The `ProducerMixin` enables outbound transaction production:

- **`prepare_transaction(transaction)`**: The method you implement. Prepares a transaction for dispatch.
- **`handle_prepare_success(transaction, result)`**: Hook called after successful preparation.
- **`handle_prepare_exception(transaction, exception)`**: Hook called when preparation fails.
- **`produce_transactions(transactions, policy)`**: Processes a list of transactions for output.

#### HybridTask

For end-to-end pipelines, `HybridTask` combines both mixins. A hybrid task can consume from one source, transform the data, and produce to another destination—all within a single atomic operation.

### Dependency Injection

When a task is instantiated, the framework injects:

1. **Connectors**: A dictionary of connector instances (`self.connectors` or `self.{name}_connector`)
2. **Services**: A dictionary of service instances (`self.services` or `self.{name}_service`)
3. **Policies**: A list of execution policies (`self.policies` or `self.{name}_policy`)

This injection happens automatically based on your `TaskConfig`. You never manually instantiate connectors or services inside a task.

---

## 4. Integration Layer: Connectors and Services

### Services: The Protocol Experts

A **Service** is a standalone component that knows how to communicate with an external system. It handles:

- **Protocol mechanics**: HTTP headers, authentication, serialization
- **Connection management**: Connection pools, session reuse, keepalives
- **Resilience**: Retries, backoff, circuit breakers
- **Pagination**: Cursors, offsets, continuation tokens

Services are completely independent of the Ergon task framework. You can instantiate and use a service in a script, a notebook, or any other context. They are designed to be reusable and testable in isolation.

**Key principle**: Services do NOT know about Transactions. They work with raw data (dictionaries, strings, bytes). The translation between raw data and Transactions happens in the Connector layer.

### Connectors: The Transaction Boundary

A **Connector** wraps a Service and adapts it to the framework's transaction interface. It has exactly two responsibilities:

1. **Fetch Transactions**: Call the service to get raw data, wrap each unit in a `Transaction` object.
2. **Dispatch Transactions**: Receive `Transaction` objects from the task, extract the payload, pass it to the service.

The connector is the boundary where raw protocol data becomes typed, traceable, atomic transactions.

**Connector Interface**:

```python
class Connector(ABC):
    @abstractmethod
    def fetch_transactions(self, batch_size, **kwargs) -> List[Transaction]:
        """Fetch and wrap raw data into Transactions."""
  
    @abstractmethod
    def dispatch_transactions(self, transactions: List[Transaction], **kwargs) -> Any:
        """Unwrap Transactions and send via the service."""
  
    def get_transactions_count(self, **kwargs) -> int:
        """Optional: Get pending transaction count."""
```

**AsyncConnector** provides the same interface with `async` methods.

### When to Use Which

- **Service only**: When you need to call an API for enrichment (e.g., OpenAI, geocoding, database lookup). Inject it directly into the task.
- **Connector + Service**: When the service is the source or sink of your pipeline (e.g., RabbitMQ consumer, Kafka producer, file reader). The connector plugs into the framework's execution loop.

### Configuration

Both connectors and services are configured declaratively:

```python
ConnectorConfig(
    connector=MyConnector,
    args=(),
    kwargs={"host": "localhost", "port": 5672}
)

ServiceConfig(
    service=MyService,
    args=(),
    kwargs={"api_key": "..."}
)
```

The runner instantiates these at startup and injects them into the task.

---

## 5. Platform Layer: Runners, Policies, and Telemetry

### The Runner: Orchestrating Execution

The **Runner** is responsible for the entire lifecycle of task execution:

1. **Initialize telemetry** (logging, tracing, metrics)
2. **Instantiate connectors** with spans for observability
3. **Instantiate services** with spans for observability
4. **Create the task instance** with all dependencies injected
5. **Execute the task** (sync or async)
6. **Call the exit hook** for cleanup
7. **Flush telemetry** to ensure all data is exported

The runner also handles **scaling**:

- **Single process mode** (`max_workers=1`): Runs directly, useful for debugging.
- **Multi-process mode**: Uses `ProcessPoolExecutor` to spawn isolated workers.

For async tasks, the runner uses `asyncio.run()` and manages the event loop.

### Policies: Fine-Grained Control

Policies are configuration objects that control execution behavior without code changes.

#### ConsumerPolicy

Controls the consumption loop. Policies are created as instances and configured by setting properties directly:

```python
from ergon_framework.task import policies

# Create policy instance
CONSUMER_POLICY = policies.ConsumerPolicy()
CONSUMER_POLICY.name = "consumer"

# Configure loop behavior
CONSUMER_POLICY.loop.concurrency.value = 1
CONSUMER_POLICY.loop.batch.size = 1
CONSUMER_POLICY.loop.limit = 1
CONSUMER_POLICY.loop.streaming = False

# Configure fetch phase (e.g., pass extra params to connector)
CONSUMER_POLICY.fetch.extra.setdefault("phase_id", settings.PHASE_ID)

# Configure process retry behavior
CONSUMER_POLICY.process.retry.max_attempts = 3
CONSUMER_POLICY.process.retry.backoff = 1
CONSUMER_POLICY.process.retry.backoff_multiplier = 2
CONSUMER_POLICY.process.retry.backoff_cap = 10

# Configure exception handler retry behavior
CONSUMER_POLICY.exception.retry.max_attempts = 3
CONSUMER_POLICY.exception.retry.backoff = 1
CONSUMER_POLICY.exception.retry.backoff_multiplier = 2
CONSUMER_POLICY.exception.retry.backoff_cap = 10
```

Each phase (fetch, process, success, exception) has independent retry configuration. This allows you to, for example, retry business logic 3 times but only retry the exception handler once.

#### ProducerPolicy

Similar structure for production:

```python
from ergon_framework.task import policies

# Create policy instance
PRODUCER_POLICY = policies.ProducerPolicy()
PRODUCER_POLICY.name = "producer"

# Configure loop behavior
PRODUCER_POLICY.loop.concurrency.value = 10
PRODUCER_POLICY.loop.batch.size = 100
PRODUCER_POLICY.loop.timeout = 1800

# Configure prepare retry behavior
PRODUCER_POLICY.prepare.retry.max_attempts = 3
PRODUCER_POLICY.prepare.retry.backoff = 1
PRODUCER_POLICY.prepare.retry.backoff_multiplier = 2
PRODUCER_POLICY.prepare.retry.backoff_cap = 10

# Configure exception handler retry behavior
PRODUCER_POLICY.exception.retry.max_attempts = 2
```

#### Passing Policies to TaskConfig

Policies are passed to the task configuration and automatically injected into the task instance:

```python
TASK_CONFIG = TaskConfig(
    task=MyTask,
    name="my-task",
    connectors={"input": settings.INPUT_CONNECTOR},
    services={"openai": settings.OPENAI_SERVICE},
    policies=[CONSUMER_POLICY],
    logging=settings.LOGGING,
    tracing=settings.TRACING,
)
```

#### RetryPolicy Fields

The `RetryPolicy` is the building block for all retry behavior. Each step policy (fetch, process, success, exception, prepare) contains a `retry` field with these options:

- `max_attempts`: Hard limit on retries (default: 1)
- `timeout`: Time limit per attempt in seconds (default: None)
- `backoff`: Initial delay in seconds after first failure (default: 0)
- `backoff_multiplier`: Exponential factor for subsequent retries (default: 0)
- `backoff_cap`: Maximum delay in seconds (default: 0)

### Telemetry: Observability by Default

Ergon integrates OpenTelemetry natively. When you run a task, you get:

#### Logging

- Structured JSON logging (via `python-json-logger`)
- Automatic injection of `trace_id` and `span_id` into every log record
- Multiple handler support: Console, File, Rotating File, OTLP (gRPC export)

#### Tracing

- Automatic spans for task execution, connector operations, and service calls
- Hierarchical span structure: Task > Batch > Transaction > Attempt
- Full context propagation across async boundaries
- Export to any OpenTelemetry-compatible backend (Jaeger, Tempo, etc.)

#### Metrics

- Push-based metrics via `PeriodicExportingMetricReader`
- Automatic resource attributes (task name, host, PID, execution ID)
- Export to Prometheus, OTLP, or console

#### Configuration

```python
TaskConfig(
    name="my_task",
    task=MyTask,
    connectors={...},
    logging=LoggingConfig(
        level="INFO",
        handlers=[
            ConsoleLogHandler(),
            OTLPLogHandler(processors=[...])
        ]
    ),
    tracing=TracingConfig(
        processors=[SpanProcessor(processor=BatchSpanProcessor, exporters=[OTLPSpanExporter()])]
    ),
    metrics=MetricsConfig(
        readers=[MetricReader(reader=PeriodicExportingMetricReader, exporters=[OTLPMetricExporter()])]
    )
)
```

---

## 6. Concurrency Models

### Synchronous Execution

Best for:

- CPU-bound processing
- Legacy libraries that are not async-compatible
- Simple pipelines where throughput is not critical

Mechanism:

- The consumer loop runs in the main thread
- Each transaction is submitted to a `ThreadPoolExecutor`
- Concurrency is controlled by `ConcurrencyPolicy.value`

Benefits:

- High isolation between transactions
- Easy debugging (stack traces are straightforward)
- Compatible with any Python library

### Asynchronous Execution

Best for:

- I/O-bound workloads (API calls, database queries)
- High-throughput pipelines
- Scenarios requiring thousands of concurrent operations

Mechanism:

- The consumer loop runs as a coroutine
- Each transaction is an async task
- Concurrency is controlled by `asyncio.Semaphore`

Benefits:

- Extremely low overhead per transaction
- Can handle massive concurrency on a single core
- Natural fit for modern async libraries (httpx, aiohttp, asyncpg)

---

## 7. Benefits Summary

| Concern                     | Traditional Approach                    | Ergon Approach                                   |
| --------------------------- | --------------------------------------- | ------------------------------------------------ |
| **Business Logic**    | Mixed with I/O, retries, error handling | Isolated in Tasks, pure and testable             |
| **Retries**           | Ad-hoc, inconsistent across codebase    | Centralized in Policies, consistent behavior     |
| **Observability**     | Manual instrumentation, often forgotten | Automatic, every transaction is traced           |
| **Transport Changes** | Requires business logic refactoring     | Swap Connector/Service, business logic unchanged |
| **Testing**           | Requires mocks, integration tests       | Unit tests with plain Transaction objects        |
| **Scaling**           | Custom implementation per project       | Built-in sync/async, single/multi-process        |
| **Error Handling**    | Try/except everywhere                   | Structured lifecycle with dedicated handlers     |

---

## 8. Conclusion

Ergon is not just a framework—it is an architectural discipline. By enforcing a strict separation between business logic, transport mechanics, and infrastructure concerns, it enables teams to build data pipelines that are:

- **Reliable**: Structured retry policies and atomic transactions prevent data loss.
- **Observable**: Every transaction is traced, logged, and metered automatically.
- **Maintainable**: Business logic is isolated and testable; transport changes do not ripple through the codebase.
- **Scalable**: Built-in support for sync/async execution, single/multi-process scaling.

The Transaction is the heart of this architecture. Everything else exists to serve it.
