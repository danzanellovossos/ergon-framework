"""Real signals against a real sync worker process (tests/task/_shutdown_app.py).

This is what systemd does on ``stop``/``restart``: SIGTERM, then SIGKILL once
``TimeoutStopSec`` runs out. With ``KillMode=control-group`` every process in the unit
gets the SIGTERM, which ``test_whole_process_group_signalled`` reproduces.
"""

import json
import os
import signal
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

SDK_ROOT = Path(__file__).resolve().parents[2]


class App:
    def __init__(
        self,
        tmp_path: Path,
        *,
        workers=1,
        process_seconds=1.0,
        queue=20,
        batch=3,
        new_session=False,
        start_method="",
        entry="main",
        extra_env=None,
    ):
        self.new_session = new_session
        self.events_path = tmp_path / "events.jsonl"
        self.events_path.touch()
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(SDK_ROOT / "src"), str(SDK_ROOT)]),
            "SHUTDOWN_APP_EVENTS": str(self.events_path),
            "SHUTDOWN_APP_WORKERS": str(workers),
            "SHUTDOWN_APP_PROCESS_SECONDS": str(process_seconds),
            "SHUTDOWN_APP_QUEUE": str(queue),
            "SHUTDOWN_APP_BATCH": str(batch),
            "SHUTDOWN_APP_START_METHOD": start_method,
            **(extra_env or {}),
        }
        self.log_path = tmp_path / "output.log"
        self._log = open(self.log_path, "w")
        self.proc = subprocess.Popen(
            [sys.executable, "-c", f"from tests.task._shutdown_app import {entry}; {entry}()"],
            cwd=SDK_ROOT,
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            start_new_session=new_session,
        )

    def events(self):
        lines = self.events_path.read_text().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def by_pid(self):
        out = defaultdict(lambda: defaultdict(list))
        for e in self.events():
            if e["event"] == "fetched":
                out[e["pid"]]["fetched"].extend(e["ids"])
            elif "id" in e:
                out[e["pid"]][e["event"]].append(e["id"])
        return out

    def wait_for(self, predicate, timeout=30.0, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self.events()):
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.02)
        raise AssertionError(f"timed out waiting for {what}; exit={self.proc.poll()}\n{self.output()}")

    def wait_exit(self, timeout):
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.dump_stacks()
            raise AssertionError(f"process did not exit within {timeout}s\n{self.output()}") from None

    def dump_stacks(self):
        """faulthandler (registered on SIGUSR1 by the app) writes every thread's stack to the log."""
        children = subprocess.run(["pgrep", "-P", str(self.proc.pid)], capture_output=True, text=True).stdout.split()
        for pid in [self.proc.pid, *{int(c) for c in children}, *{e["pid"] for e in self.events()}]:
            try:
                os.kill(pid, signal.SIGUSR1)
            except ProcessLookupError:
                pass
            time.sleep(0.3)

    def output(self):
        self._log.flush()
        return self.log_path.read_text()[-12000:]

    def close(self):
        if self.proc.poll() is None:
            try:
                if self.new_session:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                else:
                    self.proc.kill()
            except ProcessLookupError:
                pass
            self.proc.wait(5)
        self._log.close()


@pytest.fixture
def app(tmp_path):
    started = []

    def start(**kwargs):
        instance = App(tmp_path, **kwargs)
        started.append(instance)
        return instance

    yield start
    for instance in started:
        instance.close()


def started_in(n_pids):
    def predicate(events):
        return len({e["pid"] for e in events if e["event"] == "started"}) >= n_pids

    return predicate


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def assert_drained(worker, *, must_have_started=True):
    """Started == finished, the rest of the batch released, and no fetch after the stop."""
    if must_have_started:
        assert worker["started"], worker
    assert sorted(worker["finished"]) == sorted(worker["started"])
    assert sorted(worker["started"] + worker["released"]) == sorted(worker["fetched"])


def assert_no_survivors(pids, timeout=5.0):
    deadline = time.monotonic() + timeout
    while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(alive(pid) for pid in pids), "orphaned worker processes"


def test_sigterm_mid_transaction_finishes_it_and_releases_the_rest(app):
    a = app(process_seconds=1.0, batch=3)
    a.wait_for(started_in(1), what="first transaction")

    a.proc.send_signal(signal.SIGTERM)

    assert a.wait_exit(15) == 143
    (worker,) = a.by_pid().values()
    assert_drained(worker)
    assert len(worker["started"]) == 1
    assert len(worker["released"]) == 2


def test_sigterm_while_idle_exits_without_waiting_for_the_empty_backoff(app):
    a = app(queue=0)
    a.wait_for(lambda events: any(e["event"] == "fetched" for e in events), what="first fetch")
    time.sleep(0.2)

    started = time.monotonic()
    a.proc.send_signal(signal.SIGTERM)

    assert a.wait_exit(10) == 143
    assert time.monotonic() - started < 5


def test_second_sigterm_exits_immediately(app):
    a = app(process_seconds=60)
    a.wait_for(started_in(1), what="first transaction")

    a.proc.send_signal(signal.SIGTERM)
    time.sleep(0.5)
    assert a.proc.poll() is None, "first SIGTERM must not kill an in-flight transaction"
    a.proc.send_signal(signal.SIGTERM)

    assert a.wait_exit(5) == 128 + signal.SIGTERM
    (worker,) = a.by_pid().values()
    assert worker["finished"] == []


