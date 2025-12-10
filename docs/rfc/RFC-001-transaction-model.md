
# RFC-001: Transaction Model

**Author**: Daniel A. Vossos  
**Status**: Draft  
**Version**: 0.1.0  
**Created**: 2025-12-04  
**Updated**: 2025-12-04  
**Depends on**: RFC-000  
**Supersedes**: None  

---

## 1. Abstract

This document defines the **Transaction** as the atomic, immutable unit of work within the Ergon Framework.
It specifies the data model, identity guarantees, lifecycle boundaries, and invariants that all Transactions must obey.
The Transaction is the sole currency of exchange between Connectors and Tasks.

This RFC is normative.
Implementations MUST comply with the rules defined herein.

---

## 2. Motivation

In distributed automation systems, ambiguity about what constitutes a "unit of work" leads to:
- Inconsistent retry behavior.
- Loss of context across producer/consumer boundaries.
- Difficulties in tracing execution flow end-to-end.
- Leaky abstractions where transport details bleed into business logic.

To guarantee deterministic execution, retries, and observability (RFC-000 Goals 4 & 5), the framework requires a standardized, transport-agnostic envelope for data.
The Transaction Model provides this standardization.

---

## 3. Design Goals (Normative)

The Transaction Model MUST:
1.  **Guarantee Atomicity**: A Transaction represents the smallest indivisible unit of computation.
2.  **Enforce Immutability**: Once created, a Transaction’s identity and payload cannot change.
3.  **Preserve Context**: Metadata (IDs, timestamps, attempts) must persist across retry boundaries.
4.  **Abstract Transport**: The Transaction payload must be decoupled from the connector that produced it.
5.  **Enable Tracing**: Every Transaction must carry correlation identifiers for observability.

---

## 4. Non-Goals

The Transaction Model does not:
- Enforce schema validation on the payload (this is a user-level concern).
- Define serialization formats for transport (this is a Connector concern).
- Manage state persistence or durability (this is an infrastructure concern).
- Track complex DAG dependencies (this is a workflow orchestration concern).

---

## 5. Formal Specification

### 5.1 Definition of a Transaction

A **Transaction** is a transport-agnostic, schema-flexible envelope containing:
1.  **Identity** (Unique ID)
2.  **Metadata** (System context, timestamps, correlation IDs)
3.  **Payload** (Business data)

It is the atomic unit of processing for the Ergon Task Engine.
Tasks MUST accept Transactions as input and MAY produce Transactions as output.

### 5.2 Transaction Identity

Every Transaction MUST possess a globally unique identifier (`transaction_id`).
- The ID MUST be a non-empty string.
- The ID MUST be generated at creation time (typically by the Ingress Connector or Producer Task).
- The ID MUST remain stable across all retry attempts of the same logical work unit.
- The ID MUST be preserved across producer-consumer boundaries where applicable (e.g., via message headers).

Transactions MAY include a `parent_id` to explicitly link child transactions to a causal parent, supporting distributed tracing chains.

### 5.3 Transaction Immutability

Transactions are strictly immutable.
- **Mutation Prohibition**: User code MUST NOT modify a Transaction instance in place.
- **Transformation**: Operations that transform data MUST produce a **new** Transaction instance with a new ID (unless explicitly forwarding the payload, in which case a new envelope is still created).
- **Determinism**: This immutability guarantees that concurrent workers or retries operating on the same Transaction input start from an identical state.

### 5.4 Transaction Metadata Model

The Transaction envelope MUST contain the following metadata fields:

| Field | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `transaction_id` | `str` | Yes | Globally unique identifier. |
| `correlation_id` | `str` | No | ID spanning a larger business workflow or saga. |
| `trace_id` | `str` | No | OpenTelemetry trace ID context. |
| `created_at` | `datetime` | Yes | UTC timestamp of creation. |
| `source` | `str` | No | Identifier of the producing connector/system. |
| `metadata` | `dict` | No | Arbitrary KV pairs for system context (not business data). |

The engine MUST maintain internal, non-user-editable metadata for:
- `attempt_count`: Current processing attempt number.
- `retry_history`: Timestamps of previous attempts (optional).

