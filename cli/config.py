"""
CLI commands for configuration management
"""

from typing import Optional
import typer
from rich.table import Table

from cli.utils import (
    console,
    error,
    info,
    load_config,
    print_table,
    run_command,
    success,
)

from utils.settings_service import read_settings, write_settings

app = typer.Typer(no_args_is_help=True, help="Manage configuration")


@app.command(name="show")
def show_config():
    """Show current configuration"""
    config = load_config()

    # Panel info
    panel = config.get("panel", {})
    info("📡 Panel Configuration:")
    print(f"  Domain:   {panel.get('domain', 'Not set')}")
    print(f"  API key: {'Set' if panel.get('api_key') else 'Not set'}")
    print()

    # Limits
    limits = config.get("limits", {})
    info("🎯 Limits:")
    print(f"  General limit: {limits.get('general', 2)}")
    special = limits.get("special", {})
    if special:
        print(f"  Special limits: {len(special)} users")
    else:
        print("  Special limits: None")
    print()

    # Timing
    info("⏱️ Timing:")
    print(f"  Check interval:     {config['check_interval']}s")
    print(f"  Time to re-enable:  {config['time_to_active_users']}s")
    print()

    # Users
    except_users = config["except_users"]
    info("👥 Users:")
    print(f"  Except users: {len(except_users)}")
    print()

    # Telegram
    telegram = config.get("telegram", {})
    info("📱 Telegram:")
    print(f"  Bot token: {'Set' if telegram.get('bot_token') else 'Not set'}")
    print(f"  Admins:    {len(telegram.get('admins', []))}")


@app.command(name="set-limit")
def set_general_limit(
    limit: int = typer.Option(..., "-l", "--limit", prompt=True, help="General IP limit"),
):
    """Set the general IP limit"""
    if limit < 1:
        error("Limit must be at least 1")

    run_command(lambda: write_settings({"general_limit": limit}))

    success(f"General limit set to {limit}")


@app.command(name="set-interval")
def set_check_interval(
    interval: int = typer.Option(..., "-i", "--interval", prompt=True, help="Check interval in seconds"),
):
    """Set the check interval"""
    if interval < 30:
        error("Interval must be at least 30 seconds")

    run_command(lambda: write_settings({"check_interval": interval}))

    success(f"Check interval set to {interval} seconds")


@app.command(name="set-reenable-time")
def set_reenable_time(
    time: int = typer.Option(..., "-t", "--time", prompt=True, help="Time to re-enable users in seconds"),
):
    """Set the time to automatically re-enable disabled users"""
    if time < 60:
        error("Time must be at least 60 seconds")

    run_command(lambda: write_settings({"time_to_active_users": time}))

    success(f"Re-enable time set to {time} seconds ({time // 60} minutes)")


@app.command(name="set-country")
def set_country_filter(
    code: Optional[str] = typer.Option(
        None, "-c", "--code", help="Country code to filter (e.g., IR, RU) or 'none' to disable"
    ),
):
    """Set or disable country code filter"""
    if code is None or code.lower() == "none":
        run_command(lambda: write_settings({"country_code": ""}))
        success("Country filter disabled")
    else:
        code = code.upper()
        run_command(lambda: write_settings({"country_code": code}))
        success(f"Country filter set to {code}")


@app.command(name="cleanup")
def cleanup_deleted_users():
    """Remove deleted users from limiter config (special limits, except users, disabled list)"""
    from rich.progress import Progress, SpinnerColumn, TextColumn

    from utils.types import PanelType
    from utils.panel_api import cleanup_deleted_users as do_cleanup

    async def run_cleanup():
        # Load config for panel data
        config = await read_settings()
        panel_data = PanelType.from_config(config)

        info("🔍 Fetching users from panel...")
        result = await do_cleanup(panel_data)
        return result

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
        ) as progress:
            progress.add_task(description="Cleaning up deleted users...", total=None)
            result = run_command(run_cleanup)

        if result is None:
            return

        total_removed = (
            len(result["special_limits_removed"])
            + len(result["except_users_removed"])
            + len(result["disabled_users_removed"])
            + len(result["user_groups_backup_removed"])
        )

        if total_removed == 0:
            success("✅ No deleted users found. Everything is clean!")
            return

        # Build result table
        table = Table(title="🧹 Cleanup Results")
        table.add_column("Category", style="cyan")
        table.add_column("Removed", style="green")
        table.add_column("Users", style="yellow")

        if result["special_limits_removed"]:
            users_str = ", ".join(result["special_limits_removed"][:5])
            if len(result["special_limits_removed"]) > 5:
                users_str += f" (+{len(result['special_limits_removed']) - 5} more)"
            table.add_row("Special Limits", str(len(result["special_limits_removed"])), users_str)

        if result["except_users_removed"]:
            users_str = ", ".join(result["except_users_removed"][:5])
            if len(result["except_users_removed"]) > 5:
                users_str += f" (+{len(result['except_users_removed']) - 5} more)"
            table.add_row("Except Users", str(len(result["except_users_removed"])), users_str)

        if result["disabled_users_removed"]:
            users_str = ", ".join(result["disabled_users_removed"][:5])
            if len(result["disabled_users_removed"]) > 5:
                users_str += f" (+{len(result['disabled_users_removed']) - 5} more)"
            table.add_row("Disabled Users", str(len(result["disabled_users_removed"])), users_str)

        if result["user_groups_backup_removed"]:
            users_str = ", ".join(result["user_groups_backup_removed"][:5])
            if len(result["user_groups_backup_removed"]) > 5:
                users_str += f" (+{len(result['user_groups_backup_removed']) - 5} more)"
            table.add_row("Groups Backup", str(len(result["user_groups_backup_removed"])), users_str)

        console.print(table)
        success(f"Total removed: {total_removed} user entries")

    except Exception as e:
        error(f"Cleanup failed: {e}")