START_METHODS = pytest.mark.parametrize("start_method", ["fork", "spawn"])


@START_METHODS
def test_multiprocess_parent_forwards_sigterm_and_every_worker_drains(app, start_method):
    a = app(workers=2, process_seconds=1.5, batch=3, start_method=start_method)
    a.wait_for(started_in(2), timeout=60, what="both workers")

    a.proc.send_signal(signal.SIGTERM)

    assert a.wait_exit(20) == 143
    workers = a.by_pid()
    assert len(workers) == 2
    for worker in workers.values():
        assert_drained(worker)


@START_METHODS
def test_whole_process_group_signalled_like_systemd_does_not_kill_workers(app, start_method):
    a = app(workers=2, process_seconds=1.5, batch=3, new_session=True, start_method=start_method)
    a.wait_for(started_in(2), timeout=60, what="both workers")

    os.killpg(a.proc.pid, signal.SIGTERM)

    assert a.wait_exit(20) == 143
    workers = a.by_pid()
    assert len(workers) == 2
    for worker in workers.values():
        assert_drained(worker)


@START_METHODS
def test_multiprocess_second_sigterm_kills_the_workers_too(app, start_method):
    a = app(workers=2, process_seconds=60, start_method=start_method)
    a.wait_for(started_in(2), timeout=60, what="both workers")
    worker_pids = list(a.by_pid())

    a.proc.send_signal(signal.SIGTERM)
    time.sleep(0.5)
    a.proc.send_signal(signal.SIGTERM)

    assert a.wait_exit(10) == 128 + signal.SIGTERM
    assert_no_survivors(worker_pids)


def test_second_sigint_exits_immediately_even_with_a_transaction_in_flight(app):
    a = app(process_seconds=60)
    a.wait_for(started_in(1), what="first transaction")

    a.proc.send_signal(signal.SIGINT)
    time.sleep(0.5)
    assert a.proc.poll() is None, "first SIGINT must not kill an in-flight transaction"
    started = time.monotonic()
    a.proc.send_signal(signal.SIGINT)

    assert a.wait_exit(5) == 128 + signal.SIGINT
    assert time.monotonic() - started < 3


@START_METHODS
def test_sigterm_while_workers_are_starting_stops_every_worker(app, start_method):
    """The stop must survive the gap between forking a worker and it installing its handlers."""
    a = app(
        workers=2,
        process_seconds=1.0,
        start_method=start_method,
        extra_env={"SHUTDOWN_APP_SIGTERM_AFTER_FIRST_SUBMIT": "1"},
    )

    assert a.wait_exit(30) == 143
    for worker in a.by_pid().values():
        assert_drained(worker, must_have_started=False)
        assert len(worker["started"]) <= 1


@START_METHODS
def test_crashed_worker_stops_its_sibling_and_fails_the_run(app, start_method):
    a = app(workers=2, process_seconds=1.5, batch=3, start_method=start_method)
    a.wait_for(started_in(2), timeout=60, what="both workers")
    crashed, survivor = list(a.by_pid())

    os.kill(crashed, signal.SIGKILL)

    assert a.wait_exit(20) == 1
    assert_drained(a.by_pid()[survivor])
    assert_no_survivors([survivor])


@START_METHODS
def test_crashed_worker_then_sigterm_lets_the_survivor_drain(app, start_method):
    a = app(workers=2, process_seconds=2.0, batch=3, start_method=start_method)
    a.wait_for(started_in(2), timeout=60, what="both workers")
    crashed, survivor = list(a.by_pid())

    os.kill(crashed, signal.SIGKILL)
    time.sleep(0.3)
    a.proc.send_signal(signal.SIGTERM)

    assert a.wait_exit(20) == 143
    assert_drained(a.by_pid()[survivor])
    assert_no_survivors([survivor])


@START_METHODS
def test_crashed_worker_then_two_sigterms_leaves_no_orphan(app, start_method):
    a = app(workers=2, process_seconds=60, start_method=start_method)
    a.wait_for(started_in(2), timeout=60, what="both workers")
    crashed, survivor = list(a.by_pid())

    os.kill(crashed, signal.SIGKILL)
    time.sleep(0.3)
    a.proc.send_signal(signal.SIGTERM)
    time.sleep(0.5)
    a.proc.send_signal(signal.SIGTERM)

    assert a.wait_exit(10) == 128 + signal.SIGTERM
    assert_no_survivors([survivor])


def test_signal_storm_never_deadlocks_the_handler(app):
    """Signals landing while the main thread sits inside ``wait``/logging locks."""
    a = app(entry="storm_main", extra_env={"SHUTDOWN_APP_STORM_SECONDS": "3"})
    a.wait_for(lambda events: any(e["event"] == "ready" for e in events), what="storm ready")

    for _ in range(400):
        if a.proc.poll() is not None:
            break
        a.proc.send_signal(signal.SIGTERM)
        time.sleep(0.002)

    assert a.wait_exit(20) == 0
    assert any(e["event"] == "done" for e in a.events())
