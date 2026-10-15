"""Background worker. OWNER: agent A.

Run with `python -m studytool.worker`. Takes the single worker_lease (exits if another
live worker holds it), heartbeats, recovers stale 'running' jobs/steps on start, then
claims queued jobs atomically and runs them through studytool.pipeline.
"""


def main() -> None:
    raise NotImplementedError


if __name__ == "__main__":
    main()
