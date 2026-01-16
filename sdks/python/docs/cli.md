# CLI Reference

The Ergon CLI provides commands for listing, running, and managing tasks. This document covers all available commands, their options, and exit codes.

---

## Overview

The CLI is invoked via the `ergon` command, which is defined as an entry point in your project's `pyproject.toml`:

```toml
[project.scripts]
ergon = "my_project.main:main"
```

This entry point must import task configurations before invoking the CLI to ensure tasks are registered.

---

## Commands

### `ergon list`

Lists all registered tasks.

```bash
ergon list
```

**Output:**

```
Registered tasks:
  - order-ingestion
  - order-enrichment
  - order-dispatch
```

If no tasks are registered:

```
No tasks registered. Did you import your config files?
```

**Exit Codes:**

| Code | Meaning |
|------|---------|
| `0`  | Success |

---

### `ergon run <task-name>`

Runs a registered task by name.

```bash
ergon run <task-name>
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `task-name` | The name of the task to run (as defined in `TaskConfig.name`) |

**Example:**

```bash
ergon run order-ingestion
```

**Behavior:**

- Retrieves the task configuration from the task manager
- Initializes connectors, services, and telemetry
- Executes the task runner loop
- Handles graceful shutdown on interrupt signals

**Exit Codes:**

| Code | Meaning |
|------|---------|
| `0`  | Task completed successfully |
| `1`  | General error (task not found, configuration error) |
| `130` | Interrupted by SIGINT (Ctrl+C) |

---

### `ergon process-transaction`

Processes a single transaction with an inline JSON payload. Useful for testing and debugging.

```bash
ergon process-transaction <task> <policy> --payload-json '<json>'
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `task` | The name of the task |
| `policy` | The name of the policy to use |
| `--payload-json` | Transaction payload as a JSON string (required) |

**Example:**

```bash
ergon process-transaction order-processor consumer \
  --payload-json '{"order_id": "12345", "amount": 99.99}'
```

**Exit Codes:**

| Code | Meaning |
|------|---------|
| `0`  | Transaction processed successfully |
| `1`  | General error |
| `2`  | Invalid JSON payload |

---

### `ergon process-transaction-by-id`

Processes a single transaction by fetching it from the connector using its ID.

```bash
ergon process-transaction-by-id <task> <policy> <transaction-id>
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `task` | The name of the task |
| `policy` | The name of the policy to use |
| `transaction-id` | The ID of the transaction to fetch and process |

**Example:**

```bash
ergon process-transaction-by-id order-processor consumer tx-abc-123
```

**Exit Codes:**

| Code | Meaning |
|------|---------|
| `0`  | Transaction processed successfully |
| `1`  | General error (task not found, transaction not found) |

---

### `ergon bootstrap`

Scaffolds a new Ergon project with the recommended structure.

```bash
ergon bootstrap <project-name> [target-dir]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `project-name` | Name of the new project |
| `target-dir` | Target directory (default: current directory) |

**Example:**

```bash
ergon bootstrap my_pipeline
ergon bootstrap my_pipeline /path/to/projects
```

**Output:**

```
✔ Bootstrapped successfully at src/my_pipeline
```

**What It Creates:**

```
target-dir/
└── src/
    └── my_pipeline/
        ├── main.py
        ├── _observability/
        │   └── [telemetry configs]
        ├── connectors/
        │   └── __init__.py
        └── tasks/
            ├── __init__.py
            ├── settings.py
            ├── constants.py
            ├── schemas.py
            ├── exceptions.py
            ├── helpers.py
            └── example_task/
                ├── __init__.py
                ├── task.py
                ├── config.py
                ├── schemas.py
                ├── exceptions.py
                └── helpers.py
```

**Exit Codes:**

| Code | Meaning |
|------|---------|
| `0`  | Project created successfully |
| `1`  | Error (template missing, permission denied) |

---

## Exit Codes Summary

The CLI follows POSIX conventions for exit codes:

| Code | Category | Description |
|------|----------|-------------|
| `0`  | Success | Command completed successfully |
| `1`  | General Error | Command failed (configuration, runtime error) |
| `2`  | Usage Error | Invalid arguments or malformed input |
| `130` | Interrupted | Process terminated by SIGINT (128 + 2) |

---

## How the CLI Works

### Architecture

The CLI is implemented in `ergon.cli` and interacts with the task manager and runner:

```
ergon command
    │
    ▼
┌─────────────────┐
│  CLI Parser     │  argparse-based command routing
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Task Manager   │  Retrieves registered TaskConfig
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Task Runner    │  Executes task with connectors, services, telemetry
└─────────────────┘
```

**Important:** The CLI does not scan the filesystem. It only has access to tasks that are explicitly registered before commands execute.

### The `ergon()` Function

```python
def ergon(tasks: Optional[List[TaskConfig]] = None) -> None
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `tasks` | `Optional[List[TaskConfig]]` | List of task configurations to register before CLI execution. Tasks already registered are skipped. |

**Example:**

```python
from ergon.cli import ergon
from my_project.tasks import TASKS

def main():
    ergon(TASKS)  # Registers tasks, then executes CLI
```

### Task Registration

Tasks are **not** auto-discovered. The CLI is deterministic and explicit by design. There is one recommended way to register tasks:

#### Recommended: Pass Tasks Directly

Pass a list of `TaskConfig` objects to `ergon()`. Tasks are automatically registered if not already present:

```python
from ergon.cli import ergon
from my_project.tasks import TASKS

def main():
    ergon(TASKS)  # Registers all tasks in the list
```

Define your task list in `tasks/__init__.py`:

```python
from . import settings
from .document_search.config import TASK_DOCUMENT_SEARCH
from .data_extraction.config import TASK_DATA_EXTRACTION

settings.load_env()

TASKS = [
    TASK_DOCUMENT_SEARCH,
    TASK_DATA_EXTRACTION,
]
```

**Why tasks must be registered before CLI execution:**

1. The CLI does not scan the filesystem for tasks
2. Tasks must be explicitly imported and registered
3. The task manager only knows about tasks that were registered before `ergon()` is called
4. This ensures deterministic, predictable behavior

**The difference between passing tasks and import-based registration:**

- **Passing tasks to `ergon(TASKS)`**: Tasks are explicitly listed in `tasks/__init__.py` and passed to the CLI. This is the recommended approach.
- **Import-based registration**: Tasks are registered via `manager.register()` calls in their config modules when imported. This requires wildcard imports and linter suppressions.

The recommended approach is explicit, avoids linter warnings, and makes it clear which tasks are available at runtime.

---

## Invocation Methods

### Via Entry Point (Recommended)

```bash
ergon list
ergon run my-task
```

### Via Python Module

```bash
python -m my_project.main list
python -m my_project.main run my-task
```

### Programmatic Invocation

```python
from my_project.main import main
import sys

sys.argv = ["ergon", "run", "my-task"]
main()
```

---

## Next Steps

- **[Getting Started Guide](getting-started.md)** — Project setup and configuration
- **[Project Structure Guide](project-structure.md)** — Organizing your codebase

<br/>


