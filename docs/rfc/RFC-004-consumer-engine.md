
# RFC-004: Consumer Engine

**Author**: Daniel A. Vossos  
**Status**: Draft  
**Version**: 0.1.0  
**Created**: 2025-12-04  
**Updated**: 2025-12-04  
**Depends on**: RFC-000, RFC-001, RFC-002, RFC-003  
**Supersedes**: None  

---

## 1. Abstract

This document defines the Consumer Engine, the core execution subsystem responsible for consuming and processing inbound Transaction objects.

The Consumer Engine provides:
- deterministic, isolated per-transaction lifecycles
- step-level retries, timeouts, and exponential backoff
- worker-level concurrency control (sync & async)
- complete OpenTelemetry instrumentation
- strict business/system/timeouts classification
- support for streaming and non-streaming consumption modes
- strict separation of domain logic (Tasks) from operational concerns

This RFC is normative and MUST match implementation behavior.

---

## 2. Motivation

Tasks should express pure business logic.
They SHOULD NOT implement retries, timeouts, concurrency, batching, backoff, or telemetry.

The Consumer Engine centralizes all of this operational complexity, ensuring:
- predictable behavior
- uniform error handling
- reproducible execution
- strong observability
- connector-agnostic consumption
- deterministic failure modes

Without a well-defined Consumer Engine, tasks would diverge in behavior and correctness, resulting in non-deterministic automation pipelines.

---

## 3. Design Goals (Normative)

The Consumer Engine MUST:

1.  Execute each transaction inside a strict, isolated lifecycle.
2.  Enforce all operational semantics defined by policies (RFC-003).
3.  Maintain full telemetry for all processing steps and attempts.
4.  Support both synchronous (`ThreadPoolExecutor`) and asynchronous (`asyncio.Semaphore`) concurrency models.
5.  Guarantee that no two workers process the same Transaction concurrently.
6.  Correctly classify errors as `BUSINESS`, `SYSTEM`, or `TIMEOUT`.
7.  Apply exponential backoff according to the exact `RetryPolicy` and `EmptyQueuePolicy` rules.
8.  Respect loop-level controls:
    - streaming vs non-streaming mode
    - batch size
    - concurrency limit
    - loop timeout
    - lifecycle timeout
    - global processing limit
9.  Propagate OpenTelemetry context across worker threads and tasks.

---

## 4. Non-Goals

The Consumer Engine does NOT:
- deduplicate or de-dupe messages
- guarantee exactly-once delivery
- store transactions durably
- implement sharding, partitioning, or consumer groups
- validate business data schemas
- enforce business idempotency
- execute producer behavior (see RFC-005)
- manage external system connections (connectors/services do)

---

## 5. Formal Specification

### 5.1 Engine Structure

The Consumer Engine is implemented in:
- `task/mixins/consumer.py`
- `task/mixins/utils.py`
- `task/mixins/policies.py`
- `task/exceptions.py`

It provides two variants:
- `ConsumerMixin` — synchronous (threaded)
- `AsyncConsumerMixin` — asynchronous (asyncio)

Both variants MUST expose identical semantics.

---

## 6. Per-Transaction Lifecycle

Each transaction follows the deterministic finite state machine:

```text
START_PROCESSING
    → PROCESS
        → SUCCESS_HANDLER
        → EXCEPTION_HANDLER
```

This lifecycle is governed by `ConsumerSteps` (RFC-003):
- `fetch.retry`
- `process.retry`
- `success.retry`
- `exception.retry`

### 6.1 Process Phase

- Engine calls `process_transaction(transaction)` on the Task.
- Behavior:

| Scenario | Engine Behavior |
| :--- | :--- |
| Returns value | Treat as success → go to success handler |
| Raises `TransactionException(BUSINESS)` | Do not retry, immediately go to exception handler |
| Raises `TransactionException(SYSTEM/TIMEOUT)` | Retry according to `process.retry` |
| Raises any other exception | Wrap as `TransactionException(SYSTEM)` and retry |

### 6.2 Success Phase

- Engine invokes `handle_transaction_success`.
- If the handler fails:
    - retry according to `success.retry`
    - if exhausted → fallback to exception handler

### 6.3 Exception Phase

- Engine invokes `handle_transaction_exception`.
- If the handler fails:
    - retry according to `exception.retry`
    - if exhausted → the final exception is returned

---

## 7. Fetch Semantics

### 7.1 Fetch Function

Sync:
```python
conn.fetch_transactions(batch_size, **extra)
```

Async:
```python
await conn.fetch_transactions_async(batch_size, **extra)
```

The engine MUST:
- enforce fetch-retry policy
- convert timeouts into `FetchTimeoutException`
- convert other errors into `FetchException`
- terminate loop on unrecoverable fetch failures

### 7.2 Empty Queue Handling

If fetch result is empty:
- `streaming=False` → terminate loop immediately
- `streaming=True` → apply exponential backoff:

