
# RFC-005: Producer Engine

**Author**: Daniel A. Vossos  
**Status**: Draft  
**Version**: 0.1.0  
**Created**: 2025-12-04  
**Updated**: 2025-12-04  
**Depends on**: RFC-000, RFC-001, RFC-002, RFC-003  
**Supersedes**: None  

---

## 1. Abstract

This document defines the Producer Engine, the subsystem responsible for delivering outbound Transaction objects to external systems via Connectors.

The Producer Engine provides:
- deterministic, isolated per-transaction outbound lifecycles
- step-level retries, timeouts, exponential backoff
- concurrency-controlled production loops (sync & async)
- context propagation across threads and async tasks
- full OpenTelemetry instrumentation
- strict classification of business/system/timeout errors
- batch-based throughput control

The Producer Engine mirrors the Consumer Engine (RFC-004) in structure but applies outbound semantics instead of inbound ones.

This RFC is normative.

---

## 2. Motivation

A task that produces outbound work MUST NOT deal with:
- timeouts
- retries
- thread pools
- concurrent workers
- telemetry
- connector behavior
- backoff
- lifecycle orchestration

Instead, the task should implement a pure:

```python
def produce_transaction(self, transaction: Transaction) -> Any
```

The Producer Engine abstracts all operational complexity so that producer tasks remain deterministic and domain-focused.

---

## 3. Design Goals (Normative)

The Producer Engine MUST:

1.  Execute each outbound operation inside an isolated lifecycle.
2.  Enforce per-step retry and timeout semantics.
3.  Enforce concurrency limits across batches.
4.  Guarantee backpressure protection and bounded throughput.
5.  Propagate OpenTelemetry context correctly.
6.  Support synchronous (`ThreadPoolExecutor`) and async (`Semaphore`) implementations.
7.  Apply strict classification of business/system/timeout exceptions.
8.  Honor loop-level execution limits, timeouts, and batch sizes.
9.  Ensure producer tasks remain stateless and free of connector logic.

---

## 4. Non-Goals

The engine does not:
- guarantee message durability or delivery semantics
- implement idempotency (left to connector or domain)
- shard or partition producer workloads
- coordinate multi-transaction transactions
- optimize batching at connector level (connectors handle protocol-specific batching)

---

## 5. Engine Structure

The Producer subsystem consists of:
- `ProducerMixin` (sync)
- `AsyncProducerMixin` (async)
- `ProducerPolicy`, `ProducerSteps`, `ProducePolicy` (RFC-003)
- timeout/backoff helpers (`utils.py`)
- exception models (`TransactionException`, categories)

---

## 6. Per-Transaction Lifecycle

The outbound lifecycle mirrors the Consumer FSM:

```text
START_PRODUCING
    → PRODUCE
        → SUCCESS_HANDLER
        → EXCEPTION_HANDLER
```

Unlike consumers, producers do not fetch — they operate on user-provided input lists.

---

## 7. Produce Phase Semantics

The user-defined function:

```python
def produce_transaction(self, transaction) -> Any
```

or async equivalent:

```python
async def produce_transaction(self, transaction)
```

is the only place where domain logic interacts with outbound connectors.

### 7.1 Behavior

| Scenario | Engine Behavior |
| :--- | :--- |
| returns a value | success → success handler |
| raises `TransactionException(BUSINESS)` | non-retryable → exception handler |
| raises `TransactionException(SYSTEM/TIMEOUT)` | retry using `produce.retry` |
| raises arbitrary exception | wrap as `SYSTEM` → retry |
| timeout occurs | wrap as `TIMEOUT` → retry |

Producer tasks MUST NOT manage:
- connector calls
- retries
- policies
- timeouts
- concurrency

All operational responsibilities belong to the Producer Engine.

---

## 8. Success Phase Semantics

When produce returns a value:
- engine calls `handle_produce_success(transaction, result)`

If the success handler:
- raises any exception → always treated as `SYSTEM`
- MUST retry via `success.retry`
- after exhaustion → fallback to exception handler
- MUST NOT be retried with produce policy
- MUST NOT raise `BUSINESS` exceptions (but if they do, engine treats as `SYSTEM`)

---

## 9. Exception Phase Semantics

When either:
- produce fails with `BUSINESS`
- produce fails with `SYSTEM`/`TIMEOUT` after retries
- success handler fails after retries

the engine calls:

```python
handle_produce_exception(transaction, exc)
```

Exception handler rules:
- Retries follow `exception.retry`
- Failure after retries → final terminal outcome
- No fallback after this step

---

## 10. Concurrency & Batch Semantics

Both sync and async variants use the same batch/concurrency model.

### 10.1 Batch Layout

Input: `List[Transaction]`

Producer Engine MUST:
1.  Split input list into batches of `ProducerLoopPolicy.batch.size`.
2.  Sequentially process each batch.
3.  Maintain controlled concurrency within each batch.
4.  Refill worker pool as active tasks complete.

### 10.2 Synchronous Engine

Uses:
```python
ThreadPoolExecutor(max_workers=loop.concurrency.value)
```

For each worker:
- engine binds OTel context via `utils.with_context`
- enforces lifecycle timeout using:
  ```python
  future.result(timeout=transaction_timeout)
  ```

