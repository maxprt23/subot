"""Process supervision for the polling and notification workers.

The supervisor intentionally knows nothing about the queue implementation.  It
starts a poller and a worker with a pair of shared events, watches their
lifecycle, and coordinates the finite ``once`` mode.  The child target
contract is:

``target(stop_event, poller_done_event, once, *target_args, **target_kwargs)``

The poller should return after one pass when ``once`` is true.  The supervisor
sets ``poller_done_event`` after that process exits, allowing the worker to
finish draining any queued work.  A parent-side ``worker_done`` callback can
be supplied when the queue has an explicit terminal-state query.
"""

from __future__ import annotations

import logging
import multiprocessing
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any


LOGGER = logging.getLogger("subot")
_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class Supervisor:
    """Run and supervise independent poller and worker processes.

    ``poller_target`` and ``worker_target`` are called in child processes with
    the shared ``stop_event``, the ``poller_done_event``, and the selected
    ``once`` flag prepended to their configured arguments.  A target should
    observe ``stop_event`` and return promptly after it is set.

    In ``once`` mode, the worker is allowed to drain after the poller exits.
    If ``worker_done`` is provided, it is polled only after the poller has
    completed.  It should return true once all queued jobs are terminal.  The
    callback runs in the supervisor process and therefore may safely inspect a
    queue database without being pickled into a child.
    """

    def __init__(
        self,
        poller_target: Callable[..., Any],
        worker_target: Callable[..., Any],
        *,
        once: bool = False,
        worker_done: Callable[[], bool] | None = None,
        poller_args: Sequence[Any] = (),
        worker_args: Sequence[Any] = (),
        poller_kwargs: Mapping[str, Any] | None = None,
        worker_kwargs: Mapping[str, Any] | None = None,
        process_factory: Callable[..., Any] = multiprocessing.Process,
        event_factory: Callable[[], Any] = multiprocessing.Event,
        wait_interval: float = 0.25,
        shutdown_timeout: float = 10.0,
        install_signal_handlers: bool = True,
        sleep: Callable[[float], Any] = time.sleep,
    ) -> None:
        if not callable(poller_target):
            raise TypeError("poller_target must be callable")
        if not callable(worker_target):
            raise TypeError("worker_target must be callable")
        if worker_done is not None and not callable(worker_done):
            raise TypeError("worker_done must be callable or None")
        if wait_interval < 0:
            raise ValueError("wait_interval must not be negative")
        if shutdown_timeout < 0:
            raise ValueError("shutdown_timeout must not be negative")

        self.poller_target = poller_target
        self.worker_target = worker_target
        self.once = once
        self.worker_done = worker_done
        self.poller_args = tuple(poller_args)
        self.worker_args = tuple(worker_args)
        self.poller_kwargs = dict(poller_kwargs or {})
        self.worker_kwargs = dict(worker_kwargs or {})
        self.process_factory = process_factory
        self.event_factory = event_factory
        self.wait_interval = wait_interval
        self.shutdown_timeout = shutdown_timeout
        self.install_signal_handlers = install_signal_handlers
        self._sleep = sleep

        self.stop_event = event_factory()
        self.poller_done_event = event_factory()
        self._poller = None
        self._worker = None
        self._started_processes = []
        self._old_signal_handlers = {}
        self._requested_exit_code: int | None = None

    @property
    def poller_process(self):
        """Return the created poller process, if ``run`` has started it."""

        return self._poller

    @property
    def worker_process(self):
        """Return the created worker process, if ``run`` has started it."""

        return self._worker

    def request_shutdown(self, signum: int | None = None) -> None:
        """Request a coordinated shutdown.

        Signal handlers use the conventional ``128 + signal`` exit status.
        A normal internal shutdown leaves the eventual result unchanged.
        """

        if signum is not None:
            self._requested_exit_code = 128 + int(signum)
        self.stop_event.set()

    def run(self) -> int:
        """Start both children, supervise them, and return a process status."""

        try:
            self._install_signal_handlers()
            self._start_children()
            if self.once:
                return self._run_once()
            return self._run_continuously()
        except KeyboardInterrupt:
            self.request_shutdown(signal.SIGINT)
            return 128 + signal.SIGINT
        except Exception:
            LOGGER.exception("supervisor failed")
            self.request_shutdown()
            return 1
        finally:
            self._shutdown_children()
            self._restore_signal_handlers()

    def _start_children(self) -> None:
        common_args = (self.stop_event, self.poller_done_event, self.once)
        self._poller = self.process_factory(
            name="subot-poller",
            target=self.poller_target,
            args=common_args + self.poller_args,
            kwargs=dict(self.poller_kwargs),
        )
        self._worker = self.process_factory(
            name="subot-worker",
            target=self.worker_target,
            args=common_args + self.worker_args,
            kwargs=dict(self.worker_kwargs),
        )

        for process in (self._poller, self._worker):
            process.start()
            self._started_processes.append(process)

    def _run_continuously(self) -> int:
        while not self.stop_event.is_set():
            if not self._process_alive(self._poller):
                LOGGER.error("poller process exited unexpectedly exitcode=%s", self._poller.exitcode)
                self.request_shutdown()
                return 1
            if not self._process_alive(self._worker):
                LOGGER.error("worker process exited unexpectedly exitcode=%s", self._worker.exitcode)
                self.request_shutdown()
                return 1
            self._pause()

        return self._requested_exit_code or 0

    def _run_once(self) -> int:
        while self._process_alive(self._poller):
            if self.stop_event.is_set():
                return self._requested_exit_code or 0
            # A worker that dies before the producer has finished cannot drain
            # the queue, even if it happened to return status zero.
            if not self._process_alive(self._worker):
                LOGGER.error(
                    "worker process exited before poller completion exitcode=%s",
                    self._worker.exitcode,
                )
                self.request_shutdown()
                return 1
            self._pause()

        if self.stop_event.is_set():
            return self._requested_exit_code or 0
        if self._process_exit_code(self._poller) != 0:
            LOGGER.error("poller failed in once mode exitcode=%s", self._poller.exitcode)
            self.request_shutdown()
            return 1

        self.poller_done_event.set()

        if self.worker_done is None:
            return self._wait_for_worker_exit()

        while not self.stop_event.is_set():
            if not self._process_alive(self._worker):
                if self._process_exit_code(self._worker) == 0:
                    try:
                        if self.worker_done():
                            return 0
                    except Exception:
                        LOGGER.exception("worker completion check failed")
                LOGGER.error("worker failed in once mode exitcode=%s", self._worker.exitcode)
                self.request_shutdown()
                return 1
            try:
                if self.worker_done():
                    self.request_shutdown()
                    return 0
            except Exception:
                LOGGER.exception("worker completion check failed")
                self.request_shutdown()
                return 1
            self._pause()

        return self._requested_exit_code or 0

    def _wait_for_worker_exit(self) -> int:
        while self._process_alive(self._worker):
            if self.stop_event.is_set():
                return self._requested_exit_code or 0
            self._pause()
        if self._process_exit_code(self._worker) != 0:
            LOGGER.error("worker failed in once mode exitcode=%s", self._worker.exitcode)
            self.request_shutdown()
            return 1
        return 0

    def _pause(self) -> None:
        self._sleep(self.wait_interval)

    @staticmethod
    def _process_alive(process: Any) -> bool:
        return process.is_alive()

    @staticmethod
    def _process_exit_code(process: Any) -> int:
        # A test double may leave exitcode as None after reporting that it has
        # stopped.  Treat that as a clean return; a real Process sets it.
        return 0 if process.exitcode is None else process.exitcode

    def _install_signal_handlers(self) -> None:
        if not self.install_signal_handlers:
            return
        for signum in _SIGNALS:
            self._old_signal_handlers[signum] = signal.signal(signum, self._handle_signal)

    def _restore_signal_handlers(self) -> None:
        for signum, handler in self._old_signal_handlers.items():
            signal.signal(signum, handler)
        self._old_signal_handlers.clear()

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        self.request_shutdown(signum)

    def _shutdown_children(self) -> None:
        if not self._started_processes:
            return

        self.stop_event.set()
        for process in self._started_processes:
            try:
                process.join(self.shutdown_timeout)
            except (AssertionError, ValueError):
                LOGGER.debug("could not join process name=%s", process.name)

            if not self._process_alive(process):
                continue
            LOGGER.warning("terminating child process name=%s", process.name)
            process.terminate()
            process.join(self.shutdown_timeout)
            if not self._process_alive(process):
                continue
            kill = getattr(process, "kill", None)
            if kill is None:
                LOGGER.error("child process remains alive name=%s", process.name)
                continue
            LOGGER.error("killing child process name=%s", process.name)
            kill()
            process.join(self.shutdown_timeout)


def run_supervisor(
    poller_target: Callable[..., Any],
    worker_target: Callable[..., Any],
    **kwargs: Any,
) -> int:
    """Convenience wrapper around :class:`Supervisor`."""

    return Supervisor(poller_target, worker_target, **kwargs).run()


__all__ = ["Supervisor", "run_supervisor"]
