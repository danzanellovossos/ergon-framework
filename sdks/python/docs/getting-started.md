# Getting Started with the Python SDK

This guide walks you through setting up a project with the Ergon Python SDK—from installation to running your first task.

---

## Installation

Ergon is not yet published to PyPI. To use the framework, clone the repository and install it as a local dependency.

### Clone the Repository

```bash
git clone https://github.com/your-org/ergon-framework.git
```

### Install as a Path Dependency

Add Ergon to your project's `pyproject.toml`:

```toml
[project]
dependencies = [
    "ergon @ file:///path/to/ergon-framework/sdks/python",
]
```

Or install directly in editable mode:

```bash
pip install -e /path/to/ergon-framework/sdks/python
```

With `uv`:

```bash
uv add /path/to/ergon-framework/sdks/python
```

---

## Project Configuration

### Required `pyproject.toml` Settings

Your project must configure the package structure and define a CLI entry point.

```toml
[project]
name = "my_project"
version = "0.1.0"
dependencies = [
    "ergon @ file:///path/to/ergon-framework/sdks/python",
]

[project.scripts]
ergon = "my_project.main:main"

[tool.setuptools]
package-dir = {"" = "src"}

[tool.setuptools.packages.find]
where = ["src"]
```

#### Entry Point Configuration

The `[project.scripts]` section defines the CLI entry point:

```toml
[project.scripts]
ergon = "my_project.main:main"
```

This configuration:

- Creates an `ergon` command that invokes your project's `main()` function
- Ensures task configurations are imported before the CLI executes
- Enables `ergon run <task-name>` to discover and run your registered tasks

---

## Task Registration

Ergon uses **explicit imports** for task registration. Tasks are registered when their configuration modules are imported—the framework does not auto-discover tasks from the filesystem.

**If a task config is not imported, it is not registered.**

This is an intentional design decision that provides:

- Full control over which tasks are available at runtime
- Predictable, deterministic behavior
- No hidden dependencies on filesystem structure or naming conventions

### How Registration Works

When a task's `config.py` module is imported, it typically calls `manager.register(TaskConfig)`. This registers the task with the framework's task manager, making it available to the CLI.

---

## Entry Point Setup

Create a `main.py` file in your project that serves as the CLI entry point:

```python
from ergon.cli import ergon
from ergon.utils import load_env

load_env()

# Import task configs for registration
from my_project.tasks.example_task.config import *

def main():
    ergon()
```

### What This Pattern Ensures

1. **Environment variables are loaded** before any task configuration is evaluated
2. **Task configurations are imported** and registered with the task manager
3. **The Ergon CLI has access** to all registered tasks when it executes

### Registering Multiple Tasks

Import each task's config module to register it:

```python
from ergon.cli import ergon
from ergon.utils import load_env

load_env()

# Import all task configs
from my_project.tasks.order_ingestion.config import *
from my_project.tasks.order_enrichment.config import *
from my_project.tasks.order_dispatch.config import *

def main():
    ergon()
```

---

## Running Tasks

### List Registered Tasks

```bash
ergon list
```

Output:

```
Registered tasks:
  - order-ingestion
  - order-enrichment
  - order-dispatch
```

### Run a Task

```bash
ergon run <task-name>
```

Example:

```bash
ergon run order-ingestion
```

### Alternative Invocation

You can also run tasks directly via Python:

```bash
python -m my_project.main run <task-name>
```

This is useful for debugging or when the `ergon` command is not available in your PATH.

---

## Project Structure

A typical Ergon project follows this structure:

```
my_project/
├── pyproject.toml
├── src/
│   └── my_project/
│       ├── __init__.py
│       ├── main.py                 # CLI entry point
│       ├── connectors/             # Custom connectors
│       │   └── {connector_name}/
│       │       ├── connector.py
│       │       └── service.py
│       └── tasks/
│           ├── __init__.py
│           ├── settings.py         # Shared connector/service configs
│           ├── constants.py        # Shared constants
│           ├── schemas.py          # Shared Pydantic models
│           ├── exceptions.py       # Shared exceptions
│           ├── helpers.py          # Shared utilities
│           └── {task_name}/
│               ├── __init__.py
│               ├── task.py         # Task implementation
│               ├── config.py       # TaskConfig and registration
│               ├── schemas.py      # Task-specific models
│               ├── exceptions.py   # Task-specific exceptions
│               └── helpers.py      # Task-specific utilities
```

For detailed guidance on project organization, see the [Project Structure Guide](project-structure.md).

---

## Bootstrapping a New Project

Use the CLI to scaffold a new project with the recommended structure:

```bash
ergon bootstrap my_project
```

This creates a project skeleton with:

- Entry point (`main.py`)
- Example task with config, schemas, and helpers
- Shared module templates
- Observability configuration templates

---

## What Ergon Does Not Require

Ergon's explicit registration model means you do **not** need:

- Filesystem scanning or directory conventions
- Magic discovery mechanisms
- Framework-level auto-imports
- Decorator-based registration at module load time

You have complete control over task registration through standard Python imports.

---

## Next Steps

- **[CLI Reference](cli.md)** — Full command documentation and exit codes
- **[Project Structure Guide](project-structure.md)** — Organizing connectors, tasks, and shared modules
- **[Framework Architecture](../../../docs/architecture.md)** — Core concepts and design philosophy

<br/>

