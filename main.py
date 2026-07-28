# main.py - DEVELOPED BY @DEADxEVIL
import asyncio
import re
import os
import sys
import hashlib
import json
import threading
import time
from datetime import datetime
from dotenv import load_dotenv
import logging
from html import escape

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# Import aiogram
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand, MessageEntity
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode

# Import migration system
try:
    from migration_system import MigrationManager, MigrationPlan, MigrationStep, SSHCredentials
    MIGRATION_AVAILABLE = True
except ImportError:
    MIGRATION_AVAILABLE = False
    logger.warning("Migration system not available")

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
SIGNATURE = os.getenv("SIGNATURE", "📝 Powered by @YourBot")
MAX_QUEUE = 500

if not BOT_TOKEN or not ADMIN_ID or not CHANNEL_ID:
    logger.error("Missing required env vars")
    sys.exit(1)

# ==================== HELPER FUNCTIONS ====================
def format_text_with_entities(text: str, entities: list) -> str:
    """Convert Telegram entities to HTML for preserving clickable links"""
    if not text or not entities:
        return escape(text) if text else ""
    
    sorted_entities = sorted(entities, key=lambda e: e.offset, reverse=True)
    html_text = escape(text)
    result = list(html_text)
    
    for entity in sorted_entities:
        start = entity.offset
        end = entity.offset + entity.length
        html_start = len(escape(text[:start]))
        html_end = len(escape(text[:end]))
        
        if entity.type == "url":
            url = text[start:end]
            tag = f'<a href="{url}">{escape(url)}</a>'
            result[html_start:html_end] = [tag]
        elif entity.type == "text_link":
            url = entity.url
            link_text = text[start:end]
            tag = f'<a href="{url}">{escape(link_text)}</a>'
            result[html_start:html_end] = [tag]
        elif entity.type == "bold":
            tag = f"<b>{''.join(result[html_start:html_end])}</b>"
            result[html_start:html_end] = [tag]
        elif entity.type == "italic":
            tag = f"<i>{''.join(result[html_start:html_end])}</i>"
            result[html_start:html_end] = [tag]
        elif entity.type == "underline":
            tag = f"<u>{''.join(result[html_start:html_end])}</u>"
            result[html_start:html_end] = [tag]
        elif entity.type == "strikethrough":
            tag = f"<s>{''.join(result[html_start:html_end])}</s>"
            result[html_start:html_end] = [tag]
        elif entity.type == "code":
            tag = f"<code>{''.join(result[html_start:html_end])}</code>"
            result[html_start:html_end] = [tag]
        elif entity.type == "pre":
            tag = f"<pre>{''.join(result[html_start:html_end])}</pre>"
            result[html_start:html_end] = [tag]
    
    return ''.join(result)

