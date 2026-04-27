"""Handler CLI — start, stop, status, run."""

import argparse
import os
import signal
import subprocess
import sys
import time

from pathlib import Path

from .paths import (
    DATA_DIR as _DATA_DIR,
    PID_PATH as _PID_PATH,
    LOG_PATH as _LOG_PATH,
)

_RUN_DIR = Path.home()
_ENV_PATH = _DATA_DIR / ".env"


_PROVIDERS = {
    "1": ("openai", "OPENAI_API_KEY", "https://platform.openai.com/api-keys"),
    "2": ("claude", "ANTHROPIC_API_KEY", "https://console.anthropic.com/settings/keys"),
}


def _prompt(label: str) -> str:
    try:
        return input(label).strip()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.")
        sys.exit(1)


def _init() -> None:
    """Prompt for required config on first run and save to ~/.handler/.env."""
    from dotenv import load_dotenv, set_key

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    load_dotenv(_ENV_PATH)
    load_dotenv()

    # Already configured — nothing to do
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
        return

    print("Welcome to Handler!")
    print("─" * 42)
    print("Choose a model provider:")
    print("  1. OpenAI  (OPENAI_API_KEY)")
    print("  2. Claude  (ANTHROPIC_API_KEY)")
    print()

    choice = _prompt("Provider [1]: ") or "1"
    if choice not in _PROVIDERS:
        print(f"Invalid choice '{choice}'. Exiting.")
        sys.exit(1)

    backend, env_var, url = _PROVIDERS[choice]
    print(f"\nGet your key at: {url}")
    key = _prompt(f"{env_var}: ")

    if not key:
        print("No key provided. Exiting.")
        sys.exit(1)

    _ENV_PATH.touch(mode=0o600)
    set_key(str(_ENV_PATH), env_var, key)
    set_key(str(_ENV_PATH), "HANDLER_AGENT", backend)
    os.environ[env_var] = key
    os.environ["HANDLER_AGENT"] = backend
    print(f"\nSaved to {_ENV_PATH}")

    # Optional: Google credentials for Gmail / Drive
    print()
    print("─" * 42)
    print("Google integration (Gmail, Drive) — optional")
    print("Paste the path to your desktop.json OAuth file, or press Enter to skip.")
    print()
    raw = _prompt("Path to desktop.json: ")
    if raw:
        src = Path(raw).expanduser().resolve()
        if not src.exists():
            print(f"File not found: {src} — skipping Google setup.")
        else:
            dest = _DATA_DIR / "credentials" / "desktop.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
            dest.chmod(0o600)
            print(f"Saved to {dest}")

    print("─" * 42)
    print()


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
    _init()
    first_run = not (_DATA_DIR / "config" / "identity.md").exists()

    pid, alive = _read_pid()
    if alive:
        print(f"Handler is already running (PID {pid})")
        return

    _PID_PATH.unlink(missing_ok=True)
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    log_file = open(_LOG_PATH, "a")
    subprocess.Popen(
        [sys.executable, "-m", "handler"],
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(_RUN_DIR),
    )

    # Wait for PID file
    for _ in range(10):
        time.sleep(0.3)
        pid, alive = _read_pid()
        if alive:
            print(f"Handler started (PID {pid})")
            if first_run:
                print("Web UI: http://localhost:8000  ← open this to set up your agent")
            else:
                print("Web UI: http://localhost:8000")
            return

    print("Handler may have failed to start. Check: handler logs")
    sys.exit(1)


def _stop_watchdog() -> None:
    """Suspend the watchdog so it doesn't restart the handler while we're stopped."""
    try:
        from .watchdog import suspend_watchdog

        if suspend_watchdog():
            print("Watchdog suspended.")
    except Exception:
        pass


def cmd_init(args: argparse.Namespace) -> None:
    _init()
    print("Handler is configured and ready. Run: handler start")


def cmd_stop(args: argparse.Namespace) -> None:
    _stop_watchdog()

    pid, alive = _read_pid()
    if not alive or pid is None:
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
    if alive and pid is not None:
        print(f"Handler is running (PID {pid})")
    else:
        print("Handler is not running")
        _PID_PATH.unlink(missing_ok=True)


def cmd_run(args: argparse.Namespace) -> None:
    _init()
    from .__main__ import main

    main()


def cmd_restart(args: argparse.Namespace) -> None:
    cmd_stop(args)
    cmd_start(args)


def cmd_auth(args: argparse.Namespace) -> None:
    """Run OAuth flow for Gmail or Google Drive interactively."""
    from dotenv import load_dotenv
    from .users import get_user, list_users

    load_dotenv(_ENV_PATH)
    load_dotenv()

    service = args.service
    console = args.console
    user = args.user or None

    if user is not None:
        try:
            user = get_user(user).id
        except KeyError:
            valid_users = ", ".join(
                sorted(
                    {
                        candidate
                        for known_user in list_users()
                        for candidate in (known_user.id, *known_user.aliases)
                    }
                )
            )
            print(f"Error: unknown user '{user}'")
            print(f"Valid user ids: {valid_users}")
            sys.exit(1)

    creds_path = _DATA_DIR / "credentials" / "desktop.json"
    if not creds_path.exists():
        print(f"Error: credentials not found at {creds_path}")
        print(
            "Download OAuth client JSON from Google Cloud Console → APIs & Services → Credentials"
        )
        sys.exit(1)

    import re
    from google_auth_oauthlib.flow import InstalledAppFlow

    if service == "gmail":
        from .tools.gmail import SCOPES, _token_path
    else:
        from .tools.gdrive import SCOPES, _token_path

    token_path = _token_path(user)
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)

    print(f"Authorizing {service}{f' for user {user}' if user else ''}...")
    if console:
        auth_url, _ = flow.authorization_url(prompt="consent")
        print("Open this URL in your browser and complete the Google sign-in flow:")
        print(auth_url)
        code = _prompt("Authorization code: ")
        creds = flow.fetch_token(code=code)
        creds = flow.credentials
    else:
        creds = flow.run_local_server(port=0)

    Path(token_path).parent.mkdir(parents=True, exist_ok=True)
    Path(token_path).write_text(creds.to_json())
    print(f"Token saved to {token_path}")
    print("Authorization complete. Restart handler to pick up the new token.")


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

    sub.add_parser("init", help="Configure Handler (API key, workspace setup)")
    sub.add_parser("start", help="Start handler in the background")
    sub.add_parser("stop", help="Stop the running handler")
    sub.add_parser("restart", help="Stop and start handler")
    sub.add_parser("status", help="Check if handler is running")
    sub.add_parser("run", help="Run handler in the foreground")

    logs_parser = sub.add_parser("logs", help="Show recent log output")
    logs_parser.add_argument(
        "-n", "--lines", type=int, default=50, help="Number of lines (default: 50)"
    )

    auth_parser = sub.add_parser("auth", help="Authorize Gmail or Google Drive")
    auth_parser.add_argument(
        "service", choices=["gmail", "gdrive"], help="Service to authorize"
    )
    auth_parser.add_argument(
        "--console", action="store_true", help="Use console flow (headless/remote)"
    )
    auth_parser.add_argument(
        "--user",
        metavar="USER_ID",
        help="Authorize for a specific shared-instance user (for example: danny or zhijian)",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
    elif args.command == "init":
        cmd_init(args)
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
    elif args.command == "auth":
        cmd_auth(args)


if __name__ == "__main__":
    cli()
