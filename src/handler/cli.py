"""Handler CLI — start, stop, status, run."""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time

from pathlib import Path

from . import paths as _paths
from .instance import (
    DEFAULT_INSTANCE_ID,
    discover_instances,
    ensure_instance_layout,
    instance_meta_path,
    is_instance_dir,
    resolve_instance_dir,
)

_RUN_DIR = Path.home()

_paths.ensure_scripts_dir_on_path()


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

    env_path = _paths.DATA_DIR / ".env"
    _paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    load_dotenv(env_path)
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

    env_path.touch(mode=0o600)
    set_key(str(env_path), env_var, key)
    set_key(str(env_path), "HANDLER_AGENT", backend)
    os.environ[env_var] = key
    os.environ["HANDLER_AGENT"] = backend
    print(f"\nSaved to {env_path}")

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
            dest = _paths.DATA_DIR / "credentials" / "desktop.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
            dest.chmod(0o600)
            print(f"Saved to {dest}")

    print("─" * 42)
    print()


def _read_pid() -> tuple[int | None, bool]:
    """Read PID from file. Returns (pid, alive)."""
    if not _paths.PID_PATH.exists():
        return None, False
    try:
        pid = int(_paths.PID_PATH.read_text().strip())
        os.kill(pid, 0)
        return pid, True
    except (ValueError, ProcessLookupError, PermissionError):
        return None, False


def _configure_runtime_instance(instance_id: str | None) -> None:
    if instance_id:
        _paths.configure_instance(instance_id)


def _ensure_instance_selected(args: argparse.Namespace) -> None:
    _configure_runtime_instance(getattr(args, "instance", None))


def _ensure_named_instance_exists(instance_id: str | None) -> None:
    if not instance_id:
        return
    resolved_dir = resolve_instance_dir(instance_id)
    if instance_id == DEFAULT_INSTANCE_ID:
        return
    if is_instance_dir(resolved_dir):
        return
    print(
        f"Instance '{instance_id}' does not exist yet. "
        f"Create it first with: handler instance create {instance_id}"
    )
    sys.exit(1)


def cmd_start(args: argparse.Namespace) -> None:
    _ensure_instance_selected(args)
    _ensure_named_instance_exists(getattr(args, "instance", None))
    _init()
    first_run = not (_paths.DATA_DIR / "config" / "identity.md").exists()

    pid, alive = _read_pid()
    if alive:
        print(f"Handler is already running (PID {pid})")
        return

    _paths.PID_PATH.unlink(missing_ok=True)
    _paths.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    log_file = open(_paths.LOG_PATH, "a")
    subprocess.Popen(
        [sys.executable, "-m", "handler"],
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(_RUN_DIR),
        env=_paths.with_scripts_dir_on_path(),
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
    _ensure_instance_selected(args)
    _init()
    print("Handler is configured and ready. Run: handler start")


def cmd_stop(args: argparse.Namespace) -> None:
    _ensure_instance_selected(args)
    _ensure_named_instance_exists(getattr(args, "instance", None))
    _stop_watchdog()

    pid, alive = _read_pid()
    if not alive or pid is None:
        print("Handler is not running")
        _paths.PID_PATH.unlink(missing_ok=True)
        return

    print(f"Stopping handler (PID {pid})...")
    os.kill(pid, signal.SIGTERM)

    for _ in range(10):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            _paths.PID_PATH.unlink(missing_ok=True)
            print("Handler stopped.")
            return

    print("Force killing...")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _paths.PID_PATH.unlink(missing_ok=True)
    print("Handler stopped.")


def cmd_status(args: argparse.Namespace) -> None:
    _ensure_instance_selected(args)
    _ensure_named_instance_exists(getattr(args, "instance", None))
    pid, alive = _read_pid()
    if alive and pid is not None:
        print(f"Handler is running (PID {pid})")
    else:
        print("Handler is not running")
        _paths.PID_PATH.unlink(missing_ok=True)


def cmd_run(args: argparse.Namespace) -> None:
    _ensure_instance_selected(args)
    _ensure_named_instance_exists(getattr(args, "instance", None))
    _init()
    from .__main__ import main

    main()


def cmd_restart(args: argparse.Namespace) -> None:
    _ensure_instance_selected(args)
    cmd_stop(args)
    cmd_start(args)


def cmd_auth(args: argparse.Namespace) -> None:
    """Run OAuth flow for Gmail or Google Drive interactively."""
    from dotenv import load_dotenv
    from .users import get_user, list_users

    _ensure_instance_selected(args)
    _ensure_named_instance_exists(getattr(args, "instance", None))

    env_path = _paths.DATA_DIR / ".env"
    load_dotenv(env_path)
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

    creds_path = _paths.DATA_DIR / "credentials" / "desktop.json"
    if not creds_path.exists():
        print(f"Error: credentials not found at {creds_path}")
        print(
            "Download OAuth client JSON from Google Cloud Console → APIs & Services → Credentials"
        )
        sys.exit(1)

    from google_auth_oauthlib.flow import InstalledAppFlow

    from .google_oauth import (
        build_console_authorization_url,
        exchange_console_authorization,
    )

    if service == "gmail":
        from .tools.gmail import SCOPES, _token_path
    else:
        from .tools.gdrive import SCOPES, _token_path

    token_path = _token_path(user)
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)

    print(f"Authorizing {service}{f' for user {user}' if user else ''}...")
    if console:
        auth_url = build_console_authorization_url(flow)
        print("Open this URL in your browser and complete the Google sign-in flow:")
        print(auth_url)
        print(
            "After Google redirects to localhost and the page fails to load, "
            "copy the full URL from your browser's address bar and paste it here."
        )
        response = _prompt("Authorization response URL (or code): ")
        creds = exchange_console_authorization(flow, response)
    else:
        creds = flow.run_local_server(port=0)

    Path(token_path).parent.mkdir(parents=True, exist_ok=True)
    Path(token_path).write_text(creds.to_json())
    print(f"Token saved to {token_path}")
    print("Authorization complete. Restart handler to pick up the new token.")


