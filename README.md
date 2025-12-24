
<p align="left">
  <img src="assets/logo.png" alt="Ergon Framework Logo" height="200">
</p>

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**A transaction-first, observability-native execution framework for building high-throughput automation pipelines.**

---

## Overview

Ergon is a framework designed to solve the "maturity gap" in background processing. While simple job queues are great for basic workloads and raw async scripts are flexible but unstructured, Ergon provides a **rigorous architectural foundation** for mission-critical workloads.

The framework enforces a strict **layered architecture** where business logic (`Tasks`) is completely isolated from transport mechanics (`Connectors` & `Services`). This ensures your automation code remains deterministic, testable, and portable—whether you're consuming from message queues, reading from object storage, or streaming from databases.

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

```
Transaction {
    id: string           // Unique identifier for tracing
    payload: any         // The actual data content
    metadata: map        // Contextual information (optional)
}
```

Transactions are **immutable** once created. This guarantees:

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

## Core Concepts

### Tasks & Mixins

Tasks are **thin** orchestration units containing only business logic. Behavior is added via mixins:

| Mixin             | Purpose                        | Key Methods                                                                             |
| ----------------- | ------------------------------ | --------------------------------------------------------------------------------------- |
| `ConsumerMixin` | Inbound transaction processing | `process_transaction()`, `handle_process_success()`, `handle_process_exception()` |
| `ProducerMixin` | Outbound transaction dispatch  | `prepare_transaction()`, `handle_prepare_success()`, `handle_prepare_exception()` |

**Task types:**

- `ConsumerTask` — Synchronous consumer
- `ProducerTask` — Synchronous producer
- `HybridTask` — Synchronous consumer + producer
- `AsyncConsumerTask` — Asynchronous consumer
- `AsyncProducerTask` — Asynchronous producer
- `AsyncHybridTask` — Asynchronous consumer + producer

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

- **Service only**: Enrichment APIs (LLMs, geocoding, database lookups)—inject directly into tasks
- **Connector + Service**: Sources/sinks of your pipeline (queues, files, streams)

### Policies

Policies provide fine-grained control over execution without code changes:

**ConsumerPolicy** controls:

- Loop behavior (concurrency, batch size, streaming mode, timeouts, limits)
- Empty queue behavior (backoff, backoff multiplier, backoff cap)
- Per-step retry configuration (fetch, process, success, exception)

**ProducerPolicy** controls:

- Loop behavior (concurrency, batch size, timeout)
- Per-step retry configuration (prepare, success, exception)

**RetryPolicy** fields:

- `max_attempts`: Maximum number of retries
- `timeout`: Timeout per attempt
- `backoff`: Initial backoff delay
- `backoff_multiplier`: Exponential factor for subsequent retries
- `backoff_cap`: Maximum backoff delay

---

## Dependency Injection

Tasks receive all dependencies automatically:

- **Connectors**: Access as `self.{name}_connector` (e.g., `self.input_connector`)
- **Services**: Access as `self.{name}_service` (e.g., `self.openai_service`)
- **Policies**: Access as `self.{name}_policy` (e.g., `self.consumer_policy`)

---

## Observability

Ergon integrates **OpenTelemetry** natively. Every task execution automatically generates:

### Logging

- Structured JSON logging
- Automatic `trace_id` and `span_id` injection
- Multiple handlers: Console, File, RotatingFile, OTLP

### Tracing

- Hierarchical spans: Task → Batch → Transaction → Attempt
- Full context propagation across async boundaries
- Export to Jaeger, Tempo, or any OTLP-compatible backend

### Metrics

- Push-based via periodic metric readers
- Automatic resource attributes (task name, host, PID, execution ID)
- Export to Prometheus, OTLP, or console

---

## Concurrency Models

### Synchronous Execution

Best for CPU-bound processing or legacy libraries:

- Concurrency controlled by thread pool size
- Each transaction processed in its own thread
- Multi-process scaling via worker count configuration

### Asynchronous Execution

Best for I/O-bound workloads with high concurrency:

- Concurrency controlled by semaphore
- Thousands of concurrent operations with minimal overhead
- Perfect for API calls, database queries, network I/O

---

## Scaling

| Axis                | Mechanism                    | Configuration                              |
| ------------------- | ---------------------------- | ------------------------------------------ |
| **Process**   | Process pool                 | Worker count in task configuration         |
| **Thread**    | Thread pool                  | Concurrency value in policy                |
| **Async**     | Semaphore                    | Concurrency value in policy                |
| **Connector** | External system partitioning | Service-specific (consumer groups, shards) |

---

## SDKs

Ergon is a language-agnostic framework specification. Official SDK implementations:

| Language         | Status       | Documentation           |
| ---------------- | ------------ | ----------------------- |
| **Python** | ✅ Available | [Python SDK](sdks/python/) |

### Installing an SDK

See the respective SDK documentation for installation and usage instructions.

---

## Documentation

Deep dive into the core framework concepts:

- **[Architecture Guide](docs/architecture.md)** — Full system specification and design philosophy
- **[Transaction Abstraction](docs/modules/1.transaction.md)** — Understanding atomicity rules
- **[Task Module](docs/modules/2.task.md)** — Mixins, lifecycles, and execution modes
- **[Connector Module](docs/modules/3.connector.md)** — Building integration boundaries
- **[Service Module](docs/modules/4.service.md)** — Protocol engineering and reliability
- **[Telemetry Module](docs/modules/5.telemetry.md)** — Configuring logs, metrics, and traces

---

## License

This project is licensed under the [MIT License](LICENSE).
