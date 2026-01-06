import sys
from typing import Dict, List

from ..connector import Transaction
from .base import BaseAsyncTask, BaseTask, TaskConfig
from .runner import run_task


class TaskManager:
    """
    Global registry and orchestrator for Ergon tasks.
    Supports both synchronous (BaseTask) and asynchronous (BaseAsyncTask).
    """

    def __init__(self):
        self._registry: Dict[str, TaskConfig] = {}

    # -------------------------------------------------------------
    # REGISTER TASK
    # -------------------------------------------------------------
    def register(self, config: TaskConfig) -> None:
        """
        Register a task by its name inside TaskConfig.
        """

        # Validate task inheritance
        if not issubclass(config.task, (BaseTask, BaseAsyncTask)):
            raise TypeError(f"Task '{config.name}' must inherit from BaseTask or BaseAsyncTask. Got: {config.task}")

        # Duplicate check
        if config.name in self._registry:
            raise ValueError(f"Task name '{config.name}' already registered.")

        self._registry[config.name] = config

    # -------------------------------------------------------------
    # RUN TASK BY NAME
    # -------------------------------------------------------------
    def run(self, name: str, debug: bool = False, *args, **kwargs) -> None:
        if name not in self._registry:
            raise ValueError(f"Task '{name}' is not registered.")

        config = self._registry[name]
        sys.exit(run_task(config=config, debug=debug, mode="task", *args, **kwargs))

    def process_transaction(self, task: str, policy: str, transaction: Transaction, *args, **kwargs):
        if task not in self._registry:
            raise ValueError(f"Task '{task}' is not registered.")
        config = self._registry[task]
        sys.exit(run_task(
            config=config, debug=True, mode="transaction", transaction=transaction, policy=policy, *args, **kwargs
        ))

    def process_transaction_by_id(self, task: str, policy: str, transaction_id: str, *args, **kwargs) -> None:
        if task not in self._registry:
            raise ValueError(f"Task '{task}' is not registered.")
        config = self._registry[task]
        sys.exit(run_task(
            config=config, debug=True, mode="transaction", transaction_id=transaction_id, policy=policy, *args, **kwargs
        ))

    # -------------------------------------------------------------
    # LIST / GET
    # -------------------------------------------------------------
    def list_tasks(self) -> List[str]:
        return list(self._registry.keys())

    def get(self, name: str) -> TaskConfig:
        return self._registry.get(name)


manager = TaskManager()
