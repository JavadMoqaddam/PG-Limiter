"""
Limiter - IP connection limiter for PasarGuard panel.
Monitors active connections and limits users based on their IP count.
"""

import argparse
import asyncio
import sys
import time

from run_telegram import run_telegram_bot
from telegram_bot.send_message import send_logs
from utils.check_usage import run_check_users_usage
from utils.get_logs import (
    TASKS,
    check_and_add_new_nodes,
    create_node_task,
    handle_cancel,
    handle_cancel_all,
    init_node_status_message,
)
from utils.logs import get_logger, log_startup_info, log_shutdown_info, log_crash_info
from utils.panel_api import get_nodes
from utils.parse_logs import close_geo_client
from utils.read_config import read_config
from utils.types import PanelType

VERSION = "1.3.1"

# Main logger
main_logger = get_logger("limiter.main")

# Strong references container for root background tasks
_BACKGROUND_TASKS: set[asyncio.Task] = set()

parser = argparse.ArgumentParser(
    description="Limiter - IP connection limiter for PasarGuard panel"
)
parser.add_argument("--version", action="version", version=f"Limiter v{VERSION}")
args = parser.parse_args()


async def _supervise(name: str, factory, restart_delay: float = 10.0) -> None:
    """
    Keep a long-running background coroutine alive and, above all, audible.

    The Telegram poller and the message dispatcher live outside the main
    TaskGroup on purpose: enforcement has to survive a Telegram outage. The
    price is that a crash there used to be invisible - the task simply vanished
    and its exception was never retrieved - which is exactly how the bot can go
    silent while the limiter keeps banning users.
    """
    attempt = 0
    while True:
        try:
            await factory()
            main_logger.warning(f"⚠️ Background task '{name}' returned unexpectedly, restarting")
        except asyncio.CancelledError:
            main_logger.debug(f"Background task '{name}' cancelled")
            raise
        except Exception as error:  # pylint: disable=broad-except
            attempt += 1
            main_logger.error(
                f"❌ Background task '{name}' crashed (#{attempt}): {error}", exc_info=True
            )
        await asyncio.sleep(min(restart_delay * max(1, min(attempt, 6)), 120.0))


async def _telegram_watchdog(interval: float = 60.0) -> None:
    """
    Restart Telegram polling if it stops without taking the process down.

    ``run_telegram_bot()`` returns as soon as polling has been started, so any
    later failure inside python-telegram-bot's updater leaves the bot
    unreachable while every other loop keeps running - the operator sees a dead
    bot and a healthy log. This checks the actual updater state instead.
    """
    from telegram_bot import main as bot_main

    healthy = True
    while True:
        await asyncio.sleep(interval)
        try:
            application = getattr(bot_main, "application", None)
            updater = getattr(application, "updater", None)
            if application is not None and application.running and updater is not None and updater.running:
                if not healthy:
                    main_logger.info("✅ Telegram polling is back up")
                    healthy = True
                continue

            healthy = False
            main_logger.error(
                f"❌ Telegram polling is down (application={getattr(application, 'running', None)}, "
                f"updater={getattr(updater, 'running', None)}), attempting restart"
            )
            if application is None:
                continue

            if not application.running:
                await application.initialize()
                await application.start()
            if updater is not None and not updater.running:
                await updater.start_polling(
                    allowed_updates=["message", "callback_query"],
                    drop_pending_updates=True,
                )
            try:
                from telegram_bot.dispatcher import get_dispatcher
                get_dispatcher().set_bot(application.bot)
            except Exception as inject_error:  # pylint: disable=broad-except
                main_logger.debug(f"Dispatcher bot re-injection note: {inject_error}")
            main_logger.info("✓ Telegram polling restarted by watchdog")
        except Exception as error:  # pylint: disable=broad-except
            main_logger.error(
                f"❌ Telegram watchdog could not restore polling: {error}. "
                f"A container restart is required.",
                exc_info=True,
            )


