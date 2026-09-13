"""CLI utility functions"""
import os
from typing import List, Tuple

import typer
from rich.console import Console
from rich.table import Table
from utils.atomic_io import JsonSnapshot, atomic_update_json, read_json_snapshot

console = Console()

FLAGS = {
    "name": ["-n", "--name"],
    "limit": ["-l", "--limit"],
    "username": ["-u", "--username"],
    "ip": ["-i", "--ip"],
}

CONFIG_FILE = "config.json"
BACKUP_FILE = "backup.json"


def print_table(table: Table, rows: List[Tuple]):
    """Print a rich table with rows"""
    for row in rows:
        table.add_row(*row)
    console.print(table)


def success(message: str):
    """Print a success message"""
    console.print(f"[green]✓[/green] {message}")


def error(message: str):
    """Print an error message and exit"""
    console.print(f"[red]✗[/red] {message}")
    raise typer.Exit(1)


def warning(message: str):
    """Print a warning message"""
    console.print(f"[yellow]⚠[/yellow] {message}")


def info(message: str):
    """Print an info message"""
    console.print(f"[blue]ℹ[/blue] {message}")


def load_config() -> JsonSnapshot:
    """Load a mergeable config snapshot under the cross-process sidecar lock."""
    if not os.path.exists(CONFIG_FILE):
        error(f"Config file '{CONFIG_FILE}' not found. Run the limiter first to create it.")
    return read_json_snapshot(CONFIG_FILE)


def update_config(mutator):
    """Apply a config mutation to the latest document under its sidecar lock."""
    return atomic_update_json(CONFIG_FILE, mutator)


def update_backup(mutator):
    """Apply a backup mutation to the latest document under its sidecar lock."""
    return atomic_update_json(
        BACKUP_FILE,
        mutator,
        default={"special": {}, "except_users": []},
    )


def save_config(config: dict):
    """Commit this config snapshot without replacing unrelated concurrent edits."""
    if isinstance(config, JsonSnapshot):
        return atomic_update_json(CONFIG_FILE, config.commit)
    return atomic_update_json(CONFIG_FILE, lambda _latest: config)


def load_backup() -> JsonSnapshot:
    """Load a mergeable compatibility-backup snapshot."""
    return read_json_snapshot(
        BACKUP_FILE,
        default={"special": {}, "except_users": []},
    )


def save_backup(backup: dict):
    """Commit this backup snapshot without replacing unrelated concurrent edits."""
    if isinstance(backup, JsonSnapshot):
        return atomic_update_json(
            BACKUP_FILE,
            backup.commit,
            default={"special": {}, "except_users": []},
        )
    return atomic_update_json(
        BACKUP_FILE,
        lambda _latest: backup,
        default={"special": {}, "except_users": []},
    )
