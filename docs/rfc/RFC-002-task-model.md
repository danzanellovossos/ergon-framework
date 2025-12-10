
# RFC-002: Task Model

**Author**: Daniel A. Vossos  
**Status**: Draft  
**Version**: 0.1.0  
**Created**: 2025-12-04  
**Updated**: 2025-12-04  
**Depends on**: RFC-000, RFC-001  
**Supersedes**: None  

---

## 1. Abstract

This document defines the **Task Model**, the core abstraction for domain logic execution within the Ergon Framework.
It specifies the strict boundaries of a Task: immutability, purity, dependency injection, execution contracts, and concurrency guarantees.
The Task serves as the exclusive container for business rules, completely decoupled from transport, infrastructure, and orchestration mechanics.

This RFC is normative.
Implementations MUST comply with the rules defined herein.

---

## 2. Motivation

In many automation systems, business logic is often intertwined with transport details (e.g., reading from Kafka, writing to S3) and operational concerns (retries, concurrency). This coupling leads to code that is difficult to test, hard to migrate, and unpredictable under load.

To achieve the architectural goals of RFC-000 (Separation of Concerns, Determinism), the framework requires a strict definition of what a Task is and—more importantly—what it is not.
The Task Model enforces these boundaries to ensure that domain logic remains pure, testable, and portable.

---

## 3. Design Goals (Normative)

The Task Model MUST:
1.  **Enforce Purity**: Tasks must contain only domain logic, free from infrastructure concerns.
2.  **Mandate Dependency Injection**: All external capabilities (I/O, API calls) must be injected, never instantiated internally.
3.  **Guarantee Isolation**: Task execution must be side-effect-free relative to the engine's control flow.
4.  **Ensure Determinism**: Given identical inputs (Transaction + Service responses), a Task must produce identical outputs.
5.  **Abstract Concurrency**: Tasks must be agnostic to whether they are running in a thread pool, process pool, or event loop.

---

## 4. Non-Goals

The Task Model does not:
- Define how tasks are scheduled or distributed (this is a Runner/Engine concern).
- Specify the transport mechanism for inputs/outputs (this is a Connector concern).
- Manage the lifecycle of external resources (this is a Service/Connector concern).
- Provide a framework for general-purpose computing outside the Transaction lifecycle.

---

## 5. Formal Specification

### 5.1 Definition of a Task

A **Task** is a pure execution unit responsible for processing a single atomic **Transaction** (RFC-001).
- It is the **only** component in the framework where business rules reside.
- It operates exclusively on `Transaction` objects.
- It delegates all non-computational operations to injected dependencies.

### 5.2 Task Immutability, Purity, and Side-Effects

Tasks MUST adhere to strict purity rules regarding infrastructure:

- **I/O Prohibition**: Tasks MUST NOT perform direct network calls, file I/O, or socket operations.
- **Resource Prohibition**: Tasks MUST NOT instantiate clients (e.g., `boto3`, `requests`, database drivers).
- **Concurrency Prohibition**: Tasks MUST NOT manage threads, processes, locks, or event loops.

Tasks MAY:
- Perform CPU-bound computations.
- Allocate memory.
- Instantiate helper classes for domain logic.
- Create new `Transaction` objects for outbound data.
- Call methods on injected **Services** and **Connectors**.

### 5.3 Task Dependencies (Connectors and Services)

Dependencies are provided solely via Dependency Injection (DI).
- **Injection**: The framework (Runner) injects dependencies at Task instantiation time.
- **Structure**:
    - `connectors: dict[str, Connector]`
    - `services: dict[str, Service]`
- **Lifecycle**: Dependencies are fully constructed and initialized *before* the Task receives them.
- **Invariant**: A Task MUST NEVER attempt to construct or manage the lifecycle of a dependency.

### 5.4 Task Execution Model

Task execution is strictly **engine-driven**. The Task is a passive entity invoked by the engine.

- **Control Flow**:
    - The **Consumer Engine** invokes `process_transaction(transaction)`.
    - The **Producer Engine** invokes `produce_transaction(transaction)`.
- **Operational Semantics**: The Task DOES NOT own or control:
    - Retries
    - Timeouts
    - Concurrency limits
    - Batching
    - Backoff strategies

All operational behavior is governed by the Engine policies, externally to the Task code.

### 5.5 Task API Surface

The Task interface is defined by the following methods. Tasks MUST implement the relevant subset based on their role (Consumer/Producer).

#### Synchronous API
| Method | Required? | Description |
| :--- | :--- | :--- |
| `process_transaction(self, transaction)` | **Yes** (Consumer) | Main entry point for inbound processing. |
| `produce_transaction(self, transaction)` | **Yes** (Producer) | Entry point for outbound transmission. |
| `handle_transaction_success(self, transaction, result)` | No | Hook invoked after successful processing. |
| `handle_transaction_exception(self, transaction, exc)` | No | Hook invoked after processing failure. |

