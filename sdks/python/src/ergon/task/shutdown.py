"""Process-wide shutdown state shared by the runner, the consume loops and the backoff sleeps.

The runner installs the signal handlers; everything else only reads this module, which
imports nothing from ``ergon.task`` so ``utils`` and the mixins can use it without a cycle.

First SIGINT/SIGTERM: a cooperative stop. Sync consume loops stop fetching, hand back
fetched-but-unstarted transactions and let the in-flight ones finish. A second signal
while the stop is in progress gets the default action (immediate exit), so an operator
is never stuck waiting for a long transaction.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

_event = threading.Event()
_signal: int | None = None
_callbacks: set[Callable[[], None]] = set()
_escalation_callbacks: set[Callable[[], None]] = set()
# Reentrant: the handler runs on the main thread, possibly while that thread holds it.
_callbacks_lock = threading.RLock()
_handlers_installed = False
_escalate = True

_HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def _handle_signal(signum, frame) -> None:
    global _signal
    if _event.is_set() and _handlers_installed and _escalate:
        logger.warning("Second %s during shutdown; exiting immediately", signal.Signals(signum).name)
        with _callbacks_lock:
            escalation = tuple(_escalation_callbacks)
        for callback in escalation:
            try:
                callback()
            except Exception:
                logger.exception("Shutdown escalation callback failed")
        restore_default_handlers()
        signal.raise_signal(signum)
        return
    _signal = signum
    _event.set()
    if signum in _HANDLED_SIGNALS:
        logger.warning(
            "%s received; finishing in-flight work before exiting (send it again to exit now)",
            signal.Signals(signum).name,
        )
    with _callbacks_lock:
        callbacks = tuple(_callbacks)
    for callback in callbacks:
        try:
            callback()
        except Exception:
            logger.exception("Shutdown callback failed")


def install_handlers(*, escalate: bool = True) -> None:
    """Route SIGINT/SIGTERM to the cooperative stop. Main thread only (``signal`` rule).

    ``escalate=False`` is for worker processes: systemd signals the whole cgroup and the
    parent forwards its own signal too, so a worker can legitimately get two. The parent
    escalates for them (``register_escalation_callback``).
    """
    global _handlers_installed, _escalate
    for signum in _HANDLED_SIGNALS:
        signal.signal(signum, _handle_signal)
    _handlers_installed = True
    _escalate = escalate


def restore_default_handlers() -> None:
    global _handlers_installed
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    _handlers_installed = False


def reset() -> None:
    global _signal
    _event.clear()
    _signal = None
    with _callbacks_lock:
        _callbacks.clear()
        _escalation_callbacks.clear()


def is_requested() -> bool:
    return _event.is_set()


def requested_signal() -> int | None:
    return _signal


def wait(timeout: float) -> bool:
    """Sleep up to ``timeout`` seconds; return True as soon as a shutdown is requested."""
    return _event.wait(timeout)


def register_callback(callback: Callable[[], None]) -> None:
    """Run ``callback`` on shutdown; immediately when the shutdown already started."""
    with _callbacks_lock:
        _callbacks.add(callback)
        already = _event.is_set()
    if already:
        callback()


def unregister_callback(callback: Callable[[], None]) -> None:
    with _callbacks_lock:
        _callbacks.discard(callback)


def register_escalation_callback(callback: Callable[[], None]) -> None:
    """Run ``callback`` right before a second signal kills the process."""
    with _callbacks_lock:
        _escalation_callbacks.add(callback)


def unregister_escalation_callback(callback: Callable[[], None]) -> None:
    with _callbacks_lock:
        _escalation_callbacks.discard(callback)
