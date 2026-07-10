import asyncio
import inspect
import logging as stdlib_logging
import os
import signal
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from enum import IntEnum
from typing import Any, Literal

from ..connector import Transaction
from ..telemetry import logging, metrics, tracing
from .base import (
    BaseAsyncTask,
    BaseTask,
    TaskConfig,
    TaskExecMetadata,
)
from .liveness import LivenessProvider, TaskSupervisionPolicy

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
_shutdown_callbacks: set[Callable[[], None]] = set()
_shutdown_callbacks_lock = threading.Lock()
_supervisor_logger = stdlib_logging.getLogger(__name__)


def _signal_handler(signum, frame):
    global _shutdown_signal
    _shutdown_signal = signum
    _shutdown_event.set()
    with _shutdown_callbacks_lock:
        callbacks = tuple(_shutdown_callbacks)
    for callback in callbacks:
        try:
            callback()
        except Exception:
            _supervisor_logger.exception("Async shutdown callback failed")


def _install_signal_handlers():
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)


def _reset_shutdown_state() -> None:
    global _shutdown_signal
    _shutdown_event.clear()
    _shutdown_signal = None
    with _shutdown_callbacks_lock:
        _shutdown_callbacks.clear()


def _register_shutdown_callback(callback: Callable[[], None]) -> None:
    with _shutdown_callbacks_lock:
        _shutdown_callbacks.add(callback)
        shutdown_requested = _shutdown_event.is_set()
    if shutdown_requested:
        callback()


def _unregister_shutdown_callback(callback: Callable[[], None]) -> None:
    with _shutdown_callbacks_lock:
        _shutdown_callbacks.discard(callback)


def is_shutdown_requested() -> bool:
    return _shutdown_event.is_set()


def get_shutdown_exit_code() -> ExitCode:
    if _shutdown_signal == signal.SIGINT:
        return ExitCode.SIGINT
    if _shutdown_signal == signal.SIGTERM:
        return ExitCode.SIGTERM
    return ExitCode.ERROR


class _TaskLivenessSupervisor:
    """Independent process watchdog for tasks exposing liveness snapshots."""

    def __init__(
        self,
        provider: LivenessProvider,
        policy: TaskSupervisionPolicy,
        request_shutdown,
        hard_exit=os._exit,
    ) -> None:
        self.provider = provider
        self.policy = policy
        self.request_shutdown = request_shutdown
        self.hard_exit = hard_exit
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="ergon-liveness-supervisor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(1.0, self.policy.check_interval * 2))

    def _run(self) -> None:
        started_at = time.monotonic()
        unhealthy_since: float | None = None
        shutdown_requested_at: float | None = None

        while not self._stop.wait(self.policy.check_interval):
            now = time.monotonic()
            if now - started_at < self.policy.startup_grace:
                continue

            try:
                snapshot = self.provider.liveness_snapshot()
            except Exception as exc:  # noqa: BLE001 - a broken health check is unhealthy
                healthy = False
                reason = f"liveness check raised {exc!r}"
            else:
                healthy = snapshot.healthy
                reason = snapshot.reason or snapshot.state

            if healthy:
                unhealthy_since = None
                continue

            if unhealthy_since is None:
                unhealthy_since = now
                _supervisor_logger.error("Task liveness check failed: %s", reason)
                continue

            if shutdown_requested_at is None and now - unhealthy_since >= self.policy.unhealthy_grace:
                shutdown_requested_at = now
                _supervisor_logger.critical(
                    "Task remained unhealthy for %.1fs (%s); requesting process shutdown",
                    now - unhealthy_since,
                    reason,
                )
                self.request_shutdown(reason)
                continue

            if shutdown_requested_at is not None and now - shutdown_requested_at >= self.policy.shutdown_grace:
                _supervisor_logger.critical(
                    "Task did not stop within %.1fs after liveness shutdown request; forcing exit",
                    self.policy.shutdown_grace,
                )
                self.hard_exit(int(ExitCode.ERROR))