def cmd_logs(args: argparse.Namespace) -> None:
    _ensure_instance_selected(args)
    _ensure_named_instance_exists(getattr(args, "instance", None))
    if not _paths.LOG_PATH.exists():
        print("No log file found")
        return
    n = args.lines
    # Read last N lines
    try:
        with open(_paths.LOG_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            buf = min(size, n * 200)
            f.seek(max(0, size - buf))
            content = f.read().decode("utf-8", errors="replace")
            for line in content.splitlines()[-n:]:
                print(line)
    except Exception as e:
        print(f"Error reading logs: {e}")


def cmd_kb_index(args: argparse.Namespace) -> None:
    """Index Gmail messages for the given year (and optional month) into the user's emails.db."""
    _ensure_instance_selected(args)
    from dotenv import load_dotenv
    load_dotenv(_paths.DATA_DIR / ".env")
    load_dotenv()

    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, BarColumn, MofNCompleteColumn, TimeElapsedColumn, TextColumn
    from .users import get_user

    console = Console()
    user = get_user(args.user)
    console.print(f"\n[bold cyan]Gmail Indexer[/bold cyan] — {user.display_name} — {args.year}" + (f"-{args.month:02d}" if args.month else ""))

    from .kb.indexer import GmailIndexer

    user.base_dir.mkdir(parents=True, exist_ok=True)
    # desktop.json is instance-level (shared)
    creds_path = str(_paths.DATA_DIR / "credentials" / "desktop.json")
    # token: prefer per-user gmail_token.json, fall back to instance-level token.json
    # Use _paths.DATA_DIR directly (not gmail._DATA_DIR which is captured at import time)
    _per_user_token = user.credentials_dir / "gmail_token.json"
    _instance_token = _paths.DATA_DIR / "credentials" / "token.json"
    token_path = str(_per_user_token) if _per_user_token.exists() else str(_instance_token)
    db_path = str(user.emails_db_path)

    progress_bar = None
    task_id = None

    def on_progress(current, total, data):
        nonlocal progress_bar, task_id
        if progress_bar is None:
            return
        progress_bar.update(task_id, completed=current, total=total)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        progress_bar = progress
        task_id = progress.add_task("Indexing", total=None)

        with GmailIndexer(credentials_path=creds_path, token_path=token_path, db_path=db_path) as indexer:
            stats = indexer.index_messages(
                year=args.year,
                month=args.month,
                max_emails=args.limit,
                overwrite=args.overwrite,
                progress_callback=on_progress,
            )

    console.print(f"\n[green]Done[/green] — downloaded: {stats['downloaded']}, skipped: {stats['skipped']}, errors: {stats['errors']}")


def cmd_kb_build(args: argparse.Namespace) -> None:
    """Run the KB pipeline: filter emails then extract life facts to the user's knowledge/ dir."""
    _ensure_instance_selected(args)
    from dotenv import load_dotenv
    load_dotenv(_paths.DATA_DIR / ".env")
    load_dotenv()

    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, BarColumn, MofNCompleteColumn, TimeElapsedColumn, TextColumn
    from .users import get_user

    console = Console()
    user = get_user(args.user)
    year_str = f" — {args.year}" if getattr(args, "year", None) else ""
    console.print(f"\n[bold cyan]KB Pipeline[/bold cyan] — {user.display_name}{year_str}")

    from .kb.pipeline import run_pipeline, KnowledgeBase

    refilter = args.refilter or getattr(args, "force", False)
    reextract = args.reextract or getattr(args, "force", False)

    # Always show cached state so it's clear what's being reused vs reprocessed
    try:
        with KnowledgeBase(str(user.emails_db_path)) as _kb:
            _s = _kb.get_stats()
        already_filtered = _s["total_filtered"]
        already_extracted = _s["total_notes"]
        if already_filtered:
            parts = [f"{already_filtered} already filtered"]
            if refilter:
                parts = ["refiltering all"]
            if already_extracted:
                parts.append(f"{already_extracted} notes extracted" + (" (re-extracting)" if reextract else ""))
            console.print(f"  [dim]{', '.join(parts)}[/dim]")
    except Exception:
        pass

    counts = {"skip": 0, "cached": 0, "extracted": 0, "extract_skip": 0, "errors": 0}
    errors_shown = 0
    progress_bar = task_id = None

    def on_progress(event):
        nonlocal errors_shown
        phase = event.get("phase", "")
        counts[phase] = counts.get(phase, 0) + 1

        if phase == "error" and errors_shown < 3:
            errors_shown += 1
            console.print(f"  [red]Error[/red]: {event.get('error', '')[:120]}")

        if progress_bar and task_id is not None:
            desc = (f"skip={counts['skip']} cached={counts['cached']} "
                    f"extracted={counts['extracted']} err={counts['errors']}")
            progress_bar.update(
                task_id,
                completed=event.get("index", sum(counts.values())),
                total=event.get("total"),
                description=desc,
            )

    model = getattr(args, "model", None)
    extra = {"filter_model": model, "extract_model": model} if model else {}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        progress_bar = progress
        task_id = progress.add_task("skip=0 cached=0 extracted=0 err=0", total=None)

        stats = run_pipeline(
            user_id=user.id,
            limit=args.limit,
            refilter=refilter,
            reextract=reextract,
            progress_callback=on_progress,
            year=getattr(args, "year", None),
            **extra,
        )
        progress.update(task_id, completed=stats["total"], total=stats["total"])

    kb = stats.get("kb_stats", {})
    console.print(f"\n[green]Done[/green]")
    console.print(f"  Total emails     : {stats['total']}")
    console.print(f"  Filtered out     : {counts['skip']}")
    console.print(f"  Already cached   : {counts['cached']}")
    console.print(f"  Extracted        : {stats['extracted']}")
    console.print(f"  Extract skipped  : {counts.get('extract_skip', 0)}")
    console.print(f"  Errors           : {stats['errors']}")
    console.print(f"  Filter API calls : {stats['filter_api_calls']}")
    console.print(f"  Extract API calls: {stats['extract_api_calls']}")
    if kb:
        console.print(f"  KB notes total   : {kb.get('total_notes', 0)}")
    console.print(f"\nKnowledge base written to: [cyan]{stats['output_dir']}[/cyan]")
    if kb.get("by_category"):
        for cat, count in sorted(kb["by_category"].items()):
            console.print(f"  {cat:<15} {count} entries")


def cmd_instance_create(args: argparse.Namespace) -> None:
    instance_id = args.instance_id
    resolved_dir = resolve_instance_dir(instance_id)
    if instance_meta_path(resolved_dir).exists():
        print(f"Instance '{instance_id}' already exists at {resolved_dir}")
        return

    data_dir, metadata = ensure_instance_layout(
        instance_id,
        host=args.host,
        port=args.port,
        display_name=args.display_name,
    )
    for relative in (
        "config",
        "memory",
        "uploads",
        "credentials",
        "logs",
        "shell_logs",
        "users",
    ):
        (data_dir / relative).mkdir(parents=True, exist_ok=True)

    print(f"Created instance '{metadata.id}' at {data_dir}")
    print(f"Web UI will bind to http://{metadata.host}:{metadata.port}")


def cmd_instance_list(args: argparse.Namespace) -> None:
    instances = discover_instances()
    if not instances:
        print("No instances found")
        return

    current_dir = _paths.DATA_DIR.resolve()
    for data_dir, metadata in instances:
        marker = "*" if data_dir.resolve() == current_dir else " "
        print(
            f"{marker} {metadata.id:<12} {metadata.host}:{metadata.port:<5} "
            f"{metadata.display_name:<20} {data_dir}"
        )


def cmd_instance_remove(args: argparse.Namespace) -> None:
    instance_id = args.instance_id
    if instance_id == DEFAULT_INSTANCE_ID:
        print("Refusing to remove the default legacy instance")
        sys.exit(1)

    resolved_dir = resolve_instance_dir(instance_id)
    if not resolved_dir.exists():
        print(f"Instance '{instance_id}' does not exist")
        return

    if not args.force:
        print(
            f"Refusing to remove {resolved_dir} without --force. "
            "This deletes the full instance workspace."
        )
        sys.exit(1)

    shutil.rmtree(resolved_dir)
    print(f"Removed instance '{instance_id}'")


def cli() -> None:
    parser = argparse.ArgumentParser(
        prog="handler",
        description="Handler — autonomous personal agent",
    )
    parser.add_argument(
        "--instance",
        help="Named instance to use (stored under ~/.handler/instances/<name>)",
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

    kb_parser = sub.add_parser("kb", help="Gmail knowledge base tools")
    kb_sub = kb_parser.add_subparsers(dest="kb_command")

    kb_index = kb_sub.add_parser("index", help="Index Gmail messages into emails.db")
    kb_index.add_argument("--user", default="danny", help="User to index for (default: danny)")
    kb_index.add_argument("--year", type=int, required=True, help="Year to index")
    kb_index.add_argument("--month", type=int, choices=range(1, 13), help="Month (1-12)")
    kb_index.add_argument("--limit", type=int, help="Max emails (for testing)")
    kb_index.add_argument("--overwrite", action="store_true", help="Re-download existing")

    kb_build = kb_sub.add_parser("build", help="Run KB pipeline: filter + extract facts")
    kb_build.add_argument("--user", default="danny", help="User to build KB for (default: danny)")
    kb_build.add_argument("--year", type=int, help="Only process emails from this year")
    kb_build.add_argument("--model", help="Override model for both filter and extract passes")
    kb_build.add_argument("--limit", type=int, help="Max emails to process (for testing)")
    kb_build.add_argument("--refilter", action="store_true", help="Re-run filter on already-filtered emails")
    kb_build.add_argument("--reextract", action="store_true", help="Re-run extraction on already-extracted emails")
    kb_build.add_argument("--force", action="store_true", help="Reprocess everything (implies --refilter --reextract)")

    kb_export = kb_sub.add_parser("export", help="Re-export markdown files from existing notes")
    kb_export.add_argument("--user", default="danny", help="User to export for (default: danny)")

    instance_parser = sub.add_parser("instance", help="Manage Handler instances")
    instance_sub = instance_parser.add_subparsers(dest="instance_command")

    instance_create = instance_sub.add_parser("create", help="Create a named instance")
    instance_create.add_argument(
        "instance_id", help="Instance id, for example: personal"
    )
    instance_create.add_argument("--display-name", help="Human-readable display name")
    instance_create.add_argument(
        "--host", default="0.0.0.0", help="Web bind host (default: 0.0.0.0)"
    )
    instance_create.add_argument(
        "--port", type=int, default=8000, help="Web bind port (default: 8000)"
    )

    instance_sub.add_parser("list", help="List discovered instances")

    instance_remove = instance_sub.add_parser("remove", help="Delete a named instance")
    instance_remove.add_argument("instance_id", help="Instance id to delete")
    instance_remove.add_argument(
        "--force", action="store_true", help="Delete the instance workspace"
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
    elif args.command == "kb":
        if args.kb_command == "index":
            cmd_kb_index(args)
        elif args.kb_command == "build":
            cmd_kb_build(args)
        elif args.kb_command == "export":
            _ensure_instance_selected(args)
            from .kb.pipeline import KnowledgeBase
            from .users import get_user
            user = get_user(args.user)
            kb = KnowledgeBase(str(user.emails_db_path))
            out = kb.export_markdown(user.knowledge_dir)
            kb.close()
            print(f"Exported to {out}")
        else:
            kb_parser.print_help()
    elif args.command == "instance":
        if args.instance_command == "create":
            cmd_instance_create(args)
        elif args.instance_command == "list":
            cmd_instance_list(args)
        elif args.instance_command == "remove":
            cmd_instance_remove(args)
        else:
            instance_parser.print_help()


if __name__ == "__main__":
    cli()
