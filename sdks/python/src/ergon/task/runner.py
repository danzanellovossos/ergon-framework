import asyncio
import os
import traceback
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from typing import Literal

from ..telemetry import logging, metrics, tracing
from .base import (
    BaseAsyncTask,
    BaseTask,
    TaskConfig,
    TaskExecMetadata,
)

from ..connector import Transaction
from .policies import ConsumerPolicy


# -------------------------------------------------------------
# TELEMETRY INITIALIZATION
# -------------------------------------------------------------
def __init_telemetry(config: TaskConfig, task_exec_metadata: TaskExecMetadata):
    if getattr(config, "logging", None):
        logging._apply_logging_config(cfg=config.logging, metadata=task_exec_metadata)

    if getattr(config, "tracing", None):
        tracing._apply_tracing_config(cfg=config.tracing, metadata=task_exec_metadata)

    if getattr(config, "metrics", None):
        metrics._apply_metrics_config(cfg=config.metrics, metadata=task_exec_metadata)


# -------------------------------------------------------------
# ASYNC TRANSACTION EXECUTION PATH
# -------------------------------------------------------------
async def __run_transaction_async(
    instance: BaseAsyncTask,
    policy: str,
    transaction: Transaction = None,
    transaction_id: str = None,
):
    tracer = tracing.get_tracer(__name__)
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


