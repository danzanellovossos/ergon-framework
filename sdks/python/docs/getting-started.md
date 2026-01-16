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

## Step-by-Step Implementation

This section walks you through building an Ergon project from top to bottom, following production patterns.

### Step 1: Entry Point (`main.py`)

Every Ergon project has exactly one entry point: `main.py`. This file is minimal and focused—it imports tasks and hands control to the CLI.

```python
from ergon.cli import ergon
from my_project.tasks import TASKS

def main():
    ergon(TASKS)

if __name__ == "__main__":
    main()
```

**Key properties:**

- Single entry point
- CLI invoked via `ergon(TASKS)`
- Task registration is explicit
- No auto-discovery
- No environment loading here—that happens in `tasks/__init__.py`

This pattern ensures deterministic, predictable behavior suitable for production workloads.

---

### Step 2: Task Registration (`tasks/__init__.py`)

Tasks are registered in `tasks/__init__.py`. This file follows a specific pattern that ensures environment variables are loaded before task configurations are evaluated.

```python
from . import settings
from .document_search.config import TASK_DOCUMENT_SEARCH
from .data_extraction.config import TASK_DATA_EXTRACTION
from .xml_generation.config import TASK_XML_GENERATION
from .initial_routing.config import TASK_INITIAL_ROUTING

settings.load_env()

TASKS = [
    TASK_DOCUMENT_SEARCH,
    TASK_DATA_EXTRACTION,
    TASK_XML_GENERATION,
    TASK_INITIAL_ROUTING,
]

__all__ = [
    "settings",
    "TASK_DOCUMENT_SEARCH",
    "TASK_DATA_EXTRACTION",
    "TASK_XML_GENERATION",
    "TASK_INITIAL_ROUTING",
    "TASKS",
]
```

**Key properties:**

- `settings.load_env()` is executed before task configs are evaluated
- Tasks are explicitly listed in `TASKS`
- Import order matters and is intentional
- No auto-discovery or magic registration

**Why this order matters:**

1. **Settings imported first** — Makes `settings` module available
2. **Task configs imported** — Each config module is evaluated
3. **Environment loaded** — `settings.load_env()` makes environment variables available
4. **Tasks listed** — Explicit list of registered tasks

This ensures that when task configuration modules are evaluated, all environment variables are already loaded and available via `os.getenv()` in `settings.py`.

---

### Step 3: Settings Configuration (`tasks/settings.py`)

Environment variables are loaded in `tasks/settings.py` via `load_env()`. This function is called at the top of `settings.py`, before any connector or service configurations are created.

**Important:** `load_env()` is called in `settings.py`, not in `main.py`. This ensures environment variables are available when connector and service configurations are created.

See the [Project Structure Guide](project-structure.md#settingspy--the-control-center) for a complete `settings.py` example with telemetry, connectors, and services configuration.

---

### Step 4: Constants (`tasks/constants.py`)

Constants file contains retry policies, configuration objects, and business constants. It may reference schemas and environment variables.

See the [Project Structure Guide](project-structure.md#constantspy--configuration-and-constants) for a complete `constants.py` example.

---

### Step 5: Schemas (`tasks/schemas.py`)

Schemas file contains Pydantic models that multiple tasks use. This includes configuration schemas (for external systems), shared payload models, and output schemas.

See the [Project Structure Guide](project-structure.md#schemaspy--shared-data-models) for a complete `schemas.py` example.

### Environment File Pattern

The `load_env()` function uses the `ENV_FILE` environment variable to determine which `.env` file to load:

```bash
# Development
ENV_FILE=.env.local

# Production
ENV_FILE=.env.production

# Staging
ENV_FILE=.env.staging
```

If `ENV_FILE` is not set, `load_env()` will raise a `ValueError` with clear instructions.

### Why Environment Loading Exists

Even though `os.getenv()` works, Ergon provides `load_env()` for several reasons:

1. **Centralized loading** — Environment variables are loaded in one place, before any task configuration is evaluated
2. **Environment-specific files** — Supports `.env.local`, `.env.production`, etc. based on the `ENV_FILE` variable
3. **Consistent initialization** — Ensures all environment variables are available before connectors and services are configured
4. **Production-ready** — Explicit control over which environment file is loaded

### Why Tasks Never Touch Environment Variables Directly

Tasks reference pre-configured connectors and services from `settings.py`:

```python
# ❌ Don't do this in task code
api_key = os.getenv("OPENAI_API_KEY")

# ✅ Do this instead
from .. import settings
# Use settings.OPENAI_SERVICE which already has the API key configured
```

This separation ensures:

- **Single source of truth** — Configuration lives in `settings.py`
- **Easy testing** — Mock `settings` instead of mocking environment variables
- **Environment-agnostic tasks** — Tasks don't need to know which environment they're running in

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
│       ├── main.py                 # Single entry point: ergon(TASKS)
│       ├── connectors/             # Custom connectors (optional)
│       │   └── {connector_name}/
│       │       ├── connector.py
│       │       └── service.py
│       ├── services/               # Custom services (optional)
│       │   └── {service_name}/
│       │       ├── service.py
│       │       └── client.py
│       └── tasks/
│           ├── __init__.py         # Task registration + settings.load_env()
│           ├── settings.py         # Telemetry, connectors, services
│           ├── constants.py        # Shared constants, retry policies
│           ├── schemas.py          # Shared Pydantic models
│           ├── exceptions.py       # Shared exceptions
│           ├── helpers.py          # Shared utilities
│           └── {task_name}/
│               ├── __init__.py
│               ├── task.py         # Task implementation
│               ├── config.py       # TaskConfig definition
│               ├── schemas.py      # Task-specific models
│               ├── exceptions.py   # Task-specific exceptions
│               └── helpers.py      # Task-specific utilities
```

**Key points:**

- `main.py` is minimal—just imports `TASKS` and calls `ergon(TASKS)`
- `tasks/__init__.py` handles task registration and calls `settings.load_env()`
- `tasks/settings.py` is the control center for telemetry, connectors, and services
- `tasks/constants.py` contains retry policies and configuration objects
- `tasks/schemas.py` contains shared Pydantic models
- `connectors/` and `services/` are at the same level—both optional
- Each task has its own folder with `task.py` and `config.py` separated

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
- Environment variable access in task code
- Dev/prod branching logic in code

You have complete control over task registration through standard Python imports. Configuration is environment-driven, not code-driven.

---

## Next Steps

- **[CLI Reference](cli.md)** — Full command documentation and exit codes
- **[Project Structure Guide](project-structure.md)** — Organizing connectors, tasks, and shared modules
- **[Framework Architecture](../../../docs/architecture.md)** — Core concepts and design philosophy

<br/>

