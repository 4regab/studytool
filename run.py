"""Local launcher. Run with `uv run python run.py`.

Applies pending SQLite migrations once, then starts exactly one background
worker (`python -m studytool.worker`) and the web app as subprocesses.
If either process exits, the other is stopped too. Ctrl+C stops both.

`uv run python run.py --streamlit` starts the old Streamlit screen instead (kept until the
new web app is confirmed; it will be removed).
"""
import subprocess
import sys
import time

from studytool import config, db, settings


def main() -> None:
    conn = db.connect()
    db.migrate(conn)
    conn.close()

    if not settings.active()["key"]:
        print("Note: no AI API key yet. Add one on the Settings page before creating a deck.")

    if "--streamlit" in sys.argv:
        ui_cmd = [sys.executable, "-m", "streamlit", "run", "app.py",
                  "--server.address", config.HOST, "--server.port", str(config.PORT), "--server.headless", "true"]
    else:
        ui_cmd = [sys.executable, "-m", "uvicorn", "studytool.web:app",
                  "--host", config.HOST, "--port", str(config.PORT), "--log-level", "warning"]

    worker_proc = subprocess.Popen([sys.executable, "-m", "studytool.worker"])
    ui_proc = subprocess.Popen(ui_cmd)
    print(f"Open http://{config.HOST}:{config.PORT} in your browser. Press Ctrl+C to stop.", flush=True)

    def _stop_both() -> None:
        for proc in (worker_proc, ui_proc):
            if proc.poll() is None:
                proc.terminate()
        for proc in (worker_proc, ui_proc):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    try:
        while True:
            if worker_proc.poll() is not None:
                print(f"Worker exited (code {worker_proc.returncode}); stopping the web app.")
                break
            if ui_proc.poll() is not None:
                print(f"Web app exited (code {ui_proc.returncode}); stopping worker.")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        _stop_both()


if __name__ == "__main__":
    main()
