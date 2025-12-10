import asyncio
import os
import traceback
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

from ..telemetry import logging, metrics, tracing
from .base import (
    BaseAsyncTask,
    BaseTask,
    TaskConfig,
    TaskExecMetadata,
)


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
# ASYNC EXECUTION PATH
# -------------------------------------------------------------
async def __run_task_async(config: TaskConfig, *args, **kwargs):
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
        # EXECUTE TASK
        # -------------------------------------------------------------
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


# -------------------------------------------------------------
# SYNC EXECUTION PATH
# -------------------------------------------------------------
def __run_task_sync(config: TaskConfig, *args, **kwargs):
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
        # EXECUTE TASK
        # -------------------------------------------------------------
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
def run(config: TaskConfig, debug: bool = False, *args, **kwargs):
    # ---------------------------------------------------------
    # Detect async vs sync
    # ---------------------------------------------------------
    is_async = issubclass(config.task, BaseAsyncTask)

    if debug or config.max_workers == 1:
        # SINGLE PROCESS MODE
        if is_async:
            return asyncio.run(__run_task_async(config, *args, **kwargs))
        else:
            return __run_task_sync(config, *args, **kwargs)

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
            f = executor.submit(__run_task_sync, config, *args, **worker_kwargs)
            futures.append(f)

        # Wait for all futures to complete and log results/exceptions
        for i, f in enumerate(futures):
            try:
                f.result()
            except Exception:
                traceback.print_exc()

    return futures