# ==================== DATA STORAGE ====================
class MessageStore:
    def __init__(self):
        self.messages = []
        self.duplicates_count = 0
        self.duplicate_hashes = set()
        self.processing = False
        self.stop_processing = False
        self.sequence_patterns = [
            r'(?:part|episode|#|lecture|lesson|video|media)\s*(\d+)',
            r'(\d+)[\.\)\:]',
            r'\[(\d+)\]',
        ]
    
    def get_hash(self, msg_data: dict) -> str:
        content = f"{msg_data.get('type')}_{msg_data.get('file_id', '')}_{msg_data.get('text', '')}"
        return hashlib.md5(content.encode()).hexdigest()
    
    def extract_sequence(self, text: str) -> int:
        if not text:
            return 999999
        for pattern in self.sequence_patterns:
            match = re.search(pattern, text.lower())
            if match:
                try:
                    return int(match.group(1))
                except:
                    continue
        return 999999
    
    def add_message(self, msg_data: dict) -> tuple:
        if len(self.messages) >= MAX_QUEUE:
            return False, f"Queue full! Max {MAX_QUEUE}", len(self.messages)
        
        msg_hash = self.get_hash(msg_data)
        if msg_hash in self.duplicate_hashes:
            self.duplicates_count += 1
            return False, f"Duplicate!", self.duplicates_count
        
        text = msg_data.get('text') or msg_data.get('caption') or ''
        seq = self.extract_sequence(text)
        
        msg_data['sequence'] = seq
        msg_data['original_text'] = text
        msg_data['edited_text'] = text
        msg_data['hash'] = msg_hash
        
        self.messages.append(msg_data)
        self.duplicate_hashes.add(msg_hash)
        self.sort_messages()
        
        return True, f"Added! Seq: {seq if seq != 999999 else 'No number'}", len(self.messages)
    
    def sort_messages(self):
        self.messages.sort(key=lambda x: x['sequence'])
    
    def edit_message(self, index: int, new_text: str):
        if 0 <= index < len(self.messages):
            self.messages[index]['edited_text'] = new_text
            self.messages[index]['html_text'] = None
            return True
        return False
    
    def edit_all(self, new_text: str):
        count = 0
        for i in range(len(self.messages)):
            if self.edit_message(i, new_text):
                count += 1
        return count
    
    def remove(self, index: int):
        if 0 <= index < len(self.messages):
            if 'hash' in self.messages[index]:
                self.duplicate_hashes.discard(self.messages[index]['hash'])
            del self.messages[index]
            self.sort_messages()
            return True
        return False
    
    def get_all(self):
        return self.messages
    
    def get_count(self):
        return len(self.messages)
    
    def clear(self):
        self.messages = []
        self.duplicate_hashes.clear()
        self.duplicates_count = 0
    
    def start_processing(self):
        self.processing = True
        self.stop_processing = False
    
    def stop(self):
        self.stop_processing = True
    
    def should_stop(self):
        return self.stop_processing

store = MessageStore()

# ==================== STATES ====================
class EditStates(StatesGroup):
    waiting_index = State()
    waiting_text = State()
    waiting_all = State()

class MigrationStates(StatesGroup):
    waiting_source_host = State()
    waiting_source_user = State()
    waiting_source_password = State()
    waiting_dest_host = State()
    waiting_dest_user = State()
    waiting_dest_password = State()
    waiting_confirm = State()

