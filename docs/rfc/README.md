# Ergon Task Engine RFCs

This folder contains normative, implementation-derived RFCs for the Ergon Task engine.

The RFCs are generated from the concrete implementation in:

- `src/ergon_framework/task/mixins/consumer.py`
- `src/ergon_framework/task/mixins/producer.py`
- `src/ergon_framework/task/mixins/policies.py`
- `src/ergon_framework/task/mixins/utils.py`
- `src/ergon_framework/task/exceptions.py`

The goal is to specify the **engine-level semantics** of the Task execution model:

- Consumer engine (fetch → process → success/exception)
- Producer engine (produce → success/exception)
- Shared policy model (retry, backoff, batching, concurrency, timeouts)
- Contracts between Tasks, Connectors, and Exceptions
- Observability and OpenTelemetry requirements

The authoritative behavior is always the code; the RFCs are a structured restatement of that behavior.