#### Asynchronous API
| Method | Required? | Description |
| :--- | :--- | :--- |
| `async process_transaction(self, transaction)` | **Yes** (Consumer) | Async entry point for inbound processing. |
| `async produce_transaction(self, transaction)` | **Yes** (Producer) | Async entry point for outbound transmission. |
| `async handle_transaction_success(self, transaction, result)` | No | Async success hook. |
| `async handle_transaction_exception(self, transaction, exc)` | No | Async exception hook. |

### 6.6 Task Lifecycle

The lifecycle of a Task instance is deterministic:

1.  **Instantiation**: Created by the Runner.
2.  **Injection**: `connectors` and `services` are assigned.
3.  **Initialization**: Telemetry context is established.
4.  **Execution**: The Engine repeatedly calls `process_transaction` or `produce_transaction`.
5.  **Shutdown**: The Task is garbage collected when the Runner terminates.

**Note**: A single Task instance MAY process multiple transactions sequentially or concurrently (depending on the Runner model). Therefore, Tasks MUST NOT store transaction-specific state on `self`.

### 6.7 Task Concurrency Semantics

Tasks MUST be designed for concurrent execution:
- **Thread Safety**: In threaded runners, multiple methods on the same Task instance may be called simultaneously.
- **Statelessness**: Tasks SHOULD be stateless. Any shared state MUST be thread-safe (e.g., read-only configuration).
- **External State**: Stateful operations MUST be delegated to external Services (e.g., Redis, Database).
- **Non-Blocking**: In async mode, Task code MUST NOT block the event loop (no `time.sleep`, no blocking I/O).

### 6.8 Task Error Semantics

Tasks participate in the Engine's error model (RFC-000) by raising specific exceptions:

- **Business Failure**: Raise `TransactionException(category=BUSINESS)`.
    - Semantic: "This transaction is invalid and cannot be processed."
    - Outcome: The Engine halts retries immediately and routes to the exception handler.
- **System Failure**: Raise `TransactionException(category=SYSTEM)` (or let any exception bubble up).
    - Semantic: "Infrastructure failed; try again later."
    - Outcome: The Engine applies retry policies and backoff.
- **Retry Control**: Tasks MUST NOT implement retry loops internally. Retries are the exclusive responsibility of the Engine.

### 6.9 Task Observability Boundaries

- **Spans**: Tasks MUST NOT manually start or end Engine-level spans (e.g., `process`, `fetch`).
- **Child Spans**: Tasks MAY create child spans for significant internal domain operations.
- **Logging**: Tasks SHOULD log only domain-relevant events. Infrastructure logging is handled by the Engine.
- **Context**: Tasks inherit the active OpenTelemetry context from the Engine.

### 6.10 Task Determinism Requirements

A Task must behave like a pure function relative to its inputs:
- **Input**: `Transaction` payload + `Service` responses.
- **Output**: `Result` (value or exception).
- **Invariant**: Given the same Transaction and same Service responses, the Task MUST produce the same outcome.
- **Global State**: Tasks MUST NOT rely on global mutable variables.

---

## 7. Invariants (Normative)

1.  **Purity**: A Task never opens a socket or file descriptor directly.
2.  **Isolation**: No two transactions being processed by a Task may influence each other's execution state.
3.  **Immutability**: A Task never modifies the incoming `Transaction` object in place.
4.  **Delegation**: A Task never implements a retry loop.
5.  **Observability**: A Task never breaks the trace context chain.

---

## 8. Examples of Valid and Invalid Task Behaviors

### Valid Behavior
- ✅ Validating a payload schema.
- ✅ Calculating a tax value in memory.
- ✅ Calling `self.db_service.get_user(id)` to fetch data.
- ✅ Raising `TransactionException(BUSINESS)` when validation fails.
- ✅ Returning a result dictionary.

### Invalid Behavior
- ❌ Opening a file with `open('data.txt')` (Violation: Direct I/O).
- ❌ Creating a `requests.Session()` in `__init__` (Violation: Resource Management).
- ❌ Using a `while` loop to retry a failed API call (Violation: Retry ownership).
- ❌ Modifying `transaction.metadata['attempts']` (Violation: Immutability/Engine ownership).
- ❌ Creating a `threading.Thread` (Violation: Concurrency management).

---

## 9. Security Considerations

- **Credential Isolation**: Tasks MUST NOT access or store credentials. Credentials belong to Services/Connectors.
- **Injection Safety**: Tasks MUST NOT trust payloads to be safe for evaluation (e.g., `eval()`).
- **State Leakage**: Tasks MUST NOT store sensitive transaction data in instance variables (`self.data`), as this may leak to subsequent transactions in the same worker.

---

## 10. Backwards Compatibility Rules

- **API Stability**: The signatures of `process_transaction` and `produce_transaction` MUST remain stable across minor versions.
- **Mixin Composition**: The Engine MUST support Tasks composed of standard Mixins without requiring Task code changes.

---

## 11. Reference Implementation Notes

Reference Task implementations reside in `src/ergon_framework/task/base.py` and `src/ergon_framework/task/mixins/`.
The `BaseTask` and `BaseAsyncTask` classes implement the contract defined here.

---

## 12. Change Log

- **0.1.0**: Initial draft based on RFC-000 system overview.

