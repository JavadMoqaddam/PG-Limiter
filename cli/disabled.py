"""
CLI commands for disabled users management.

The registry lives in the ``users`` table of the limiter's database. This module
used to keep its own JSON reader and writer, and that writer only ever wrote
``{"disabled_users": ...}`` - so enabling one user from the CLI silently turned
every other user's permanent or timed ban into a default-window one.

The database URL is resolved exactly as the limiter resolves it, so run these
commands from the installation directory (or export ``DATABASE_URL``), otherwise
the CLI would look at a different database file than the running limiter.
"""
import asyncio
import time
from typing import Optional

import typer
from rich.table import Table

from cli.utils import (
    FLAGS,
    console,
    error,
    info,
    print_table,
    success,
    warning,
)
from utils import handel_dis_users as dis_registry

app = typer.Typer(no_args_is_help=True, help="Manage disabled users")


def _run(work):
    """
    Run one unit of async work for a synchronous CLI command.

    ``work`` must be a callable returning a coroutine, not a coroutine: the
    database engine binds itself to the event loop it is first used on, so every
    command gets exactly one loop, and disposes of it before returning.
    """

    async def _main():
        from db.database import close_db, init_db

        await init_db()
        try:
            return await work()
        finally:
            await close_db()

    return asyncio.run(_main())


def _describe_timer(entry) -> str:
    """Human-readable re-enable plan for one entry."""
    if entry.is_permanent:
        return "manual only"
    if entry.enable_at:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.enable_at))
    return "default window"


@app.command(name="list")
def list_disabled_users(
    name: Optional[str] = typer.Option(
        None, *FLAGS["name"], help="Filter by username"
    ),
):
    """List all currently disabled users"""
    entries = _run(dis_registry.entries)

    if name:
        entries = {k: v for k, v in entries.items() if name.lower() in k.lower()}

    if not entries:
        info("No disabled users found.")
        return

    current_time = time.time()
    rows = []

    for username, entry in sorted(
        entries.items(), key=lambda item: item[1].disabled_at, reverse=True
    ):
        elapsed = int(current_time - entry.disabled_at)
        minutes = elapsed // 60
        seconds = elapsed % 60
        disabled_at = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(entry.disabled_at)
        )
        rows.append((username, disabled_at, f"{minutes}m {seconds}s", _describe_timer(entry)))

    print_table(
        table=Table("Username", "Disabled At", "Elapsed", "Enable At"),
        rows=rows,
    )

    info(f"Total: {len(entries)} disabled users")


@app.command(name="enable")
def enable_user(
    name: str = typer.Option(..., *FLAGS["name"], prompt=True, help="Username to enable"),
):
    """Enable a specific disabled user (clear their disable record)

    Note: This only clears the limiter's record.
    You may need to manually enable the user on the panel if needed.
    """
    async def _enable():
        if not await dis_registry.is_disabled(name):
            return "missing"
        return "cleared" if await dis_registry.enable(name) else "failed"

    outcome = _run(_enable)

    if outcome == "missing":
        error(f"User '{name}' is not in the disabled list")
        return
    if outcome == "failed":
        error(f"Could not clear the disable record for '{name}'")
        return

    success(f"User '{name}' removed from disabled list")
    warning("Note: If the user is disabled on the panel, you may need to enable them manually.")


@app.command(name="enable-all")
def enable_all_users():
    """Enable all disabled users (clear every disable record)"""
    cleared = _run(dis_registry.clear_all)

    if not cleared:
        info("No disabled users to enable.")
        return

    success(f"Cleared {len(cleared)} users from disabled list")
    warning("Note: If users are disabled on the panel, you may need to enable them manually.")


@app.command(name="info")
def show_user_info(
    name: str = typer.Option(..., *FLAGS["name"], prompt=True, help="Username to show"),
):
    """Show info about a specific disabled user"""
    entry = _run(lambda: dis_registry.entry_of(name))

    if entry is None:
        info(f"User '{name}' is not in the disabled list")
        return

    elapsed = int(time.time() - entry.disabled_at)
    disabled_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.disabled_at))
    hours = elapsed // 3600
    minutes = (elapsed % 3600) // 60
    seconds = elapsed % 60

    console.print(f"\n[bold]User Info: {name}[/bold]")
    console.print("  Status:      [red]Disabled[/red]")
    console.print(f"  Disabled at: {disabled_at}")
    console.print(f"  Elapsed:     {hours}h {minutes}m {seconds}s")
    console.print(f"  Enable at:   {_describe_timer(entry)}")
