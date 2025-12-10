
# RFC-003: Policy Model

**Author**: Daniel A. Vossos  
**Status**: Draft  
**Version**: 0.1.0  
**Created**: 2025-12-04  
**Updated**: 2025-12-04  
**Depends on**: RFC-000, RFC-001, RFC-002  
**Supersedes**: None  

---

## 1. Abstract

This document defines the **Policy Model** for the Ergon Framework.
Policies are purely data-driven, immutable configurations that dictate the operational behavior of the Framework's engines (Consumer, Producer, Runners).
They govern retries, timeouts, backoff strategies, concurrency limits, batching, and loop lifecycles.

This RFC is normative.
Implementations of engines MUST strictly adhere to the semantics defined herein.

---

## 2. Motivation

In distributed systems, operational parameters (like how many times to retry, how long to wait, or how many threads to use) are often hardcoded or scattered across business logic. This leads to:
- Inconsistent resilience behavior across tasks.
- Difficulty in tuning system performance without code changes.
- "Magic numbers" buried in implementation details.

By centralizing these parameters into a structured, typed, and validated Policy Model, the Ergon Framework ensures that operational behavior is:
1.  **Explicit**: Defined in a single location.
2.  **Consistent**: Shared across all engines.
3.  **Configurable**: capable of being injected or loaded at runtime.
4.  **Decoupled**: Separated entirely from business logic (Tasks) and transport (Connectors).

---

## 3. Design Goals (Normative)

The Policy Model MUST:
1.  **Be Pure Data**: Policies contain only configuration values, never behavior or logic.
2.  **Be Serializable**: All policies must be expressible as JSON/dict structures.
3.  **Be Deterministic**: A given policy applied to a given sequence of events must yield the same operational decisions (e.g., backoff duration).
4.  **Enforce Safety**: Default values must be safe for production use (e.g., bounded retries, non-infinite loops).
5.  **Provide Isolation**: Step-level policies must not bleed into each other (e.g., fetch retries do not affect process retries).

---

## 4. Non-Goals

The Policy Model does not:
- Define *how* an engine implements the behavior (e.g., `sleep` vs `await asyncio.sleep` is an implementation detail).
- Include business rules or validation logic.
- Manage credentials or connection strings (this is for Connectors/Services).
- Provide dynamic adaptive algorithms (e.g., auto-scaling concurrency based on CPU load).

---

## 5. Formal Specification

### 6.1 Overview of the Policy System

Policies define the *operational behavior* of engines.
- **Structure**: Policies are hierarchical Pydantic models.
- **Immutability**: Engines MUST NOT mutate policy objects at runtime.
- **Scope**: Policies apply to the *engine lifecycle*, not the *task domain logic*.

### 6.2 RetryPolicy

The `RetryPolicy` controls the re-execution of a single operation (step) upon failure.

**Fields:**
- `max_attempts: int` (≥ 1)
    - Total number of attempts allowed, including the first one.
- `timeout: float | None`
    - Maximum duration (in seconds) for a **single attempt**.
    - `None` means no timeout is enforced by the engine.
- `backoff: float` (≥ 0.0)
    - Base delay (in seconds) before the first retry.
- `backoff_multiplier: float` (≥ 1.0)
    - Factor by which the delay increases after each subsequent failure.
- `backoff_cap: float` (≥ 0.0)
    - Maximum delay (in seconds) allowed. `0.0` means no cap (unbounded).

**Requirements:**
- Engines MUST interpret `max_attempts` as the strict upper bound of executions.
- Engines MUST wrap **each attempt** individually with the specified `timeout`.
- Engines MUST NOT apply the timeout to the aggregate duration of all attempts.

### 6.3 Backoff Model

Engines MUST compute the sleep duration between attempts using the following formula:

```python
delay = backoff * (backoff_multiplier ** attempt_index)
if backoff_cap > 0:
    delay = min(delay, backoff_cap)
```

- `attempt_index` is 0-based (0 = first retry, i.e., after the first failure).
- Engines MUST apply this delay **before** the next attempt is made.

### 6.4 Timeout Semantics

