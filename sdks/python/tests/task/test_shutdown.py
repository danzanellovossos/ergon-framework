"""ergon.task.shutdown, the interruptible backoff and the runner's sync/multiprocess wiring."""

import signal
import threading
import time
from unittest.mock import patch

import pytest

from ergon.connector import Connector, ConnectorConfig
from ergon.task import policies, runner, shutdown
from ergon.task.base import TaskConfig
from ergon.task.mixins import ConsumerTask
from ergon.task.utils import backoff


@pytest.fixture(autouse=True)
def isolated_shutdown_state(monkeypatch):
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    raised = []
    monkeypatch.setattr(shutdown.signal, "raise_signal", raised.append)
    shutdown.reset()
    yield raised
    shutdown.reset()
    for signum, handler in previous.items():
        signal.signal(signum, handler)
    shutdown._handlers_installed = False
    shutdown._escalate = True


class TestFirstSignal:
    def test_marks_shutdown_and_remembers_the_signal(self):
        assert not shutdown.is_requested()

        shutdown._handle_signal(signal.SIGTERM, None)

        assert shutdown.is_requested()
        assert shutdown.requested_signal() == signal.SIGTERM

    @pytest.mark.parametrize(
        ("signum", "code"),
        [(signal.SIGTERM, 143), (signal.SIGINT, 130)],
    )
    def test_exit_code_follows_the_signal(self, signum, code):
        shutdown._handle_signal(signum, None)
        assert runner.get_shutdown_exit_code() == code

    def test_exit_code_without_signal_is_error(self):
        assert runner.get_shutdown_exit_code() == runner.ExitCode.ERROR

    def test_callbacks_run_and_a_failing_one_does_not_block_the_rest(self):
        calls = []

        def broken():
            raise RuntimeError("boom")

        shutdown.register_callback(broken)
        shutdown.register_callback(lambda: calls.append("ok"))

        shutdown._handle_signal(signal.SIGTERM, None)

        assert calls == ["ok"]

    def test_callback_registered_after_the_signal_runs_immediately(self):
        shutdown._handle_signal(signal.SIGTERM, None)
        calls = []

        shutdown.register_callback(lambda: calls.append("late"))

        assert calls == ["late"]

    def test_unregistered_callback_is_not_run(self):
        calls = []

        def callback():
            calls.append("x")

        shutdown.register_callback(callback)
        shutdown.unregister_callback(callback)
        shutdown._handle_signal(signal.SIGTERM, None)

        assert calls == []

    def test_reset_clears_everything(self):
        shutdown.register_callback(lambda: None)
        shutdown._handle_signal(signal.SIGINT, None)

        shutdown.reset()

        assert not shutdown.is_requested()
        assert shutdown.requested_signal() is None
        assert not shutdown._callbacks


class TestSecondSignal:
    def test_escalates_when_handlers_are_installed(self, isolated_shutdown_state):
        killed = []
        shutdown.install_handlers()
        shutdown.register_escalation_callback(lambda: killed.append("workers"))

        shutdown._handle_signal(signal.SIGTERM, None)
        assert isolated_shutdown_state == []

        shutdown._handle_signal(signal.SIGTERM, None)

        assert killed == ["workers"]
        assert isolated_shutdown_state == [signal.SIGTERM]
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler

    def test_failing_escalation_callback_still_exits(self, isolated_shutdown_state):
        def broken():
            raise RuntimeError("boom")

        shutdown.install_handlers()
        shutdown.register_escalation_callback(broken)
        shutdown._handle_signal(signal.SIGINT, None)
        shutdown._handle_signal(signal.SIGINT, None)

        assert isolated_shutdown_state == [signal.SIGINT]

    def test_workers_never_escalate(self, isolated_shutdown_state):
        shutdown.install_handlers(escalate=False)

        for _ in range(3):
            shutdown._handle_signal(signal.SIGTERM, None)

        assert isolated_shutdown_state == []
        assert signal.getsignal(signal.SIGTERM) is shutdown._handle_signal
        assert shutdown.is_requested()

    def test_no_escalation_without_installed_handlers(self, isolated_shutdown_state):
        shutdown._handle_signal(signal.SIGTERM, None)
        shutdown._handle_signal(signal.SIGTERM, None)

        assert isolated_shutdown_state == []

    def test_install_routes_both_signals_to_the_handler(self):
        shutdown.install_handlers()

        assert signal.getsignal(signal.SIGTERM) is shutdown._handle_signal
        assert signal.getsignal(signal.SIGINT) is shutdown._handle_signal


class TestWait:
    def test_returns_false_after_the_timeout(self):
        assert shutdown.wait(0.01) is False

    def test_wakes_up_as_soon_as_shutdown_is_requested(self):
        threading.Timer(0.05, shutdown._handle_signal, args=(signal.SIGTERM, None)).start()

        started = time.monotonic()
        assert shutdown.wait(10) is True
        assert time.monotonic() - started < 2


