import asyncio
import os
import sys
import json
import time
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

from dotenv import load_dotenv
load_dotenv()

# Import migration system
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from migration_system.state_manager import StateManager, MigrationStatus, StepStatus
from migration_system.event_bus import EventBus, Event, EventCategory

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("monitor_bot")

try:
    from aiogram import Bot, Dispatcher, Router, F
    from aiogram.types import (
        Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
        BotCommand, ParseMode
    )
    from aiogram.filters import Command
    AIOGRAM_AVAILABLE = True
except ImportError:
    AIOGRAM_AVAILABLE = False
    print("Error: aiogram not installed. Run: pip install aiogram")
    sys.exit(1)

# ==================== CONFIG ====================
MONITOR_TOKEN = os.getenv("MONITOR_BOT_TOKEN")
ADMIN_ID = int(os.getenv("MONITOR_ADMIN_ID", os.getenv("ADMIN_ID", "0")))

if not MONITOR_TOKEN:
    logger.error("MONITOR_BOT_TOKEN not set in .env")
    sys.exit(1)

if not ADMIN_ID:
    logger.error("ADMIN_ID not set in .env")
    sys.exit(1)

# ==================== BOT ====================
bot = Bot(token=MONITOR_TOKEN)
router = Router()
dp = Dispatcher()
dp.include_router(router)

# State
state_manager = StateManager("migration_state.db")
event_bus = EventBus(state_manager_ref=state_manager)

# Active migration tracking
_active_migration_id: Optional[str] = None

# Console tracking
console_chat_id: Optional[int] = None
console_message_id: Optional[int] = None
console_last_update: float = 0

# ==================== EVENT HANDLERS ====================

def on_migration_event(event: Event) -> None:
    """Handle migration events."""
    global _active_migration_id
    
    if event.event_type in ("MIGRATION_STARTED", "MIGRATION_COMPLETED", "MIGRATION_FAILED"):
        if event.data and "migration_id" in event.data:
            _active_migration_id = event.data["migration_id"]
            logger.info(f"Active migration: {_active_migration_id}")

# Subscribe to events
event_bus.subscribe("MIGRATION_*", on_migration_event)
event_bus.subscribe("STEP_*", on_migration_event)

# ==================== COMMANDS ====================