- **Per-Attempt**: The `timeout` field in `RetryPolicy` applies to a single execution of the step (e.g., one call to `process_transaction`).
- **Step vs Loop**: This timeout does NOT constrain the total time spent retrying.
- **Loop Timeout**: Loop-level timeouts are defined separately in `ConsumerLoopPolicy` / `ProducerLoopPolicy`.

### 6.5 ConcurrencyPolicy

Controls the parallelism of the engine.

**Fields:**
- `value: int` (≥ 1)
    - The target concurrency limit (number of active workers/tasks).
- `min: int` (≥ 1)
    - Validation boundary for minimum allowed concurrency.
- `max: int` (≥ 1)
    - Validation boundary for maximum allowed concurrency.

**Rules:**
- Engines MUST use `value` to size their thread pools or semaphores.
- Engines MUST ensure the number of concurrent in-flight transactions never exceeds `value`.

### 6.6 BatchPolicy

Controls the grouping of transactions during I/O.

**Fields:**
- `size: int` (≥ 1)
    - Target number of transactions per batch.
- `min_size: int` (≥ 1)
    - Minimum acceptable batch size (reserved for future use).
- `max_size: int` (≥ 1)
    - Maximum acceptable batch size (reserved for future use).
- `interval: float` (≥ 0.0)
    - Max time to wait for a batch to fill (reserved for future use).

**Rules:**
- Engines MUST request exactly `size` transactions from Connectors during fetch.
- Engines MUST slice input lists into chunks of `size` during produce.

### 6.7 EmptyQueuePolicy

Controls behavior when a Consumer fetches no data.

**Fields:**
- `backoff: float` (≥ 0.0)
- `backoff_multiplier: float` (≥ 1.0)
- `backoff_cap: float` (≥ 0.0)
- `interval: float` (Reserved, MUST be unused).

**Rules:**
- Applies **ONLY** when `streaming=True`.
- Engines MUST track consecutive empty fetches.
- Engines MUST apply exponential backoff (using the standard Backoff Model) between empty fetches to prevent busy-waiting.
- The counter MUST reset to 0 upon a successful fetch.

### 6.8 ConsumerSteps

Defines the policies for each phase of the Consumer lifecycle.

**Fields:**
- `fetch: FetchPolicy` (contains `RetryPolicy`, `extra: dict`)
- `process: ProcessPolicy` (contains `RetryPolicy`)
- `success: SuccessPolicy` (contains `RetryPolicy`)
- `exception: ExceptionPolicy` (contains `RetryPolicy`)

**Rules:**
- Each step MUST operate under its own independent retry envelope.
- Failures in one step MUST NOT consume retry budgets of other steps.

### 6.9 ConsumerLoopPolicy

Controls the outer execution loop of the Consumer.

**Fields:**
- `batch: BatchPolicy`
- `concurrency: ConcurrencyPolicy`
- `timeout: float | None`
    - Maximum duration for the entire `consume_transactions` call.
- `limit: int | None`
    - Maximum number of transactions to process before exiting.
- `transaction_timeout: float | None`
    - Hard timeout for the end-to-end lifecycle of a single transaction (fetch → process → finish).
- `streaming: bool`
    - `True`: Loop continues indefinitely (subject to timeouts), backing off on empty queues.
    - `False`: Loop terminates immediately upon the first empty fetch.
- `empty_queue: EmptyQueuePolicy`

**Rules:**
- Engines MUST enforce `transaction_timeout` on the worker task wrapping the transaction lifecycle.
- Engines MUST abort the loop if `timeout` (loop-level) is exceeded.

### 6.10 ConsumerPolicy (Top-Level)

The root configuration object for Consumers.

**Fields:**
- `loop: ConsumerLoopPolicy`
- `steps: ConsumerSteps`

**Rules:**
- This object serves as the complete, deterministic specification of Consumer behavior.
- Default values MUST be safe (finite retries, reasonable concurrency).

### 6.11 ProducerSteps

Defines the policies for each phase of the Producer lifecycle.

**Fields:**
- `produce: ProducePolicy` (contains `RetryPolicy`)
- `success: SuccessPolicy` (contains `RetryPolicy`)
- `exception: ExceptionPolicy` (contains `RetryPolicy`)

### 6.12 ProducerLoopPolicy

Controls the outer execution loop of the Producer.

