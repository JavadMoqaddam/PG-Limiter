"""
Telegram bot runner module.
This module provides the function to run the Telegram bot in the background.
"""

import asyncio
from datetime import timedelta
import os
import json
from telegram.ext import ApplicationBuilder
from utils.logs import get_logger

# Module logger
telegram_runner_logger = get_logger("telegram.runner")


async def run_telegram_bot():
    """
    Run the Telegram bot in polling mode.
    This function starts the bot and keeps it running to receive updates.
    """
    # Import telegram bot main module first
    from telegram_bot import main as bot_main
    
    telegram_runner_logger.info("🤖 Initializing Telegram bot...")
    
    # Never log any part of the token itself: the id half is public but the 35-char
    # secret half is not, and `token[:15]` at INFO leaked four characters of it into
    # every normal log the operator shares.
    telegram_runner_logger.debug(f"Bot token configured: {bool(bot_main.bot_token)}")

    # Check if application was already created with valid token at module import
    if bot_main.bot_token and bot_main.bot_token != "0000000000:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX":
        # Token was loaded successfully at import time
        application = bot_main.application
        telegram_runner_logger.info(
            f"✓ Bot token loaded (id={bot_main.bot_token.split(':', 1)[0]}, secret redacted)"
        )
    else:
        # Raise rather than return: this used to end the coroutine normally, so the
        # task completed "successfully" and the limiter ran on with a mute bot while
        # it kept banning users. The task lives outside the TaskGroup, so raising
        # here cannot stop enforcement - it only makes the failure visible.
        telegram_runner_logger.error("✗ Bot token not found!")
        telegram_runner_logger.error("Please set BOT_TOKEN in your environment or config")
        raise RuntimeError("BOT_TOKEN is missing or still the placeholder value")
    
    # Initialize the application
    try:
        # Check if already running
        if application.running:
            telegram_runner_logger.info("✓ Telegram bot is already running!")
            return
        
        # Both start calls are bounded on purpose. _telegram_watchdog in limiter.py
        # wakes 60s after registration and, finding updater.running still False,
        # would issue a second start_polling on the same updater - two getUpdates
        # loops, which Telegram answers with HTTP 409. A hang here can no longer
        # outlive the watchdog's first check.
        telegram_runner_logger.debug("Initializing application...")
        async with asyncio.timeout(30):
            await application.initialize()

        telegram_runner_logger.debug("Starting application...")
        async with asyncio.timeout(30):
            await application.start()

        # Register initialized bot instance with Dispatcher
        try:
            from telegram_bot.dispatcher import get_dispatcher
            get_dispatcher().set_bot(application.bot)
            telegram_runner_logger.debug("✓ Dispatcher bot instance synchronized")
        except Exception as disp_err:
            telegram_runner_logger.debug(f"Dispatcher sync note: {disp_err}")
        
        # Start polling for updates
        telegram_runner_logger.info("🔄 Starting polling for updates...")
        async with asyncio.timeout(30):
            await application.updater.start_polling(
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=True,  # Ignore old updates
            )
        
        telegram_runner_logger.info("✓ Telegram bot started successfully!")
        telegram_runner_logger.info("✓ Bot is now polling for updates")
        
        # Get bot info to confirm connection
        try:
            bot_info = await application.bot.get_me()
            telegram_runner_logger.info(f"✓ Connected as @{bot_info.username} (ID: {bot_info.id})")
        except Exception as e:
            telegram_runner_logger.warning(f"Could not get bot info: {e}")
        
        # Schedule automatic backup based on config
        try:
            from telegram_bot.handlers.backup import (
                send_automatic_backup,
                get_auto_backup_config,
            )
            
            job_queue = application.job_queue
            if job_queue:
                config = await asyncio.to_thread(get_auto_backup_config)
                if config.get("enabled", True):
                    interval_hours = config.get("interval_hours", 1)
                    async def _auto_backup_job(context):
                        await send_automatic_backup()

                    job_queue.run_repeating(
                        _auto_backup_job,
                        interval=timedelta(hours=interval_hours),
                        first=timedelta(hours=interval_hours),
                        name="automatic_backup"
                    )
                    telegram_runner_logger.info(f"✓ Automatic backup scheduled (every {interval_hours} hour(s))")
                else:
                    telegram_runner_logger.info("✓ Automatic backup is disabled")
            else:
                telegram_runner_logger.warning("⚠️ Job queue not available, automatic backup disabled")
        except Exception as e:
            telegram_runner_logger.warning(f"⚠️ Could not schedule automatic backup: {e}")
            
    except RuntimeError as e:
        if "already running" in str(e).lower():
            telegram_runner_logger.info("✓ Telegram bot is already running!")
            return
        telegram_runner_logger.error(f"✗ Failed to start Telegram bot: {e}")
        import traceback
        telegram_runner_logger.debug(f"Traceback:\n{traceback.format_exc()}")
        raise
    except Exception as e:
        # Re-raised so the failure is real. Swallowing it here ended the coroutine
        # normally, so limiter.py's done-callback saw no exception and the operator
        # got a healthy-looking log while the bot was unreachable and enforcement
        # kept disabling users. The task sits outside the TaskGroup, so raising
        # cannot stop enforcement; the watchdog retries every 60s.
        telegram_runner_logger.error(f"✗ Failed to start Telegram bot: {e}")
        telegram_runner_logger.error("Please verify your BOT_TOKEN is correct")
        import traceback
        telegram_runner_logger.debug(f"Traceback:\n{traceback.format_exc()}")
        raise
