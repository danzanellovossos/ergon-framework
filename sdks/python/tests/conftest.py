import signal
import threading

import pytest

from ergon.task import shutdown

_HANDLED = (signal.SIGINT, signal.SIGTERM)


@pytest.fixture(autouse=True)
def _isolated_shutdown_state():
    """No test sees, or leaks, a stop request, an installed handler or a blocked signal."""
    handlers = {signum: signal.getsignal(signum) for signum in _HANDLED}
    shutdown.reset()
    yield
    shutdown.unblock_signals()
    shutdown.restore_default_handlers()
    for signum, handler in handlers.items():
        signal.signal(signum, handler)
    shutdown._escalate = True
    shutdown.reset()


@pytest.fixture
def stop_later():
    """``stop_later(seconds, signum=SIGTERM)``: request a stop from another thread, cancelled at teardown."""
    timers = []

    def schedule(seconds: float, signum: int = signal.SIGTERM) -> threading.Timer:
        timer = threading.Timer(seconds, shutdown._handle_signal, args=(signum, None))
        timer.daemon = True
        timers.append(timer)
        timer.start()
        return timer

    yield schedule
    for timer in timers:
        timer.cancel()
        timer.join(1)
