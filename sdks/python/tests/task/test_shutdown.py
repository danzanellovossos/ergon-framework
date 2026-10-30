"""ergon.task.shutdown, the interruptible backoff and the runner's sync/multiprocess wiring."""

import errno
import faulthandler
import os
import signal
import sys
import threading
import time
from unittest.mock import patch

import pytest

from ergon.connector import AsyncConnector, Connector, ConnectorConfig
from ergon.task import policies, runner, shutdown
from ergon.task.base import BaseAsyncTask, TaskConfig
from ergon.task.mixins import ConsumerTask
from ergon.task.utils import backoff

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


@pytest.fixture
def no_real_exit(monkeypatch):
    """Escalation would kill pytest: record ``os._exit`` instead."""
    calls = []
    monkeypatch.setattr(shutdown.os, "_exit", lambda code: calls.append(("exit", code)))
    return calls


@pytest.fixture
def hang_guard():
    """A regression here deadlocks the process: abort with all stacks instead of hanging CI."""
    faulthandler.dump_traceback_later(20, exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()


def send(signum):
    os.kill(os.getpid(), signum)


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class TestFirstSignal:
    def test_marks_shutdown_and_remembers_the_signal(self):
        assert not shutdown.is_requested()

        shutdown._handle_signal(signal.SIGTERM, None)

        assert shutdown.is_requested()
        assert shutdown.requested_signal() == signal.SIGTERM

    def test_a_later_signal_does_not_overwrite_the_first(self):
        shutdown._handle_signal(signal.SIGTERM, None)
        shutdown._handle_signal(signal.SIGINT, None)

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
        shutdown.register_escalation_callback(lambda: None)
        shutdown._handle_signal(signal.SIGINT, None)

        shutdown.reset()

        assert not shutdown.is_requested()
        assert shutdown.requested_signal() is None
        assert shutdown._callbacks == ()
        assert shutdown._escalation_callbacks == ()


@posix_only
class TestSignalSafety:
    """Real signals to this process with the handlers installed."""

    def test_handler_runs_while_the_main_thread_holds_the_event_lock(self, hang_guard):
        shutdown.install_handlers(escalate=False)
        # What the main thread holds for a few bytecodes inside ``Event.wait``.
        with shutdown._event._cond:
            send(signal.SIGTERM)
            time.sleep(0.1)  # the Python handler runs here, on this thread
            assert shutdown.is_requested()
        assert wait_until(shutdown._event.is_set)

    def test_a_signal_delivered_to_another_thread_still_wakes_a_sleeping_main_thread(self, hang_guard):
        """The main thread blocks the signal, so the kernel hands it to some other thread."""
        shutdown.install_handlers(escalate=False)
        woke = []

        def deliver():
            time.sleep(0.1)
            send(signal.SIGTERM)

        threading.Thread(target=deliver, daemon=True).start()
        with shutdown.blocked_signals():
            started = time.monotonic()
            woke.append(shutdown.wait(10))
            elapsed = time.monotonic() - started

        assert woke == [True]
        assert elapsed < 5

    def test_logging_and_callbacks_happen_on_the_watcher_thread(self, caplog):
        threads = []
        shutdown.install_handlers(escalate=False)
        shutdown.register_callback(lambda: threads.append(threading.current_thread().name))

        with caplog.at_level("WARNING", logger="ergon.task.shutdown"):
            send(signal.SIGTERM)
            assert wait_until(lambda: threads)

        assert threads == ["ergon-shutdown-watcher"]
        (record,) = [r for r in caplog.records if "SIGTERM received" in r.getMessage()]
        assert record.threadName == "ergon-shutdown-watcher"

    def test_repeated_signals_keep_running_callbacks_without_escalating(self, no_real_exit):
        calls = []
        shutdown.install_handlers(escalate=False)
        shutdown.register_callback(lambda: calls.append("cancel"))

        for _ in range(3):
            send(signal.SIGTERM)
            assert wait_until(lambda n=len(calls): len(calls) > n)

        assert calls == ["cancel"] * 3
        assert no_real_exit == []


@posix_only
class TestSecondSignal:
    @pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
    def test_escalates_with_128_plus_signum(self, no_real_exit, signum):
        killed = []
        shutdown.install_handlers()
        shutdown.register_escalation_callback(lambda: killed.append("workers"))

        send(signum)
        assert wait_until(shutdown._event.is_set)
        assert no_real_exit == []

        send(signum)

        assert wait_until(lambda: no_real_exit)
        assert killed == ["workers"]
        assert no_real_exit == [("exit", 128 + signum)]

    def test_failing_escalation_callback_still_exits(self, no_real_exit):
        def broken():
            raise RuntimeError("boom")

        shutdown.install_handlers()
        shutdown.register_escalation_callback(broken)
        send(signal.SIGINT)
        assert wait_until(shutdown._event.is_set)
        send(signal.SIGINT)

        assert wait_until(lambda: no_real_exit == [("exit", 130)])

    def test_workers_never_escalate(self, no_real_exit):
        shutdown.install_handlers(escalate=False)

        for _ in range(3):
            send(signal.SIGTERM)
            time.sleep(0.05)

        time.sleep(0.2)
        assert no_real_exit == []
        assert signal.getsignal(signal.SIGTERM) is shutdown._handle_signal
        assert shutdown.is_requested()

    def test_no_escalation_without_installed_handlers(self, no_real_exit):
        shutdown._handle_signal(signal.SIGTERM, None)
        shutdown._handle_signal(signal.SIGTERM, None)

        assert no_real_exit == []

    def test_install_routes_both_signals_to_the_handler(self):
        shutdown.install_handlers()

        assert signal.getsignal(signal.SIGTERM) is shutdown._handle_signal
        assert signal.getsignal(signal.SIGINT) is shutdown._handle_signal


class TestWait:
    def test_returns_false_after_the_timeout(self):
        assert shutdown.wait(0.01) is False

    def test_wakes_up_as_soon_as_shutdown_is_requested(self, stop_later):
        stop_later(0.05)

        started = time.monotonic()
        assert shutdown.wait(10) is True
        assert time.monotonic() - started < 2


class TestBackoff:
    def test_interruptible_backoff_returns_on_shutdown(self, stop_later):
        stop_later(0.05)

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


@posix_only
class TestSignalMask:
    def test_blocked_signals_defer_delivery_until_unblocked(self):
        shutdown.install_handlers(escalate=False)

        with shutdown.blocked_signals():
            # The mask is per thread (what a forked/spawned worker inherits from it).
            signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)
            time.sleep(0.05)
            assert signal.SIGTERM in signal.sigpending()
            assert not shutdown.is_requested()

        assert wait_until(shutdown.is_requested)


