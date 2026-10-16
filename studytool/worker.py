"""Background worker. OWNER: agent A.

Run with `python -m studytool.worker`. Takes the single worker_lease (exits if another
live worker holds it), heartbeats, recovers stale 'running' jobs/steps on start, then
claims queued jobs atomically and runs them through studytool.pipeline.
"""
import logging
import os
import signal
import socket
import threading
import time
import uuid

from studytool import config, db, jobs, pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=None)
log = logging.getLogger("studytool.worker")

POLL_SECONDS = 1.0

_stop = threading.Event()
_lease_lost = threading.Event()


def _worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _holder_is_dead(holder_id: str) -> bool:
    """True if the lease holder ran on this computer and its process no longer exists.

    A worker killed without cleanup (crash, End Task) leaves a fresh-looking lease for up to
    WORKER_STALE_SECONDS; without this check a relaunch in that window exits at once.
    ponytail: a reused PID looks alive, which only falls back to waiting for the stale timeout.
    """
    try:
        host, pid, _ = holder_id.rsplit("-", 2)
        pid = int(pid)
    except ValueError:
        return False
    if host != socket.gethostname() or pid == os.getpid():
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return True
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        return code.value != 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)  # POSIX: signal 0 only checks the process exists
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _acquire_lease(conn, worker_id: str) -> bool:
    """Take worker_lease id=1 if absent or stale. One short BEGIN IMMEDIATE transaction."""
    with db.tx(conn):
        row = conn.execute("SELECT worker_id, heartbeat FROM worker_lease WHERE id = 1").fetchone()
        if row is not None:
            age = conn.execute(
                "SELECT (julianday('now') - julianday(?)) * 86400.0 AS age", (row["heartbeat"],)
            ).fetchone()["age"]
            if age is not None and age < config.WORKER_STALE_SECONDS and not _holder_is_dead(row["worker_id"]):
                return False  # someone else is alive
            conn.execute(
                "UPDATE worker_lease SET worker_id = ?, heartbeat = datetime('now') WHERE id = 1",
                (worker_id,),
            )
        else:
            conn.execute(
                "INSERT INTO worker_lease (id, worker_id, heartbeat) VALUES (1, ?, datetime('now'))",
                (worker_id,),
            )
    return True


def _recover_stale(conn, worker_id: str) -> None:
    """On start: running jobs/steps left behind by a dead worker go back to queued/pending."""
    with db.tx(conn):
        conn.execute(
            "UPDATE jobs SET status = 'queued', worker_id = NULL, heartbeat = NULL, "
            "updated_at = datetime('now') "
            "WHERE status = 'running' AND (worker_id IS NULL OR worker_id != ?)",
            (worker_id,),
        )
        conn.execute(
            "UPDATE job_steps SET status = 'pending', updated_at = datetime('now') "
            "WHERE status = 'running' AND job_id IN "
            "(SELECT id FROM jobs WHERE status IN ('queued','running'))"
        )


def _heartbeat_loop(worker_id: str, current_job_id: "list[int | None]") -> None:
    """Runs on its own connection. Updates the lease and the running job's heartbeat.
    Stops the worker if the lease has been taken by someone else."""
    conn = db.connect()
    try:
        while not _stop.is_set():
            row = conn.execute("SELECT worker_id FROM worker_lease WHERE id = 1").fetchone()
            if row is None or row["worker_id"] != worker_id:
                log.error("worker_lease taken by another worker; stopping")
                _lease_lost.set()
                _stop.set()
                return
            with db.tx(conn):
                conn.execute(
                    "UPDATE worker_lease SET heartbeat = datetime('now') WHERE id = 1 AND worker_id = ?",
                    (worker_id,),
                )
                job_id = current_job_id[0]
                if job_id is not None:
                    conn.execute(
                        "UPDATE jobs SET heartbeat = datetime('now') WHERE id = ? AND status = 'running'",
                        (job_id,),
                    )
            _stop.wait(config.WORKER_HEARTBEAT_SECONDS)
    finally:
        conn.close()


def _claim_job(conn) -> int | None:
    """Atomically claim the oldest queued job."""
    with db.tx(conn):
        row = conn.execute(
            "SELECT id FROM jobs WHERE status = 'queued' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        job_id = row["id"]
        conn.execute(
            "UPDATE jobs SET status = 'running', worker_id = ?, heartbeat = datetime('now'), "
            "updated_at = datetime('now') WHERE id = ? AND status = 'queued'",
            (_current_worker_id, job_id),
        )
    return job_id


_current_worker_id = ""  # set in main(); used by _claim_job


def _release_lease(conn, worker_id: str) -> None:
    conn.execute("DELETE FROM worker_lease WHERE id = 1 AND worker_id = ?", (worker_id,))


def _run_job(conn, job_id: int, worker_id: str) -> None:
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    kind = job["kind"]
    try:
        if kind == "regenerate":
            result = pipeline.run_regenerate(conn, job_id, worker_id)
        else:
            result = pipeline.run_job(conn, job_id, worker_id)
    except Exception as exc:  # unexpected bug: fail the job, keep the worker alive
        log.exception("job %s crashed", job_id)
        with db.tx(conn):
            conn.execute(
                "UPDATE jobs SET status = 'failed', error = ?, updated_at = datetime('now') WHERE id = ?",
                (str(exc), job_id),
            )
        return

    status = result if isinstance(result, str) else "completed"
    if status not in ("completed", "failed", "cancelled"):
        status = "completed"
    error = None
    if isinstance(result, dict):
        status = result.get("status", "completed")
        error = result.get("error")
    with db.tx(conn):
        conn.execute(
            "UPDATE jobs SET status = ?, error = ?, updated_at = datetime('now') WHERE id = ?",
            (status, error, job_id),
        )


def main() -> None:
    global _current_worker_id
    worker_id = _worker_id()
    _current_worker_id = worker_id

    conn = db.connect()
    db.migrate(conn)

    if not _acquire_lease(conn, worker_id):
        print("Another worker is already running (lease held and recent heartbeat). Exiting.")
        conn.close()
        return

    log.info("worker %s acquired lease", worker_id)
    _recover_stale(conn, worker_id)

    current_job_id: list[int | None] = [None]
    hb_thread = threading.Thread(
        target=_heartbeat_loop, args=(worker_id, current_job_id), daemon=True
    )
    hb_thread.start()

    def _handle_signal(signum, frame):
        log.info("signal %s received, shutting down", signum)
        _stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (AttributeError, ValueError):
        pass  # SIGTERM not available on this platform

    try:
        while not _stop.is_set():
            if _lease_lost.is_set():
                break
            job_id = _claim_job(conn)
            if job_id is None:
                _stop.wait(POLL_SECONDS)
                continue
            current_job_id[0] = job_id
            log.info("running job %s", job_id)
            _run_job(conn, job_id, worker_id)
            log.info("finished job %s", job_id)
            current_job_id[0] = None
    finally:
        _stop.set()
        hb_thread.join(timeout=2)
        if not _lease_lost.is_set():
            _release_lease(conn, worker_id)
            log.info("released lease")
        conn.close()


if __name__ == "__main__":
    main()
