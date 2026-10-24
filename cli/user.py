"""CLI commands for user management (special limits)."""

from typing import Optional

import typer
from rich.table import Table

from cli.utils import FLAGS, error, info, load_config, print_table, run_command, success, warning
from utils.settings_service import read_settings, set_special_limit

app = typer.Typer(no_args_is_help=True, help="Manage user limits")


@app.command(name="list")
def list_users(
    name: Optional[str] = typer.Option(None, *FLAGS["name"], help="Filter by username"),
):
    """List all users with special limits."""
    special_limits = load_config()["limits"]["special"]
    if name:
        special_limits = {k: v for k, v in special_limits.items() if name.lower() in k.lower()}
    if not special_limits:
        info("No users with special limits found.")
        return
    print_table(
        table=Table("Username", "Limit"),
        rows=[(username, str(limit)) for username, limit in sorted(special_limits.items())],
    )


@app.command(name="add")
def add_user(
    name: str = typer.Option(..., *FLAGS["name"], prompt=True, help="Username"),
    limit: int = typer.Option(..., *FLAGS["limit"], prompt=True, help="IP limit"),
):
    """Add or update a special limit for a user."""
    if limit < 1:
        error("Limit must be at least 1")

    async def add():
        config = await read_settings()
        existing = config["limits"]["special"].get(name)
        return existing, await set_special_limit(name, limit)

    existing, changed = run_command(add)
    if not changed:
        error(f"User '{name}' not found or could not be fetched from the panel")
    if existing is not None:
        warning(f"User '{name}' already has limit {existing}. Updating...")
    success(f"User '{name}' limit set to {limit}")


@app.command(name="delete")
def delete_user(
    name: str = typer.Option(..., *FLAGS["name"], prompt=True, help="Username to remove"),
):
    """Remove a user's special limit."""
    if not run_command(lambda: set_special_limit(name, None, require_existing=True)):
        error(f"User '{name}' not found in special limits")
    success(f"User '{name}' special limit removed")


@app.command(name="update")
def update_user(
    name: str = typer.Option(..., *FLAGS["name"], prompt=True, help="Username"),
    limit: int = typer.Option(..., *FLAGS["limit"], prompt=True, help="New IP limit"),
):
    """Update a user's existing special limit."""
    if limit < 1:
        error("Limit must be at least 1")
    if not run_command(lambda: set_special_limit(name, limit, require_existing=True)):
        error(f"User '{name}' not found. Use 'add' command instead.")
    success(f"User '{name}' limit updated to {limit}")


@app.command(name="show")
def show_user(
    name: str = typer.Option(..., *FLAGS["name"], prompt=True, help="Username to show"),
):
    """Show a specific user's special or general limit."""
    config = load_config()
    limit = config["limits"]["special"].get(name)
    if limit is not None:
        info(f"User '{name}' has special limit: {limit}")
    else:
        general = config["limits"]["general"]
        info(f"User '{name}' has no special limit. Using general limit: {general}")
