import asyncio
import os
import signal
import threading
import traceback
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from enum import IntEnum
from typing import Literal

from ..connector import Transaction
from ..telemetry import logging, metrics, tracing
from .base import (
    BaseAsyncTask,
    BaseTask,
    TaskConfig,
    TaskExecMetadata,
)

# =============================================================
# EXIT CODES (POSIX-ALIGNED)
# =============================================================


class ExitCode(IntEnum):
    SUCCESS = 0
    ERROR = 1
    CONFIG_ERROR = 2

    SIGINT = 130  # 128 + SIGINT(2)
    SIGTERM = 143  # 128 + SIGTERM(15)


# =============================================================
# SHUTDOWN STATE (PROCESS-LOCAL)
# =============================================================

_shutdown_event = threading.Event()
_shutdown_signal: int | None = None


def _signal_handler(signum, frame):
    global _shutdown_signal
    _shutdown_signal = signum
    _shutdown_event.set()


def _install_signal_handlers():
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)


def is_shutdown_requested() -> bool:
    return _shutdown_event.is_set()


def get_shutdown_exit_code() -> ExitCode:
    if _shutdown_signal == signal.SIGINT:
        return ExitCode.SIGINT
    if _shutdown_signal == signal.SIGTERM:
        return ExitCode.SIGTERM
    return ExitCode.ERROR


# =============================================================
# TELEMETRY INITIALIZATION
# =============================================================


def __init_telemetry(config: TaskConfig, task_exec_metadata: TaskExecMetadata):
    if getattr(config, "logging", None):
        logging._apply_logging_config(cfg=config.logging, metadata=task_exec_metadata)

    if getattr(config, "tracing", None):
        tracing._apply_tracing_config(cfg=config.tracing, metadata=task_exec_metadata)

    if getattr(config, "metrics", None):
        metrics._apply_metrics_config(cfg=config.metrics, metadata=task_exec_metadata)


# =============================================================
# ASYNC TRANSACTION EXECUTION
# =============================================================


async def __run_transaction_async(
    instance: BaseAsyncTask,
    policy: str,
    transaction: Transaction = None,
    transaction_id: str = None,
):
    policy_obj = next((p for p in instance.policies if p.name == policy), None)
    if not policy_obj:
        raise ValueError(f"Policy '{policy}' not found")

    if not transaction and not transaction_id:
        raise ValueError("Either transaction or transaction_id must be provided")

    if transaction_id:
        conn = instance._resolve_connector(policy_obj.fetch.connector_name)
        transaction = await conn.fetch_transaction_by_id_async(transaction_id)

    success, result = await instance._start_processing(transaction, policy_obj)
    if not success:
        raise result
    return result


# =============================================================
# ASYNC TASK EXECUTION
# =============================================================


async def __run_task_async(
    config: TaskConfig,
    mode: Literal["task", "transaction"] = "task",
    *args,
    **kwargs,
):
    if not issubclass(config.task, BaseAsyncTask):
        raise ValueError(f"Invalid async task: {config.task}")

    worker_id = kwargs.pop("worker_id", None)

    task_exec_metadata = TaskExecMetadata(
        task_name=config.name,
        execution_id=str(uuid.uuid4()),
        execution_start_time=datetime.now().isoformat(),
        pid=os.getpid(),
        worker_id=worker_id,
    ).model_dump()

    __init_telemetry(config, task_exec_metadata)
    tracer = tracing.get_tracer(f"task.{config.name}")

    instance = None

    try:
        async with tracer.start_as_current_span(
            f"{config.task.__name__}.run",
            attributes={"task.execution.id": task_exec_metadata["execution_id"]},
        ):
            connectors = {}
            for name, cfg in config.connectors.items():
                conn = cfg.connector(*cfg.args, **cfg.kwargs)
                if hasattr(conn, "init_async"):
                    await conn.init_async()
                connectors[name] = conn

            services = {name: cfg.service(*cfg.args, **cfg.kwargs) for name, cfg in config.services.items()}

            instance = config.task(
                connectors=connectors,
                services=services,
                policies=config.policies,
                worker_id=worker_id,
                task_config=config,
                *args,
                **kwargs,
            )

            if mode == "transaction":
                await __run_transaction_async(
                    instance=instance,
                    policy=kwargs.get("policy"),
                    transaction=kwargs.get("transaction"),
                    transaction_id=kwargs.get("transaction_id"),
                )
            else:
                await instance.execute()

    finally:
        if instance is not None:
            await instance.exit()


# =============================================================
# SYNC TRANSACTION EXECUTION
# =============================================================


def __run_transaction_sync(
    instance: BaseTask,
    policy: str,
    transaction: Transaction = None,
    transaction_id: str = None,
):
    policy_obj = next((p for p in instance.policies if p.name == policy), None)
    if not policy_obj:
        raise ValueError(f"Policy '{policy}' not found")

    if not transaction and not transaction_id:
        raise ValueError("Either transaction or transaction_id must be provided")

    if transaction_id:
        conn = instance._resolve_connector(policy_obj.fetch.connector_name)
        transaction = conn.fetch_transaction_by_id(transaction_id)

    success, result = instance._start_processing(transaction, policy_obj)
    if not success:
        raise result
    return result


