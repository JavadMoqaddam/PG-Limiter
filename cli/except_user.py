"""
CLI commands for except users (whitelisted users)
"""
from typing import Optional
import typer
from rich.table import Table

from cli.utils import (
    FLAGS,
    error,
    info,
    load_backup,
    load_config,
    print_table,
    update_backup,
    update_config,
    success,
)

app = typer.Typer(no_args_is_help=True, help="Manage except (whitelisted) users")


@app.command(name="list")
def list_except_users(
    name: Optional[str] = typer.Option(
        None, *FLAGS["name"], help="Filter by username"
    ),
):
    """List all except users (users exempt from IP limits)"""
    config = load_config()
    backup = load_backup()
    
    # Get except users from both config and backup
    except_users = set()
    
    # From config (new format)
    if "users" in config and "except" in config["users"]:
        except_users.update(config["users"]["except"])
    
    # From backup
    if "except_users" in backup:
        except_users.update(backup["except_users"])
    
    if name:
        except_users = {u for u in except_users if name.lower() in u.lower()}
    
    if not except_users:
        info("No except users found.")
        return
    
    print_table(
        table=Table("#", "Username"),
        rows=[
            (str(i + 1), username)
            for i, username in enumerate(sorted(except_users))
        ],
    )


@app.command(name="add")
def add_except_user(
    name: str = typer.Option(..., *FLAGS["name"], prompt=True, help="Username to exempt"),
):
    """Add a user to the except list (exempt from IP limits)"""
    config = load_config()
    backup = load_backup()
    
    # Ensure structure exists
    if "users" not in config:
        config["users"] = {}
    if "except" not in config["users"]:
        config["users"]["except"] = []
    if "except_users" not in backup:
        backup["except_users"] = []
    
    # Check if already exists
    if name in config["users"]["except"] or name in backup["except_users"]:
        error(f"User '{name}' is already in the except list")
    
    def add_to_config(latest):
        users = latest.setdefault("users", {}).setdefault("except", [])
        if name not in users:
            users.append(name)

    def add_to_backup(latest):
        users = latest.setdefault("except_users", [])
        if name not in users:
            users.append(name)

    update_config(add_to_config)
    update_backup(add_to_backup)
    
    success(f"User '{name}' added to except list")


@app.command(name="delete")
def delete_except_user(
    name: str = typer.Option(..., *FLAGS["name"], prompt=True, help="Username to remove"),
):
    """Remove a user from the except list"""
    config = load_config()
    backup = load_backup()
    
    removed = False
    
    # Remove from config
    if "users" in config and "except" in config["users"]:
        if name in config["users"]["except"]:
            def remove_from_config(latest):
                users = latest.get("users", {}).get("except", [])
                if name in users:
                    users.remove(name)

            update_config(remove_from_config)
            removed = True

    # Remove from backup
    if "except_users" in backup and name in backup["except_users"]:
        def remove_from_backup(latest):
            users = latest.get("except_users", [])
            if name in users:
                users.remove(name)

        update_backup(remove_from_backup)
        removed = True
    
    if removed:
        success(f"User '{name}' removed from except list")
    else:
        error(f"User '{name}' not found in except list")


@app.command(name="check")
def check_except_user(
    name: str = typer.Option(..., *FLAGS["name"], prompt=True, help="Username to check"),
):
    """Check if a user is in the except list"""
    config = load_config()
    backup = load_backup()
    
    is_except = False
    
    # Check config
    if "users" in config and "except" in config["users"]:
        if name in config["users"]["except"]:
            is_except = True
    
    # Check backup
    if "except_users" in backup and name in backup["except_users"]:
        is_except = True
    
    if is_except:
        info(f"✓ User '{name}' IS in the except list (exempt from limits)")
    else:
        info(f"✗ User '{name}' is NOT in the except list")
