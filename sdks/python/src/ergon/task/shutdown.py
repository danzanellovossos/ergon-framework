"""Process-wide shutdown state shared by the runner, the consume loops and the backoff sleeps.

The runner installs the signal handlers; everything else only reads this module, which
imports nothing from ``ergon.task`` so ``utils`` and the mixins can use it without a cycle.

First SIGINT/SIGTERM: a cooperative stop. Sync consume loops stop fetching, hand back
fetched-but-unstarted transactions and let the in-flight ones finish. With escalation on
(sync runner), a second signal while the stop is in progress exits at once with
``128 + signum``, so an operator is never stuck waiting for a long transaction.

How a signal is processed:

- The kernel may deliver it to any thread that does not block it, and Python runs the
  Python-level handler only on the main thread, at its next bytecode. A main thread asleep
  in a lock (``Future.result()``, ``Event.wait``) would never notice. So the runner owns
  ``signal.set_wakeup_fd``: CPython's C handler writes the signal number to a pipe from
  whichever thread received it, and a watcher thread acts on it.
- The watcher is the only place that counts signals, sets the ``Event``, logs, runs the
  callbacks and escalates. The Python handler only flips a flag so the main thread sees
  the stop right away; it never takes a lock, because the main thread may hold one
  (``threading.Event``, logging handlers, buffered stdio) when it runs.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

_HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM)

_requested = False
_signal: int | None = None
_seen = False
_event = threading.Event()

# Copy-on-write tuples: the watcher reads them without a lock.
_callbacks: tuple[Callable[[], None], ...] = ()
_escalation_callbacks: tuple[Callable[[], None], ...] = ()
_registry_lock = threading.Lock()

_handlers_installed = False
_escalate = True

_wakeup_read: int | None = None
_wakeup_write: int | None = None
_wakeup_installed = False
_watcher: threading.Thread | None = None


def _handle_signal(signum, frame) -> None:
    global _requested, _signal
    if _signal is None:
        _signal = signum
    _requested = True
    if not _wakeup_installed:
        # Direct call (tests), or a forked child that has not run ``prepare_worker`` yet.
        _on_signal(signum)


def _on_signal(signum: int) -> None:
    global _requested, _signal, _seen
    if _seen and _handlers_installed and _escalate:
        _escalate_now(signum)
        return
    _seen = True
    if _signal is None:
        _signal = signum
    _requested = True
    _event.set()
    logger.warning(
        "%s received; finishing in-flight work before exiting%s",
        signal.Signals(signum).name,
        " (send it again to exit now)" if _handlers_installed and _escalate else "",
    )
    for callback in _callbacks:
        try:
            callback()
        except Exception:
            logger.exception("Shutdown callback failed")


def _escalate_now(signum: int) -> None:
    try:
        os.write(2, f"Second {signal.Signals(signum).name} during shutdown; exiting immediately\n".encode())
    except OSError:
        pass
    for callback in _escalation_callbacks:
        try:
            callback()
        except Exception:
            pass
    os._exit(128 + signum)


def _watch(read_fd: int) -> None:
    while True:
        try:
            data = os.read(read_fd, 64)
        except OSError:
            return
        if not data:
            return
        for signum in data:
            if signum in _HANDLED_SIGNALS:
                _on_signal(signum)


def _start_watcher() -> None:
    global _wakeup_read, _wakeup_write, _watcher
    if _watcher is not None and _watcher.is_alive():
        return
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    _wakeup_read, _wakeup_write = read_fd, write_fd
    _watcher = threading.Thread(target=_watch, args=(read_fd,), name="ergon-shutdown-watcher", daemon=True)
    _watcher.start()


def install_handlers(*, escalate: bool = True) -> None:
    """Route SIGINT/SIGTERM to the cooperative stop. Main thread only (``signal`` rule).

    ``escalate=False`` keeps every signal cooperative. Worker processes use it because
    systemd's ``KillMode=control-group`` signals every process and the parent forwards
    its own signal too; the parent escalates for them (``register_escalation_callback``).
    The async runner uses it as well: its supervisor owns the hard deadline.
    """
    global _handlers_installed, _escalate, _wakeup_installed
    _escalate = escalate
    _start_watcher()
    assert _wakeup_write is not None
    for signum in _HANDLED_SIGNALS:
        signal.signal(signum, _handle_signal)
    signal.set_wakeup_fd(_wakeup_write, warn_on_full_buffer=False)
    _wakeup_installed = True
    _handlers_installed = True


def restore_default_handlers() -> None:
    global _handlers_installed, _wakeup_installed
    signal.set_wakeup_fd(-1)
    _wakeup_installed = False
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    _handlers_installed = False


def reset() -> None:
    """Forget a previous stop request and all callbacks (start of a run, tests)."""
    global _requested, _signal, _seen, _callbacks, _escalation_callbacks
    _requested = False
    _signal = None
    _seen = False
    _event.clear()
    with _registry_lock:
        _callbacks = ()
        _escalation_callbacks = ()


def prepare_worker() -> None:
    """Enter a worker process of the multiprocess runner.

    Keeps a stop request that already arrived (inherited through fork, or delivered
    while signals were blocked), drops the parent's callbacks and never escalates.
    """
    global _callbacks, _escalation_callbacks
    with _registry_lock:
        _callbacks = ()
        _escalation_callbacks = ()
    install_handlers(escalate=False)
    if _requested:
        _event.set()
    unblock_signals()


def _after_fork_in_child() -> None:
    global _escalate, _callbacks, _escalation_callbacks, _registry_lock
    global _wakeup_read, _wakeup_write, _wakeup_installed, _watcher
    # The parent's watcher thread does not exist here, and the parent's callbacks
    # (forward to workers, SIGKILL workers) must never run in a child.
    _escalate = False
    _callbacks = ()
    _escalation_callbacks = ()
    _registry_lock = threading.Lock()
    _wakeup_installed = False
    try:
        signal.set_wakeup_fd(-1)
    except ValueError:
        # Forked from a non-main thread: the C handler may still write to the old pipe,
        # so leave it open rather than let the fd number be reused.
        pass
    else:
        for fd in (_wakeup_read, _wakeup_write):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
    _wakeup_read = _wakeup_write = None
    _watcher = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def is_requested() -> bool:
    return _requested


def requested_signal() -> int | None:
    return _signal


def wait(timeout: float) -> bool:
    """Sleep up to ``timeout`` seconds; return True as soon as a shutdown is requested."""
    if _requested:
        return True
    return _event.wait(timeout) or _requested


def register_callback(callback: Callable[[], None]) -> None:
    """Run ``callback`` on every stop request; immediately when a stop was already requested."""
    global _callbacks
    with _registry_lock:
        _callbacks = (*_callbacks, callback)
    if _requested:
        callback()


def unregister_callback(callback: Callable[[], None]) -> None:
    global _callbacks
    with _registry_lock:
        _callbacks = tuple(c for c in _callbacks if c is not callback)


def register_escalation_callback(callback: Callable[[], None]) -> None:
    """Run ``callback`` right before a second signal ends the process (keep it to ``os.kill``-like work)."""
    global _escalation_callbacks
    with _registry_lock:
        _escalation_callbacks = (*_escalation_callbacks, callback)


def unregister_escalation_callback(callback: Callable[[], None]) -> None:
    global _escalation_callbacks
    with _registry_lock:
        _escalation_callbacks = tuple(c for c in _escalation_callbacks if c is not callback)


def blocked_signals():
    """Context manager that defers SIGINT/SIGTERM for the calling thread (POSIX), e.g. while forking workers.

    A child process inherits the mask, so a worker cannot be killed by a signal that
    arrives before it installed its handlers; ``prepare_worker`` unblocks it.
    """
    return _BlockedSignals()


class _BlockedSignals:
    def __enter__(self):
        if hasattr(signal, "pthread_sigmask"):
            signal.pthread_sigmask(signal.SIG_BLOCK, _HANDLED_SIGNALS)
        return self

    def __exit__(self, *exc) -> None:
        unblock_signals()


def unblock_signals() -> None:
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_UNBLOCK, _HANDLED_SIGNALS)
