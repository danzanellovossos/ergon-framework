
# RFC-000: Ergon Framework – System Overview

**Author**: Daniel A. Vossos  
**Status**: Draft  
**Version**: 0.1.1  
**Created**: 2025-12-03  
**Updated**: 2025-12-04  
**Depends on**: None  
**Supersedes**: None  

---

## 1. Abstract

This document defines the architecture-wide primitives, invariants, and conceptual foundations of the Ergon Framework.
It formalizes the system’s philosophical boundaries, component roles, execution guarantees, and observability requirements.

Every subsequent RFC — Transaction Model, Task Model, Consumer Engine, Producer Engine, Telemetry, Connectors, Services, Runners — depends on the guarantees established here.

This RFC is normative.
All implementations MUST comply with the rules defined herein.

---

## 2. Motivation

The Ergon Framework is a high-throughput, fault-tolerant automation engine for distributed business workflows.
It aims to provide:
- deterministic and replayable transaction processing
- strict separation of concerns between logic, transport, and capabilities
- predictable concurrency semantics
- consistent and complete observability
- predictable retry/backoff/timeout behavior
- modularity and testability
- connector-agnostic I/O pipelines
- a standardized execution lifecycle

Without a unifying system overview, implementation drift and inconsistent component semantics become inevitable.
RFC-000 establishes the foundation for the entire architecture.

---

## 3. Goals (Normative)

The Ergon Framework MUST:

1.  Represent all units of work as atomic Transactions (RFC-001).
2.  Execute business logic exclusively inside Tasks with strict isolation (RFC-002).
3.  Delegate all I/O to Connectors and external interactions to Services.
4.  Enforce deterministic lifecycle semantics, including:
    - per-step retry budgets
    - per-step timeout envelopes
    - exponential backoff
    - concurrency bounding
    - loop-level timeouts
5.  Guarantee complete observability:
    - structured logging
    - hierarchical distributed tracing
    - metrics for throughput, latency, retries, backoff, and failures
6.  Preserve concurrency correctness across:
    - threaded workers
    - asynchronous event loop workers
    - multi-process runners
7.  Support streaming and batch processing uniformly.
8.  Keep user code free of infrastructure responsibilities (threading, I/O, context propagation).

---

## 4. Non-Goals (Normative)

Ergon explicitly does not attempt to:
- implement durable storage
- provide global exactly-once delivery
- orchestrate distributed sagas
- replace workflow engines (Temporal/Airflow)
- enforce business payload schema validation
- enforce idempotency at the connector layer
- auto-shard or auto-partition workloads

These responsibilities belong to external systems or user-level architecture.

---

## 5. System Architecture

### 5.1 Layered Architecture (Normative)

```text
+----------------------+
| Application Logic    |  <-- User-defined Tasks
+----------------------+
| Ergon Task Engine    |  <-- Consumer/Producer Engines
+----------------------+
| Connectors & Services|  <-- Transport + external capabilities
+----------------------+
| Environment          |  <-- Python runtime, OS, containers
+----------------------+
```

Each layer MUST maintain strict boundaries:

| Layer | Responsibilities | MUST NOT |
| :--- | :--- | :--- |
| **Tasks** | Pure domain logic | Perform I/O or manage concurrency |
| **Engine** | Lifecycle, retries, backoff, timeouts, concurrency | Contain business logic |
| **Connectors** | Ingress/egress transport | Apply domain logic |
| **Services** | External/API capabilities | Control engine flow |

### 5.2 Transaction-First Execution Model

Ergon enforces a transaction-first model:

`Source → Connector → Transaction → Task Logic → Transaction(s) → Connector → Sink`

Tasks do not touch transport.
Connectors do not touch business rules.
Services expose capabilities without control flow.

This is an architectural invariant.

### 5.3 Distributed Execution Context

Ergon maintains a unified execution context that includes:
- Transaction ID
- Trace ID and Span ID
- Connector metadata
- Task metadata
- Retry attempt counters
- Policy metadata
- OpenTelemetry context

The context MUST propagate through:
- threads
- thread pools
- asyncio tasks
- mixed sync/async boundaries
- consumer → producer pipelines

Context MUST NOT leak across transactions.

---

## 6. Fundamental Invariants (Normative)

These rules MUST hold everywhere:

1.  **Tasks MUST NOT perform I/O.**
    All external actions must flow through Connectors or Services.
