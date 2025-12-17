# Framework Project Structure

This document describes the conceptual and repository-level structure of the Ergon Framework. It is language-agnostic and applies to the framework as a whole, not any specific SDK implementation.

For SDK-specific project structure, tooling, and implementation details, refer to the documentation within each SDK directory.

---

## Repository Layout

The Ergon Framework is organized as a monorepo with clear separation between framework-level concerns and SDK implementations.

```
ergon-framework/
├── docs/           # Canonical framework documentation
├── sdks/           # SDK implementations per language
└── assets/         # Shared visual assets
```

### `docs/` — Framework Documentation

This directory contains the canonical, language-agnostic documentation for the Ergon Framework:

- **Architecture documentation** — High-level system design and component relationships
- **Module documentation** — Conceptual guides for each framework module (Transaction, Task, Connector, Service, Telemetry)
- **RFCs** — Formal specifications that define framework behavior, invariants, and guarantees

*  Everything in `docs/` describes *what* the framework does and *why*, without prescribing *how* any particular language should implement it.

### `sdks/` — SDK Implementations

Each SDK lives in its own subdirectory under `sdks/`. An SDK is a complete, language-specific implementation of the Ergon Framework specification.

SDKs are self-contained:

- Each SDK has its own build system, dependency management, and tooling
- Each SDK may define its own internal project structure
- Each SDK maintains its own documentation for language-specific concerns

The `docs/` directory at the repository root does not describe SDK internals. SDK project structure is documented within each SDK.

### `assets/` — Shared Visual Assets

Diagrams, logos, and other visual assets used across documentation.

---

## Framework Conceptual Structure

The Ergon Framework is built around five core concepts, organized into distinct layers. These concepts are language-agnostic—they define responsibilities and boundaries, not implementation details.

### Architectural Layers

```
┌─────────────────────────────────────────┐
│          Application Logic              │  ← Tasks
├─────────────────────────────────────────┤
│          Ergon Task Engine              │  ← Execution, Policies
├─────────────────────────────────────────┤
│       Connectors and Services           │  ← Transport, Capabilities
├─────────────────────────────────────────┤
│             Telemetry                   │  ← Observability
└─────────────────────────────────────────┘
```

Each layer has strict boundaries. Higher layers depend on lower layers, never the reverse.

### Core Concepts

#### Transaction

The **Transaction** is the fundamental unit of work in Ergon. It represents a single, atomic piece of data that must be processed indivisibly.

A Transaction is:

- **Immutable** — Once created, it cannot be modified
- **Traceable** — Every transaction carries a unique identifier for observability
- **Atomic** — The framework never splits or merges transactions

Everything else in the framework exists to support the lifecycle of Transactions.

#### Task

A **Task** contains business logic and nothing else. Tasks are deliberately thin:

- They transform, validate, enrich, and make decisions
- They do not perform I/O, manage connections, or handle retries
- They receive their dependencies (Connectors, Services) via injection

This constraint ensures business logic is pure, testable, and transport-agnostic.

Tasks are extended with **Mixins** that add specific behaviors:

- **Consumer** — Receives and processes Transactions from a source
- **Producer** — Prepares and dispatches Transactions to a destination
- **Hybrid** — Combines consumption and production in a single atomic operation

#### Connector

A **Connector** bridges the gap between the framework's Transaction model and external systems. Connectors have two responsibilities:

1. **Fetch** — Retrieve raw data from an external system and wrap it as Transactions
2. **Dispatch** — Receive Transactions and send their payloads to an external system

Connectors define the transaction boundary. They transform protocol-specific data into typed, traceable, atomic units of work.

#### Service

A **Service** is a standalone component that knows how to communicate with an external system. Services handle:

- Protocol mechanics (headers, authentication, serialization)
- Connection management (pools, sessions, keepalives)
- Resilience (retries, backoff, circuit breakers)

Services are independent of the Ergon task framework. They can be used anywhere and are designed to be reusable and testable in isolation.

**Key distinction**: Services do not know about Transactions. Connectors wrap Services and provide the Transaction interface.

