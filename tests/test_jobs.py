import tempfile
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from lumen.catalog import Catalog
from lumen.jobs import LocalJobRunner


class FakeProcess:
    def __init__(self, *, exit_on_terminate=True, exit_on_kill=True):
        self.exit_on_terminate = exit_on_terminate
        self.exit_on_kill = exit_on_kill
        self.terminated = 0
        self.killed = 0
        self.waited = 0
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        if self.exit_on_terminate:
            self.returncode = -15

    def wait(self, timeout=None):
        self.waited += 1
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def kill(self):
        self.killed += 1
        if self.exit_on_kill:
            self.returncode = -9


class LocalJobRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "catalog.sqlite3"
        self.catalog = Catalog(self.db_path)
        self.runners = []

    def tearDown(self):
        for runner in reversed(self.runners):
            runner.shutdown()
        self.catalog.close()
        self.tmp.cleanup()

    def job(self, name="job"):
        return self.catalog.create_job(
            kind="astro", manifest_path=f"/{name}/manifest.json", log_path=f"/{name}/run.log"
        )

    def runner(self):
        runner = LocalJobRunner(self.db_path)
        self.runners.append(runner)
        return runner

    def wait_status(self, job_id, status):
        self.assertTrue(self.runner_under_test.wait(job_id, timeout=2.0))
        job = self.catalog.get_job(job_id)
        self.assertEqual(status, job.status)
        return job

    def test_success_records_progress_and_log_path_without_leaking_worker(self):
        job = self.job("success")
        observed = {}
        self.runner_under_test = self.runner()

        def work(context):
            context.update_progress(0.35)
            observed["cancelled"] = context.cancelled

        self.runner_under_test.submit(job.id, work)
        saved = self.wait_status(job.id, "succeeded")

        self.assertEqual(1.0, saved.progress)
        self.assertEqual("/success/run.log", saved.log_path)
        self.assertFalse(observed["cancelled"])
        self.runner_under_test.shutdown()
        self.assertFalse(self.runner_under_test.is_alive)

    def test_recorded_result_is_never_undone_by_a_late_cancel(self):
        job = self.job("result")
        self.runner_under_test = self.runner()
        outcome = {}

        def work(context):
            with Catalog(self.db_path) as catalog:
                catalog.update_job(job.id, result_path="/result/out.tif")
            outcome["cancel"] = self.runner_under_test.cancel(job.id)

        self.runner_under_test.submit(job.id, work)
        saved = self.wait_status(job.id, "succeeded")
        self.assertFalse(outcome["cancel"])
        self.assertEqual("/result/out.tif", saved.result_path)

    def test_rejects_second_submission_while_one_job_is_active(self):
        entered, release = threading.Event(), threading.Event()
        first, second = self.job("first"), self.job("second")
        self.runner_under_test = self.runner()

        def work(context):
            entered.set()
            release.wait(2)

        self.runner_under_test.submit(first.id, work)
        self.assertTrue(entered.wait(2))
        with self.assertRaises(RuntimeError):
            self.runner_under_test.submit(second.id, work)
        release.set()
        self.wait_status(first.id, "succeeded")
        self.assertEqual("queued", self.catalog.get_job(second.id).status)

    def test_submit_accepts_only_existing_queued_job_and_stops_after_shutdown(self):
        cancelled = self.job("cancelled")
        self.catalog.update_job(cancelled.id, status="cancelled")
        self.runner_under_test = self.runner()

        for job_id in (99999, cancelled.id):
            with self.subTest(job_id=job_id), self.assertRaises(ValueError):
                self.runner_under_test.submit(job_id, lambda context: None)

        self.runner_under_test.shutdown()
        with self.assertRaises(RuntimeError):
            self.runner_under_test.submit(self.job("late").id, lambda context: None)

    def test_work_exception_becomes_bounded_failed_diagnostic(self):
        job = self.job("failure")
        self.runner_under_test = self.runner()

        def fail(context):
            raise RuntimeError("broken " + "x" * 2000)

        self.runner_under_test.submit(job.id, fail)
        saved = self.wait_status(job.id, "failed")

        self.assertLessEqual(len(saved.error), 1000)
        self.assertIn("broken", saved.error)

    def test_queued_cancellation_prevents_work_from_running(self):
        first_entered, release = threading.Event(), threading.Event()
        first, second = self.job("active"), self.job("queued")
        self.runner_under_test = self.runner()
        self.runner_under_test.submit(first.id, lambda context: (first_entered.set(), release.wait(2)))
        self.assertTrue(first_entered.wait(2))

        self.assertTrue(self.runner_under_test.cancel(second.id))
        self.assertEqual("cancelled", self.catalog.get_job(second.id).status)
        self.assertTrue(self.runner_under_test.cancel(second.id))
        release.set()
        self.wait_status(first.id, "succeeded")

    def test_running_cancellation_terminates_only_owned_process_then_kills_on_timeout(self):
        registered, release = threading.Event(), threading.Event()
        owned, unrelated = FakeProcess(exit_on_terminate=False), FakeProcess()
        job = self.job("cancel-process")
        self.runner_under_test = self.runner()

        def work(context):
            context.register_process(owned)
            registered.set()
            release.wait(2)
            context.clear_process(owned)

        self.runner_under_test.submit(job.id, work)
        self.assertTrue(registered.wait(2))
        self.assertTrue(self.runner_under_test.cancel(job.id))
        release.set()
        saved = self.wait_status(job.id, "cancelled")

        self.assertEqual((1, 1), (owned.terminated, owned.killed))
        self.assertEqual((0, 0), (unrelated.terminated, unrelated.killed))
        self.assertLess(saved.progress, 1.0)

    def test_cancellation_wins_race_with_normal_return(self):
        returning, release = threading.Event(), threading.Event()
        job = self.job("race")
        self.runner_under_test = self.runner()

        def work(context):
            returning.set()
            release.wait(2)

        self.runner_under_test.submit(job.id, work)
        self.assertTrue(returning.wait(2))
        self.assertTrue(self.runner_under_test.cancel(job.id))
        release.set()

        self.wait_status(job.id, "cancelled")

    def test_context_rejects_two_owned_processes_and_uncleared_process_blocks_success(self):
        process = FakeProcess()
        job = self.job("ownership")
        self.runner_under_test = self.runner()

        def work(context):
            context.register_process(process)
            with self.assertRaises(RuntimeError):
                context.register_process(FakeProcess())

        self.runner_under_test.submit(job.id, work)
        saved = self.wait_status(job.id, "failed")
        self.assertIn("registered process", saved.error)
        self.assertEqual(1, process.terminated)

    def test_live_real_process_cannot_be_cleared_or_escape_as_success(self):
        child = None
        job = self.job("early-clear")
        self.runner_under_test = self.runner()

        def work(context):
            nonlocal child
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            context.register_process(child)
            context.clear_process(child)

        try:
            self.runner_under_test.submit(job.id, work)
            saved = self.wait_status(job.id, "failed")

            self.assertIn("still running", saved.error)
            self.assertIsNotNone(child.poll())
            self.runner_under_test.shutdown()
            self.assertIsNotNone(child.poll())
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=2)

    def test_unreaped_process_is_retained_and_shutdown_can_retry(self):
        stubborn = FakeProcess(exit_on_terminate=False, exit_on_kill=False)
        job = self.job("stubborn")
        self.runner_under_test = self.runner()

        def work(context):
            context.register_process(stubborn)

        try:
            self.runner_under_test.submit(job.id, work)
            saved = self.wait_status(job.id, "failed")
            self.assertIn("registered process", saved.error)

            with self.assertRaisesRegex(RuntimeError, "owned process"):
                self.runner_under_test.shutdown()
            self.assertIsNone(stubborn.poll())
        finally:
            stubborn.returncode = -9
            self.runner_under_test.shutdown()

        self.assertFalse(self.runner_under_test.is_alive)

    def test_second_runner_cannot_recover_or_overlap_the_catalog_owner(self):
        entered, release = threading.Event(), threading.Event()
        first_job, second_job = self.job("owner"), self.job("other")
        self.runner_under_test = self.runner()
        duplicate = None

        def work(context):
            entered.set()
            release.wait(2)

        self.runner_under_test.submit(first_job.id, work)
        self.assertTrue(entered.wait(2))
        try:
            with self.assertRaisesRegex(RuntimeError, "already has a job runner"):
                duplicate = LocalJobRunner(self.db_path)
        finally:
            if duplicate is not None:
                duplicate.shutdown()
            release.set()

        self.wait_status(first_job.id, "succeeded")
        self.assertEqual("queued", self.catalog.get_job(second_job.id).status)
        self.runner_under_test.shutdown()

        replacement = self.runner()
        replacement.shutdown()

    def test_worker_thread_shutdown_cannot_unlock_the_live_runner(self):
        attempted, release = threading.Event(), threading.Event()
        errors = []
        job = self.job("self-shutdown")
        self.runner_under_test = self.runner()
        duplicate = None

        def work(context):
            try:
                self.runner_under_test.shutdown()
            except RuntimeError as error:
                errors.append(str(error))
            attempted.set()
            release.wait(2)

        self.runner_under_test.submit(job.id, work)
        self.assertTrue(attempted.wait(2))
        try:
            self.assertEqual(
                ["worker-thread shutdown requires an external retry"], errors
            )
            self.assertTrue(self.runner_under_test.is_alive)
            with self.assertRaisesRegex(RuntimeError, "already has a job runner"):
                duplicate = LocalJobRunner(self.db_path)
        finally:
            if duplicate is not None:
                duplicate.shutdown()
            release.set()

        self.wait_status(job.id, "cancelled")
        self.runner_under_test.shutdown()
        replacement = self.runner()
        replacement.shutdown()

    def test_constructor_purges_placeholder_left_by_a_crashed_preparation(self):
        kept = self.job("kept")
        self.catalog.connection.execute(
            "INSERT INTO jobs (kind, status, progress, manifest_path, log_path, created_at, updated_at) "
            "VALUES ('astro', 'queued', 0, 'pending', 'pending', '2026-10-22T00:00:00+00:00', '2026-10-22T00:00:00+00:00')"
        )
        self.catalog.connection.commit()
        self.runner_under_test = self.runner()
        self.assertEqual([kept.id], [job.id for job in self.catalog.list_jobs()])

    def test_constructor_recovers_orphaned_running_job(self):
        job = self.job("orphan")
        self.catalog.update_job(job.id, status="running", progress=0.3)

        self.runner_under_test = self.runner()

        self.assertEqual("interrupted", self.catalog.get_job(job.id).status)
        self.assertFalse(self.runner_under_test.wait(job.id, timeout=0.01))

    def test_shutdown_cancels_cooperative_work_and_is_idempotent(self):
        entered = threading.Event()
        job = self.job("shutdown")
        self.runner_under_test = self.runner()

        def work(context):
            entered.set()
            context.wait_for_cancel(2)

        self.runner_under_test.submit(job.id, work)
        self.assertTrue(entered.wait(2))
        self.runner_under_test.shutdown()
        self.runner_under_test.shutdown()

        self.assertFalse(self.runner_under_test.is_alive)
        self.assertEqual("cancelled", self.catalog.get_job(job.id).status)

    def test_system_exit_is_not_converted_to_an_ordinary_failure(self):
        job = self.job("fatal")
        raised = threading.Event()
        original_hook = threading.excepthook
        threading.excepthook = lambda args: raised.set()
        try:
            self.runner_under_test = self.runner()
            self.runner_under_test.submit(job.id, lambda context: (_ for _ in ()).throw(SystemExit("stop")))
            self.assertTrue(raised.wait(2))
            self.assertTrue(self.runner_under_test.wait(job.id, timeout=2))
        finally:
            threading.excepthook = original_hook

        saved = self.catalog.get_job(job.id)
        self.assertEqual("interrupted", saved.status)
        self.assertIn("SystemExit", saved.error)
        self.assertFalse(self.runner_under_test.is_alive)

    def test_shutdown_reaps_the_real_child_process_owned_by_work(self):
        registered = threading.Event()
        job = self.job("real-process")
        self.runner_under_test = self.runner()
        child = None

        def work(context):
            nonlocal child
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            context.register_process(child)
            registered.set()
            context.wait_for_cancel(2)

        try:
            self.runner_under_test.submit(job.id, work)
            self.assertTrue(registered.wait(2))
            self.runner_under_test.shutdown()

            self.assertIsNotNone(child.poll())
            self.assertFalse(self.runner_under_test.is_alive)
            self.assertEqual("cancelled", self.catalog.get_job(job.id).status)
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
