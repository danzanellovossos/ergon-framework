import socket
from abc import ABC, ABCMeta, abstractmethod
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from .. import telemetry
from ..connector import Connector, ConnectorConfig, ServiceConfig


class TaskConfig(BaseModel):
    """
    Declarative configuration for a task.
    Specifies:
      - which Task class to run
      - input/output connectors
      - logger configuration
      - worker scaling
    """

    name: str
    task: object
    max_workers: int = 1
    connectors: Dict[str, ConnectorConfig] = Field(default_factory=dict)
    services: Dict[str, ServiceConfig] = Field(default_factory=dict)
    policies: List[Any] = Field(default_factory=list)
    logging: Optional[telemetry.logging.LoggingConfig] = None
    metrics: Optional[telemetry.metrics.MetricsConfig] = None
    tracing: Optional[telemetry.tracing.TracingConfig] = None

    @model_validator(mode="after")
    def validate(self) -> "TaskConfig":
        if not self.connectors:
            raise ValueError("Task must define at least one connector.")

        if self.max_workers < 1:
            raise ValueError("max_workers must be at least 1.")

        return self


class TaskMeta(ABCMeta):
    """
    Metaclass that injects connectors, services and execution policies into task instances.
    Consumed by BaseTask and BaseAsyncTask.
    """

    def __call__(
        cls,
        connectors: Dict[str, Connector],
        services: Dict[str, Any],
        policies: List[Any],
        worker_id: Optional[int] = None,
        task_config: Optional[TaskConfig] = None,
        *args,
        **kwargs,
    ):
        # ---------------------------------------------------------
        # Create instance normally
        # ---------------------------------------------------------
        self = super().__call__(
            connectors=connectors,
            services=services,
            policies=policies,
            worker_id=worker_id,
            task_config=task_config,
            *args,
            **kwargs,
        )

        # ---------------------------------------------------------
        # Store DI containers
        # ---------------------------------------------------------
        self.connectors = connectors
        self.services = services
        self.policies = policies
        self.worker_id = worker_id
        self.task_config = task_config

        # ---------------------------------------------------------
        # Expose connectors as attributes
        # Example: pipefy_connector, rabbitmq_connector
        # ---------------------------------------------------------
        for name, conn in connectors.items():
            setattr(self, f"{name}_connector", conn)

        # ---------------------------------------------------------
        # Expose services as attributes
        # Example: openai_service, s3_service
        # ---------------------------------------------------------
        for name, service in services.items():
            setattr(self, f"{name}_service", service)

        # ---------------------------------------------------------
        # Expose policies as attributes
        # Example: policy_name
        # ---------------------------------------------------------
        for policy in policies:
            name = policy.name or policy.__class__.__name__
            setattr(self, f"{name}_policy", policy)

        # ---------------------------------------------------------
        # Task identity
        # ---------------------------------------------------------
        self.name = getattr(self, "name", cls.__name__)

        return self


# ---------- BASE TASKS ----------


class BaseTask(ABC, metaclass=TaskMeta):
    """
    Base class for all tasks in the Ergon Task Framework.

    Behavior (produce/consume) is added via mixins:
    - ProduceMixin
    - ConsumeMixin
    """

    name = "base"

    def __init__(
        self,
        connectors: Dict[str, Connector],
        services: Dict[str, Any],
        policies: List[Any],
        worker_id: Optional[int] = None,
        task_config: Optional["TaskConfig"] = None,
        *args,
        **kwargs,
    ):
        # Don't pass args/kwargs to object.__init__()
        super().__init__()

    @abstractmethod
    def execute(self) -> Any:
        """Main entry point for running the task."""
        raise NotImplementedError

    def exit(self):
        """
        Gracefully exit the task.

        Tasks may override this to:
        - close resources
        - flush buffers
        - notify services

        IMPORTANT: By default, this does NOT kill the interpreter.
        """
        return


class BaseAsyncTask(ABC, metaclass=TaskMeta):
    """
    Asynchronous base class for all async tasks in the Ergon Task Framework.

    Behavior (async produce/consume) will be added via async mixins.
    """

    name = "base_async"

    def __init__(
        self,
        connectors: Dict[str, Connector],
        services: Dict[str, Any],
        policies: List[Any],
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

    @abstractmethod
    async def execute(self) -> Any:
        """Main async entry point for running the task."""
        raise NotImplementedError

    async def exit(self):
        """Optional async cleanup hook."""
        return


class TaskExecMetadata(BaseModel):
    task_name: str
    execution_id: str
    execution_start_time: str
    pid: int
    worker_id: int
    host_name: str = Field(default_factory=lambda: TaskExecMetadata._get_host_name())
    host_ip: str = Field(default_factory=lambda: TaskExecMetadata._get_host_ip())

    # ---------- STATIC HELPERS ----------
    @staticmethod
    def _get_host_ip() -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            s.close()

    @staticmethod
    def _get_host_name() -> str:
        return socket.gethostname()
