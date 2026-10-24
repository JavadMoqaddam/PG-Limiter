"""Output helpers and the database lifetime for synchronous CLI commands."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import List, Tuple, TypeVar

import typer
from rich.console import Console
from rich.table import Table

console = Console()

FLAGS = {
    "name": ["-n", "--name"],
    "limit": ["-l", "--limit"],
    "username": ["-u", "--username"],
    "ip": ["-i", "--ip"],
}

Result = TypeVar("Result")


def print_table(table: Table, rows: List[Tuple]):
    """Print a rich table with rows."""
    for row in rows:
        table.add_row(*row)
    console.print(table)


def success(message: str):
    """Print a success message."""
    console.print(f"[green]✓[/green] {message}")


def error(message: str):
    """Print an error message and exit."""
    console.print(f"[red]✗[/red] {message}")
    raise typer.Exit(1)


def warning(message: str):
    """Print a warning message."""
    console.print(f"[yellow]⚠[/yellow] {message}")


def info(message: str):
    """Print an info message."""
    console.print(f"[blue]ℹ[/blue] {message}")


def run_command(work: Callable[[], Awaitable[Result]]) -> Result:
    """Initialize, execute and close a command's database in one event loop."""

    async def main():
        from db.database import close_db, init_db

        try:
            await init_db()
            return await work()
        finally:
            await close_db()

    try:
        return asyncio.run(main())
    except Exception as exc:
        error(f"Command failed: {exc}")


def load_config() -> dict:
    """Read current committed configuration for a synchronous CLI command."""
    from utils.settings_service import read_settings

    return run_command(read_settings)
