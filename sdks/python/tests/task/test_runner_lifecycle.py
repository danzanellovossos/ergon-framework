import asyncio
import signal
import threading

import pytest

from ergon.connector import AsyncConnector, ConnectorConfig
from ergon.service import ServiceConfig
from ergon.task import runner
from ergon.task.base import BaseAsyncTask, TaskConfig
from ergon.task.liveness import TaskLivenessSnapshot, TaskSupervisionPolicy


class _AsyncTestConnector(AsyncConnector):
    async def fetch_transactions_async(self, *args, **kwargs):
        return []

    async def dispatch_transactions_async(self, transactions, *args, **kwargs):
        return None


@pytest.fixture(autouse=True)
def reset_shutdown_state():
    runner._reset_shutdown_state()
    yield
    runner._reset_shutdown_state()


def test_run_task_returns_signal_exit_code(monkeypatch):
    class Task(BaseAsyncTask):
        async def execute(self):
            runner._signal_handler(signal.SIGTERM, None)
            await asyncio.sleep(0)

    monkeypatch.setattr(runner, "_install_signal_handlers", lambda: None)

    config = TaskConfig(
        name="signal-exit-code",
        task=Task,
        connectors={"test": ConnectorConfig(connector=_AsyncTestConnector)},
    )

    assert runner.run_task(config) == 143


@pytest.mark.asyncio
async def test_signal_cancels_async_execution_and_runs_cleanup():
    started = asyncio.Event()
    state = {
        "cancelled": False,
        "task_exited": False,
        "connector_closed": False,
    }

    class Connector(_AsyncTestConnector):
        async def init_async(self):
            return None

        async def close(self):
            state["connector_closed"] = True

    class Task(BaseAsyncTask):
        async def execute(self):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        async def exit(self):
            state["task_exited"] = True

    config = TaskConfig(
        name="signal-lifecycle",
        task=Task,
        connectors={"test": ConnectorConfig(connector=Connector)},
    )

    execution = asyncio.create_task(runner.__run_task_async(config, "task"))
    await asyncio.wait_for(started.wait(), timeout=1)
    runner._signal_handler(signal.SIGTERM, None)
    await asyncio.wait_for(execution, timeout=1)

    assert state == {
        "cancelled": True,
        "task_exited": True,
        "connector_closed": True,
    }
    assert runner.get_shutdown_exit_code() == runner.ExitCode.SIGTERM


@pytest.mark.asyncio
async def test_resources_close_in_reverse_initialization_order():
    events: list[str] = []

    class Connector(_AsyncTestConnector):
        async def init_async(self):
            events.append("connector.init")

        async def close(self):
            events.append("connector.close")

    class Service:
        def __init__(self):
            events.append("service.init")

        async def close(self):
            events.append("service.close")

    class Task(BaseAsyncTask):
        async def execute(self):
            events.append("task.execute")

        async def exit(self):
            events.append("task.exit")

    config = TaskConfig(
        name="resource-lifecycle",
        task=Task,
        connectors={"test": ConnectorConfig(connector=Connector)},
        services={"test": ServiceConfig(service=Service)},
    )

    await runner.__run_task_async(config, "task")

    assert events == [
        "connector.init",
        "service.init",
        "task.execute",
        "task.exit",
        "service.close",
        "connector.close",
    ]


@pytest.mark.asyncio
async def test_partially_initialized_connector_closes_after_init_failure():
    closed = False

    class Connector(_AsyncTestConnector):
        async def init_async(self):
            raise RuntimeError("init failed")

        async def close(self):
            nonlocal closed
            closed = True

    class Task(BaseAsyncTask):
        async def execute(self):
            raise AssertionError("task should not be constructed")

    config = TaskConfig(
        name="partial-init",
        task=Task,
        connectors={"test": ConnectorConfig(connector=Connector)},
    )

    with pytest.raises(RuntimeError, match="init failed"):
        await runner.__run_task_async(config, "task")

    assert closed is True


@pytest.mark.asyncio
async def test_task_failure_keeps_hard_exit_armed_through_stalled_cleanup(monkeypatch):
    cleanup_started = asyncio.Event()
    hard_exit = threading.Event()

    class Task(BaseAsyncTask):
        def liveness_snapshot(self):
            return TaskLivenessSnapshot(healthy=True, state="idle")

        async def execute(self):
            raise RuntimeError("fetch failed")

        async def exit(self):
            cleanup_started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(runner.os, "_exit", lambda _code: hard_exit.set())
    config = TaskConfig(
        name="failed-task-cleanup-deadline",
        task=Task,
        connectors={"test": ConnectorConfig(connector=_AsyncTestConnector)},
        supervision=TaskSupervisionPolicy(
            check_interval=0.01,
            startup_grace=0,
            unhealthy_grace=0.01,
            shutdown_grace=0.02,
        ),
    )

    execution = asyncio.create_task(runner.__run_task_async(config, "task"))
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    assert await asyncio.to_thread(hard_exit.wait, 0.5)

    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