**Fields:**
- `concurrency: ConcurrencyPolicy`
- `batch: BatchPolicy`
- `timeout: float | None`
- `limit: int | None`
- `transaction_timeout: float | None`

**Rules:**
- Same semantics as `ConsumerLoopPolicy`, except `streaming` and `empty_queue` are absent (Producers process a finite input set).

### 6.13 ProducerPolicy (Top-Level)

The root configuration object for Producers.

**Fields:**
- `loop: ProducerLoopPolicy`
- `steps: ProducerSteps`

### 6.14 Policy Immutability & Versioning Rules

- **Immutability**: Once instantiated, Policy objects MUST NOT be modified.
- **Versioning**: Changes to Policy schemas MUST be managed via RFC updates.
- **Compatibility**: New fields MUST have default values that preserve existing behavior.

---

## 7. Invariants (Normative)

1.  **Purity**: Policies MUST NOT contain executable code (lambdas, functions).
2.  **Types**: All durations MUST be represented as `float` (seconds).
3.  **Bounds**:
    - `concurrency.value` MUST be ≥ 1.
    - `backoff_multiplier` MUST be ≥ 1.0.
    - `max_attempts` MUST be ≥ 1.
4.  **Locality**: An engine MUST evaluate policies locally within its context; policies MUST NOT depend on shared global state.

---

## 8. Cross-Component Contract Requirements

- **Tasks**: MUST NOT access, inspect, or mutate Policy objects. Tasks are policy-agnostic.
- **Runners**: MUST validate Policy objects before initializing the Engine.
- **Connectors**: MUST NOT read Policy objects. Connectors simply execute requests; operational wrapping is the Engine's job.
- **Services**: MUST NOT reference Policy objects.

---

## 9. Examples of Valid and Invalid Policy Configurations

### Valid Configuration 1: High-Throughput Batch Consumer
```python
ConsumerPolicy(
    loop=ConsumerLoopPolicy(
        batch=BatchPolicy(size=100),
        concurrency=ConcurrencyPolicy(value=10),
        streaming=True
    ),
    steps=ConsumerSteps(
        process=ProcessPolicy(retry=RetryPolicy(max_attempts=3, timeout=5.0))
    )
)
```
*Explanation*: Processes 100 items at a time with 10 workers, retrying logic up to 3 times.

### Valid Configuration 2: Strict Exactly-Once-Attempt Producer
```python
ProducerPolicy(
    steps=ProducerSteps(
        produce=ProducePolicy(retry=RetryPolicy(max_attempts=1))
    )
)
```
*Explanation*: `max_attempts=1` means "try once, never retry". Suitable for non-idempotent sinks.

### Valid Configuration 3: Long-Polling Streamer
```python
ConsumerPolicy(
    loop=ConsumerLoopPolicy(
        streaming=True,
        empty_queue=EmptyQueuePolicy(backoff=1.0, backoff_cap=60.0)
    )
)
```
*Explanation*: Backs off up to 60s when the queue is empty to reduce API costs.

### Invalid Configuration 1: Zero Attempts
```python
RetryPolicy(max_attempts=0)
```
*Violation*: `max_attempts` MUST be ≥ 1.

### Invalid Configuration 2: Infinite Concurrency
```python
ConcurrencyPolicy(value=-1)
```
*Violation*: `value` MUST be ≥ 1. Negative values for "unlimited" are not supported; explicit limits are required.

### Invalid Configuration 3: Accelerating Backoff < 1
```python
RetryPolicy(backoff_multiplier=0.5)
```
*Violation*: `backoff_multiplier` MUST be ≥ 1.0. Decaying intervals are not a supported pattern.

---

## 10. Backwards Compatibility & Migration Rules

- **Defaults**: If a new policy field is added, its default value MUST result in behavior identical to the previous version.
- **Deprecation**: If a field is removed, it MUST remain present but ignored (with a warning) for at least one minor version cycle.

---

## 11. Reference Implementation Notes

The reference implementation resides in `src/ergon_framework/task/mixins/policies.py`.
It uses Pydantic models to enforce types and validation rules (e.g., `ge=1`) at runtime.

---

## 12. Change Log

- **0.1.0**: Initial draft based on RFC-000 system overview.
