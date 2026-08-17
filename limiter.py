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
from utils.handel_dis_users import DisabledUsers
from utils.logs import get_logger, log_startup_info, log_shutdown_info, log_crash_info
from utils.panel_api import enable_selected_users, get_nodes
from utils.parse_logs import close_geo_client
from utils.read_config import read_config
from utils.types import PanelType

# Import Redis cache utilities
try:
    from utils.redis_cache import get_cache, close_cache
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

VERSION = "0.9.8"

# Main logger
main_logger = get_logger("limiter.main")

parser = argparse.ArgumentParser(
    description="Limiter - IP connection limiter for PasarGuard panel"
)
parser.add_argument("--version", action="version", version=f"Limiter v{VERSION}")
args = parser.parse_args()

dis_obj = DisabledUsers()


async def main():
    """Main function to run the limiter."""
    log_startup_info("Limiter", f"v{VERSION}")
    main_logger.info(f"🚀 Starting Limiter v{VERSION}")
    main_logger.info("=" * 50)
    
    # Ensure database tables and columns are initialized
    from db.database import init_db
    try:
        await init_db()
        main_logger.info("✓ Database initialized")
    except Exception as db_err:
        main_logger.error(f"Database initialization error: {db_err}")
    
    # Initialize Redis cache
    if REDIS_AVAILABLE:
        try:
            cache = await get_cache()
            if cache.is_connected:
                main_logger.info("✓ Redis cache connected")
            else:
                main_logger.info("⚠ Redis not available, using in-memory cache fallback")
        except Exception as e:
            main_logger.warning(f"Redis initialization failed: {e}, using in-memory fallback")
    else:
        main_logger.info("ℹ Redis cache module not available, using in-memory cache")
    
    # Start Telegram bot in background task
    main_logger.debug("Starting Telegram bot task...")
    asyncio.create_task(run_telegram_bot())
    await asyncio.sleep(2)
    main_logger.info("✓ Telegram bot started")

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
    
    # Get available nodes
    main_logger.debug("Fetching available nodes...")
    await get_nodes(panel_data)
    
    async with asyncio.TaskGroup() as tg:
        await asyncio.sleep(2)
        
        # Start Redis Pub/Sub listener for L1/L2 cache invalidations
        if REDIS_AVAILABLE:
            from utils.redis_cache import start_pubsub_listener
            tg.create_task(start_pubsub_listener(), name="redis_pubsub")
            main_logger.info("✓ Redis Pub/Sub listener registered in TaskGroup")

        # Start unknown user background worker
        from utils.user_sync import run_unknown_user_worker
        tg.create_task(run_unknown_user_worker(panel_data), name="unknown_user_worker")
        main_logger.info("✓ Unknown user worker registered in TaskGroup")

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
        
        from utils.user_sync import refresh_user_metadata_cache
        await refresh_user_metadata_cache()
        
        await run_check_users_usage(panel_data)


async def cleanup_resources():
    """Gracefully close all shared network clients, Redis, and database connections."""
    # 1. Close GeoIP client
    try:
        await close_geo_client()
        main_logger.debug("✓ GeoIP client closed")
    except Exception as e:
        main_logger.debug(f"Error closing Geo client: {e}")

    # 2. Close Redis connection
    if REDIS_AVAILABLE:
        try:
            await close_cache()
            main_logger.info("✓ Redis cache closed")
        except Exception as e:
            main_logger.debug(f"Error closing Redis cache: {e}")

    # 3. Close database connections
    try:
        from db.database import close_db
        await close_db()
    except Exception as e:
        main_logger.debug(f"Error closing DB: {e}")


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
            
            if restart_count >= max_restarts:
                main_logger.error(f"Maximum restart attempts ({max_restarts}) reached")
                main_logger.error("Please check the logs and fix the issue")
                try:
                    asyncio.run(cleanup_resources())
                except Exception:
                    pass
                break
            
            # Exponential backoff for restarts
            delay = min(10 * (2 ** (restart_count - 1)), 120)
            main_logger.info(f"⏳ Restart #{restart_count}/{max_restarts} in {delay} seconds...")
            time.sleep(delay)