class TestBackoff:
    def test_interruptible_backoff_returns_on_shutdown(self):
        threading.Timer(0.05, shutdown._handle_signal, args=(signal.SIGTERM, None)).start()

        started = time.monotonic()
        backoff(backoff=30, multiplier=1, cap=0, attempt=0, interruptible=True)

        assert time.monotonic() - started < 2

    def test_interruptible_backoff_is_immediate_when_already_stopping(self):
        shutdown._handle_signal(signal.SIGTERM, None)

        started = time.monotonic()
        backoff(backoff=30, multiplier=1, cap=0, attempt=0, interruptible=True)

        assert time.monotonic() - started < 0.5

    def test_interruptible_backoff_sleeps_the_full_delay_otherwise(self):
        with patch("ergon.task.utils.shutdown.wait", return_value=False) as wait:
            backoff(backoff=1.0, multiplier=2, cap=0, attempt=2, interruptible=True)
        wait.assert_called_once_with(4.0)

    @patch("ergon.task.utils.time.sleep")
    def test_retry_backoff_is_not_shortened_by_shutdown(self, mock_sleep):
        shutdown._handle_signal(signal.SIGTERM, None)

        backoff(backoff=2.0, multiplier=1, cap=0, attempt=0)

        mock_sleep.assert_called_once_with(2.0)


class _FakeExecutor:
    def __init__(self, pids):
        self._processes = {pid: object() for pid in pids}


class TestWorkerSignalRelays:
    def test_forwards_the_received_signal_to_every_worker(self, monkeypatch):
        sent = []
        monkeypatch.setattr(runner.os, "kill", lambda pid, signum: sent.append((pid, signum)))
        forward, _ = runner._worker_signal_relays(_FakeExecutor([101, 202]))

        shutdown._handle_signal(signal.SIGINT, None)
        forward()

        assert sorted(sent) == [(101, signal.SIGINT), (202, signal.SIGINT)]

    def test_kill_sends_sigkill(self, monkeypatch):
        sent = []
        monkeypatch.setattr(runner.os, "kill", lambda pid, signum: sent.append((pid, signum)))
        _, kill = runner._worker_signal_relays(_FakeExecutor([101]))

        kill()

        assert sent == [(101, signal.SIGKILL)]

    def test_reads_workers_started_after_registration(self, monkeypatch):
        sent = []
        monkeypatch.setattr(runner.os, "kill", lambda pid, signum: sent.append(pid))
        executor = _FakeExecutor([])
        forward, _ = runner._worker_signal_relays(executor)

        executor._processes[303] = object()
        forward()

        assert sent == [303]

    def test_a_worker_that_already_exited_is_skipped(self, monkeypatch):
        sent = []

        def kill(pid, signum):
            if pid == 101:
                raise ProcessLookupError
            sent.append(pid)

        monkeypatch.setattr(runner.os, "kill", kill)
        forward, _ = runner._worker_signal_relays(_FakeExecutor([101, 202]))

        forward()

        assert sent == [202]

    def test_executor_without_a_worker_table_is_a_no_op(self, monkeypatch):
        sent = []
        monkeypatch.setattr(runner.os, "kill", lambda pid, signum: sent.append(pid))
        forward, kill = runner._worker_signal_relays(object())

        forward()
        kill()

        assert sent == []

    def test_worker_entrypoint_installs_non_escalating_handlers(self, monkeypatch):
        seen = {}

        def fake_run(config, mode, *args, **kwargs):
            seen["escalate"] = shutdown._escalate
            seen["installed"] = shutdown._handlers_installed
            seen["requested"] = shutdown.is_requested()
            seen["kwargs"] = kwargs
            return "done"

        monkeypatch.setattr(runner, "__run_task_sync", fake_run)
        shutdown._handle_signal(signal.SIGTERM, None)

        assert runner._run_worker_sync(object(), "task", worker_id=1) == "done"
        assert seen == {"escalate": False, "installed": True, "requested": False, "kwargs": {"worker_id": 1}}


class _EmptyConnector(Connector):
    def fetch_transactions(self, *args, **kwargs):
        return []

    def dispatch_transactions(self, transactions, *args, **kwargs):
        return None


class _IdleStreamingTask(ConsumerTask):
    def process_transaction(self, transaction):
        return None

    def execute(self):
        return self.consume_transactions(
            policies.ConsumerPolicy(
                loop=policies.ConsumerLoopPolicy(streaming=True),
                fetch=policies.FetchPolicy(empty=policies.EmptyFetchPolicy(backoff=60)),
            )
        )


def test_sync_streaming_task_exits_with_143_on_sigterm(monkeypatch):
    """The production bug: a sync streaming consumer ignored SIGTERM until systemd's SIGKILL."""
    monkeypatch.setattr(runner, "_install_signal_handlers", lambda **_: None)
    threading.Timer(0.2, shutdown._handle_signal, args=(signal.SIGTERM, None)).start()

    config = TaskConfig(
        name="idle-sync-consumer",
        task=_IdleStreamingTask,
        connectors={"queue": ConnectorConfig(connector=_EmptyConnector)},
    )

    started = time.monotonic()
    assert runner.run_task(config) == 143
    assert time.monotonic() - started < 5