# =============================================================
# SYNC TASK EXECUTION
# =============================================================


def __run_task_sync(
    config: TaskConfig,
    mode: Literal["task", "transaction"] = "task",
    *args,
    **kwargs,
):
    if not issubclass(config.task, BaseTask):
        raise ValueError(f"Invalid sync task: {config.task}")

    worker_id = kwargs.pop("worker_id", None)

    task_exec_metadata = TaskExecMetadata(
        task_name=config.name,
        execution_id=str(uuid.uuid4()),
        execution_start_time=datetime.now().isoformat(),
        pid=os.getpid(),
        worker_id=worker_id,
    ).model_dump()

    __init_telemetry(config, task_exec_metadata)
    tracer = tracing.get_tracer(__name__)

    instance = None

    with tracer.start_as_current_span(
        f"{config.task.__name__}.run",
        attributes={"task.execution.id": task_exec_metadata["execution_id"]},
    ):
        try:
            connectors = {}
            with tracer.start_as_current_span(
                f"{config.task.__name__}.connectors.init",
                attributes={"task.execution.id": task_exec_metadata["execution_id"]},
            ):
                for name, cfg in config.connectors.items():
                    with tracer.start_as_current_span(
                        f"{config.task.__name__}.connectors.{name}.init",
                        attributes={"task.execution.id": task_exec_metadata["execution_id"]},
                    ):
                        connectors[name] = cfg.connector(*cfg.args, **cfg.kwargs)

            services = {}
            with tracer.start_as_current_span(
                f"{config.task.__name__}.services.init",
                attributes={"task.execution.id": task_exec_metadata["execution_id"]},
            ):
                for name, cfg in config.services.items():
                    with tracer.start_as_current_span(
                        f"{config.task.__name__}.services.{name}.init",
                        attributes={"task.execution.id": task_exec_metadata["execution_id"]},
                    ):
                        services[name] = cfg.service(*cfg.args, **cfg.kwargs)

            with tracer.start_as_current_span(
                f"{config.task.__name__}.instance.init",
                attributes={"task.execution.id": task_exec_metadata["execution_id"]},
            ):
                instance = config.task(
                    connectors=connectors,
                    services=services,
                    policies=config.policies,
                    worker_id=worker_id,
                    task_config=config,
                    *args,
                    **kwargs,
                )

            if mode == "transaction":
                __run_transaction_sync(
                    instance=instance,
                    policy=kwargs.get("policy"),
                    transaction=kwargs.get("transaction", None),
                    transaction_id=kwargs.get("transaction_id", None),
                )
            else:
                with tracer.start_as_current_span(
                    f"{config.task.__name__}.execute",
                    attributes={"task.execution.id": task_exec_metadata["execution_id"]},
                ):
                    instance.execute()

        finally:
            if instance is not None:
                with tracer.start_as_current_span(
                    f"{config.task.__name__}.exit",
                    attributes={"task.execution.id": task_exec_metadata["execution_id"]},
                ):
                    instance.exit()


# =============================================================
# PUBLIC API — RUNNER
# =============================================================


def run_task(
    config: TaskConfig,
    debug: bool = False,
    mode: Literal["task", "transaction"] = "task",
    *args,
    **kwargs,
) -> int:
    """
    Process entrypoint.

    Returns POSIX-compatible exit code.
    """

    _install_signal_handlers()
    is_async = issubclass(config.task, BaseAsyncTask)

    # ---------------------------------------------------------
    # SINGLE PROCESS
    # ---------------------------------------------------------
    if debug or config.max_workers == 1:
        try:
            if is_async:
                asyncio.run(__run_task_async(config, mode, *args, **kwargs))
            else:
                __run_task_sync(config, mode, *args, **kwargs)

            if is_shutdown_requested():
                return int(get_shutdown_exit_code())

            return int(ExitCode.SUCCESS)

        except ValueError:
            traceback.print_exc()
            return int(ExitCode.CONFIG_ERROR)

        except Exception:
            traceback.print_exc()
            return int(ExitCode.ERROR)

    # ---------------------------------------------------------
    # MULTI-PROCESS (SYNC ONLY)
    # ---------------------------------------------------------
    if is_async:
        raise RuntimeError("Async tasks cannot be executed with multiple processes. Use debug=True or max_workers=1.")

    has_error = False

    with ProcessPoolExecutor(max_workers=config.max_workers) as executor:
        futures = []
        for worker_id in range(config.max_workers):
            worker_kwargs = {
                **kwargs,
                "worker_id": worker_id,
                "total_workers": config.max_workers,
            }
            futures.append(executor.submit(__run_task_sync, config, mode, *args, **worker_kwargs))

        for f in futures:
            try:
                f.result()
            except Exception:
                traceback.print_exc()
                has_error = True

    if is_shutdown_requested():
        return int(get_shutdown_exit_code())

    if has_error:
        return int(ExitCode.ERROR)

    return int(ExitCode.SUCCESS)
