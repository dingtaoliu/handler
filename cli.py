"""Handler CLI — start, stop, status, run."""

import argparse
import os
import signal
import subprocess
import sys
import time

from .paths import PACKAGE_DIR as _PACKAGE_DIR, DATA_DIR as _DATA_DIR, PID_PATH as _PID_PATH, LOG_PATH as _LOG_PATH


def _read_pid() -> tuple[int | None, bool]:
    """Read PID from file. Returns (pid, alive)."""
    if not _PID_PATH.exists():
        return None, False
    try:
        pid = int(_PID_PATH.read_text().strip())
        os.kill(pid, 0)
        return pid, True
    except (ValueError, ProcessLookupError, PermissionError):
        return None, False


def cmd_start(args: argparse.Namespace) -> None:
    pid, alive = _read_pid()
    if alive:
        print(f"Handler is already running (PID {pid})")
        return

    _PID_PATH.unlink(missing_ok=True)
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    log_file = open(_LOG_PATH, "a")
    subprocess.Popen(
        [sys.executable, "-m", "handler"],
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(_PACKAGE_DIR),
    )

    # Wait for PID file
    for _ in range(10):
        time.sleep(0.3)
        pid, alive = _read_pid()
        if alive:
            print(f"Handler started (PID {pid})")
            print("Web UI: http://localhost:8000")
            return

    print("Handler may have failed to start. Check: handler logs")
    sys.exit(1)


def cmd_stop(args: argparse.Namespace) -> None:
    pid, alive = _read_pid()
    if not alive:
        print("Handler is not running")
        _PID_PATH.unlink(missing_ok=True)
        return

    print(f"Stopping handler (PID {pid})...")
    os.kill(pid, signal.SIGTERM)

    for _ in range(10):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            _PID_PATH.unlink(missing_ok=True)
            print("Handler stopped.")
            return

    print("Force killing...")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _PID_PATH.unlink(missing_ok=True)
    print("Handler stopped.")


def cmd_status(args: argparse.Namespace) -> None:
    pid, alive = _read_pid()
    if alive:
        print(f"Handler is running (PID {pid})")
    else:
        print("Handler is not running")
        _PID_PATH.unlink(missing_ok=True)


def cmd_run(args: argparse.Namespace) -> None:
    from .__main__ import main

    main()


def cmd_restart(args: argparse.Namespace) -> None:
    cmd_stop(args)
    cmd_start(args)


def cmd_logs(args: argparse.Namespace) -> None:
    if not _LOG_PATH.exists():
        print("No log file found")
        return
    n = args.lines
    # Read last N lines
    try:
        with open(_LOG_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            buf = min(size, n * 200)
            f.seek(max(0, size - buf))
            content = f.read().decode("utf-8", errors="replace")
            for line in content.splitlines()[-n:]:
                print(line)
    except Exception as e:
        print(f"Error reading logs: {e}")


def cli() -> None:
    parser = argparse.ArgumentParser(
        prog="handler",
        description="Handler — autonomous personal agent",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("start", help="Start handler in the background")
    sub.add_parser("stop", help="Stop the running handler")
    sub.add_parser("restart", help="Stop and start handler")
    sub.add_parser("status", help="Check if handler is running")
    sub.add_parser("run", help="Run handler in the foreground")

    logs_parser = sub.add_parser("logs", help="Show recent log output")
    logs_parser.add_argument(
        "-n", "--lines", type=int, default=50, help="Number of lines (default: 50)"
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
    elif args.command == "start":
        cmd_start(args)
    elif args.command == "stop":
        cmd_stop(args)
    elif args.command == "restart":
        cmd_restart(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "logs":
        cmd_logs(args)


if __name__ == "__main__":
    cli()