2.  **Transactions are atomic.**
    A task MUST process a transaction as an indivisible unit.
3.  **Every transaction MUST terminate in exactly one outcome:**
    - success handler
    - exception handler
4.  **Concurrency safety:**
    The engine MUST guarantee that no two workers process the same Transaction ID concurrently within a task instance.
5.  **Retry isolation:**
    All retry attempts MUST share the same distributed context.
6.  **Trace isolation:**
    All spans for a transaction MUST belong to a single trace.
7.  **Execution purity:**
    User code MUST NOT create threads, manage executors, or manipulate event loops.
8.  **Deterministic exception classification:**
    - `BUSINESS` → not retryable
    - `SYSTEM` → retryable
    - `TIMEOUT` → retryable
9.  **Connector purity:**
    Connectors MUST NOT embed any business rules.
10. **Service statelessness:**
    Services MUST NOT hold engine state.
11. **Lifecycle completeness:**
    Every process attempt MUST be wrapped in OTEL spans, logs, and metrics.

---

## 7. Component Boundaries

### 7.1 Tasks (RFC-002)

Tasks MUST:
- contain only domain logic
- never perform transport or credential operations
- only interact via injected Services
- not manage concurrency

### 7.2 Transactions (RFC-001)

Transactions MUST:
- be immutable
- encapsulate payload + metadata
- represent the smallest unit of indivisible work
- propagate across all engine components

### 7.3 Connectors (RFC-006)

Connectors MUST:
- own all ingress/egress transport
- return or accept Transaction objects
- remain free of business logic

### 7.4 Services (RFC-007)

Services MUST:
- expose external capabilities (API, DB, LLM, KV store, etc.)
- be dependency-injected
- remain stateless with respect to engine control flow

### 7.5 Engines (Consumer/Producer Engines)

Engines MUST:
- fully control transaction lifecycle
- apply retries, timeouts, and backoff
- enforce concurrency limits
- enforce policies for fetch/process/success/exception steps
- implement full observability

### 7.6 Runners (RFC-009)

Runners MUST:
- initialize telemetry
- instantiate connectors, services, and tasks
- manage multi-process or multi-thread execution
- ensure graceful shutdown

---

## 8. Error Model Summary (Normative)

| Category | Meaning | Retryable? |
| :--- | :--- | :--- |
| **BUSINESS** | Domain rule failure | ❌ No |
| **SYSTEM** | Infrastructure or service failure | ✔️ Yes |
| **TIMEOUT** | Operation exceeded timing budget | ✔️ Yes |

All errors MUST be expressed or wrapped as `TransactionException`.

---

## 9. Observability Requirements (Normative)

Ergon provides first-class observability via OpenTelemetry.

### 9.1 Tracing

The engine MUST emit a hierarchical span structure:

```text
transaction
 ├── fetch
 │     └── fetch.attempt.N
 ├── process
 │     └── process.attempt.N
 ├── success
 │     └── handle_success.attempt.N
 └── exception
       └── handle_exception.attempt.N
```

Each attempt MUST be its own span.
All spans MUST share the same trace context.

### 9.2 Logging

All logs MUST automatically include:
- transaction id
- trace id
- span id
- task name
- component/phase/attempt

User code MUST NOT manually instrument logs.

### 9.3 Metrics

The system MUST emit metrics for:
- processed transactions
- success / exception counts
- timeout counts
- retry attempts
- backoff intervals
- per-phase durations
- concurrency usage

These metrics MUST NOT require user instrumentation.

---

## 10. Security Considerations

The engine MUST NOT:
- expose connector credentials to Tasks
- allow cross-transaction context leakage
- allow Services to mutate engine state
- log sensitive metadata unless explicitly configured

---

## 11. Backwards Compatibility Rules

- Behavioral changes MUST be versioned via RFC updates.
- Pydantic policy models MUST maintain backwards-compatible defaults.
- Deprecated features MUST have a deprecation period.

---

## 12. Reference Implementation

Reference implementation:

```text
src/ergon_framework/
├── task/
├── connectors/
├── services/
├── telemetry/
└── runner/
```

The RFC defines the behavior.
The code MUST conform to the specification.

---

## 13. Change Log

- **0.1.1**: Expanded observability, concurrency guarantees, lifecycle invariants.
- **0.1.0**: Initial draft.
