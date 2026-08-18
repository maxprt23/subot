import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from subot_core import state
from subot_core.models import JobStatus, SeenState


class SeenStateTests(unittest.TestCase):
    def test_missing_state_starts_with_no_initialized_searches(self):
        with tempfile.TemporaryDirectory() as directory:
            seen_state = state.load_seen(os.path.join(directory, "seen.json"))

        self.assertEqual(seen_state, SeenState())

    def test_non_object_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "seen.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(["existing"], f)

            with self.assertRaisesRegex(ValueError, "unsupported format"):
                state.load_seen(path)

    def test_structured_state_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "seen.json")
            expected = SeenState({"2", "1"}, {"search-key"})

            state.save_seen(path, expected)

            self.assertEqual(state.load_seen(path), expected)

    def test_failed_write_preserves_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "seen.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(["existing"], f)

            with patch("subot_core.state.json.dump", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    state.save_seen(
                        path, SeenState({"replacement"}, set())
                    )

            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), ["existing"])
            self.assertEqual(os.listdir(directory), ["seen.json"])


class SQLiteStateTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "id": "listing-1",
            "subject": "A listing",
            "price": 125,
            "url": "https://example.test/listing-1.htm",
        }

    def open_store(self, directory):
        return state.StateStore(os.path.join(directory, "state.sqlite3"))

    def state_path(self, directory):
        return os.path.join(directory, "state.sqlite3")

    def test_new_store_has_no_initialized_searches_or_work(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.open_store(directory) as store:
                self.assertFalse(store.is_search_initialized("search-1"))
                self.assertIsNone(store.claim_next())
                self.assertEqual(store.pending_count(), 0)

    def test_baseline_initialization_is_atomic_and_global(self):
        payload_2 = dict(self.payload, id="listing-2")
        with tempfile.TemporaryDirectory() as directory:
            with self.open_store(directory) as store:
                self.assertEqual(
                    store.initialize_search(
                        "search-1", [self.payload, payload_2]
                    ),
                    2,
                )
                self.assertTrue(store.is_search_initialized("search-1"))
                self.assertEqual(store.pending_count(), 0)

                # A second baseline does not overwrite global listing state.
                self.assertIsNone(
                    store.initialize_search("search-1", [self.payload])
                )

            with self.open_store(directory) as reopened:
                self.assertTrue(reopened.is_listing_terminal("listing-1"))
                self.assertEqual(reopened.get_listing("listing-1").status,
                                 JobStatus.BASELINE)

    def test_enqueue_is_durable_and_duplicate_safe_across_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.open_store(directory) as first:
                self.assertTrue(first.enqueue_listing(self.payload))
                self.assertFalse(first.enqueue_listing(self.payload))

            with self.open_store(directory) as second:
                job = second.claim_next()
                self.assertEqual(job.listing_id, "listing-1")
                self.assertEqual(job.payload, self.payload)
                self.assertEqual(job.status, JobStatus.CLAIMED)
                self.assertEqual(job.attempts, 1)

    def test_only_one_connection_can_claim_a_queued_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.open_store(directory)
            second = self.open_store(directory)
            try:
                first.enqueue_listing(self.payload)
                claimed = [first.claim_next(), second.claim_next()]
                self.assertEqual(
                    [job.listing_id for job in claimed if job], ["listing-1"]
                )
            finally:
                first.close()
                second.close()

    def test_terminal_transitions_and_drain_count(self):
        payload_2 = dict(self.payload, id="listing-2")
        with tempfile.TemporaryDirectory() as directory:
            with self.open_store(directory) as store:
                store.enqueue_listing(self.payload)
                store.enqueue_listing(payload_2)
                self.assertEqual(store.pending_count(), 2)
                store.claim_next()
                store.mark_notified("listing-1")
                self.assertEqual(store.get_listing("listing-1").status,
                                 JobStatus.NOTIFIED)
                self.assertEqual(store.pending_count(), 1)
                store.claim_next()
                store.mark_rejected("listing-2")
                self.assertEqual(store.get_listing("listing-2").status,
                                 JobStatus.REJECTED)
                self.assertEqual(store.pending_count(), 0)
                self.assertTrue(store.all_terminal())
                self.assertTrue(store.once_successful())

    def test_once_successful_is_false_when_a_job_permanently_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.open_store(directory) as store:
                store.enqueue_listing(self.payload)
                store.claim_next()
                self.assertEqual(
                    store.fail_or_retry(
                        self.payload["id"], "permanent failure", max_retries=0
                    ),
                    JobStatus.FAILED,
                )

                # FAILED is terminal for draining, but not successful for
                # once-mode.
                self.assertTrue(store.all_terminal())
                self.assertFalse(store.once_successful())

    def test_once_successful_ignores_failures_before_the_run_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.open_store(directory) as store:
                store.enqueue_listing(self.payload)
                store.claim_next()
                store.fail_or_retry(
                    self.payload["id"], "historical failure", max_retries=0
                )

                boundary = store.failure_boundary()
                self.assertEqual(boundary, 1)
                self.assertTrue(store.once_successful(boundary))

    def test_once_successful_rejects_failures_created_after_the_run_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.open_store(directory) as store:
                boundary = store.failure_boundary()
                store.enqueue_listing(self.payload)
                store.claim_next()
                store.fail_or_retry(
                    self.payload["id"], "new failure", max_retries=0
                )

                self.assertFalse(store.once_successful(boundary))

    def test_once_successful_is_false_while_work_is_queued_or_claimed(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.open_store(directory) as store:
                store.enqueue_listing(self.payload)
                self.assertFalse(store.once_successful())
                store.claim_next()
                self.assertFalse(store.once_successful())

    def test_read_only_store_queries_without_modifying_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.state_path(directory)
            with self.open_store(directory) as writable:
                writable.initialize_search("search-1", [self.payload])

            before = (os.stat(path).st_size, os.stat(path).st_mtime_ns)
            with state.StateStore(path, read_only=True) as readonly:
                self.assertTrue(readonly.read_only)
                self.assertTrue(readonly.is_search_initialized("search-1"))
                self.assertEqual(
                    readonly.get_listing("listing-1").status,
                    JobStatus.BASELINE,
                )
                with self.assertRaisesRegex(
                    sqlite3.OperationalError, "read-only"
                ):
                    readonly.enqueue_listing(
                        dict(self.payload, id="listing-2")
                    )

            after = (os.stat(path).st_size, os.stat(path).st_mtime_ns)
            self.assertEqual(after, before)

    def test_listing_known_query_is_read_only_and_validates_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.state_path(directory)
            with self.open_store(directory) as writable:
                writable.initialize_search("search-1", [self.payload])

            before = (os.stat(path).st_size, os.stat(path).st_mtime_ns)
            with state.StateStore(path, read_only=True) as readonly:
                self.assertTrue(readonly.is_listing_known("listing-1"))
                self.assertFalse(readonly.is_listing_known("missing"))
                with self.assertRaisesRegex(
                    ValueError, "listing ID must be a non-empty string"
                ):
                    readonly.is_listing_known("")
                with self.assertRaisesRegex(
                    ValueError, "listing ID must be a non-empty string"
                ):
                    readonly.is_listing_known(None)

            after = (os.stat(path).st_size, os.stat(path).st_mtime_ns)
            self.assertEqual(after, before)

    def test_read_only_store_does_not_create_a_missing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.state_path(directory)
            with self.assertRaises(sqlite3.OperationalError):
                state.StateStore(path, read_only=True)
            self.assertFalse(os.path.exists(path))

    def test_failed_claim_is_requeued_until_three_retries_then_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.open_store(directory) as store:
                store.enqueue_listing(self.payload)
                statuses = []
                # Three retries after the initial attempt means four attempts.
                for attempt in range(1, 5):
                    job = store.claim_next()
                    self.assertEqual(job.attempts, attempt)
                    statuses.append(
                        store.fail_or_retry(
                            job.listing_id,
                            f"failure-{attempt}",
                            max_retries=3,
                        )
                    )

                self.assertEqual(
                    statuses,
                    [JobStatus.QUEUED, JobStatus.QUEUED,
                     JobStatus.QUEUED, JobStatus.FAILED],
                )
                job = store.get_listing("listing-1")
                self.assertEqual(job.status, JobStatus.FAILED)
                self.assertEqual(job.attempts, 4)
                self.assertEqual(job.last_error, "failure-4")
                self.assertEqual(store.pending_count(), 0)

    def test_recover_claimed_jobs_requeues_work_without_resetting_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.open_store(directory) as store:
                store.enqueue_listing(self.payload)
                claimed = store.claim_next()
                self.assertEqual(claimed.status, JobStatus.CLAIMED)
                self.assertEqual(store.recover_claimed_jobs(), 1)
                self.assertEqual(store.pending_count(), 1)

            with self.open_store(directory) as reopened:
                recovered = reopened.claim_next()
                self.assertEqual(recovered.status, JobStatus.CLAIMED)
                self.assertEqual(recovered.attempts, 2)

    def test_invalid_job_transition_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.open_store(directory) as store:
                store.enqueue_listing(self.payload)
                with self.assertRaisesRegex(ValueError, "must be claimed"):
                    store.mark_notified("listing-1")