async def main():
    """Main function to run the limiter."""
    log_startup_info("Limiter", f"v{VERSION}")
    main_logger.info(f"🚀 Starting Limiter v{VERSION}")
    main_logger.info("=" * 50)
    
    try:
        # Ensure database tables and columns are initialized
        from db.database import init_db
        try:
            await init_db()
            main_logger.info("✓ Database initialized")
        except Exception as db_err:
            main_logger.error(f"Database initialization error: {db_err}")
        
        # Start Telegram bot and message dispatcher in background tasks (keep strong references)
        main_logger.debug("Starting Telegram bot and dispatcher tasks...")
        from telegram_bot.dispatcher import get_dispatcher
        dispatcher = get_dispatcher()
        dispatcher_task = asyncio.create_task(
            _supervise("dispatcher_worker", dispatcher.start_worker), name="dispatcher_worker"
        )
        bot_task = asyncio.create_task(run_telegram_bot(), name="telegram_bot_runner")
        _BACKGROUND_TASKS.add(dispatcher_task)
        _BACKGROUND_TASKS.add(bot_task)
        dispatcher_task.add_done_callback(_BACKGROUND_TASKS.discard)
        bot_task.add_done_callback(_BACKGROUND_TASKS.discard)

        def _log_bot_task_failure(task: asyncio.Task) -> None:
            """Surface a crash in the bot starter instead of swallowing it."""
            if task.cancelled():
                return
            error = task.exception()
            if error is not None:
                main_logger.error(f"❌ Telegram bot starter failed: {error}", exc_info=error)

        bot_task.add_done_callback(_log_bot_task_failure)
        await asyncio.sleep(2)
        main_logger.info("✓ Telegram bot and dispatcher started")

        # Load configuration
        main_logger.debug("Loading configuration...")
        while True:
            try:
                config_file = await read_config(check_required_elements=True)
                main_logger.info("✓ Configuration loaded successfully")
                break
            except ValueError as error:
                main_logger.error(f"Configuration error: {error}")
                await send_logs(f"<code>{error}</code>")
                await send_logs(
                    "Please configure the required settings:\n"
                    "/create_config - Panel credentials\n"
                    "/set_general_limit_number - Default IP limit\n"
                    "/set_check_interval - Check interval\n"
                    "/set_time_to_active_users - Re-enable timeout\n\n"
                    "Retrying in <b>60 seconds</b>..."
                )
                await asyncio.sleep(60)
        
        # Initialize panel connection
        panel_data = PanelType(
            config_file["panel"]["username"],
            config_file["panel"]["password"],
            config_file["panel"]["domain"],
        )
        main_logger.info(f"✓ Panel configured: {config_file['panel']['domain']}")
        
        # Preserve disabled users across restarts (enable_dis_user background loop handles expired bans)
        main_logger.info("✓ Disabled users state preserved across restarts")
        
        async with asyncio.TaskGroup() as tg:
            # Start unknown user background worker
            from utils.user_sync import run_unknown_user_worker
            tg.create_task(run_unknown_user_worker(panel_data), name="unknown_user_worker")
            main_logger.info("✓ Unknown user worker registered in TaskGroup")

            tg.create_task(_telegram_watchdog(), name="telegram_watchdog")
            main_logger.info("✓ Telegram polling watchdog registered in TaskGroup")

            main_logger.debug("Fetching available nodes...")
            nodes_list = await get_nodes(panel_data)
            
            if nodes_list and not isinstance(nodes_list, ValueError):
                await init_node_status_message(nodes_list)
                connected_nodes = [n for n in nodes_list if n.status == "connected"]
                main_logger.info(f"🖥️ Found {len(nodes_list)} nodes ({len(connected_nodes)} connected)")
                
                for node in nodes_list:
                    if node.status == "connected":
                        main_logger.debug(f"Connecting to node: {node.node_name} (id={node.node_id})")
                        await create_node_task(panel_data, tg, node)
                
                main_logger.info(f"✓ Connected to {len(connected_nodes)} nodes")
            else:
                main_logger.warning("No nodes available or error fetching nodes")

            # Start background management tasks
            main_logger.info("🔄 Starting background tasks...")
            tg.create_task(check_and_add_new_nodes(panel_data, tg), name="add_new_nodes")
            tg.create_task(handle_cancel(panel_data, TASKS), name="cancel_disable_nodes")
            tg.create_task(handle_cancel_all(TASKS, panel_data, tg), name="cancel_all")
            
            from utils.panel_api import enable_dis_user
            tg.create_task(enable_dis_user(panel_data), name="enable_disabled_users")
            
            from utils.user_sync import run_user_sync_loop
            tg.create_task(run_user_sync_loop(panel_data), name="user_sync")
            
            main_logger.info("✓ All background tasks registered in TaskGroup")
            main_logger.info("=" * 50)
            main_logger.info("🟢 Limiter is now running and monitoring connections")
            
            from utils.user_sync import refresh_user_metadata_cache, recompute_all_user_limits
            await refresh_user_metadata_cache()
            await recompute_all_user_limits()
            
            await run_check_users_usage(panel_data)
    finally:
        await cleanup_resources()