@posix_only
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_forked_child_drops_parent_callbacks_and_never_escalates():
    shutdown.install_handlers()
    shutdown.register_callback(lambda: None)
    shutdown.register_escalation_callback(lambda: None)

    pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        ok = (
            shutdown._escalate is False
            and shutdown._callbacks == ()
            and shutdown._escalation_callbacks == ()
            and shutdown._watcher is None
            and shutdown._wakeup_write is None
            and shutdown._wakeup_installed is False
            and signal.set_wakeup_fd(-1) == -1
        )
        os._exit(0 if ok else 1)

    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert shutdown._escalate is True
    assert len(shutdown._callbacks) == 1


class TestWorkerSignalRelays:
    def test_forwards_the_received_signal_to_every_worker(self, monkeypatch):
        sent = []
        monkeypatch.setattr(runner.os, "kill", lambda pid, signum: sent.append((pid, signum)))
        forward, _ = runner._worker_signal_relays(lambda: [101, 202])

        shutdown._handle_signal(signal.SIGINT, None)
        forward()

        assert sorted(sent) == [(101, signal.SIGINT), (202, signal.SIGINT)]

    def test_kill_sends_sigkill(self, monkeypatch):
        sent = []
        monkeypatch.setattr(runner.os, "kill", lambda pid, signum: sent.append((pid, signum)))
        _, kill = runner._worker_signal_relays(lambda: [101])

        kill()

        assert sent == [(101, signal.SIGKILL)]

    def test_kill_never_logs_because_it_runs_in_the_signal_handler(self, monkeypatch):
        def kill(pid, signum):
            raise OSError(errno.EPERM, "not permitted")

        monkeypatch.setattr(runner.os, "kill", kill)
        _, kill_workers = runner._worker_signal_relays(lambda: [101])

        with (
            patch.object(runner._supervisor_logger, "error") as error,
            patch.object(runner._supervisor_logger, "exception") as exception,
        ):
            kill_workers()

        error.assert_not_called()
        exception.assert_not_called()

    def test_forward_logs_a_worker_it_cannot_signal(self, monkeypatch):
        def kill(pid, signum):
            raise OSError(errno.EPERM, "not permitted")

        monkeypatch.setattr(runner.os, "kill", kill)
        forward, _ = runner._worker_signal_relays(lambda: [101])

        with patch.object(runner._supervisor_logger, "error") as error:
            forward()

        error.assert_called_once()

    def test_reads_workers_started_after_registration(self, monkeypatch):
        sent = []
        monkeypatch.setattr(runner.os, "kill", lambda pid, signum: sent.append(pid))
        pids = []
        forward, _ = runner._worker_signal_relays(lambda: pids)

        pids.append(303)
        forward()

        assert sent == [303]

    def test_a_worker_that_already_exited_is_skipped(self, monkeypatch):
        sent = []

        def kill(pid, signum):
            if pid == 101:
                raise ProcessLookupError
            sent.append(pid)

        monkeypatch.setattr(runner.os, "kill", kill)
        forward, _ = runner._worker_signal_relays(lambda: [101, 202])

        forward()

        assert sent == [202]

    def test_no_workers_is_a_no_op(self, monkeypatch):
        sent = []
        monkeypatch.setattr(runner.os, "kill", lambda pid, signum: sent.append(pid))
        forward, kill = runner._worker_signal_relays(lambda: [])

        forward()
        kill()

        assert sent == []