# =============================================================
# TELEMETRY INITIALIZATION
# =============================================================


def __init_telemetry(config: TaskConfig, task: object, task_exec_metadata: dict[str, Any]):
    if config.logging is not None:
        logging._apply_logging_config(cfg=config.logging, task=task, metadata=task_exec_metadata)

    if config.tracing is not None:
        tracing._apply_tracing_config(cfg=config.tracing, metadata=task_exec_metadata)

    if config.metrics is not None:
        metrics._apply_metrics_config(cfg=config.metrics, metadata=task_exec_metadata)


# =============================================================
# ASYNC TRANSACTION EXECUTION
# =============================================================


async def __run_transaction_async(
    instance: BaseAsyncTask,
    policy: str,
    transaction: Transaction | None = None,
    transaction_id: str | None = None,
):
    policy_obj = next((p for p in instance.policies if p.name == policy), None)
    if not policy_obj:
        raise ValueError(f"Policy '{policy}' not found")

    if not transaction and not transaction_id:
        raise ValueError("Either transaction or transaction_id must be provided")

    if transaction_id:
        conn = instance._resolve_connector(policy_obj.fetch.connector_name)  # type: ignore[attr-defined]
        transaction = await conn.fetch_transaction_by_id_async(transaction_id)

    success, result = await instance._start_processing(transaction, policy_obj)  # type: ignore[attr-defined]
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
    if not issubclass(config.task, BaseAsyncTask):  # type: ignore[arg-type]
        raise ValueError(f"Invalid async task: {config.task}")

    worker_id = kwargs.pop("worker_id", None)

    task_exec_metadata = TaskExecMetadata(
        task_name=config.name,
        execution_id=str(uuid.uuid4()),
        execution_start_time=datetime.now().isoformat(),
        pid=os.getpid(),
        worker_id=worker_id,
    ).model_dump()

    __init_telemetry(config, task=config.task, task_exec_metadata=task_exec_metadata)
    tracer = tracing.get_tracer(f"task.{config.name}")

    instance = None
    supervisor = None
    liveness_shutdown_requested = threading.Event()
    connectors: dict[str, Any] = {}
    services: dict[str, Any] = {}
    loop = asyncio.get_running_loop()
    runner_task = asyncio.current_task()

    def request_signal_shutdown() -> None:
        if runner_task is not None and not runner_task.done():
            loop.call_soon_threadsafe(runner_task.cancel, "Process shutdown requested")

    _register_shutdown_callback(request_signal_shutdown)

    try:
        with tracer.start_as_current_span(  # type: ignore[attr-defined]
            f"{config.task.__name__}.run",
            attributes={"task.execution.id": task_exec_metadata["execution_id"]},
        ):
            for name, cfg in config.connectors.items():
                conn = cfg.connector(*cfg.args, **cfg.kwargs)
                connectors[name] = conn
                if hasattr(conn, "init_async"):
                    await conn.init_async()  # type: ignore[attr-defined]

            for name, cfg in config.services.items():
                services[name] = cfg.service(*cfg.args, **cfg.kwargs)

            instance = config.task(
                connectors=connectors,
                services=services,
                policies=config.policies,
                worker_id=worker_id,
                task_config=config,
                *args,
                **kwargs,
            )

            if config.supervision.enabled and isinstance(instance, LivenessProvider):

                def request_liveness_shutdown(reason: str) -> None:
                    liveness_shutdown_requested.set()
                    if runner_task is not None and not runner_task.done():
                        loop.call_soon_threadsafe(runner_task.cancel, f"Task unhealthy: {reason}")

                supervisor = _TaskLivenessSupervisor(
                    provider=instance,
                    policy=config.supervision,
                    request_shutdown=request_liveness_shutdown,
                )
                supervisor.start()

            if mode == "transaction":
                await __run_transaction_async(
                    instance=instance,
                    policy=kwargs.get("policy"),  # type: ignore[arg-type]
                    transaction=kwargs.get("transaction"),
                    transaction_id=kwargs.get("transaction_id"),
                )
            else:
                await instance.execute()

    except asyncio.CancelledError:
        if not is_shutdown_requested():
            raise
    finally:
        _unregister_shutdown_callback(request_signal_shutdown)
        if supervisor is not None and not liveness_shutdown_requested.is_set():
            supervisor.stop()
        try:
            if instance is not None:
                await instance.exit()
        finally:
            try:
                resources = [*connectors.values(), *services.values()]
                for resource in reversed(resources):
                    close = getattr(resource, "close", None)
                    if close is None:
                        close = getattr(resource, "aclose", None)
                    if not callable(close):
                        continue
                    try:
                        result = close()
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        _supervisor_logger.exception(
                            "Failed to close async task resource %s",
                            type(resource).__name__,
                        )
            finally:
                if supervisor is not None:
                    supervisor.stop()