async def cleanup_resources():
    """Gracefully close all shared network clients and database connections."""
    # 1. Close GeoIP client
    try:
        await close_geo_client()
        main_logger.debug("✓ GeoIP client closed")
    except Exception as e:
        main_logger.debug(f"Error closing Geo client: {e}")

    # 2. Close database connections
    try:
        from db.database import close_db
        await close_db()
    except Exception as e:
        main_logger.debug(f"Error closing DB: {e}")

    # 3. Close Panel API shared HTTP client
    try:
        from utils.panel_api.request_helper import close_panel_client
        await close_panel_client()
        main_logger.debug("✓ Panel HTTP client closed")
    except Exception as e:
        main_logger.debug(f"Error closing Panel client: {e}")

    # 4. Stop Telegram Dispatcher cleanly
    try:
        from telegram_bot.dispatcher import get_dispatcher
        dispatcher = get_dispatcher()
        await dispatcher.stop(wait_seconds=3.0)
        main_logger.debug("✓ Telegram Dispatcher stopped")
    except Exception as e:
        main_logger.debug(f"Error stopping Dispatcher: {e}")

    # 5. Stop the Telegram application so a restarted event loop does not find
    #    an application that still reports itself as running (its polling tasks
    #    would belong to the dead loop and the bot would never answer again).
    try:
        from telegram_bot.main import application

        if application is not None:
            if getattr(application, "updater", None) is not None and application.updater.running:
                await application.updater.stop()
            if application.running:
                await application.stop()
            await application.shutdown()
            main_logger.debug("✓ Telegram application shut down")
    except Exception as e:
        main_logger.debug(f"Error shutting down Telegram application: {e}")


if __name__ == "__main__":
    restart_count = 0
    max_restarts = 5
    
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            main_logger.info("🛑 Received keyboard interrupt, shutting down...")
            try:
                asyncio.run(cleanup_resources())
            except Exception:
                pass
            log_shutdown_info("Limiter", "Keyboard interrupt")
            break
        except SystemExit as e:
            if e.code != 0 and e.code is not None:
                main_logger.error(f"System exit with code: {e.code}")
            try:
                asyncio.run(cleanup_resources())
            except Exception:
                pass
            break
        except Exception as er:  # pylint: disable=broad-except
            restart_count += 1
            exc_type, exc_value, exc_tb = sys.exc_info()
            
            # Use centralized crash logging
            log_crash_info(exc_type, exc_value, exc_tb, component="Limiter")
            log_shutdown_info("Limiter", f"Error: {er}")
            
            try:
                asyncio.run(cleanup_resources())
            except Exception:
                pass

            if restart_count >= max_restarts:
                main_logger.error(f"Maximum restart attempts ({max_restarts}) reached")
                main_logger.error("Please check the logs and fix the issue")
                break
            
            # Exponential backoff for restarts
            delay = min(10 * (2 ** (restart_count - 1)), 120)
            main_logger.info(f"⏳ Restart #{restart_count}/{max_restarts} in {delay} seconds...")
            time.sleep(delay)
