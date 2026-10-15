"""Local launcher. Run with `uv run python run.py`.

Applies pending SQLite migrations once, then starts exactly one background
worker (`python -m studytool.worker`) and the Streamlit app as subprocesses.
If either process exits, the other is stopped too. Ctrl+C stops both.
"""
import os
import subprocess
import sys
import time

from studytool import config, db


def main() -> None:
    conn = db.connect()
    db.migrate(conn)
    conn.close()

    if not os.environ.get("GEMINI_API_KEY"):
        print("Warning: GEMINI_API_KEY is not set. AI calls will fail until it is configured.")

    worker_proc = subprocess.Popen([sys.executable, "-m", "studytool.worker"])
    streamlit_proc = subprocess.Popen([
        sys.executable, "-m", "streamlit", "run", "app.py",
        "--server.address", config.HOST,
        "--server.port", str(config.PORT),
        "--server.headless", "true",
    ])

    def _stop_both() -> None:
        for proc in (worker_proc, streamlit_proc):
            if proc.poll() is None:
                proc.terminate()
        for proc in (worker_proc, streamlit_proc):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    try:
        while True:
            if worker_proc.poll() is not None:
                print(f"Worker exited (code {worker_proc.returncode}); stopping Streamlit.")
                break
            if streamlit_proc.poll() is not None:
                print(f"Streamlit exited (code {streamlit_proc.returncode}); stopping worker.")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        _stop_both()


if __name__ == "__main__":
    main()