# =============================================================
# SYNC TRANSACTION EXECUTION
# =============================================================


def __run_transaction_sync(
    instance: BaseTask,
    policy: str,
    transaction: Transaction | None = None,
    transaction_id: str | None = None,
):
    policy_obj = next((p for p in instance.policies if p.name == policy), None)
    if not policy_obj:
        raise ValueError(f"Policy '{policy}' not found")

    if not transaction and not transaction_id:
        raise ValueError("Either transaction or transaction_id must be provided")

    if transaction_id:
        conn = instance._resolve_connector(policy_obj.fetch.connector_name)  # type: ignore[attr-defined]
        transaction = conn.fetch_transaction_by_id(transaction_id)

    success, result = instance._start_processing(transaction, policy_obj)  # type: ignore[attr-defined]
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
    if not issubclass(config.task, BaseTask):  # type: ignore[arg-type]
        raise ValueError(f"Invalid sync task: {config.task}")

    worker_id = kwargs.pop("worker_id", None)

    execution_start_time = datetime.now().isoformat()
    task_exec_metadata = TaskExecMetadata(
        task_name=config.name,
        execution_id=str(uuid.uuid4()),
        execution_start_time=execution_start_time,
        pid=os.getpid(),
        worker_id=worker_id,
    ).model_dump()

    __init_telemetry(config, task=config.task, task_exec_metadata=task_exec_metadata)
    tracer = tracing.get_tracer(__name__)
    logger = logging.get_logger(__name__)

    instance = None

    logger.info(f"Task {config.name} started at {execution_start_time}.")

    with tracer.start_as_current_span(
        f"{config.task.__name__}.run",
        attributes={"task.execution.id": task_exec_metadata["execution_id"]},
    ):
        try:
            logger.info("Initializing connectors...")
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

            logger.info("Initializing services...")
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
                logger.info("Creating task instance...")
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
                logger.info("Running task in transaction execution mode...")
                __run_transaction_sync(
                    instance=instance,
                    policy=kwargs.get("policy"),  # type: ignore[arg-type]
                    transaction=kwargs.get("transaction", None),
                    transaction_id=kwargs.get("transaction_id", None),
                )
            else:
                with tracer.start_as_current_span(
                    f"{config.task.__name__}.execute",
                    attributes={"task.execution.id": task_exec_metadata["execution_id"]},
                ):
                    logger.info("Running task in full execution mode...")
                    instance.execute()

        finally:
            if instance is not None:
                with tracer.start_as_current_span(
                    f"{config.task.__name__}.exit",
                    attributes={"task.execution.id": task_exec_metadata["execution_id"]},
                ):
                    logger.info(f"Exiting task {config.name}...")
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

    _reset_shutdown_state()
    _install_signal_handlers()
    is_async = issubclass(config.task, BaseAsyncTask)  # type: ignore[arg-type]

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

        except asyncio.CancelledError:
            traceback.print_exc()
            return int(ExitCode.ERROR)

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