# ==================== KEYBOARDS ====================
def main_keyboard():
    buttons = [
        [KeyboardButton(text="📋 View Queue"), KeyboardButton(text="📊 Stats")],
        [KeyboardButton(text="✏️ Edit Single"), KeyboardButton(text="✏️ Edit All")],
        [KeyboardButton(text="🗑️ Clear Queue"), KeyboardButton(text="❌ Remove One")],
        [KeyboardButton(text="🚀 Publish"), KeyboardButton(text="⏹️ Stop")],
        [KeyboardButton(text="🔄 Migrate Bot"), KeyboardButton(text="📈 Migration Status")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def publish_keyboard():
    buttons = [
        [InlineKeyboardButton(text="✅ Confirm Publish", callback_data="publish")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ==================== MIGRATION INTEGRATION ====================
_migration_manager = None
_migration_thread = None
_migration_running = False

def create_migration_plan(source_host, source_user, source_pass, 
                         dest_host, dest_user, dest_pass,
                         bot_name="publisher_bot"):
    """Create migration plan from user input."""
    if not MIGRATION_AVAILABLE:
        return None
    
    project_dir = os.getcwd()
    
    # Read .env for bot config
    env_vars = {}
    if os.path.exists('.env'):
        with open('.env', 'r') as f:
            for line in f:
                if '=' in line and not line.startswith('#'):
                    key, value = line.strip().split('=', 1)
                    env_vars[key] = value
    
    source = SSHCredentials(
        host=source_host,
        port=22,
        username=source_user,
        password=source_pass,
    )
    
    destination = SSHCredentials(
        host=dest_host,
        port=22,
        username=dest_user,
        password=dest_pass,
    )
    
    # Create steps for migration
    steps = [
        MigrationStep(
            name="create_project_dir",
            command=f"mkdir -p /opt/{bot_name}",
            use_sudo=True,
            timeout=30,
            critical=True
        ),
        MigrationStep(
            name="upload_project",
            command=f"rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' {project_dir}/ /opt/{bot_name}/",
            use_sudo=False,
            timeout=300,
            critical=True
        ),
        MigrationStep(
            name="create_venv",
            command=f"cd /opt/{bot_name} && python3 -m venv .venv",
            use_sudo=False,
            timeout=60,
            critical=True
        ),
        MigrationStep(
            name="install_dependencies",
            command=f"cd /opt/{bot_name} && .venv/bin/pip install -r requirements.txt",
            use_sudo=False,
            timeout=300,
            critical=True
        ),
        MigrationStep(
            name="setup_env",
            command=f"cd /opt/{bot_name} && cat > .env << 'ENV_EOF'\n" + 
                    "\n".join([f"{k}={v}" for k, v in env_vars.items()]) + 
                    "\nENV_EOF",
            use_sudo=False,
            timeout=30,
            critical=True
        ),
        MigrationStep(
            name="create_service",
            command=f"cat > /etc/systemd/system/{bot_name}.service << 'SERVICE_EOF'\n"
                    f"[Unit]\n"
                    f"Description={bot_name} Bot\n"
                    f"After=network.target\n\n"
                    f"[Service]\n"
                    f"Type=simple\n"
                    f"User=root\n"
                    f"WorkingDirectory=/opt/{bot_name}\n"
                    f"ExecStart=/opt/{bot_name}/.venv/bin/python3 /opt/{bot_name}/main.py\n"
                    f"Restart=always\n"
                    f"RestartSec=5\n\n"
                    f"[Install]\n"
                    f"WantedBy=multi-user.target\n"
                    f"SERVICE_EOF\n"
                    f"systemctl daemon-reload",
            use_sudo=True,
            timeout=30,
            critical=True
        ),
        MigrationStep(
            name="start_bot",
            command=f"systemctl start {bot_name}",
            use_sudo=True,
            timeout=60,
            critical=True
        ),
        MigrationStep(
            name="health_check",
            command=f"sleep 3 && systemctl is-active {bot_name}",
            use_sudo=True,
            timeout=30,
            critical=True
        )
    ]
    
    plan = MigrationPlan(
        migration_id=f"mig-{bot_name}-{int(time.time())}",
        bot_name=bot_name,
        source=source,
        destination=destination,
        steps=steps,
        project_directory=f"/opt/{bot_name}",
        bot_service_name=bot_name,
        log_path=f"/var/log/{bot_name}.log",
        metadata={
            "source_bot": bot_name,
            "timestamp": time.ctime(),
        }
    )
    
    return plan

def run_migration_async(plan, progress_callback):
    """Run migration in background thread with async callback support."""
    global _migration_manager, _migration_running
    
    if _migration_running:
        return None
    
    def run():
        global _migration_manager, _migration_running
        _migration_running = True
        
        try:
            manager = MigrationManager(
                plan,
                state_db_path="migration_state.db",
                max_step_retries=2,
                min_confidence_to_auto_heal=0.6,
                allow_risky_fixes=False,
            )
            
            _migration_manager = manager
            
            def sync_callback(step: str, message: str):
                # Call the async callback safely
                try:
                    # Create a new event loop for this call
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(progress_callback(step, message))
                    loop.close()
                except Exception as e:
                    logger.error(f"Callback error: {e}")
            
            manager.subscribe_logs(sync_callback)
            success = manager.run()
            
            # Final status
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(
                    progress_callback("FINAL", f"Migration {'✅ SUCCESS' if success else '❌ FAILED'}")
                )
                loop.close()
            except Exception as e:
                logger.error(f"Final callback error: {e}")
            
            _migration_running = False
            
        except Exception as e:
            logger.error(f"Migration error: {e}")
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(
                    progress_callback("ERROR", f"Migration error: {e}")
                )
                loop.close()
            except Exception:
                pass
            _migration_running = False
    
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread

def get_migration_status():
    """Get current migration status."""
    global _migration_manager
    
    if not _migration_manager:
        return {"status": "not_running", "is_running": False}
    
    try:
        return _migration_manager.get_status()
    except Exception as e:
        return {"status": "error", "message": str(e), "is_running": False}

def cancel_migration():
    """Cancel ongoing migration."""
    global _migration_manager
    
    if _migration_manager:
        try:
            _migration_manager.cancel()
            return True
        except Exception as e:
            logger.error(f"Cancel error: {e}")
            return False
    return False

# ==================== ROUTER ====================
router = Router()
publish_task = None

@router.message(Command("start"))
async def start(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Admin only.")
        return
    
    await message.answer(
        f"🎯 **Content Publisher Bot**\n\n"
        f"✅ Forward ANY message - links remain CLICKABLE\n"
        f"✅ Preserves bold, italic, underline formatting\n"
        f"✅ Auto-detects sequence numbers\n"
        f"✅ Auto-throttle to avoid rate limits\n\n"
        f"📝 Signature: {SIGNATURE}\n"
        f"⚠️ Max queue: {MAX_QUEUE} messages\n\n"
        f"🔄 **Migration Commands:**\n"
        f"• /migrate - Start migration\n"
        f"• /migrate_status - Check progress\n"
        f"• /migrate_cancel - Cancel migration",
        reply_markup=main_keyboard(),
        parse_mode="Markdown"
    )

# ==================== MIGRATION COMMANDS ====================

@router.message(Command("migrate"))
async def cmd_migrate(message: Message, state: FSMContext):
    """Start migration process."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Admin only.")
        return
    
    if not MIGRATION_AVAILABLE:
        await message.answer(
            "❌ Migration system not available.\n"
            "Make sure migration_system folder exists."
        )
        return
    
    status = get_migration_status()
    if status.get("is_running", False):
        await message.answer("⚠️ Migration already in progress!\nUse /migrate_status to check progress.")
        return
    
    await message.answer(
        "🔄 **Migration Wizard**\n\n"
        "Please send me the **SOURCE VPS IP address** (where bot is currently running):"
    )
    await state.set_state(MigrationStates.waiting_source_host)

@router.message(MigrationStates.waiting_source_host)
async def migrate_source_host(message: Message, state: FSMContext):
    await state.update_data(source_host=message.text.strip())
    await message.answer("Now send **SOURCE VPS USERNAME** (default: root):")
    await state.set_state(MigrationStates.waiting_source_user)

@router.message(MigrationStates.waiting_source_user)
async def migrate_source_user(message: Message, state: FSMContext):
    await state.update_data(source_user=message.text.strip() or "root")
    await message.answer("Now send **SOURCE VPS PASSWORD**:")
    await state.set_state(MigrationStates.waiting_source_password)

@router.message(MigrationStates.waiting_source_password)
async def migrate_source_pass(message: Message, state: FSMContext):
    await state.update_data(source_password=message.text.strip())
    await message.answer("Now send **DESTINATION VPS IP address**:")
    await state.set_state(MigrationStates.waiting_dest_host)

@router.message(MigrationStates.waiting_dest_host)
async def migrate_dest_host(message: Message, state: FSMContext):
    await state.update_data(dest_host=message.text.strip())
    await message.answer("Now send **DESTINATION VPS USERNAME** (default: root):")
    await state.set_state(MigrationStates.waiting_dest_user)

@router.message(MigrationStates.waiting_dest_user)
async def migrate_dest_user(message: Message, state: FSMContext):
    await state.update_data(dest_user=message.text.strip() or "root")
    await message.answer("Now send **DESTINATION VPS PASSWORD**:")
    await state.set_state(MigrationStates.waiting_dest_password)

@router.message(MigrationStates.waiting_dest_password)
async def migrate_dest_pass(message: Message, state: FSMContext):
    await state.update_data(dest_password=message.text.strip())
    
    data = await state.get_data()
    
    summary = (
        "📋 **Migration Summary**\n\n"
        f"🔄 **Source VPS:**\n"
        f"   Host: `{data.get('source_host')}`\n"
        f"   User: `{data.get('source_user', 'root')}`\n\n"
        f"🎯 **Destination VPS:**\n"
        f"   Host: `{data.get('dest_host')}`\n"
        f"   User: `{data.get('dest_user', 'root')}`\n\n"
        f"⚠️ **Warning:** The bot on source VPS will be stopped during migration.\n"
        f"✅ The bot on destination VPS will start automatically.\n\n"
        f"Type **CONFIRM** to start migration, or **CANCEL** to abort."
    )
    
    await message.answer(summary, parse_mode="Markdown")
    await state.set_state(MigrationStates.waiting_confirm)

@router.message(MigrationStates.waiting_confirm)
async def migrate_confirm(message: Message, state: FSMContext):
    if message.text.upper() == "CANCEL":
        await state.clear()
        await message.answer("❌ Migration cancelled.", reply_markup=main_keyboard())
        return
    
    if message.text.upper() != "CONFIRM":
        await message.answer("Please type **CONFIRM** to start or **CANCEL** to abort.")
        return
    
    data = await state.get_data()
    await state.clear()
    
    status_msg = await message.answer("🔄 **Starting migration...**\n\n⏳ Preparing...")
    
    plan = create_migration_plan(
        source_host=data.get('source_host'),
        source_user=data.get('source_user', 'root'),
        source_pass=data.get('source_password'),
        dest_host=data.get('dest_host'),
        dest_user=data.get('dest_user', 'root'),
        dest_pass=data.get('dest_password'),
    )
    
    if not plan:
        await status_msg.edit_text("❌ Failed to create migration plan.")
        return
    
    async def progress_callback(step: str, message_text: str):
        try:
            current_text = status_msg.text or ""
            if len(current_text) > 3000:
                await message.answer(f"📊 [{step}] {message_text}")
                return
            
            new_text = f"🔄 **Migration in Progress**\n\n"
            new_text += f"📌 **Step:** {step}\n"
            new_text += f"📝 **Status:** {message_text}\n\n"
            new_text += f"⏳ Please wait...\n"
            new_text += f"Use /migrate_status to check progress."
            
            await status_msg.edit_text(new_text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Progress update error: {e}")
    
    global _migration_thread
    _migration_thread = run_migration_async(plan, progress_callback)
    
    if _migration_thread:
        await message.answer(
            "🚀 **Migration started!**\n\n"
            "I'll update you on progress.\n"
            "Use /migrate_status to check status anytime.",
            reply_markup=main_keyboard()
        )
    else:
        await message.answer("❌ Failed to start migration.")

@router.message(Command("migrate_status"))
async def cmd_migrate_status(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Admin only.")
        return
    
    status = get_migration_status()
    
    if status.get("status") == "not_running" and not status.get("is_running"):
        await message.answer("📊 No migration in progress.")
        return
    
    text = "📊 **Migration Status**\n\n"
    text += f"ID: `{status.get('migration_id', 'unknown')}`\n"
    text += f"Status: **{status.get('status', 'unknown').upper()}**\n"
    
    if status.get('total_steps', 0) > 0:
        text += f"Progress: {status.get('completed_steps', 0)}/{status.get('total_steps', 0)} steps\n"
        text += f"Failed: {status.get('failed_steps', 0)}\n"
        text += f"Healed: {status.get('healed_steps', 0)}\n"
    
    if status.get('current_step'):
        text += f"Current: {status['current_step']}\n"
    
    if status.get('is_running'):
        text += "\n⏳ Migration is RUNNING"
    else:
        text += "\n⏹️ Migration is NOT running"
    
    await message.answer(text, parse_mode="Markdown")

@router.message(Command("migrate_cancel"))
async def cmd_migrate_cancel(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Admin only.")
        return
    
    if cancel_migration():
        await message.answer("⏹️ Migration cancellation requested.")
    else:
        await message.answer("⚠️ No migration in progress to cancel.")

# ==================== EXISTING COMMANDS ====================

@router.message(F.text == "📋 View Queue")
async def view_queue(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    msgs = store.get_all()
    if not msgs:
        await message.answer("📭 Queue empty")
        return
    
    response = f"📊 QUEUE: {len(msgs)}/{MAX_QUEUE} messages\n"
    response += f"⚠️ Duplicates blocked: {store.duplicates_count}\n\n"
    
    for i, msg in enumerate(msgs[:30]):
        seq = msg['sequence'] if msg['sequence'] != 999999 else "📌"
        msg_type = msg['type'].upper()
        preview = (msg['edited_text'] or '')[:35]
        response += f"{i+1}. [{seq}] {msg_type}: {preview}\n"
        
        if len(response) > 3800:
            response += f"\n... and {len(msgs) - i - 1} more"
            break
    
    if len(msgs) > 30:
        response += f"\n... and {len(msgs) - 30} more"
    
    await message.answer(response)

@router.message(F.text == "📊 Stats")
async def stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    stats_text = f"📊 **Statistics**\n\n"
    stats_text += f"📦 Messages: {store.get_count()}/{MAX_QUEUE}\n"
    stats_text += f"⚠️ Duplicates blocked: {store.duplicates_count}\n"
    stats_text += f"📝 Signature: {SIGNATURE[:30]}..."
    
    await message.answer(stats_text, parse_mode="Markdown")

@router.message(F.text == "✏️ Edit Single")
async def edit_single(message: Message, state: FSMContext):
    msgs = store.get_all()
    if not msgs:
        await message.answer("Queue empty")
        return
    
    response = "Send message number to edit description:\n\n"
    for i, msg in enumerate(msgs[:20]):
        preview = (msg['edited_text'] or '')[:30]
        response += f"{i+1}. {preview}\n"
    
    await message.answer(response)
    await state.set_state(EditStates.waiting_index)

@router.message(EditStates.waiting_index)
async def edit_index(message: Message, state: FSMContext):
    try:
        idx = int(message.text.strip()) - 1
        msgs = store.get_all()
        if 0 <= idx < len(msgs):
            await state.update_data(msg_index=idx)
            current_text = msgs[idx]['edited_text'] or '(no text)'
            await message.answer(f"Editing message #{idx+1}\nCurrent text:\n{current_text}\n\nSend new text (plain text - links will be auto-detected):")
            await state.set_state(EditStates.waiting_text)
        else:
            await message.answer(f"Invalid. Send 1-{len(msgs)}")
            await state.clear()
    except:
        await message.answer("Send a number")
        await state.clear()

@router.message(EditStates.waiting_text)
async def save_edit(message: Message, state: FSMContext):
    data = await state.get_data()
    idx = data.get('msg_index')
    if store.edit_message(idx, message.text):
        await message.answer(f"✅ Message #{idx+1} updated!", reply_markup=main_keyboard())
    else:
        await message.answer("❌ Failed to update")
    await state.clear()

@router.message(F.text == "✏️ Edit All")
async def edit_all_prompt(message: Message, state: FSMContext):
    count = store.get_count()
    if count == 0:
        await message.answer("Queue empty")
        return
    
    await message.answer(f"Editing ALL {count} messages\nSend new description (will apply to ALL):")
    await state.set_state(EditStates.waiting_all)

@router.message(EditStates.waiting_all)
async def save_edit_all(message: Message, state: FSMContext):
    count = store.edit_all(message.text)
    await message.answer(f"✅ Updated {count} messages!", reply_markup=main_keyboard())
    await state.clear()

@router.message(F.text == "❌ Remove One")
async def remove_prompt(message: Message, state: FSMContext):
    msgs = store.get_all()
    if not msgs:
        await message.answer("Queue empty")
        return
    
    response = "Send message number to remove:\n\n"
    for i, msg in enumerate(msgs[:20]):
        preview = (msg['edited_text'] or '')[:30]
        response += f"{i+1}. {preview}\n"
    
    await message.answer(response)
    await state.set_state("waiting_remove")

@router.message(StateFilter("waiting_remove"))
async def handle_remove(message: Message, state: FSMContext):
    try:
        idx = int(message.text.strip()) - 1
        if store.remove(idx):
            await message.answer(f"✅ Message #{idx+1} removed!", reply_markup=main_keyboard())
        else:
            await message.answer("Invalid message number.")
    except:
        await message.answer("Send a valid number.")
    await state.clear()

@router.message(F.text == "🗑️ Clear Queue")
async def clear_queue(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    count = store.get_count()
    store.clear()
    await message.answer(f"✅ Cleared {count} messages")

@router.message(F.text == "⏹️ Stop")
async def stop_publishing(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    global publish_task
    store.stop()
    
    if publish_task and not publish_task.done():
        publish_task.cancel()
    
    await message.answer("⏹️ Publishing stopped! Use '🚀 Publish' to resume.")

@router.message(F.text == "🚀 Publish")
async def publish_prompt(message: Message):
    count = store.get_count()
    if count == 0:
        await message.answer("Queue empty")
        return
    
    await message.answer(
        f"📊 **Ready to publish {count} messages**\n\n"
        f"✅ Links will remain CLICKABLE\n"
        f"✅ Bold, italic, underline preserved\n"
        f"⚠️ Auto-throttle to avoid rate limits\n"
        f"📝 Signature: {SIGNATURE}\n"
        f"⏱️ Estimated time: ~{count * 1.5} seconds\n\n"
        f"Confirm?",
        reply_markup=publish_keyboard(),
        parse_mode="Markdown"
    )

@router.message(F.text == "🔄 Migrate Bot")
async def migrate_button(message: Message, state: FSMContext):
    await cmd_migrate(message, state)

@router.message(F.text == "📈 Migration Status")
async def migrate_status_button(message: Message):
    await cmd_migrate_status(message)

# ==================== CONTENT HANDLER ====================

@router.message()
async def handle_content(message: Message):
    """Handle all incoming messages - PRESERVES CLICKABLE LINKS"""
    if message.from_user.id != ADMIN_ID:
        return
    
    msg_data = {'type': 'unknown'}
    
    if message.text:
        msg_data['type'] = 'text'
        msg_data['text'] = message.text
        msg_data['entities'] = message.entities
        if message.entities:
            msg_data['html_text'] = format_text_with_entities(message.text, message.entities)
        else:
            msg_data['html_text'] = escape(message.text)
    
    elif message.caption:
        if message.photo:
            msg_data['type'] = 'photo'
            msg_data['file_id'] = message.photo[-1].file_id
        elif message.video:
            msg_data['type'] = 'video'
            msg_data['file_id'] = message.video.file_id
        elif message.document:
            msg_data['type'] = 'document'
            msg_data['file_id'] = message.document.file_id
        else:
            return
        
        msg_data['caption'] = message.caption
        msg_data['caption_entities'] = message.caption_entities
        if message.caption_entities:
            msg_data['html_caption'] = format_text_with_entities(message.caption, message.caption_entities)
        else:
            msg_data['html_caption'] = escape(message.caption)
        msg_data['text'] = message.caption
    
    elif message.photo:
        msg_data['type'] = 'photo'
        msg_data['file_id'] = message.photo[-1].file_id
        msg_data['text'] = ''
        msg_data['html_caption'] = ''
    
    elif message.video:
        msg_data['type'] = 'video'
        msg_data['file_id'] = message.video.file_id
        msg_data['text'] = ''
        msg_data['html_caption'] = ''
    
    elif message.document:
        msg_data['type'] = 'document'
        msg_data['file_id'] = message.document.file_id
        msg_data['text'] = ''
        msg_data['html_caption'] = ''
    
    else:
        await message.answer("⚠️ Unsupported message type.")
        return
    
    text_content = msg_data.get('text') or msg_data.get('caption') or ''
    links = re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', text_content)
    msg_data['links'] = links
    
    success, response_msg, count = store.add_message(msg_data)
    
    if success:
        msg_type_display = msg_data['type'].upper()
        link_count = len(links)
        await message.answer(
            f"✅ [{msg_type_display}] {response_msg}\n"
            f"📊 Queue: {count}/{MAX_QUEUE}\n"
            f"🔗 Clickable links: {link_count}",
            reply_markup=main_keyboard()
        )
    else:
        await message.answer(f"⚠️ {response_msg}\nQueue: {count}/{MAX_QUEUE}")

# ==================== CALLBACKS ====================

@router.callback_query(F.data == "publish")
async def publish(callback: CallbackQuery):
    global publish_task
    
    msgs = store.get_all()
    if not msgs:
        await callback.message.answer("Queue empty")
        await callback.answer()
        return
    
    await callback.message.answer(f"🚀 Publishing {len(msgs)} messages with CLICKABLE links...\n⏱️ This may take a few minutes...")
    
    store.start_processing()
    success = 0
    failed = 0
    
    for i, msg in enumerate(msgs):
        if store.should_stop():
            await callback.message.answer(f"⏹️ Stopped at {i+1}/{len(msgs)} messages")
            break
        
        try:
            if msg['type'] == 'text':
                html_text = msg.get('html_text', escape(msg.get('edited_text') or msg.get('text') or ''))
                if SIGNATURE:
                    html_text = f"{html_text}\n\n{SIGNATURE}"
                await callback.bot.send_message(CHANNEL_ID, html_text, parse_mode=ParseMode.HTML)
            
            elif msg['type'] in ['photo', 'video', 'document']:
                html_caption = msg.get('html_caption', escape(msg.get('edited_text') or msg.get('caption') or ''))
                if SIGNATURE and html_caption:
                    html_caption = f"{html_caption}\n\n{SIGNATURE}"
                elif SIGNATURE and not html_caption:
                    html_caption = SIGNATURE
                
                if msg['type'] == 'photo':
                    await callback.bot.send_photo(CHANNEL_ID, msg['file_id'], caption=html_caption, parse_mode=ParseMode.HTML)
                elif msg['type'] == 'video':
                    await callback.bot.send_video(CHANNEL_ID, msg['file_id'], caption=html_caption, parse_mode=ParseMode.HTML)
                elif msg['type'] == 'document':
                    await callback.bot.send_document(CHANNEL_ID, msg['file_id'], caption=html_caption, parse_mode=ParseMode.HTML)
            
            success += 1
            await asyncio.sleep(1.5)
            
            if (i + 1) % 50 == 0:
                await callback.message.answer(f"📊 Progress: {i+1}/{len(msgs)} messages published...")
            
        except Exception as e:
            failed += 1
            logger.error(f"Failed to publish {i+1}: {e}")
            await asyncio.sleep(3)
    
    if not store.should_stop():
        store.clear()
        await callback.message.answer(f"✅ Published {success}/{len(msgs)} messages!\n❌ Failed: {failed}\n✅ All links are CLICKABLE!")
    else:
        for _ in range(success):
            if store.get_count() > 0:
                store.remove(0)
        remaining = store.get_count()
        await callback.message.answer(f"⏹️ Published {success} messages. {remaining} remaining in queue.")
    
    store.processing = False
    await callback.answer()

@router.callback_query(F.data == "cancel")
async def cancel(callback: CallbackQuery):
    await callback.message.answer("❌ Cancelled")
    await callback.answer()

# ==================== MAIN ====================
async def main():
    global bot
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    
    await bot.set_my_commands([
        BotCommand(command="start", description="Start bot"),
        BotCommand(command="migrate", description="Start migration"),
        BotCommand(command="migrate_status", description="Check migration status"),
        BotCommand(command="migrate_cancel", description="Cancel migration"),
    ])
    
    logger.info("Bot started! Links will remain CLICKABLE when published!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())