#### Telemetry

The **Telemetry** layer provides observability across all framework components:

- **Logging** — Structured, contextual logging with automatic trace correlation
- **Tracing** — Distributed tracing with hierarchical span structure
- **Metrics** — Throughput, latency, retry counts, and failure rates

Telemetry is automatic. User code does not need to manually instrument logs or create spans—the framework handles observability by default.

### Concept Relationships

```
              ┌──────────────────┐
              │   Transaction    │
              └────────┬─────────┘
                       │ flows through
         ┌─────────────┼─────────────┐
         ▼             ▼             ▼
   ┌──────────┐  ┌──────────┐  ┌──────────┐
   │ Connector│  │   Task   │  │ Connector│
   │  (fetch) │  │ (process)│  │(dispatch)│
   └────┬─────┘  └────┬─────┘  └────┬─────┘
        │             │             │
        │      ┌──────┴──────┐      │
        │      ▼             ▼      │
        │ ┌─────────┐ ┌─────────┐   │
        └─│ Service │ │ Service │───┘
          └─────────┘ └─────────┘
```

- **Transactions** flow from source Connectors, through Tasks, to destination Connectors
- **Tasks** use injected Services for external capabilities (enrichment, lookups)
- **Connectors** use Services internally to handle protocol mechanics
- **Telemetry** observes the entire flow automatically

### Why This Separation Exists

The layered architecture enforces strict separation of concerns:

| Layer               | Responsibility                   | Must Not                          |
| ------------------- | -------------------------------- | --------------------------------- |
| **Task**      | Business logic                   | Perform I/O or manage concurrency |
| **Connector** | Transaction boundary, transport  | Apply business rules              |
| **Service**   | Protocol mechanics, capabilities | Control execution flow            |
| **Telemetry** | Observability                    | Require user instrumentation      |

This separation provides:

- **Testability** — Business logic can be tested with plain Transaction objects, no mocks required
- **Reusability** — The same Task works with different transports
- **Maintainability** — Clear boundaries make it obvious where to look when something breaks
- **Scalability** — Infrastructure concerns are handled uniformly, not reinvented per task

---

## SDK Boundary and Responsibilities

### What an SDK Does

Each SDK maps the framework concepts into language-specific constructs:

- **Transaction** becomes an immutable data structure in the target language
- **Task** becomes a class or type that developers extend
- **Connector** becomes an interface that adapters implement
- **Service** becomes a protocol for external integrations
- **Telemetry** becomes instrumentation hooks compatible with the language ecosystem

SDKs are responsible for:

- Providing idiomatic APIs for the target language
- Implementing the execution engine (concurrency, retries, timeouts)
- Integrating with the language's package ecosystem
- Offering CLI tooling for running and managing tasks
- Maintaining SDK-specific documentation

### What an SDK Does Not Do

SDKs do not define framework semantics. The behavior of Transactions, the lifecycle of Tasks, the retry policies, the telemetry requirements—these are defined at the framework level in the RFCs.

An SDK is a faithful implementation of the specification, not an interpretation of it.

### SDK Isolation

SDKs are isolated under `sdks/<language>/`:

- Each SDK has its own directory structure
- Each SDK has its own tooling and build system
- Each SDK has its own documentation for language-specific concerns

SDK project structure—how files are organized, how tasks are registered, how configuration is managed—is an SDK concern. It is documented within each SDK, not here.

### Cross-SDK Consistency

While SDKs are isolated, they share common principles:

- All SDKs implement the same conceptual model
- All SDKs follow the same execution semantics
- All SDKs produce compatible telemetry

This ensures that knowledge transfers across SDKs. Understanding Ergon in one language prepares you for Ergon in another.

---

## Related Documentation

- [Architecture](./architecture.md) — Detailed architectural design and component interactions
- [RFC-000: System Overview](./rfc/RFC-000-system-overview.md) — Formal specification of framework invariants
- Module documentation in `docs/modules/` — Deep dives into each conceptual module

For SDK-specific documentation, navigate to the relevant SDK directory under `sdks/`.
