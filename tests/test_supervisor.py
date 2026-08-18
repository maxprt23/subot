import signal
import unittest
from unittest.mock import Mock, patch

from subot_core.supervisor import Supervisor


class FakeEvent:
    def __init__(self):
        self.set_calls = 0
        self._set = False

    def set(self):
        self.set_calls += 1
        self._set = True

    def is_set(self):
        return self._set


class FakeProcess:
    def __init__(self, *, name, target, args, kwargs, alive_sequence=()):
        self.name = name
        self.target = target
        self.args = args
        self.kwargs = kwargs
        self.started = False
        self.terminated = False
        self.killed = False
        self.join_calls = []
        self._alive = True
        self._is_alive_calls = 0
        self._alive_sequence = list(alive_sequence)
        self.exitcode = None

    def start(self):
        self.started = True

    def is_alive(self):
        self._is_alive_calls += 1
        if self._alive_sequence:
            self._alive = self._alive_sequence.pop(0)
        return self._alive

    def join(self, timeout=None):
        self.join_calls.append(timeout)

    def terminate(self):
        self.terminated = True
        self._alive = False
        self.exitcode = -signal.SIGTERM

    def kill(self):
        self.killed = True
        self._alive = False
        self.exitcode = -signal.SIGKILL


class ProcessFactory:
    def __init__(self):
        self.processes = []
        self.alive_sequences = {}

    def __call__(self, *, name, target, args, kwargs):
        process = FakeProcess(
            name=name,
            target=target,
            args=args,
            kwargs=kwargs,
            alive_sequence=self.alive_sequences.get(name, ()),
        )
        self.processes.append(process)
        return process


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.process_factory = ProcessFactory()

        def event_factory():
            event = FakeEvent()
            self.events.append(event)
            return event

        self.event_factory = event_factory
        self.poller = Mock(name="poller_target")
        self.worker = Mock(name="worker_target")

    def make_supervisor(self, **kwargs):
        return Supervisor(
            self.poller,
            self.worker,
            process_factory=self.process_factory,
            event_factory=self.event_factory,
            install_signal_handlers=False,
            wait_interval=0,
            shutdown_timeout=0,
            **kwargs,
        )

    def test_once_starts_independent_children_and_waits_for_worker_drain(self):
        self.process_factory.alive_sequences = {
            "subot-poller": (True, False),
            "subot-worker": (True,),
        }
        supervisor = self.make_supervisor(
            once=True,
            worker_done=lambda: True,
        )
        poller_process = self.process_factory.processes

        result = supervisor.run()

        self.assertEqual(result, 0)
        self.assertEqual(len(poller_process), 2)
        self.assertTrue(all(process.started for process in poller_process))
        self.assertEqual(
            [process.name for process in poller_process],
            ["subot-poller", "subot-worker"],
        )
        self.assertIs(poller_process[0].args[0], self.events[0])
        self.assertIs(poller_process[0].args[1], self.events[1])
        self.assertIs(poller_process[1].args[0], self.events[0])
        self.assertIs(poller_process[1].args[1], self.events[1])
        self.assertEqual(poller_process[0].args[2], True)
        self.assertEqual(poller_process[1].args[2], True)
        self.assertTrue(self.events[1].is_set())
        self.assertTrue(self.events[0].is_set())

    def test_once_does_not_check_worker_completion_before_poller_finishes(self):
        self.process_factory.alive_sequences = {
            "subot-poller": (True, False),
            "subot-worker": (True,),
        }
        poller_finished = []
        supervisor = self.make_supervisor(
            once=True,
            worker_done=lambda: poller_finished.append(self.events[1].is_set())
            or True,
        )
        result = supervisor.run()

        self.assertEqual(result, 0)
        self.assertEqual(poller_finished, [True])

    def test_once_rejects_clean_worker_exit_before_terminal_drain(self):
        self.process_factory.alive_sequences = {
            "subot-poller": (True, False),
            "subot-worker": (True, False),
        }
        drain_checks = []
        supervisor = self.make_supervisor(
            once=True,
            worker_done=lambda: drain_checks.append(False) or False,
        )

        result = supervisor.run()

        self.assertEqual(result, 1)
        self.assertEqual(drain_checks, [False])

    def test_signal_request_stops_children_without_leaving_them_alive(self):
        supervisor = self.make_supervisor(once=False)

        def request_shutdown(_):
            supervisor.request_shutdown(signal.SIGTERM)

        supervisor._sleep = request_shutdown

        result = supervisor.run()

        self.assertEqual(result, 143)
        self.assertTrue(self.events[0].is_set())
        self.assertTrue(all(process.terminated for process in self.process_factory.processes))
        self.assertTrue(all(not process.is_alive() for process in self.process_factory.processes))

    def test_unexpected_child_exit_stops_other_child_and_returns_failure(self):
        supervisor = self.make_supervisor(once=False)

        def fail_poller_after_start(_):
            process = self.process_factory.processes[0]
            process._alive = False
            process.exitcode = 2

        supervisor._sleep = fail_poller_after_start

        result = supervisor.run()

        self.assertEqual(result, 1)
        self.assertTrue(self.process_factory.processes[1].terminated)

    @patch("subot_core.supervisor.signal.signal")
    def test_signal_handlers_are_installed_and_restored(self, signal_call):
        signal_call.side_effect = ["old-int", "old-term", None, None]
        supervisor = Supervisor(
            self.poller,
            self.worker,
            process_factory=self.process_factory,
            event_factory=self.event_factory,
            wait_interval=0,
            shutdown_timeout=0,
        )
        supervisor.request_shutdown(signal.SIGINT)

        result = supervisor.run()

        self.assertEqual(result, 130)
        self.assertEqual(signal_call.call_count, 4)
        self.assertEqual(signal_call.call_args_list[0].args[0], signal.SIGINT)
        self.assertEqual(signal_call.call_args_list[1].args[0], signal.SIGTERM)
        self.assertEqual(signal_call.call_args_list[2].args, (signal.SIGINT, "old-int"))
        self.assertEqual(signal_call.call_args_list[3].args, (signal.SIGTERM, "old-term"))


if __name__ == "__main__":
    unittest.main()
