import asyncio
import sqlite3
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError, RetryAfter, TimedOut, NetworkError
from telegram.request import HTTPXRequest
import logging
import os
import sys
import time

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('forward_bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# CONFIGURATION
FORWARD_BOT_TOKEN = os.environ.get('FORWARD_BOT_TOKEN')
MASTER_CHANNEL = os.environ.get('MASTER_CHANNEL')
ADMIN_ID = os.environ.get('ADMIN_ID')
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', '20'))

# Database file
DB_FILE = 'channels.db'

# Global stats
stats = {
    'total_forwards': 0,
    'successful_forwards': 0,
    'failed_forwards': 0,
    'last_forward_time': None,
    'messages_processed': 0,
    'restarts': 0 
}

# Global variable to store active channels
TARGET_CHANNELS = []


# ==================== DATABASE FUNCTIONS ====================

def init_database():
    """Initialize SQLite database for channels"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY,
            channel_name TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            active INTEGER DEFAULT 1,
            total_forwards INTEGER DEFAULT 0,
            last_forward TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized")


def load_channels_from_db():
    """Load active channels from database"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT channel_id FROM channels WHERE active = 1 ORDER BY channel_id')
    channels = [row[0] for row in c.fetchall()]
    conn.close()
    return channels


def add_channel_to_db(channel_id, channel_name=None):
    """Add channel to database"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('INSERT INTO channels (channel_id, channel_name, active) VALUES (?, ?, 1)',
                  (channel_id, channel_name))
        conn.commit()
        conn.close()
        logger.info(f"✅ Added channel to database: {channel_id}")
        return True
    except sqlite3.IntegrityError:
        c.execute('UPDATE channels SET active = 1, channel_name = ? WHERE channel_id = ?',
                  (channel_name, channel_id))
        conn.commit()
        conn.close()
        logger.info(f"✅ Reactivated channel: {channel_id}")
        return True
    except Exception as e:
        conn.close()
        logger.error(f"❌ Error adding channel: {e}")
        return False


def remove_channel_from_db(channel_id):
    """Remove (deactivate) channel from database"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE channels SET active = 0 WHERE channel_id = ?', (channel_id,))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    if deleted:
        logger.info(f"🗑️ Removed channel: {channel_id}")
    return deleted


def get_all_channels_from_db():
    """Get all channels with their info"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT channel_id, channel_name, added_at, active, total_forwards, last_forward 
        FROM channels 
        ORDER BY added_at DESC
    ''')
    channels = c.fetchall()
    conn.close()
    return channels


def update_channel_stats(channel_id):
    """Update forward stats for a channel"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        UPDATE channels 
        SET total_forwards = total_forwards + 1, last_forward = ? 
        WHERE channel_id = ?
    ''', (datetime.now().isoformat(), channel_id))
    conn.commit()
    conn.close()


def migrate_env_channels_to_db():
    """One-time migration: Import channels from environment variable to database"""
    env_channels = os.environ.get('TARGET_CHANNELS', '').split(',')
    env_channels = [ch.strip() for ch in env_channels if ch.strip()]
    
    if env_channels:
        logger.info(f"📄 Migrating {len(env_channels)} channels from environment to database...")
        for ch_id in env_channels:
            add_channel_to_db(ch_id, "Imported from env")
        logger.info("✅ Migration complete!")


def reload_channels():
    """Reload channels from database"""
    global TARGET_CHANNELS
    TARGET_CHANNELS = load_channels_from_db()
    logger.info(f"🔄 Reloaded {len(TARGET_CHANNELS)} active channels")


# ==================== FORWARDING FUNCTIONS ====================

async def copy_message_to_channel(bot, message, channel_id, retries=5):
    """Copy message to a single channel (NO 'Forwarded from' label) with retry logic"""
    for attempt in range(retries):
        try:
            # COPY MESSAGE BASED ON TYPE - No forwarding attribution
            if message.text:
                # Plain text message
                await bot.send_message(
                    chat_id=channel_id,
                    text=message.text,
                    entities=message.entities  # Preserve formatting
                )
            elif message.photo:
                # Photo message
                await bot.send_photo(
                    chat_id=channel_id,
                    photo=message.photo[-1].file_id,
                    caption=message.caption,
                    caption_entities=message.caption_entities
                )
            elif message.video:
                # Video message
                await bot.send_video(
                    chat_id=channel_id,
                    video=message.video.file_id,
                    caption=message.caption,
                    caption_entities=message.caption_entities,
                    duration=message.video.duration,
                    width=message.video.width,
                    height=message.video.height
                )
            elif message.document:
                # Document/File
                await bot.send_document(
                    chat_id=channel_id,
                    document=message.document.file_id,
                    caption=message.caption,
                    caption_entities=message.caption_entities
                )
            elif message.audio:
                # Audio file
                await bot.send_audio(
                    chat_id=channel_id,
                    audio=message.audio.file_id,
                    caption=message.caption,
                    caption_entities=message.caption_entities
                )
            elif message.voice:
                # Voice message
                await bot.send_voice(
                    chat_id=channel_id,
                    voice=message.voice.file_id,
                    caption=message.caption,
                    caption_entities=message.caption_entities
                )
            elif message.video_note:
                # Video note (round video)
                await bot.send_video_note(
                    chat_id=channel_id,
                    video_note=message.video_note.file_id
                )
            elif message.sticker:
                # Sticker
                await bot.send_sticker(
                    chat_id=channel_id,
                    sticker=message.sticker.file_id
                )
            elif message.animation:
                # GIF/Animation
                await bot.send_animation(
                    chat_id=channel_id,
                    animation=message.animation.file_id,
                    caption=message.caption,
                    caption_entities=message.caption_entities
                )
            elif message.poll:
                # Poll
                await bot.send_poll(
                    chat_id=channel_id,
                    question=message.poll.question,
                    options=[opt.text for opt in message.poll.options],
                    is_anonymous=message.poll.is_anonymous,
                    type=message.poll.type,
                    allows_multiple_answers=message.poll.allows_multiple_answers
                )
            elif message.location:
                # Location
                await bot.send_location(
                    chat_id=channel_id,
                    latitude=message.location.latitude,
                    longitude=message.location.longitude
                )
            elif message.contact:
                # Contact
                await bot.send_contact(
                    chat_id=channel_id,
                    phone_number=message.contact.phone_number,
                    first_name=message.contact.first_name,
                    last_name=message.contact.last_name
                )
            else:
                logger.warning(f"⚠️ Unsupported message type for channel {channel_id}")
                return False
            
            update_channel_stats(channel_id)
            logger.info(f"✅ Copied to {channel_id}")
            return True
            
        except RetryAfter as e:
            wait_time = e.retry_after + 1
            logger.warning(f"⏳ Rate limit hit for {channel_id}, waiting {wait_time}s...")
            await asyncio.sleep(wait_time)
        except TimedOut:
            logger.warning(f"⏱️ Timeout for {channel_id}, retrying ({attempt + 1}/{retries})...")
            await asyncio.sleep(2)
        except TelegramError as e:
            error_msg = str(e).lower()
            if any(err in error_msg for err in ['chat not found', 'bot was kicked', 'not a member', 'have no rights']):
                logger.error(f"❌ Permanent error {channel_id}: {e}")
                return False
            logger.error(f"❌ Error {channel_id} (attempt {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"❌ Unexpected error {channel_id}: {e}")
            return False
    
    logger.error(f"❌ Failed {channel_id} after {retries} attempts")
    return False


async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Copy any message from master channel to ALL target channels (NO forwarding label)"""
    global stats
    
    message = update.channel_post
    if not message:
        return
    
    # Reload channels to get any new additions
    reload_channels()
    
    if len(TARGET_CHANNELS) == 0:
        logger.warning("⚠️ No active channels to forward to!")
        return
    
    # Determine message type
    msg_type = "text"
    if message.photo:
        msg_type = "photo"
    elif message.video:
        msg_type = "video"
    elif message.document:
        msg_type = "document"
    elif message.audio:
        msg_type = "audio"
    elif message.voice:
        msg_type = "voice"
    elif message.sticker:
        msg_type = "sticker"
    elif message.poll:
        msg_type = "poll"
    elif message.animation:
        msg_type = "animation"
    
    logger.info("="*60)
    logger.info(f"📨 New {msg_type} message from master channel")
    logger.info(f"📤 Copying to {len(TARGET_CHANNELS)} channels (NO forwarding label)...")
    logger.info("="*60)
    
    start_time = datetime.now()
    successful = 0
    failed = 0
    
    # Process in batches for optimal performance
    total_batches = (len(TARGET_CHANNELS) + BATCH_SIZE - 1) // BATCH_SIZE
    
    for i in range(0, len(TARGET_CHANNELS), BATCH_SIZE):
        batch = TARGET_CHANNELS[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        
        logger.info(f"🔄 Batch {batch_num}/{total_batches}: Processing {len(batch)} channels...")
        
        # Parallel copying within batch
        tasks = [copy_message_to_channel(context.bot, message, ch_id) for ch_id in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        batch_success = sum(1 for r in results if r is True)
        batch_failed = len(batch) - batch_success
        
        successful += batch_success
        failed += batch_failed
        
        logger.info(f"   ✅ Success: {batch_success}/{len(batch)} | ❌ Failed: {batch_failed}")
        
        # Small delay between batches
        if i + BATCH_SIZE < len(TARGET_CHANNELS):
            await asyncio.sleep(1.0)  # 1 second delay between batches
    
    # Calculate duration
    duration = (datetime.now() - start_time).total_seconds()
    
    # Update global stats
    stats['total_forwards'] += len(TARGET_CHANNELS)
    stats['successful_forwards'] += successful
    stats['failed_forwards'] += failed
    stats['last_forward_time'] = datetime.now()
    stats['messages_processed'] += 1
    
    # Calculate rates
    success_rate = (successful / len(TARGET_CHANNELS) * 100) if len(TARGET_CHANNELS) > 0 else 0
    forwards_per_sec = len(TARGET_CHANNELS) / duration if duration > 0 else 0
    
    logger.info("="*60)
    logger.info(f"✅ COPY COMPLETE!")
    logger.info(f"📊 Results: {successful}/{len(TARGET_CHANNELS)} successful ({success_rate:.1f}%)")
    logger.info(f"⏱️  Duration: {duration:.2f} seconds ({forwards_per_sec:.1f} copies/sec)")
    logger.info(f"📈 Total processed: {stats['messages_processed']} messages")
    logger.info("="*60)
    
    # Notify admin if high failure rate
    if ADMIN_ID and len(TARGET_CHANNELS) > 0:
        failure_rate = failed / len(TARGET_CHANNELS)
        if failure_rate > 0.3:  # More than 30% failed
            try:
                await context.bot.send_message(
                    chat_id=int(ADMIN_ID),
                    text=f"⚠️ <b>HIGH FAILURE RATE ALERT</b>\n\n"
                         f"📊 Message #{stats['messages_processed']}\n"
                         f"✅ Successful: {successful}/{len(TARGET_CHANNELS)}\n"
                         f"❌ Failed: {failed}\n"
                         f"📉 Failure Rate: {failure_rate*100:.1f}%\n"
                         f"⏱️ Duration: {duration:.2f}s\n\n"
                         f"Check logs for details!",
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Failed to send admin alert: {e}")


# ==================== COMMAND HANDLERS ====================

def is_admin(user_id):
    """Check if user is admin"""
    if not ADMIN_ID:
        return False
    return str(user_id) == str(ADMIN_ID)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot status"""
    if not update.effective_user:
        return
    
    if not is_admin(update.effective_user.id):
        return
    
    uptime = "Running"
    if stats['last_forward_time']:
        time_diff = datetime.now() - stats['last_forward_time']
        minutes = time_diff.seconds // 60
        if minutes < 60:
            uptime = f"Last copy: {minutes} min ago"
        else:
            hours = minutes // 60
            uptime = f"Last copy: {hours} hours ago"
    
    success_rate = 0
    if stats['total_forwards'] > 0:
        success_rate = (stats['successful_forwards'] / stats['total_forwards']) * 100
    
    response = (
        f"🤖 <b>Auto-Copy Bot V2.0</b>\n\n"
        f"📡 Master Channel: <code>{MASTER_CHANNEL}</code>\n"
        f"📤 Active Channels: <b>{len(TARGET_CHANNELS)}</b>\n"
        f"📨 Messages Processed: <b>{stats['messages_processed']}</b>\n"
        f"📊 Total Copies: <b>{stats['total_forwards']}</b>\n"
        f"✅ Successful: <b>{stats['successful_forwards']}</b> ({success_rate:.1f}%)\n"
        f"❌ Failed: <b>{stats['failed_forwards']}</b>\n"
        f"⚙️ Batch Size: <b>{BATCH_SIZE}</b>\n"
        f"⏰ Status: {uptime}\n\n"
        f"🔒 <b>COPY MODE</b> - No 'Forwarded from' label!\n\n"
        f"<b>Commands:</b>\n"
        f"/addchannel &lt;id&gt; [name] - Add channel\n"
        f"/removechannel &lt;id&gt; - Remove channel\n"
        f"/listchannels - Show all channels\n"
        f"/stats - Detailed statistics\n"
        f"/test - Test bot status\n"
        f"/reload - Reload channels from DB\n"
        f"/setbatch &lt;size&gt; - Change batch size\n"
        f"/exportchannels - Backup channel list"
    )
    
    await update.message.reply_text(response, parse_mode='HTML')


async def add_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a new channel"""
    if not update.effective_user:
        return
    
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized")
        return
    
    if not context.args:
        await update.message.reply_text(
            "❌ <b>Usage:</b>\n\n"
            "<code>/addchannel -1001234567890</code>\n"
            "<code>/addchannel -1001234567890 Channel Name</code>\n\n"
            "💡 How to get Channel ID:\n"
            "1. Forward a message from your channel to @userinfobot\n"
            "2. It will show you the channel ID",
            parse_mode='HTML'
        )
        return
    
    channel_id = context.args[0].strip()
    channel_name = " ".join(context.args[1:]) if len(context.args) > 1 else "Unnamed"
    
    if not channel_id.startswith('-100'):
        await update.message.reply_text(
            "❌ Invalid channel ID! Must start with <code>-100</code>",
            parse_mode='HTML'
        )
        return
    
    if add_channel_to_db(channel_id, channel_name):
        reload_channels()
        await update.message.reply_text(
            f"✅ <b>Channel Added!</b>\n\n"
            f"📢 ID: <code>{channel_id}</code>\n"
            f"📝 Name: {channel_name}\n"
            f"📊 Total Active: <b>{len(TARGET_CHANNELS)}</b>",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text("❌ Failed to add channel")


async def remove_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a channel"""
    if not update.effective_user:
        return
    
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized")
        return
    
    if not context.args:
        await update.message.reply_text(
            "❌ <b>Usage:</b>\n<code>/removechannel -1001234567890</code>",
            parse_mode='HTML'
        )
        return
    
    channel_id = context.args[0].strip()
    
    if remove_channel_from_db(channel_id):
        reload_channels()
        await update.message.reply_text(
            f"✅ <b>Channel Removed!</b>\n\n"
            f"🗑️ ID: <code>{channel_id}</code>\n"
            f"📊 Remaining: <b>{len(TARGET_CHANNELS)}</b>",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(f"❌ Channel not found: <code>{channel_id}</code>", parse_mode='HTML')


async def list_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all channels with stats"""
    if not update.effective_user:
        return
    
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized")
        return
    
    channels = get_all_channels_from_db()
    
    if not channels:
        await update.message.reply_text(
            "📋 No channels configured!\n\n"
            "💡 Use /addchannel to add channels"
        )
        return
    
    response = f"📋 <b>ALL CHANNELS ({len(channels)} total)</b>\n\n"
    
    active_count = 0
    for ch_id, ch_name, added_at, active, total_fwd, last_fwd in channels[:25]:
        status = "✅" if active else "❌"
        name = ch_name or "Unnamed"
        fwd_count = total_fwd or 0
        response += f"{status} <code>{ch_id}</code>\n"
        response += f"   📝 {name} | 📊 {fwd_count} copies\n\n"
        if active:
            active_count += 1
    
    if len(channels) > 25:
        response += f"<i>...and {len(channels) - 25} more channels</i>\n\n"
    
    response += f"<b>Active:</b> {active_count} | <b>Inactive:</b> {len(channels) - active_count}"
    
    await update.message.reply_text(response, parse_mode='HTML')


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed statistics"""
    if not update.effective_user:
        return
    
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized")
        return
    
    success_rate = 0
    if stats['total_forwards'] > 0:
        success_rate = (stats['successful_forwards'] / stats['total_forwards']) * 100
    
    last_forward = "Never"
    if stats['last_forward_time']:
        time_diff = datetime.now() - stats['last_forward_time']
        seconds = time_diff.seconds
        if seconds < 60:
            last_forward = f"{seconds} seconds ago"
        elif seconds < 3600:
            last_forward = f"{seconds // 60} minutes ago"
        else:
            last_forward = f"{seconds // 3600} hours ago"
    
    avg_per_msg = 0
    if stats['messages_processed'] > 0:
        avg_per_msg = stats['total_forwards'] / stats['messages_processed']
    
    response = (
        f"📊 <b>DETAILED STATISTICS</b>\n\n"
        f"📤 Active Channels: <b>{len(TARGET_CHANNELS)}</b>\n"
        f"📨 Messages Processed: <b>{stats['messages_processed']}</b>\n"
        f"📊 Total Copies: <b>{stats['total_forwards']}</b>\n"
        f"✅ Successful: <b>{stats['successful_forwards']}</b>\n"
        f"❌ Failed: <b>{stats['failed_forwards']}</b>\n"
        f"📈 Success Rate: <b>{success_rate:.1f}%</b>\n"
        f"📉 Average Channels/Message: <b>{avg_per_msg:.1f}</b>\n"
        f"⏰ Last Copy: {last_forward}\n"
        f"⚙️ Batch Size: <b>{BATCH_SIZE}</b>\n\n"
        f"💾 Database: <code>{DB_FILE}</code>\n"
        f"🔒 Mode: <b>COPY MODE</b> (No forwarding label)"
    )
    
    await update.message.reply_text(response, parse_mode='HTML')


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send test message"""
    if not update.effective_user:
        return
    
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized")
        return
    
    response = (
        f"✅ <b>Bot is Working!</b>\n\n"
        f"📡 Monitoring: <code>{MASTER_CHANNEL}</code>\n"
        f"📤 Active Channels: <b>{len(TARGET_CHANNELS)}</b>\n"
        f"⚙️ Batch Size: <b>{BATCH_SIZE}</b>\n"
        f"🤖 Status: Online\n"
        f"🔒 Mode: <b>COPY MODE</b>\n\n"
        f"Post a message to master channel to test copying!"
    )
    
    await update.message.reply_text(response, parse_mode='HTML')


async def reload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reload channels from database"""
    if not update.effective_user:
        return
    
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized")
        return
    
    old_count = len(TARGET_CHANNELS)
    reload_channels()
    new_count = len(TARGET_CHANNELS)
    
    await update.message.reply_text(
        f"🔄 <b>Channels Reloaded!</b>\n\n"
        f"Before: {old_count}\n"
        f"After: {new_count}\n"
        f"Change: {new_count - old_count:+d}",
        parse_mode='HTML'
    )


async def setbatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Change batch size for forwarding"""
    global BATCH_SIZE
    
    if not update.effective_user:
        return
    
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized")
        return
    
    if not context.args:
        await update.message.reply_text(
            f"⚙️ <b>Current Batch Size:</b> {BATCH_SIZE}\n\n"
            f"<b>Usage:</b> <code>/setbatch 20</code>\n\n"
            f"Recommended: 15-25 for optimal speed",
            parse_mode='HTML'
        )
        return
    
    try:
        new_size = int(context.args[0])
        if new_size < 1 or new_size > 50:
            await update.message.reply_text("❌ Batch size must be between 1 and 50")
            return
        
        BATCH_SIZE = new_size
        await update.message.reply_text(
            f"✅ <b>Batch Size Updated!</b>\n\n"
            f"New size: <b>{BATCH_SIZE}</b>",
            parse_mode='HTML'
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid number")


async def export_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export all channels as addchannel commands"""
    if not update.effective_user:
        return
    
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized")
        return
    
    channels = get_all_channels_from_db()
    
    if not channels:
        await update.message.reply_text("No channels to export!")
        return
    
    # Create addchannel commands for all active channels
    commands = []
    for ch_id, ch_name, added_at, active, total_fwd, last_fwd in channels:
        if active:
            name = ch_name or ""
            if name:
                commands.append(f"/addchannel {ch_id} {name}")
            else:
                commands.append(f"/addchannel {ch_id}")
    
    export_text = "📖 <b>CHANNEL BACKUP</b>\n\n"
    export_text += "Copy these commands and save them:\n\n"
    export_text += "<code>" + "\n".join(commands) + "</code>\n\n"
    export_text += f"📊 Total: {len(commands)} channels\n\n"
    export_text += "After redeployment, paste these back to restore channels."
    
    await update.message.reply_text(export_text, parse_mode='HTML')


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log errors"""
    logger.error(f"❌ Update {update} caused error: {context.error}")


# ==================== HEARTBEAT ====================

async def heartbeat():
    """Periodic health check"""
    while True:
        await asyncio.sleep(300)  # Every 5 minutes
        logger.info(f"💓 Health Check - Bot alive | Channels: {len(TARGET_CHANNELS)} | Messages: {stats['messages_processed']}")


# ==================== MAIN ====================

def main():
    # Validate environment variables
    if not FORWARD_BOT_TOKEN:
        logger.error("❌ FORWARD_BOT_TOKEN not set!")
        sys.exit(1)
    
    if not MASTER_CHANNEL:
        logger.error("❌ MASTER_CHANNEL not set!")
        sys.exit(1)
    
    if not ADMIN_ID:
        logger.warning("⚠️ ADMIN_ID not set - commands will be disabled!")
    
    try:
        master_id = int(MASTER_CHANNEL)
    except ValueError:
        logger.error(f"❌ MASTER_CHANNEL must be integer: {MASTER_CHANNEL}")
        sys.exit(1)
    
    # Initialize database
    init_database()
    
    # Migrate environment channels to database (one-time)
    migrate_env_channels_to_db()
    
    # Load channels
    reload_channels()
    
    if len(TARGET_CHANNELS) == 0:
        logger.warning("⚠️ No channels loaded! Use /addchannel to add channels.")
    
    logger.info("=" * 60)
    logger.info("🚀 AUTO-COPY BOT V2.0 STARTING")
    logger.info(f"📡 Master Channel: {MASTER_CHANNEL}")
    logger.info(f"📤 Active Channels: {len(TARGET_CHANNELS)}")
    logger.info(f"👤 Admin ID: {ADMIN_ID or 'Not set'}")
    logger.info(f"⚙️ Batch Size: {BATCH_SIZE}")
    logger.info(f"💾 Database: {DB_FILE}")
    logger.info(f"🔒 Mode: COPY MODE - No 'Forwarded from' label!")
    logger.info("=" * 60)
    
    if TARGET_CHANNELS:
        logger.info("📋 Channels preview:")
        for i, ch in enumerate(TARGET_CHANNELS[:5], 1):
            logger.info(f"  {i}. {ch}")
        if len(TARGET_CHANNELS) > 5:
            logger.info(f"  ...and {len(TARGET_CHANNELS) - 5} more")
        logger.info("=" * 60)
    
    # ✅ FIX 1: Configure longer timeouts
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=60.0
    )
    
    # Build application with custom request
    app = Application.builder().token(FORWARD_BOT_TOKEN).request(request).build()
    
    # Add command handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("addchannel", add_channel_command))
    app.add_handler(CommandHandler("removechannel", remove_channel_command))
    app.add_handler(CommandHandler("listchannels", list_channels_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("test", test_command))
    app.add_handler(CommandHandler("reload", reload_command))
    app.add_handler(CommandHandler("setbatch", setbatch_command))
    app.add_handler(CommandHandler("exportchannels", export_channels_command))
    
    # Add message handler for copying
    app.add_handler(MessageHandler(
        filters.Chat(chat_id=master_id) & filters.ALL,
        forward_message
    ))
    
    # Add error handler
    app.add_error_handler(error_handler)
    
    # Start heartbeat
    async def post_init(application):
        asyncio.create_task(heartbeat())
    
    app.post_init = post_init
    
    logger.info("✅ Copy bot is running in stealth mode!")
    logger.info("⏳ Waiting for messages and commands...")
    logger.info("🔒 Messages will appear as original posts (no forwarding label)")
    
    # ✅ FIX 2: Auto-restart on timeout/error
    while True:
        try:
            app.run_polling(allowed_updates=['channel_post', 'message'])
            break  # Exit loop if stopped normally
        except TimedOut:
            stats['restarts'] += 1
            logger.warning(f"⏱️ TIMEOUT ERROR - Auto-restarting (restart #{stats['restarts']})...")
            time.sleep(5)
            logger.info("🔄 Reconnecting...")
            continue
        except NetworkError as e:
            stats['restarts'] += 1
            logger.warning(f"🌐 NETWORK ERROR: {e} - Auto-restarting (restart #{stats['restarts']})...")
            time.sleep(20)
            logger.info("🔄 Reconnecting...")
            continue
        except KeyboardInterrupt:
            logger.info("=" * 60)
            logger.info("🛑 SHUTDOWN INITIATED")
            logger.info(f"📊 Final Stats:")
            logger.info(f"   Messages Processed: {stats['messages_processed']}")
            logger.info(f"   Total Copies: {stats['total_forwards']}")
            logger.info(f"   Restarts: {stats['restarts']}")
            logger.info("=" * 60)
            break
        except Exception as e:
            stats['restarts'] += 1
            logger.error(f"❌ UNEXPECTED ERROR: {e} - Auto-restarting (restart #{stats['restarts']})...")
            time.sleep(15)
            if stats['restarts'] > 10:
                logger.error("❌ Too many restarts! Exiting...")
                sys.exit(1)
            logger.info("🔄 Reconnecting...")
            continue


if __name__ == "__main__":
    main()