```python
delay = backoff * (multiplier^attempt)
if cap > 0: delay = min(delay, cap)
```

Where `attempt` increments for each consecutive empty fetch.

---

## 8. Concurrency Model

### 8.1 Synchronous Engine

Uses:
```python
ThreadPoolExecutor(max_workers=policy.loop.concurrency.value)
```

For each batch:
- spawn up to `N` workers (`N` = concurrency)
- refill workers as they complete
- enforce per-transaction `transaction_timeout` via:
  ```python
  future.result(timeout=transaction_timeout)
  ```

### 8.2 Asynchronous Engine

Uses:
```python
asyncio.Semaphore(concurrency)
```

- each worker runs `_start_processing` inside a semaphore permit
- refill tasks as they complete
- enforce lifecycle timeout via:
  ```python
  await asyncio.wait_for(task, timeout=transaction_timeout)
  ```

---

## 9. Timeout Semantics

The Consumer Engine MUST support three distinct timeout scopes, each independent:

| Timeout | Scope | Applies To |
| :--- | :--- | :--- |
| **Step Timeout** | Per attempt | `process`, `success`, `exception`, `fetch` |
| **Transaction Timeout** | Entire lifecycle after fetch | worker futures/tasks |
| **Loop Timeout** | Entire consume loop | `_consume_transactions` |

Implementation uses:
- `utils.run_with_timeout`
- `utils.run_with_timeout_async`

Timeouts MUST NOT apply cumulatively; each scope is independent.

---

## 10. Retry Semantics

Each step has independent retries:
- Attempt count MUST include the first attempt (attempt=0)
- Each retry MUST apply exponential backoff
- A retry MUST preserve OTEL context
- `BUSINESS` exceptions MUST NOT be retried
- `SYSTEM` and `TIMEOUT` MUST be retried until limits exhausted

---

## 11. Error Classification Rules (Normative)

The Consumer Engine MUST classify errors as:

### 11.1 BUSINESS
- Raised explicitly by user code:
  `TransactionException(category=BUSINESS)`
- MUST bypass retries
- MUST go directly to exception handler

### 11.2 SYSTEM
- Unexpected exceptions
- Network, parsing, external service failure inside task logic
- MUST be retried according to policy

### 11.3 TIMEOUT
- Step-level timeout (process, success, exception)
- Fetch timeout
- MUST be retried until budget exhausted

---

## 12. Telemetry Semantics

Every attempt MUST produce a span with:
- task class name
- step name (fetch, process, success, exception)
- attempt number
- max attempts
- retry/backoff configs
- `transaction_id` (if known)
- exception message (if any)

Hierarchy:
```text
consume_transactions
    └ fetch_transactions
    └ start_processing (per transaction)
        ├ process
        ├ handle_success
        └ handle_exception
```

OpenTelemetry context MUST propagate across:
- threads (`utils.with_context`)
- async tasks (`utils.with_context_async`)
- timeout wrappers

---

## 13. Loop Semantics

### 13.1 Termination Conditions

The consume loop MUST stop when:
- connector returns no data AND `streaming=False`
- limit transactions processed
- loop-level timeout occurs
- unrecoverable fetch error occurs

### 13.2 Batching Rules
- engine MUST NOT modify the batch
- connectors define the batch size
- batch size MUST be ≤ `BatchPolicy.size`

---

## 14. Invariants (Normative)

The Consumer Engine MUST guarantee:

1.  No transaction is processed concurrently by two workers.
2.  Retry attempts always share the same OTEL context.
3.  Success handler MUST NOT run if process step fails with `BUSINESS`.
4.  Exception handler MUST run exactly once if:
    (a) process fails with `BUSINESS`, or
    (b) success handler fails after retries.
5.  Backoff MUST follow RFC-003 exactly.
6.  Engine MUST be deterministic given connector and task behavior.

---

## 15. Invalid Behavior (Prohibited)

The engine MUST NOT:
- swallow exceptions silently
- retry `BUSINESS` exceptions
- retry success handler failures with process policy
- merge or split batches
- let tasks perform I/O
- allow overlapping spans of different transactions
- leak OpenTelemetry context
- mutate policy objects

---

## 16. Security Considerations

The engine MUST:
- not expose credentials or connector metadata in spans
- not propagate sensitive payload fields unless allowed
- isolate transaction context to a single worker
- prevent cross-transaction state pollution

---

## 17. Backwards Compatibility Rules

- New policies MUST provide default backwards-compatible values.
- Retry semantics MUST NOT change without major RFC updates.
- Added spans MUST be non-breaking to trace consumers.

---

## 18. Reference Implementation

Reference implementation is authoritative:

- `src/ergon_framework/task/mixins/consumer.py`
- `src/ergon_framework/task/mixins/utils.py`
- `src/ergon_framework/task/mixins/policies.py`
- `src/ergon_framework/task/exceptions.py`

The engine MUST comply fully with this RFC.

---

## 19. Change Log

- **0.1.0**: Initial draft.