### 10.3 Async Engine

Uses:
```python
asyncio.Semaphore(loop.concurrency.value)
```

Each worker:
- acquires semaphore
- uses `utils.with_context_async` to preserve context
- enforces transaction timeout via:
  ```python
  await asyncio.wait_for(task, timeout=transaction_timeout)
  ```

The engine MUST:
- never exceed concurrency limit
- never process the same Transaction twice
- release semaphore after worker finishes

---

## 11. Timeout Semantics

Producer Engine enforces three independent timeout scopes:

| Timeout Type | What it Limits |
| :--- | :--- |
| **Produce Step Timeout** | Single attempt of produce, success, or exception |
| **Lifecycle Timeout** | Entire lifecycle of a transaction (post-submit) |
| **Loop Timeout** | Entire `produce_transactions` call |

All MUST use:
- `run_with_timeout` (sync)
- `run_with_timeout_async` (async)

and wrap timeouts as:
- `TransactionException(TIMEOUT)` for produce/success/exception
- raw timeout errors for loop-level timeouts

---

## 12. Retry Semantics

Every step has its own retry envelope:
- `produce.retry`
- `success.retry`
- `exception.retry`

Rules:
1.  `BUSINESS` failures MUST NOT be retried.
2.  `SYSTEM` and `TIMEOUT` get retried until budget exhausted.
3.  Backoff MUST follow:
    ```python
    delay = backoff * (multiplier**attempt)
    if cap > 0: delay = min(delay, cap)
    ```
4.  Attempts MUST preserve OTel context.
5.  `attempt=0` is the first attempt.

---

## 13. Error Classification Rules (Normative)

All errors MUST be converted to `TransactionException` unless coming from fetch (producer has no fetch).

| Condition | Category |
| :--- | :--- |
| User raises `TransactionException(BUSINESS)` | `BUSINESS` (non-retryable) |
| User raises `TransactionException(SYSTEM)` | `SYSTEM` (retryable) |
| User raises `TransactionException(TIMEOUT)` | `TIMEOUT` (retryable) |
| Timeout occurs in engine | `TIMEOUT` |
| Arbitrary exceptions | `SYSTEM` |

The engine MUST NOT let raw exceptions propagate from user code.

---

## 14. Telemetry Semantics

Producer Engine MUST generate spans for all steps:

```text
produce_transactions
    ├ produce
    │   ├ produce.attempt
    │   └ internal timeout/backoff spans (in future)
    ├ handle_produce_success
    │   └ handle_produce_success.attempt
    └ handle_produce_exception
        └ handle_produce_exception.attempt
```

Attributes MUST include:
- transaction id
- result or exception message
- attempts / max_attempts
- policy values (timeout, backoff, cap, etc.)
- concurrency/batch metadata

OTel propagation MUST use:
- `with_context` (sync)
- `with_context_async` (async)
- `run_with_timeout` wrappers

Each transaction MUST correspond to a subtree of spans inside the same trace.

---

## 15. Loop Semantics

### 15.1 Termination Conditions

The produce loop stops when:
- all transactions have been processed
- limit reached
- loop-level timeout triggered

Unlike the Consumer Engine, Producer Engine has NO empty-queue logic.

### 15.2 Batch Execution

For each batch:
1.  Fill concurrency pool
2.  Wait for at least one worker to finish
3.  Apply lifecycle timeouts
4.  Refill pool
5.  Continue until batch exhausted

This pattern ensures:
- bounded concurrency
- fairness
- predictable throughput

---

## 16. Invariants (Normative)

Producer Engine MUST guarantee:

1.  No transaction is processed concurrently more than once.
2.  Retry attempts maintain the same OTel context.
3.  Success handler MUST run only if produce step succeeded.
4.  Exception handler MUST run only once per final failure scenario.
5.  Backoff behavior MUST match RFC-003 exactly.
6.  Lifecycle timeout MUST apply to each worker.
7.  Loop timeout MUST bound the entire operation.
8.  Concurrency MUST never exceed `loop.concurrency.value`.

---

## 17. Prohibited Behavior

Producer Engine MUST NOT:
- leak spans across unrelated transactions
- mutate user-provided policy objects
- swallow exceptions
- retry `BUSINESS` failures
- bypass success handler
- allow tasks to perform I/O directly

---

## 18. Security Considerations

The engine MUST:
- bind context so sensitive metadata does not leak between transactions
- never log raw payloads unless configured to do so
- ensure service credentials remain inside Services/Connectors
- isolate worker execution environments

---

## 19. Backwards Compatibility Rules

- New policy fields MUST provide backwards-compatible defaults.
- Retry/timeouts MUST NOT change semantics without major RFC revision.
- Span naming conventions MUST remain stable.

---

## 20. Reference Implementation

Implementation is authoritative:

- `src/ergon_framework/task/mixins/producer.py`
- `src/ergon_framework/task/mixins/utils.py`
- `src/ergon_framework/task/mixins/policies.py`
- `src/ergon_framework/task/exceptions.py`

This RFC defines expected behavior, which code MUST implement exactly.

---

## 21. Change Log

- **0.1.0**: Initial draft.