class TestWorkerEntrypoint:
    def test_runs_the_task_with_non_escalating_handlers_and_no_parent_callbacks(self, monkeypatch):
        seen = {}

        def fake_run(config, mode, *args, **kwargs):
            seen.update(
                escalate=shutdown._escalate,
                installed=shutdown._handlers_installed,
                callbacks=shutdown._callbacks,
                kwargs=kwargs,
            )
            return "done"

        monkeypatch.setattr(runner, "__run_task_sync", fake_run)
        shutdown.register_callback(lambda: None)

        assert runner._run_worker_sync(object(), "task", worker_id=1) == "done"
        assert seen == {"escalate": False, "installed": True, "callbacks": (), "kwargs": {"worker_id": 1}}

    def test_keeps_a_stop_that_arrived_before_it_started_and_skips_the_task(self, monkeypatch):
        ran = []
        monkeypatch.setattr(runner, "__run_task_sync", lambda *a, **k: ran.append(True))
        shutdown._handle_signal(signal.SIGTERM, None)

        assert runner._run_worker_sync(object(), "task", worker_id=0) is None
        assert ran == []
        assert shutdown.is_requested()


class _EmptyConnector(Connector):
    def fetch_transactions(self, *args, **kwargs):
        return []

    def dispatch_transactions(self, transactions, *args, **kwargs):
        return None


class _EmptyAsyncConnector(AsyncConnector):
    async def fetch_transactions_async(self, *args, **kwargs):
        return []

    async def dispatch_transactions_async(self, transactions, *args, **kwargs):
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


class _NoopAsyncTask(BaseAsyncTask):
    async def execute(self):
        return None


def test_sync_streaming_task_exits_with_143_on_sigterm(monkeypatch, stop_later):
    """The production bug: a sync streaming consumer ignored SIGTERM until systemd's SIGKILL."""
    monkeypatch.setattr(runner, "_install_signal_handlers", lambda **_: None)
    stop_later(0.2)

    config = TaskConfig(
        name="idle-sync-consumer",
        task=_IdleStreamingTask,
        connectors={"queue": ConnectorConfig(connector=_EmptyConnector)},
    )

    started = time.monotonic()
    assert runner.run_task(config) == 143
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    ("task", "connector", "escalate"),
    [(_IdleStreamingTask, _EmptyConnector, True), (_NoopAsyncTask, _EmptyAsyncConnector, False)],
)
def test_only_sync_tasks_escalate_on_a_second_signal(monkeypatch, task, connector, escalate):
    installed = []
    monkeypatch.setattr(runner, "_install_signal_handlers", lambda **kwargs: installed.append(kwargs))
    monkeypatch.setattr(runner, "__run_task_sync", lambda *a, **k: None)

    async def noop_async(*a, **k):
        return None

    monkeypatch.setattr(runner, "__run_task_async", noop_async)
    config = TaskConfig(name="escalation", task=task, connectors={"c": ConnectorConfig(connector=connector)})

    assert runner.run_task(config) == 0
    assert installed == [{"escalate": escalate}]