@router.message(Command("start"))
async def cmd_start(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Admin only.")
        return
    
    await message.answer(
        "🤖 **Migration Monitor**\n\n"
        "I monitor migrations in real-time.\n\n"
        "**Commands:**\n"
        "/status - Current migration status\n"
        "/logs - Recent logs\n"
        "/history - Migration history\n"
        "/retry - Request retry\n"
        "/rollback - Request rollback\n"
        "/pause - Request pause\n"
        "/resume - Request resume\n"
        "/console - Live console output\n"
        "/health - Health check status\n"
        "/help - Show this message",
        parse_mode=ParseMode.MARKDOWN
    )

@router.message(Command("help"))
async def cmd_help(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Admin only.")
        return
    
    await message.answer(
        "📖 **Help**\n\n"
        "**/status** – Current migration status\n"
        "**/logs** – Recent logs\n"
        "**/history** – Complete migration history\n"
        "**/retry** – Request retry of failed step\n"
        "**/rollback** – Request emergency rollback\n"
        "**/pause** – Request pause\n"
        "**/resume** – Request resume\n"
        "**/console** – Live console output\n"
        "**/health** – System health check\n\n"
        "All commands are admin-only.",
        parse_mode=ParseMode.MARKDOWN
    )

@router.message(Command("status"))
async def cmd_status(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Admin only.")
        return
    
    status_text = get_migration_status()
    await message.answer(status_text, parse_mode=ParseMode.MARKDOWN)

@router.message(Command("logs"))
async def cmd_logs(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Admin only.")
        return
    
    logs = get_recent_logs()
    if len(logs) > 4000:
        # Split into multiple messages
        for i in range(0, len(logs), 4000):
            await message.answer(logs[i:i+4000], parse_mode=ParseMode.MARKDOWN)
    else:
        await message.answer(logs, parse_mode=ParseMode.MARKDOWN)

@router.message(Command("history"))
async def cmd_history(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Admin only.")
        return
    
    history = get_migration_history()
    await message.answer(history, parse_mode=ParseMode.MARKDOWN)

@router.message(Command("console"))
async def cmd_console(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Admin only.")
        return
    
    global console_chat_id, console_message_id
    
    console_chat_id = message.chat.id
    msg = await message.answer("📡 **Console Starting...**", parse_mode=ParseMode.MARKDOWN)
    console_message_id = msg.message_id
    
    asyncio.create_task(live_console_loop())

@router.message(Command("health"))
async def cmd_health(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Admin only.")
        return
    
    health = get_system_health()
    await message.answer(health, parse_mode=ParseMode.MARKDOWN)

@router.message(Command("retry"))
async def cmd_retry(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Admin only.")
        return
    
    # Send event (migration manager listens)
    event_bus.publish(Event(
        event_type="RETRY_REQUESTED",
        data={"migration_id": _active_migration_id, "requested_by": message.from_user.id},
        source="monitor_bot",
        category=EventCategory.SYSTEM
    ))
    await message.answer("🔄 Retry requested.")

@router.message(Command("rollback"))
async def cmd_rollback(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Admin only.")
        return
    
    # Confirm
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm", callback_data="rollback_confirm"),
                InlineKeyboardButton(text="❌ Cancel", callback_data="rollback_cancel"),
            ]
        ]
    )
    
    await message.answer(
        "⚠️ **EMERGENCY ROLLBACK**\n\n"
        "This will request rollback of the current migration.\n"
        "Source VPS will NOT be affected.\n\n"
        "Are you sure?",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )

@router.message(Command("pause"))
async def cmd_pause(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Admin only.")
        return
    
    event_bus.publish(Event(
        event_type="PAUSE_REQUESTED",
        data={"migration_id": _active_migration_id},
        source="monitor_bot",
        category=EventCategory.SYSTEM
    ))
    await message.answer("⏸️ Pause requested.")

@router.message(Command("resume"))
async def cmd_resume(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Admin only.")
        return
    
    event_bus.publish(Event(
        event_type="RESUME_REQUESTED",
        data={"migration_id": _active_migration_id},
        source="monitor_bot",
        category=EventCategory.SYSTEM
    ))
    await message.answer("▶️ Resume requested.")

# ==================== CALLBACKS ====================

@router.callback_query(F.data == "rollback_confirm")
async def rollback_confirm(callback: CallbackQuery):
    await callback.answer("Rollback requested...")
    
    event_bus.publish(Event(
        event_type="ROLLBACK_REQUESTED",
        data={"migration_id": _active_migration_id},
        source="monitor_bot",
        category=EventCategory.ROLLBACK
    ))
    
    await callback.message.edit_text(
        "🔄 Rollback requested. Check /status for progress.",
        parse_mode=ParseMode.MARKDOWN
    )

@router.callback_query(F.data == "rollback_cancel")
async def rollback_cancel(callback: CallbackQuery):
    await callback.answer("Cancelled")
    await callback.message.edit_text("❌ Rollback cancelled")

# ==================== CORE FUNCTIONS ====================

def get_migration_status() -> str:
    """Get formatted migration status from StateManager."""
    global _active_migration_id
    
    # If no active migration, show latest
    if not _active_migration_id:
        migrations = state_manager.list_migrations(limit=1)
        if migrations:
            _active_migration_id = migrations[0].migration_id
        else:
            return "📊 No migrations found."
    
    migration = state_manager.get_migration(_active_migration_id)
    if not migration:
        return f"⚠️ Migration {_active_migration_id} not found."
    
    steps = state_manager.get_steps(_active_migration_id)
    
    text = "📊 **Migration Status**\n\n"
    text += f"ID: `{migration.migration_id}`\n"
    text += f"Bot: {migration.bot_name}\n"
    text += f"Status: **{migration.status.value.upper()}**\n"
    text += f"From: {migration.source_vps}\n"
    text += f"To: {migration.destination_vps}\n"
    
    if migration.started_at:
        text += f"Started: {time.ctime(migration.started_at)}\n"
    if migration.completed_at:
        text += f"Completed: {time.ctime(migration.completed_at)}\n"
    
    if migration.total_steps > 0:
        text += f"\n**Progress**\n"
        text += f"Steps: {migration.completed_steps}/{migration.total_steps}\n"
        text += f"Failed: {migration.failed_steps}\n"
        text += f"Healed: {migration.healed_steps}\n"
        
        # Progress bar
        if migration.total_steps > 0:
            pct = int((migration.completed_steps / migration.total_steps) * 20)
            bar = "█" * pct + "░" * (20 - pct)
            text += f"`[{bar}]` {int((migration.completed_steps / migration.total_steps) * 100)}%\n"
    
    # Recent steps
    if steps:
        text += f"\n**Recent Steps**\n"
        for s in steps[-5:]:
            icon = "✅" if s.status == StepStatus.SUCCEEDED else "❌" if s.status == StepStatus.FAILED else "⏳"
            text += f"{icon} {s.step_name}: {s.status.value}\n"
    
    return text

def get_recent_logs() -> str:
    """Get recent journal entries."""
    global _active_migration_id
    
    if not _active_migration_id:
        return "📭 No active migration."
    
    journal = state_manager.get_journal(_active_migration_id, limit=50)
    if not journal:
        return "📭 No logs available."
    
    text = "📋 **Recent Logs**\n\n```\n"
    for entry in reversed(journal):
        timestamp = time.ctime(entry["timestamp"])
        step = f"[{entry['step_name']}] " if entry['step_name'] else ""
        text += f"{timestamp} | {entry['level']} | {step}{entry['message']}\n"
    text += "```"
    
    return text[:4096]  # Telegram limit

def get_migration_history() -> str:
    """Get complete migration history."""
    migrations = state_manager.list_migrations(limit=10)
    if not migrations:
        return "📜 No migration history."
    
    text = "📜 **Migration History**\n\n"
    for m in migrations:
        status_icon = "✅" if m.status == MigrationStatus.SUCCEEDED else "❌" if m.status in (MigrationStatus.FAILED, MigrationStatus.ROLLED_BACK) else "⏳"
        text += f"{status_icon} `{m.migration_id}`\n"
        text += f"   Bot: {m.bot_name}\n"
        text += f"   Status: {m.status.value}\n"
        text += f"   Steps: {m.completed_steps}/{m.total_steps}\n"
        if m.started_at:
            text += f"   Started: {time.ctime(m.started_at)}\n"
        text += "\n"
    
    return text

def get_system_health() -> str:
    """Get system health status."""
    text = "🏥 **System Health**\n\n"
    
    # Check StateManager
    try:
        migrations = state_manager.list_migrations(limit=1)
        text += "✅ StateManager: Connected\n"
        text += f"   Migrations: {len(state_manager.list_migrations(limit=100))}\n"
    except Exception as e:
        text += f"❌ StateManager: Error - {e}\n"
    
    # Check Event Bus
    text += "✅ Event Bus: Active\n"
    
    # Check active migration
    if _active_migration_id:
        text += f"\n🔄 Active Migration: {_active_migration_id}\n"
        migration = state_manager.get_migration(_active_migration_id)
        if migration:
            text += f"   Status: {migration.status.value}\n"
            steps = state_manager.get_steps(_active_migration_id)
            text += f"   Steps: {len(steps)}\n"
    
    return text

# ==================== CONSOLE ====================

async def live_console_loop():
    """Live console updates."""
    global console_chat_id, console_message_id, console_last_update
    
    while True:
        try:
            if console_chat_id is None or console_message_id is None:
                break
            
            # Get latest logs
            logs = get_recent_logs()
            
            if time.time() - console_last_update > 2:
                await bot.edit_message_text(
                    logs,
                    chat_id=console_chat_id,
                    message_id=console_message_id,
                    parse_mode=ParseMode.MARKDOWN
                )
                console_last_update = time.time()
            
            await asyncio.sleep(2)
            
        except Exception as e:
            logger.error(f"Console error: {e}")
            await asyncio.sleep(5)

# ==================== MAIN ====================

async def main():
    """Run the monitor bot."""
    await bot.set_my_commands([
        BotCommand(command="start", description="Start monitor"),
        BotCommand(command="status", description="Migration status"),
        BotCommand(command="logs", description="Recent logs"),
        BotCommand(command="history", description="Migration history"),
        BotCommand(command="retry", description="Request retry"),
        BotCommand(command="rollback", description="Request rollback"),
        BotCommand(command="pause", description="Request pause"),
        BotCommand(command="resume", description="Request resume"),
        BotCommand(command="console", description="Live console"),
        BotCommand(command="health", description="System health"),
        BotCommand(command="help", description="Help"),
    ])
    
    logger.info("Monitor Bot started!")
    print("🤖 Monitor Bot is running...")
    
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Monitor Bot error: {e}")
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())