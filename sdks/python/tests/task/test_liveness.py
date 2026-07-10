import threading

from ergon.task.liveness import TaskLivenessSnapshot, TaskSupervisionPolicy
from ergon.task.runner import _TaskLivenessSupervisor


class _Provider:
    def __init__(self, healthy: bool) -> None:
        self.healthy = healthy

    def liveness_snapshot(self) -> TaskLivenessSnapshot:
        return TaskLivenessSnapshot(
            healthy=self.healthy,
            state="polling" if self.healthy else "poll_stalled",
            reason=None if self.healthy else "poll stopped",
        )


def test_supervisor_requests_shutdown_after_sustained_failure():
    shutdown = threading.Event()
    hard_exit = threading.Event()
    supervisor = _TaskLivenessSupervisor(
        provider=_Provider(healthy=False),
        policy=TaskSupervisionPolicy(
            check_interval=0.01,
            startup_grace=0,
            unhealthy_grace=0.02,
            shutdown_grace=1,
        ),
        request_shutdown=lambda _reason: shutdown.set(),
        hard_exit=lambda _code: hard_exit.set(),
    )

    supervisor.start()
    try:
        assert shutdown.wait(timeout=0.5)
        assert not hard_exit.is_set()
    finally:
        supervisor.stop()


def test_supervisor_hard_exits_when_cooperative_shutdown_stalls():
    shutdown = threading.Event()
    hard_exit = threading.Event()
    supervisor = _TaskLivenessSupervisor(
        provider=_Provider(healthy=False),
        policy=TaskSupervisionPolicy(
            check_interval=0.01,
            startup_grace=0,
            unhealthy_grace=0.01,
            shutdown_grace=0.02,
        ),
        request_shutdown=lambda _reason: shutdown.set(),
        hard_exit=lambda _code: hard_exit.set(),
    )

    supervisor.start()
    try:
        assert shutdown.wait(timeout=0.5)
        assert hard_exit.wait(timeout=0.5)
    finally:
        supervisor.stop()