### 5.5 Transaction Payload

The `payload` field holds the business data.
- **Type**: Arbitrary Python object (dict, pydantic model, str, bytes).
- **Constraints**:
    - MUST NOT contain transport-specific objects (e.g., `boto3` clients, socket handles).
    - MUST NOT contain Connector internal state.
- **Validation**: The framework treats the payload as opaque. Validation is the responsibility of the Task logic.

### 5.6 Transaction Context Propagation

The Transaction acts as the carrier for distributed context.
- **OTEL Propagation**: The Transaction MUST carry trace context (Trace ID / Span ID) to allow distributed tracing across boundaries.
- **Isolation**: Context MUST NOT leak between unrelated transactions processed by the same worker process.
- **Stability**: Context data MUST remain attached to the Transaction throughout the entire `Fetch → Process → (Success|Exception)` lifecycle, including all retries.

### 5.7 Transaction Lifecycle Boundaries

The lifecycle of a Transaction is strictly defined:

1.  **Creation**: Instantiated by a Connector (ingress) or a Task (egress).
2.  **Ingress**: Returned by `Connector.fetch_transactions()`.
3.  **Processing**: Passed to `Task.process_transaction()`.
    - This phase includes all retries.
4.  **Completion**:
    - **Success**: Handled by `handle_transaction_success`.
    - **Exception**: Handled by `handle_transaction_exception`.
5.  **Egress**: (If applicable) Passed to `Connector.produce()` via a Producer Task.

A Transaction ceases to exist from the engine's perspective once the final handler completes or the loop terminates.

### 5.8 Error Semantics and Association

Errors occurring during the processing of a Transaction are intimately bound to it.
- **Association**: Any exception raised during the lifecycle MUST be associated with the `transaction_id`.
- **Wrapper**: The framework MUST wrap failures in `TransactionException` (as defined in RFC-000), embedding the `transaction_id`.
- **Logging**: Logs regarding the error MUST include the Transaction's identity and metadata.

---

## 6. Invariants (Normative)

1.  **Indivisibility**: A Task MUST process a Transaction as a whole. Partial processing state MUST NOT be stored on the Transaction itself.
2.  **Identity Persistence**: A Transaction's ID MUST NOT change during retries.
3.  **Payload Integrity**: The engine MUST NOT modify the payload. Only the Task may create a *new* Transaction with a modified payload.
4.  **Context Continuity**: Trace context entering with a Transaction MUST be propagated to any outbound Transactions created as a direct result.

---

## 7. Serialization Rules

While the Transaction object exists in memory as a Python object, it must support serialization for transport.
- **Lossless**: Serialization mechanics MUST support lossless round-tripping of ID, Metadata, and Payload.
- **Ownership**: Connectors own the serialization/deserialization logic (e.g., JSON, Protobuf, Avro).
- **Transparency**: Tasks MUST interact with deserialized Transaction objects, never raw bytes.

---

## 8. Observability Requirements

Every Transaction MUST be observable end-to-end.
- **Tracing**: A root span (or child span of an incoming trace) MUST be created for each Transaction lifecycle.
- **Attributes**: The span MUST include `transaction.id`, `transaction.source`, and `transaction.attempt`.
- **Correlation**: All logs emitted during the Transaction's lifecycle MUST include the `transaction_id`.

---

## 9. Security Considerations

- **Credential Isolation**: Transactions MUST NEVER contain secrets, credentials, or API keys in their metadata or payload.
- **Metadata Sanitization**: Connectors MUST sanitize incoming metadata before populating the Transaction object to prevent injection attacks or context pollution.

---

## 10. Backwards Compatibility Rules

- **Schema Evolution**: Changes to the internal Transaction class structure MUST be versioned.
- **Legacy Fields**: Removed metadata fields MUST remain accessible as deprecated properties for at least one major version.

---

## 11. Reference Implementation Notes

The authoritative class definition resides in `src/ergon_framework/connector/transaction.py` (or equivalent).
It is expected to be a Pydantic model or dataclass to enforce type safety and immutability where possible.

---

## 12. Change Log

- **0.1.0**: Initial draft based on RFC-000 system overview.

