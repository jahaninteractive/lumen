from __future__ import annotations

import fcntl
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from .catalog import Catalog
from .domain import MAX_JOB_ERROR_LENGTH


Work = Callable[["JobContext"], None]


class JobContext:
    def __init__(self, runner: "LocalJobRunner", job_id: int, cancelled: threading.Event):
        self._runner = runner
        self._job_id = job_id
        self._cancelled = cancelled

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def wait_for_cancel(self, timeout: float | None = None) -> bool:
        return self._cancelled.wait(timeout)

    def update_progress(self, progress: float) -> None:
        if self.cancelled:
            return
        with Catalog(self._runner.database_path) as catalog:
            catalog.update_job(self._job_id, progress=progress)

    def register_process(self, process: Any) -> None:
        self._runner._register_process(self._job_id, process)

    def clear_process(self, process: Any) -> None:
        self._runner._clear_process(self._job_id, process)


class LocalJobRunner:
    def __init__(self, database_path: Path):
        if not isinstance(database_path, Path):
            raise ValueError("catalog database path must be a Path")
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        owner_file = database_path.with_name(f"{database_path.name}.runner.lock").open("a+b")
        try:
            fcntl.flock(owner_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            owner_file.close()
            raise RuntimeError("catalog already has a job runner") from error
        self._owner_file = owner_file
        self._condition = threading.Condition()
        self._pending: tuple[int, Work, threading.Event] | None = None
        self._active_job_id: int | None = None
        self._active_cancel: threading.Event | None = None
        self._process: Any = None
        self._closed = False
        self._done: dict[int, threading.Event] = {}
        try:
            with Catalog(database_path) as catalog:
                catalog.recover_running_jobs()
            self._thread = threading.Thread(
                target=self._run, name="lumen-local-job-runner", daemon=True
            )
            self._thread.start()
        except BaseException:
            self._release_owner()
            raise

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def submit(self, job_id: int, work: Work) -> None:
        if not callable(work):
            raise ValueError("job work must be callable")
        with Catalog(self.database_path) as catalog:
            job = catalog.get_job(job_id)
        if job.status != "queued":
            raise ValueError(f"job {job_id} is not queued")
        with self._condition:
            if self._closed:
                raise RuntimeError("job runner is shut down")
            if self._pending is not None or self._active_job_id is not None:
                raise RuntimeError("another heavy job is already active")
            cancelled = threading.Event()
            self._done[job_id] = threading.Event()
            self._pending = (job_id, work, cancelled)
            self._condition.notify()

    def cancel(self, job_id: int) -> bool:
        process = None
        with self._condition:
            if self._active_job_id == job_id and self._active_cancel is not None:
                self._active_cancel.set()
                process = self._process
            elif self._pending is not None and self._pending[0] == job_id:
                self._pending[2].set()
        stopped = process is None or self._stop_process(process)
        if stopped and process is not None:
            with self._condition:
                done = self._done.get(job_id)
                if self._process is process and done is not None and done.is_set():
                    self._process = None
                    self._active_job_id = None
                    self._active_cancel = None
        with Catalog(self.database_path) as catalog:
            job = catalog.get_job(job_id)
            if job.status in {"queued", "running"}:
                try:
                    catalog.update_job(job_id, status="cancelled")
                except ValueError:
                    if catalog.get_job(job_id).status != "cancelled":
                        raise
                result = True
            else:
                result = job.status == "cancelled"
        if not stopped:
            raise RuntimeError("owned process did not stop within the cleanup timeout")
        return result

    def wait(self, job_id: int, timeout: float | None = None) -> bool:
        with self._condition:
            event = self._done.get(job_id)
        return False if event is None else event.wait(timeout)

    def shutdown(self) -> None:
        job_ids: set[int] = set()
        with self._condition:
            if not self._closed:
                self._closed = True
                if self._pending is not None:
                    self._pending[2].set()
                    job_ids.add(self._pending[0])
            if self._active_job_id is not None and self._active_cancel is not None:
                self._active_cancel.set()
                job_ids.add(self._active_job_id)
            self._condition.notify_all()
        cleanup_failed = False
        for job_id in job_ids:
            try:
                self.cancel(job_id)
            except RuntimeError:
                cleanup_failed = True
            except ValueError:
                pass
        if threading.current_thread() is self._thread:
            raise RuntimeError("worker-thread shutdown requires an external retry")
        self._thread.join(2.0)
        if self._thread.is_alive():
            raise RuntimeError("job worker did not stop")
        with self._condition:
            cleanup_failed = cleanup_failed or self._process is not None
        if cleanup_failed:
            raise RuntimeError("owned process did not stop within the cleanup timeout")
        self._release_owner()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._pending is None:
                    return
                job_id, work, cancelled = self._pending
                self._pending = None
                self._active_job_id = job_id
                self._active_cancel = cancelled
            fatal: BaseException | None = None
            try:
                with Catalog(self.database_path) as catalog:
                    job = catalog.get_job(job_id)
                    if cancelled.is_set() or job.status == "cancelled":
                        if job.status == "queued":
                            catalog.update_job(job_id, status="cancelled")
                        continue
                    catalog.update_job(job_id, status="running")
                context = JobContext(self, job_id, cancelled)
                work(context)
                with self._condition:
                    has_process = self._process is not None
                    was_cancelled = cancelled.is_set()
                    with Catalog(self.database_path) as catalog:
                        current = catalog.get_job(job_id)
                        if was_cancelled or current.status == "cancelled":
                            if current.status == "running":
                                catalog.update_job(job_id, status="cancelled")
                        elif has_process:
                            catalog.update_job(
                                job_id, status="failed",
                                error="Work returned with a registered process still active",
                            )
                        else:
                            catalog.update_job(job_id, status="succeeded", progress=1.0)
            except Exception as error:
                self._finish_exception(job_id, cancelled, error)
            except BaseException as error:
                fatal = error
                self._finish_fatal(job_id, error)
            finally:
                with self._condition:
                    owned_process = self._process
                stopped = owned_process is None or self._stop_process(owned_process)
                with self._condition:
                    if stopped:
                        self._process = None
                        self._active_job_id = None
                        self._active_cancel = None
                    done = self._done.get(job_id)
                    if done is not None:
                        done.set()
                    if fatal is not None:
                        self._closed = True
                    self._condition.notify_all()
            if fatal is not None:
                raise fatal

    def _finish_exception(self, job_id: int, cancelled: threading.Event, error: Exception) -> None:
        diagnostic = f"{type(error).__name__}: {error}"[:MAX_JOB_ERROR_LENGTH]
        with Catalog(self.database_path) as catalog:
            current = catalog.get_job(job_id)
            if current.status == "cancelled":
                return
            if current.status == "running":
                catalog.update_job(
                    job_id,
                    status="cancelled" if cancelled.is_set() else "failed",
                    error=None if cancelled.is_set() else diagnostic,
                )

    def _finish_fatal(self, job_id: int, error: BaseException) -> None:
        diagnostic = f"{type(error).__name__}: {error}"[:MAX_JOB_ERROR_LENGTH]
        with Catalog(self.database_path) as catalog:
            current = catalog.get_job(job_id)
            if current.status == "running":
                catalog.update_job(job_id, status="interrupted", error=diagnostic)

    def _register_process(self, job_id: int, process: Any) -> None:
        for name in ("poll", "terminate", "wait", "kill"):
            if not callable(getattr(process, name, None)):
                raise ValueError("owned process is not Popen-compatible")
        stop = False
        with self._condition:
            if self._active_job_id != job_id:
                raise RuntimeError("job is not active")
            if self._process is not None:
                raise RuntimeError("a process is already registered")
            self._process = process
            stop = self._active_cancel is not None and self._active_cancel.is_set()
        if stop:
            if not self._stop_process(process):
                raise RuntimeError("owned process did not stop within the cleanup timeout")

    def _clear_process(self, job_id: int, process: Any) -> None:
        with self._condition:
            if self._active_job_id != job_id or self._process is not process:
                raise RuntimeError("registered process does not match")
            if process.poll() is None:
                raise RuntimeError("registered process is still running")
            self._process = None

    @staticmethod
    def _stop_process(process: Any) -> bool:
        try:
            if process.poll() is not None:
                return True
            process.terminate()
            try:
                process.wait(timeout=0.25)
                return True
            except (subprocess.TimeoutExpired, TimeoutError):
                pass
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=0.25)
                    return True
                except (subprocess.TimeoutExpired, TimeoutError):
                    return process.poll() is not None
            return True
        except (OSError, ProcessLookupError):
            return process.poll() is not None

    def _release_owner(self) -> None:
        owner_file = self._owner_file
        if owner_file is None:
            return
        fcntl.flock(owner_file, fcntl.LOCK_UN)
        owner_file.close()
        self._owner_file = None