# -------------------------------------------------------------
# ASYNC EXECUTION PATH
# -------------------------------------------------------------
async def __run_task_async(config: TaskConfig, mode: Literal["task", "transaction"] = "task", *args, **kwargs):
    # -------------------------------------------------------------
    # VALIDATE TASK
    # -------------------------------------------------------------
    if not issubclass(config.task, BaseAsyncTask):
        raise ValueError(f"Invalid async task: {config.task}")

    # -------------------------------------------------------------
    # INITIALIZE TASK EXECUTION METADATA
    # -------------------------------------------------------------

    worker_id = kwargs.pop("worker_id", None)

    task_exec_metadata = TaskExecMetadata(
        task_name=config.name,
        execution_id=str(uuid.uuid4()),
        execution_start_time=datetime.now().isoformat(),
        pid=os.getpid(),
        worker_id=worker_id,
    ).model_dump()

    # -------------------------------------------------------------
    # INITIALIZE TELEMETRY
    # -------------------------------------------------------------

    __init_telemetry(config, task_exec_metadata)

    tracer = tracing.get_tracer(f"task.{config.name}")

    # -------------------------------------------------------------
    # START TASK EXECUTION SPAN
    # -------------------------------------------------------------

    async with tracer.start_as_current_span(
        f"{config.task.__name__}.run",
        attributes={"task.execution.id": task_exec_metadata["execution_id"]},
    ):
        # -----------------------------------------------------
        # INSTANTIATE CONNECTORS
        # -----------------------------------------------------

        connectors = {}
        for name, cfg in config.connectors.items():
            with tracer.start_as_current_span(
                f"{cfg.connector.__name__}.init",
                attributes={"connector.name": name},
            ):
                conn = cfg.connector(*cfg.args, **cfg.kwargs)

                # optional async init hook
                if hasattr(conn, "init_async"):
                    await conn.init_async()

                connectors[name] = conn

        # -----------------------------------------------------
        # INSTANTIATE SERVICES
        # -----------------------------------------------------
        services = {}
        for name, cfg in config.services.items():
            with tracer.start_as_current_span(
                f"{cfg.service.__name__}.init",
                attributes={"service.name": name},
            ):
                services[name] = cfg.service(*cfg.args, **cfg.kwargs)

        # -------------------------------------------------------------
        # INSTANTIATE TASK
        # -------------------------------------------------------------
        async with tracer.start_as_current_span(
            f"{config.task.__name__}.init",
            attributes={"task.class": config.task.__name__},
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

        # -------------------------------------------------------------
        # PROCESS TRANSACTION
        # -------------------------------------------------------------
        if mode == "transaction":
            async with tracer.start_as_current_span(
                f"{config.task.__name__}.process",
                attributes={"transaction_id": kwargs.get("transaction_id")},
            ):
                await __run_transaction_async(
                    instance=instance,
                    policy=kwargs.get("policy", None),
                    transaction=kwargs.get("transaction"),
                    transaction_id=kwargs.get("transaction_id"),
                )

        # -------------------------------------------------------------
        # EXECUTE TASK
        # -------------------------------------------------------------
        if mode == "task":
            async with tracer.start_as_current_span(
                f"{config.task.__name__}.execute",
                attributes={
                    "task.execution.id": task_exec_metadata["execution_id"],
                    "task.execution.pid": task_exec_metadata["pid"],
                },
            ):
                await instance.execute()

        # -------------------------------------------------------------
        # EXIT TASK
        # -------------------------------------------------------------
        async with tracer.start_as_current_span(f"{config.task.__name__}.exit"):
            await instance.exit()


def __run_transaction_sync(
    instance: BaseTask,
    policy: str,
    transaction: Transaction = None,
    transaction_id: str = None,
):
    """
    Used to run a single transaction through a task.

    Args:
        instance: The task instance to run the transaction through.
        policy: The name of the policy to use.
        transaction: The transaction to run through the task.
        transaction_id: The id of the transaction to run through the task.

    Returns:
        The result of the transaction.
    """

    policy_obj = next((p for p in instance.policies if p.name == policy), None)
    if not policy_obj:
        raise ValueError(f"Policy {policy} not found")

    if not transaction and not transaction_id:
        raise ValueError("Either transaction or transaction_id must be provided")

    if transaction_id:
        conn = instance._resolve_connector(policy_obj.fetch.connector_name)
        transaction = conn.fetch_transaction_by_id(transaction_id)

    success, result = instance._start_processing(transaction, policy_obj)
    if not success:
        raise result
    return result


# -------------------------------------------------------------
# SYNC EXECUTION PATH
# -------------------------------------------------------------
def __run_task_sync(config: TaskConfig, mode: Literal["task", "transaction"] = "task", *args, **kwargs):
    # -------------------------------------------------------------
    # VALIDATE TASK
    # -------------------------------------------------------------
    if not issubclass(config.task, BaseTask):
        raise ValueError(f"Invalid sync task: {config.task}")

    # -------------------------------------------------------------
    # INITIALIZE TASK EXECUTION METADATA
    # -------------------------------------------------------------

    worker_id = kwargs.pop("worker_id", None)

    task_exec_metadata = TaskExecMetadata(
        task_name=config.name,
        execution_id=str(uuid.uuid4()),
        execution_start_time=datetime.now().isoformat(),
        pid=os.getpid(),
        worker_id=worker_id,
    ).model_dump()

    # -------------------------------------------------------------
    # INITIALIZE TELEMETRY
    # -------------------------------------------------------------

    __init_telemetry(config, task_exec_metadata)

    tracer = tracing.get_tracer(__name__)

    # -------------------------------------------------------------
    # START TASK EXECUTION SPAN
    # -------------------------------------------------------------

    with tracer.start_as_current_span(
        f"{config.task.__name__}.run",
        attributes={"task.execution.id": task_exec_metadata["execution_id"]},
    ):
        # ------------------------------
        # INSTANTIATE CONNECTORS
        # ------------------------------
        connectors = {}
        for name, cfg in config.connectors.items():
            with tracer.start_as_current_span(
                f"{cfg.connector.__name__}.init",
                attributes={
                    "connector.name": name,
                    "connector.class": cfg.connector.__name__,
                },
            ):
                conn = cfg.connector(*cfg.args, **cfg.kwargs)
                connectors[name] = conn

        # ------------------------------
        # INSTANTIATE SERVICES
        # ------------------------------
        services = {}
        for name, cfg in config.services.items():
            with tracer.start_as_current_span(
                f"{cfg.service.__name__}.init",
                attributes={"service.name": name, "service.class": cfg.service.__name__},
            ):
                services[name] = cfg.service(*cfg.args, **cfg.kwargs)

        # -------------------------------------------------------------
        # INSTANTIATE TASK
        # -------------------------------------------------------------
        with tracer.start_as_current_span(
            f"{config.task.__name__}.init",
            attributes={"task.class": config.task.__name__},
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

        # -------------------------------------------------------------
        # PROCESS TRANSACTION
        # -------------------------------------------------------------
        if mode == "transaction":
            with tracer.start_as_current_span(
                f"{config.task.__name__}.process",
                attributes={"transaction_id": kwargs.get("transaction_id")},
            ):
                __run_transaction_sync(
                    instance=instance,
                    policy=kwargs.get("policy", None),
                    transaction=kwargs.get("transaction"),
                    transaction_id=kwargs.get("transaction_id"),
                )

        # -------------------------------------------------------------
        # EXECUTE TASK
        # -------------------------------------------------------------
        if mode == "task":
            with tracer.start_as_current_span(
                f"{config.task.__name__}.execute",
                attributes={
                    "task.execution.id": task_exec_metadata["execution_id"],
                    "task.execution.pid": task_exec_metadata["pid"],
                },
            ):
                instance.execute()

        # -------------------------------------------------------------
        # EXIT TASK
        # -------------------------------------------------------------
        with tracer.start_as_current_span(f"{config.task.__name__}.exit"):
            instance.exit()


# -------------------------------------------------------------
# PUBLIC API — RUNNER
# -------------------------------------------------------------
def run_task(config: TaskConfig, debug: bool = False, mode: Literal["task", "transaction"] = "task", *args, **kwargs):
    # ---------------------------------------------------------
    # Detect async vs sync
    # ---------------------------------------------------------
    is_async = issubclass(config.task, BaseAsyncTask)

    if debug or config.max_workers == 1:
        # SINGLE PROCESS MODE
        if is_async:
            return asyncio.run(__run_task_async(config, mode, *args, **kwargs))
        else:
            return __run_task_sync(config, mode, *args, **kwargs)

    # ---------------------------------------------------------
    # MULTI-PROCESS — SYNC ONLY
    # (Asyncio does NOT scale across processes without rewriting)
    # ---------------------------------------------------------
    if is_async:
        raise RuntimeError(
            "Async tasks cannot be executed with multiple processes yet. Use debug=True or max_workers=1."
        )

    # Normal sync multiprocessing with worker sharding
    futures = []
    with ProcessPoolExecutor(max_workers=config.max_workers) as executor:
        for worker_id in range(config.max_workers):
            # Inject sharding info into kwargs for each worker
            worker_kwargs = {
                **kwargs,
                "worker_id": worker_id,
                "total_workers": config.max_workers,
            }
            f = executor.submit(__run_task_sync, config, mode, *args, **worker_kwargs)
            futures.append(f)

        # Wait for all futures to complete and log results/exceptions
        for i, f in enumerate(futures):
            try:
                f.result()
            except Exception:
                traceback.print_exc()

    return futures
