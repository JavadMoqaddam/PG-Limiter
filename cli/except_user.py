"""CLI commands for except (whitelisted) users."""

from typing import Optional

import typer
from rich.table import Table

from cli.utils import FLAGS, error, info, load_config, print_table, run_command, success
from utils.settings_service import read_settings, set_exception

app = typer.Typer(no_args_is_help=True, help="Manage except (whitelisted) users")


@app.command(name="list")
def list_except_users(
    name: Optional[str] = typer.Option(None, *FLAGS["name"], help="Filter by username"),
):
    """List all users exempt from IP limits."""
    except_users = set(load_config()["except_users"])
    if name:
        except_users = {u for u in except_users if name.lower() in u.lower()}
    if not except_users:
        info("No except users found.")
        return
    print_table(
        table=Table("#", "Username"),
        rows=[(str(i + 1), username) for i, username in enumerate(sorted(except_users))],
    )


@app.command(name="add")
def add_except_user(
    name: str = typer.Option(..., *FLAGS["name"], prompt=True, help="Username to exempt"),
):
    """Add a user to the list exempt from IP limits."""

    async def add():
        if name in (await read_settings())["except_users"]:
            return "duplicate"
        return "added" if await set_exception(name, True) else "missing"

    outcome = run_command(add)
    if outcome == "duplicate":
        error(f"User '{name}' is already in the except list")
    if outcome == "missing":
        error(f"User '{name}' not found or could not be fetched from the panel")
    success(f"User '{name}' added to except list")


@app.command(name="delete")
def delete_except_user(
    name: str = typer.Option(..., *FLAGS["name"], prompt=True, help="Username to remove"),
):
    """Remove a user from the list exempt from IP limits."""
    if not run_command(lambda: set_exception(name, False, require_existing=True)):
        error(f"User '{name}' not found in except list")
    success(f"User '{name}' removed from except list")


@app.command(name="check")
def check_except_user(
    name: str = typer.Option(..., *FLAGS["name"], prompt=True, help="Username to check"),
):
    """Check if a user is exempt from IP limits."""
    if name in load_config()["except_users"]:
        info(f"✓ User '{name}' IS in the except list (exempt from limits)")
    else:
        info(f"✗ User '{name}' is NOT in the except list")
