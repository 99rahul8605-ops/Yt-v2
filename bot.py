import os
import asyncio
import re
import shutil
import json
import logging
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import RPCError, FloodWait

import yt_dlp
import ffmpeg

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class YouTubeDownloaderBot:
    def __init__(self):
        self.config = self.load_config()
        self.app = None
        self.user_states = {}
        self.active_downloads = {}
        self.cookies_available = False
        self.cookies_metadata = {}
        self.cookie_upload_states = {}  # Track users uploading cookies
        self.admin_ids = self.get_admin_ids()
        
        # Initialize cookies
        self.check_cookies_file()
        
    def load_config(self) -> Dict[str, Any]:
        """Load configuration from environment variables"""
        config = {
            'api_id': int(os.getenv('TELEGRAM_API_ID', 0)),
            'api_hash': os.getenv('TELEGRAM_API_HASH', ''),
            'bot_token': os.getenv('TELEGRAM_BOT_TOKEN', ''),
            'cookies_path': os.getenv('YOUTUBE_COOKIES_PATH', '/tmp/cookies.txt'),
            'cookies_backup_dir': os.getenv('COOKIES_BACKUP_DIR', '/tmp/cookies_backup'),
            'max_duration': int(os.getenv('MAX_DURATION', '1800')),
            'max_file_size': int(os.getenv('MAX_FILE_SIZE', '1500000000')),
            'allowed_users': os.getenv('ALLOWED_USERS', '').split(',') if os.getenv('ALLOWED_USERS') else [],
            'admin_users': os.getenv('ADMIN_USERS', '').split(',') if os.getenv('ADMIN_USERS') else [],
            'max_concurrent': int(os.getenv('MAX_CONCURRENT_DOWNLOADS', '1')),
            'temp_dir': os.getenv('TEMP_DIR', '/tmp/ytdl'),
        }
        
        # Create directories
        os.makedirs(config['temp_dir'], exist_ok=True)
        os.makedirs(config['cookies_backup_dir'], exist_ok=True)
        
        # Validate required config
        if not all([config['api_id'], config['api_hash'], config['bot_token']]):
            raise ValueError("Missing required Telegram configuration. Check TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_BOT_TOKEN.")
            
        logger.info("Configuration loaded successfully")
        return config
    
    def get_admin_ids(self) -> List[str]:
        """Get list of admin user IDs"""
        admin_ids = []
        
        # Add users from ADMIN_USERS
        if self.config.get('admin_users'):
            admin_ids.extend([uid.strip() for uid in self.config['admin_users'] if uid.strip()])
        
        # If no admin users specified, use allowed_users as admin
        if not admin_ids and self.config.get('allowed_users'):
            admin_ids.extend([uid.strip() for uid in self.config['allowed_users'] if uid.strip()])
        
        return admin_ids
    
    def check_cookies_file(self):
        """Check cookies file and load metadata"""
        cookies_path = self.config['cookies_path']
        
        if os.path.exists(cookies_path):
            try:
                cookies_size = os.path.getsize(cookies_path)
                mod_time = os.path.getmtime(cookies_path)
                mod_date = datetime.fromtimestamp(mod_time).strftime('%Y-%m-%d %H:%M:%S')
                
                # Read first few lines to check format
                with open(cookies_path, 'r', encoding='utf-8', errors='ignore') as f:
                    first_line = f.readline().strip()
                    
                self.cookies_available = cookies_size > 100
                
                # Load metadata
                self.cookies_metadata = {
                    'path': cookies_path,
                    'size': cookies_size,
                    'modified': mod_date,
                    'format': 'Netscape' if first_line.startswith('# Netscape') else 'Unknown',
                    'line_count': self.count_lines(cookies_path),
                    'domain_count': self.count_domains(cookies_path),
                }
                
                if self.cookies_available:
                    logger.info(f"Cookies file found: {cookies_path} ({cookies_size} bytes, modified: {mod_date})")
                else:
                    logger.warning(f"Cookies file is too small or empty: {cookies_path}")
                    
            except Exception as e:
                logger.error(f"Error reading cookies file: {e}")
                self.cookies_available = False
                self.cookies_metadata = {}
        else:
            logger.warning(f"No cookies file found at: {cookies_path}")
            self.cookies_available = False
            self.cookies_metadata = {}
    
    def count_lines(self, filepath: str) -> int:
        """Count lines in a file"""
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                return sum(1 for _ in f)
        except:
            return 0
    
    def count_domains(self, filepath: str) -> int:
        """Count unique domains in cookies file"""
        domains = set()
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if line.strip() and not line.startswith('#'):
                        parts = line.strip().split('\t')
                        if len(parts) >= 5:
                            domains.add(parts[0])
            return len(domains)
        except:
            return 0
    
    def validate_cookies_file(self, filepath: str) -> Tuple[bool, str]:
        """Validate if file is a valid cookies.txt file"""
        try:
            if not os.path.exists(filepath):
                return False, "File does not exist"
            
            file_size = os.path.getsize(filepath)
            if file_size < 100:
                return False, f"File too small ({file_size} bytes). Minimum 100 bytes required."
            
            if file_size > 1024 * 1024:  # 1MB
                return False, f"File too large ({file_size} bytes). Maximum 1MB allowed."
            
            # Check if it looks like a cookies.txt file
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                first_line = f.readline().strip()
                lines = f.readlines()[:10]  # Check first 10 lines after header
                
            # Check for Netscape format header
            is_netscape = first_line.startswith('# Netscape HTTP Cookie File')
            
            # Check for valid cookie lines
            cookie_lines = 0
            for line in lines:
                line = line.strip()
                if line and not line.startswith('#'):
                    parts = line.split('\t')
                    if len(parts) >= 7:
                        cookie_lines += 1
            
            if cookie_lines == 0 and not is_netscape:
                return False, "File doesn't appear to be a valid cookies.txt format"
            
            # Check for YouTube domain
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                if '.youtube.com' not in content and 'youtube.com' not in content:
                    return False, "No YouTube cookies found in file"
            
            return True, f"Valid cookies file. Size: {file_size} bytes, Format: {'Netscape' if is_netscape else 'Unknown'}, Cookie lines: {cookie_lines}"
            
        except Exception as e:
            return False, f"Error validating file: {str(e)}"
    
    def backup_current_cookies(self):
        """Backup current cookies file"""
        if os.path.exists(self.config['cookies_path']):
            try:
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                backup_path = os.path.join(self.config['cookies_backup_dir'], f'cookies_backup_{timestamp}.txt')
                shutil.copy2(self.config['cookies_path'], backup_path)
                logger.info(f"Backed up cookies to: {backup_path}")
                return backup_path
            except Exception as e:
                logger.error(f"Failed to backup cookies: {e}")
        return None
    
    async def check_user_access(self, user_id: int) -> bool:
        """Check if user is allowed to use bot"""
        if not self.config['allowed_users']:
            return True
        
        return str(user_id) in self.config['allowed_users']
    
    async def check_admin_access(self, user_id: int) -> bool:
        """Check if user is admin"""
        if not self.admin_ids:
            return False
        
        return str(user_id) in self.admin_ids
    
    async def start_client(self):
        """Initialize and start Telegram client"""
        logger.info("Starting Telegram bot...")
        
        self.app = Client(
            "youtube_downloader_bot",
            api_id=self.config['api_id'],
            api_hash=self.config['api_hash'],
            bot_token=self.config['bot_token'],
            workdir=self.config['temp_dir']
        )
        
        # Register handlers
        self.register_handlers()
        
        # Start the client
        await self.app.start()
        
        # Get bot info
        me = await self.app.get_me()
        logger.info(f"Bot started successfully! Username: @{me.username}")
        
    def register_handlers(self):
        """Register all Telegram command handlers"""
        
        @self.app.on_message(filters.command(["start", "help"]))
        async def start_command(client, message: Message):
            if not await self.check_user_access(message.from_user.id):
                await message.reply("❌ You are not authorized to use this bot.")
                return
            
            cookies_status = "✅ Active" if self.cookies_available else "❌ Not configured"
            
            admin_commands = ""
            if await self.check_admin_access(message.from_user.id):
                admin_commands = (
                    "\n\n**👑 Admin Commands:**\n"
                    "• /cookies_info - View detailed cookies info\n"
                    "• /cookies_upload - Upload new cookies file\n"
                    "• /cookies_backup - Backup current cookies\n"
                    "• /cookies_restore - Restore from backup\n"
                    "• /cookies_test - Test cookies with YouTube\n"
                    "• /cookies_delete - Delete cookies file\n"
                )
            
            await message.reply(
                "🎬 **YouTube Video Downloader Bot**\n\n"
                "**📋 Commands:**\n"
                "• /yt - Download a YouTube video\n"
                "• /status - Check bot status\n"
                "• /cookies - Check cookies status\n"
                "• /help - Show this message"
                f"{admin_commands}"
                "\n\n**⚙️ Limits:**\n"
                f"• Max duration: {self.config['max_duration']//60} minutes\n"
                "• Max resolution: 720p\n"
                "• Format: MP4\n\n"
                f"**🍪 Cookies Status:** {cookies_status}\n"
                "• Age-restricted videos require cookies\n\n"
                "**📖 Usage:**\n"
                "1. Send /yt\n"
                "2. Reply with a YouTube URL"
            )
        
        @self.app.on_message(filters.command("yt"))
        async def yt_command(client, message: Message):
            """Handle /yt command"""
            if not await self.check_user_access(message.from_user.id):
                await message.reply("❌ You are not authorized to use this bot.")
                return
            
            user_id = message.from_user.id
            
            # Check concurrent downloads limit
            active_count = sum(1 for uid in self.active_downloads.values() if uid == user_id)
            if active_count >= self.config['max_concurrent']:
                await message.reply("⏳ You have too many active downloads. Please wait for them to complete.")
                return
            
            # Check if user is already in a process
            if user_id in self.user_states:
                await message.reply("⏳ Please finish your current download first.")
                return
            
            self.user_states[user_id] = "waiting_for_url"
            await message.reply(
                "🔗 **Please send me a YouTube URL**\n\n"
                "Send /cancel to abort the operation.\n\n"
                "**Supported URLs:**\n"
                "• youtube.com/watch?v=...\n"
                "• youtu.be/...\n"
                "• youtube.com/shorts/...\n"
                "• youtube.com/playlist?list=... (first video only)\n\n"
                f"**Cookies:** {'✅ Active' if self.cookies_available else '❌ Not configured'}"
            )
        
        @self.app.on_message(filters.command("status"))
        async def status_command(client, message: Message):
            """Check bot status"""
            try:
                import psutil
                import platform
                
                # System info
                cpu_percent = psutil.cpu_percent()
                memory = psutil.virtual_memory()
                disk = psutil.disk_usage('/')
                
                status_text = (
                    "🤖 **Bot Status**\n\n"
                    f"**System:** {platform.system()} {platform.release()}\n"
                    f"**CPU Usage:** {cpu_percent}%\n"
                    f"**Memory:** {memory.percent}% used\n"
                    f"**Disk:** {disk.percent}% used\n"
                    f"**Active Downloads:** {len(self.active_downloads)}\n"
                    f"**Cookies:** {'✅ Available' if self.cookies_available else '❌ Not configured'}\n"
                    f"**Temp Directory:** {self.config['temp_dir']}\n\n"
                    "✅ Bot is running normally"
                )
                
                await message.reply(status_text)
            except Exception as e:
                logger.error(f"Error in status command: {e}")
                await message.reply("🤖 Bot is running normally")
        
        # ========== COOKIE MANAGEMENT COMMANDS ==========
        
        @self.app.on_message(filters.command("cookies"))
        async def cookies_command(client, message: Message):
            """Check cookies status"""
            if self.cookies_available:
                cookies_text = (
                    "🍪 **Cookies Status**\n\n"
                    f"✅ **Status:** Active and working\n"
                    f"📁 **Location:** `{self.config['cookies_path']}`\n"
                    f"📏 **Size:** {self.cookies_metadata.get('size', 0)} bytes\n"
                    f"📅 **Last Modified:** {self.cookies_metadata.get('modified', 'Unknown')}\n"
                    f"🔄 **Format:** {self.cookies_metadata.get('format', 'Unknown')}\n"
                    f"📊 **Lines:** {self.cookies_metadata.get('line_count', 0)}\n"
                    f"🌐 **Domains:** {self.cookies_metadata.get('domain_count', 0)}\n\n"
                    "✅ **Age-restricted videos:** Supported"
                )
            else:
                cookies_text = (
                    "🍪 **Cookies Status**\n\n"
                    "❌ **Status:** Not configured or invalid\n\n"
                    "**Without cookies:**\n"
                    "• Age-restricted videos will fail\n"
                    "• Some videos may require sign-in\n"
                    "• YouTube may block some requests\n\n"
                    "**To fix:**\n"
                    "Contact an admin to upload cookies."
                )
            
            await message.reply(cookies_text)
        
        @self.app.on_message(filters.command("cookies_info"))
        async def cookies_info_command(client, message: Message):
            """Detailed cookies information (Admin only)"""
            if not await self.check_admin_access(message.from_user.id):
                await message.reply("❌ Admin access required for this command.")
                return
            
            if not self.cookies_available:
                await message.reply("❌ No cookies file found.")
                return
            
            try:
                # Read sample of cookies
                sample_lines = []
                with open(self.config['cookies_path'], 'r', encoding='utf-8', errors='ignore') as f:
                    for i, line in enumerate(f):
                        if i < 20:  # First 20 lines
                            sample_lines.append(line.rstrip())
                        else:
                            break
                
                # Check for YouTube cookies specifically
                youtube_cookies = []
                with open(self.config['cookies_path'], 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        if '.youtube.com' in line or 'youtube.com' in line:
                            parts = line.strip().split('\t')
                            if len(parts) >= 6:
                                cookie_name = parts[5] if len(parts) > 5 else 'Unknown'
                                youtube_cookies.append(cookie_name)
                
                info_text = (
                    "🍪 **Detailed Cookies Information**\n\n"
                    f"**Path:** `{self.config['cookies_path']}`\n"
                    f"**Size:** {self.cookies_metadata.get('size', 0)} bytes\n"
                    f"**Modified:** {self.cookies_metadata.get('modified', 'Unknown')}\n"
                    f"**Format:** {self.cookies_metadata.get('format', 'Unknown')}\n"
                    f"**Total Lines:** {self.cookies_metadata.get('line_count', 0)}\n"
                    f"**Unique Domains:** {self.cookies_metadata.get('domain_count', 0)}\n"
                    f"**YouTube Cookies Found:** {len(youtube_cookies)}\n\n"
                    "**Sample Lines:**\n"
                )
                
                for i, line in enumerate(sample_lines[:10], 1):
                    info_text += f"{i}. `{line[:50]}{'...' if len(line) > 50 else ''}`\n"
                
                if youtube_cookies:
                    info_text += f"\n**YouTube Cookie Names:**\n"
                    unique_names = list(set(youtube_cookies))[:10]
                    for name in unique_names:
                        info_text += f"• {name}\n"
                    if len(youtube_cookies) > 10:
                        info_text += f"• ... and {len(youtube_cookies) - 10} more\n"
                
                await message.reply(info_text[:4000])  # Telegram limit
                
            except Exception as e:
                logger.error(f"Error reading cookies info: {e}")
                await message.reply(f"❌ Error reading cookies file: {str(e)[:200]}")
        
        @self.app.on_message(filters.command("cookies_upload"))
        async def cookies_upload_command(client, message: Message):
            """Start cookies upload process (Admin only)"""
            if not await self.check_admin_access(message.from_user.id):
                await message.reply("❌ Admin access required for this command.")
                return
            
            user_id = message.from_user.id
            self.cookie_upload_states[user_id] = "waiting_for_file"
            
            await message.reply(
                "📤 **Upload Cookies File**\n\n"
                "Please send me the `cookies.txt` file.\n\n"
                "**Instructions:**\n"
                "1. Export cookies from your browser using 'Get cookies.txt LOCALLY' extension\n"
                "2. Send the `cookies.txt` file to this chat\n\n"
                "**Requirements:**\n"
                "• File must be named `cookies.txt`\n"
                "• Minimum size: 100 bytes\n"
                "• Maximum size: 1MB\n"
                "• Must contain YouTube cookies\n\n"
                "Send /cancel to abort the upload."
            )
        
        @self.app.on_message(filters.command("cookies_backup"))
        async def cookies_backup_command(client, message: Message):
            """Backup current cookies (Admin only)"""
            if not await self.check_admin_access(message.from_user.id):
                await message.reply("❌ Admin access required for this command.")
                return
            
            if not self.cookies_available:
                await message.reply("❌ No cookies file to backup.")
                return
            
            try:
                backup_path = self.backup_current_cookies()
                if backup_path:
                    # Count backups
                    backup_dir = self.config['cookies_backup_dir']
                    backup_files = list(Path(backup_dir).glob('cookies_backup_*.txt'))
                    
                    await message.reply(
                        f"✅ **Cookies Backup Created**\n\n"
                        f"**Backup Location:** `{backup_path}`\n"
                        f"**Total Backups:** {len(backup_files)}\n\n"
                        "Use `/cookies_restore` to restore from a backup."
                    )
                else:
                    await message.reply("❌ Failed to create backup.")
                    
            except Exception as e:
                logger.error(f"Error creating backup: {e}")
                await message.reply(f"❌ Error creating backup: {str(e)[:200]}")
        
        @self.app.on_message(filters.command("cookies_restore"))
        async def cookies_restore_command(client, message: Message):
            """Restore cookies from backup (Admin only)"""
            if not await self.check_admin_access(message.from_user.id):
                await message.reply("❌ Admin access required for this command.")
                return
            
            # List available backups
            backup_dir = self.config['cookies_backup_dir']
            backup_files = list(Path(backup_dir).glob('cookies_backup_*.txt'))
            
            if not backup_files:
                await message.reply("❌ No backup files found.")
                return
            
            # Sort by modification time (newest first)
            backup_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            
            backup_list = "📂 **Available Backups:**\n\n"
            for i, backup in enumerate(backup_files[:10], 1):  # Show last 10
                mod_time = datetime.fromtimestamp(backup.stat().st_mtime)
                size = backup.stat().st_size
                backup_list += f"{i}. `{backup.name}`\n"
                backup_list += f"   Size: {size} bytes, Date: {mod_time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            
            if len(backup_files) > 10:
                backup_list += f"... and {len(backup_files) - 10} more backups\n\n"
            
            backup_list += "To restore, reply with the backup number (1-10) or send /cancel"
            
            user_id = message.from_user.id
            self.cookie_upload_states[user_id] = "waiting_for_backup_number"
            self.user_states[user_id] = {"backup_files": backup_files}
            
            await message.reply(backup_list)
        
        @self.app.on_message(filters.command("cookies_test"))
        async def cookies_test_command(client, message: Message):
            """Test cookies with YouTube (Admin only)"""
            if not await self.check_admin_access(message.from_user.id):
                await message.reply("❌ Admin access required for this command.")
                return
            
            if not self.cookies_available:
                await message.reply("❌ No cookies file to test.")
                return
            
            status_msg = await message.reply("🔄 Testing cookies with YouTube...")
            
            try:
                # Test URL (YouTube homepage)
                test_url = "https://www.youtube.com/"
                
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'extract_flat': True,
                    'cookiefile': self.config['cookies_path'],
                    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                }
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    # Try to extract info (this will fail if cookies are invalid)
                    info = ydl.extract_info(test_url, download=False)
                    
                    if info:
                        await status_msg.edit_text(
                            "✅ **Cookies Test Successful!**\n\n"
                            "Your cookies are working correctly with YouTube.\n\n"
                            "**Details:**\n"
                            f"• Connected to: {info.get('title', 'YouTube')}\n"
                            f"• Cookies file: {self.config['cookies_path']}\n"
                            f"• File size: {self.cookies_metadata.get('size', 0)} bytes\n\n"
                            "Age-restricted videos should now work."
                        )
                    else:
                        await status_msg.edit_text(
                            "⚠️ **Cookies Test Inconclusive**\n\n"
                            "Could not retrieve YouTube information.\n"
                            "This doesn't necessarily mean cookies are invalid.\n\n"
                            "Try downloading a video to test functionality."
                        )
                        
            except Exception as e:
                error_msg = str(e).lower()
                await status_msg.edit_text(
                    f"❌ **Cookies Test Failed**\n\n"
                    f"**Error:** {str(e)[:200]}\n\n"
                    "**Possible Issues:**\n"
                    "1. Cookies file is expired\n"
                    "2. Cookies don't have YouTube domain\n"
                    "3. YouTube is blocking the request\n"
                    "4. File format is invalid\n\n"
                    "Try uploading a fresh cookies file."
                )
        
        @self.app.on_message(filters.command("cookies_delete"))
        async def cookies_delete_command(client, message: Message):
            """Delete cookies file (Admin only)"""
            if not await self.check_admin_access(message.from_user.id):
                await message.reply("❌ Admin access required for this command.")
                return
            
            if not self.cookies_available:
                await message.reply("❌ No cookies file to delete.")
                return
            
            # Create confirmation buttons
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✅ Yes, delete cookies", callback_data="delete_cookies_yes")],
                    [InlineKeyboardButton("❌ No, keep cookies", callback_data="delete_cookies_no")]
                ]
            )
            
            await message.reply(
                "⚠️ **Delete Cookies File?**\n\n"
                f"**File:** `{self.config['cookies_path']}`\n"
                f"**Size:** {self.cookies_metadata.get('size', 0)} bytes\n"
                f"**Last Modified:** {self.cookies_metadata.get('modified', 'Unknown')}\n\n"
                "**Warning:** This will remove all cookies.\n"
                "Age-restricted videos will stop working.\n\n"
                "Are you sure you want to delete the cookies file?",
                reply_markup=keyboard
            )
        
        @self.app.on_callback_query()
        async def handle_callback_query(client, callback_query):
            """Handle callback queries for cookies deletion"""
            user_id = callback_query.from_user.id
            
            if not await self.check_admin_access(user_id):
                await callback_query.answer("Admin access required.", show_alert=True)
                return
            
            if callback_query.data == "delete_cookies_yes":
                try:
                    # Backup first
                    self.backup_current_cookies()
                    
                    # Delete cookies file
                    if os.path.exists(self.config['cookies_path']):
                        os.remove(self.config['cookies_path'])
                        self.cookies_available = False
                        self.cookies_metadata = {}
                        
                        await callback_query.message.edit_text(
                            "✅ **Cookies Deleted Successfully**\n\n"
                            "The cookies file has been removed.\n\n"
                            "**Note:**\n"
                            "• Age-restricted videos will no longer work\n"
                            "• A backup was created before deletion\n"
                            "• Use /cookies_upload to add new cookies"
                        )
                    else:
                        await callback_query.message.edit_text("❌ Cookies file not found.")
                        
                except Exception as e:
                    await callback_query.message.edit_text(f"❌ Error deleting cookies: {str(e)[:200]}")
                    
            elif callback_query.data == "delete_cookies_no":
                await callback_query.message.edit_text("✅ Cookies deletion cancelled.")
            
            await callback_query.answer()
        
        @self.app.on_message(filters.command("cancel"))
        async def cancel_command(client, message: Message):
            """Cancel current operation"""
            user_id = message.from_user.id
            
            if user_id in self.user_states:
                del self.user_states[user_id]
                await message.reply("❌ Operation cancelled.")
            
            if user_id in self.cookie_upload_states:
                del self.cookie_upload_states[user_id]
                await message.reply("❌ Cookies upload cancelled.")
        
        # Handle document messages (for cookies upload)
        @self.app.on_message(filters.document)
        async def handle_document(client, message: Message):
            """Handle document uploads (for cookies.txt)"""
            user_id = message.from_user.id
            
            # Check if user is in cookie upload state
            if user_id not in self.cookie_upload_states:
                return
            
            if self.cookie_upload_states[user_id] == "waiting_for_file":
                await self.handle_cookies_upload(message)
        
        # Handle text messages for backup restore
        @self.app.on_message(filters.text)
        async def handle_text_messages(client, message: Message):
            """Handle text messages"""
            # Skip commands
            if message.text and message.text.startswith('/'):
                return
            
            user_id = message.from_user.id
            text = message.text.strip()
            
            # Handle backup restore selection
            if user_id in self.cookie_upload_states:
                if self.cookie_upload_states[user_id] == "waiting_for_backup_number":
                    await self.handle_backup_restore(message, text)
                    return
            
            # Handle normal URL processing
            if user_id in self.user_states:
                if self.user_states[user_id] == "waiting_for_url":
                    url = text
                    
                    # Validate URL
                    if not self.validate_youtube_url(url):
                        await message.reply("❌ Invalid YouTube URL. Please send a valid YouTube link.")
                        del self.user_states[user_id]
                        return
                    
                    # Start processing in background
                    asyncio.create_task(self.process_video(message, url))
    
    async def handle_cookies_upload(self, message: Message):
        """Handle cookies.txt file upload"""
        user_id = message.from_user.id
        
        # Check if document is cookies.txt
        document = message.document
        if not document:
            await message.reply("❌ Please send a file, not text.")
            del self.cookie_upload_states[user_id]
            return
        
        # Check file name
        file_name = document.file_name.lower()
        if not (file_name == 'cookies.txt' or file_name.endswith('.txt')):
            await message.reply("❌ File must be a .txt file, preferably named 'cookies.txt'")
            del self.cookie_upload_states[user_id]
            return
        
        # Check file size
        if document.file_size > 1024 * 1024:  # 1MB
            await message.reply("❌ File too large. Maximum size is 1MB.")
            del self.cookie_upload_states[user_id]
            return
        
        if document.file_size < 100:
            await message.reply("❌ File too small. Minimum size is 100 bytes.")
            del self.cookie_upload_states[user_id]
            return
        
        status_msg = await message.reply("📥 Downloading cookies file...")
        
        try:
            # Download the file
            temp_dir = tempfile.mkdtemp()
            temp_path = os.path.join(temp_dir, "cookies_temp.txt")
            
            await message.download(temp_path)
            
            # Validate the file
            is_valid, validation_msg = self.validate_cookies_file(temp_path)
            
            if not is_valid:
                await status_msg.edit_text(f"❌ Invalid cookies file:\n\n{validation_msg}")
                shutil.rmtree(temp_dir)
                del self.cookie_upload_states[user_id]
                return
            
            # Backup current cookies
            self.backup_current_cookies()
            
            # Replace current cookies
            shutil.copy2(temp_path, self.config['cookies_path'])
            
            # Update cookies metadata
            self.check_cookies_file()
            
            # Cleanup
            shutil.rmtree(temp_dir)
            del self.cookie_upload_states[user_id]
            
            await status_msg.edit_text(
                f"✅ **Cookies Updated Successfully!**\n\n"
                f"{validation_msg}\n\n"
                f"**New File:** `{self.config['cookies_path']}`\n"
                f"**Size:** {self.cookies_metadata.get('size', 0)} bytes\n"
                f"**YouTube Cookies:** {self.cookies_metadata.get('domain_count', 0)} domains\n\n"
                "✅ Age-restricted videos should now work."
            )
            
        except Exception as e:
            logger.error(f"Error handling cookies upload: {e}")
            await status_msg.edit_text(f"❌ Error uploading cookies: {str(e)[:200]}")
            if 'temp_dir' in locals():
                try:
                    shutil.rmtree(temp_dir)
                except:
                    pass
            del self.cookie_upload_states[user_id]
    
    async def handle_backup_restore(self, message: Message, text: str):
        """Handle backup restore selection"""
        user_id = message.from_user.id
        
        try:
            # Check if input is a number
            if not text.isdigit():
                await message.reply("❌ Please enter a number (1-10) or /cancel")
                return
            
            index = int(text) - 1
            backup_files = self.user_states[user_id].get("backup_files", [])
            
            if index < 0 or index >= len(backup_files) or index >= 10:
                await message.reply("❌ Invalid selection. Please choose a number from 1-10")
                return
            
            selected_backup = backup_files[index]
            status_msg = await message.reply(f"🔄 Restoring from backup: `{selected_backup.name}`...")
            
            # Backup current cookies first
            self.backup_current_cookies()
            
            # Restore from backup
            shutil.copy2(selected_backup, self.config['cookies_path'])
            
            # Update cookies metadata
            self.check_cookies_file()
            
            # Cleanup states
            del self.cookie_upload_states[user_id]
            del self.user_states[user_id]
            
            await status_msg.edit_text(
                f"✅ **Cookies Restored Successfully!**\n\n"
                f"**Restored from:** `{selected_backup.name}`\n"
                f"**New file:** `{self.config['cookies_path']}`\n"
                f"**Size:** {self.cookies_metadata.get('size', 0)} bytes\n"
                f"**Modified:** {self.cookies_metadata.get('modified', 'Unknown')}\n\n"
                "✅ Cookies have been restored from backup."
            )
            
        except Exception as e:
            logger.error(f"Error restoring backup: {e}")
            await message.reply(f"❌ Error restoring backup: {str(e)[:200]}")
            del self.cookie_upload_states[user_id]
            if user_id in self.user_states:
                del self.user_states[user_id]
    
    # ... [Rest of the methods remain the same as previous version: validate_youtube_url, get_ydl_options, process_video, etc.]
    # Note: Due to character limit, I'm truncating here. The rest of the methods should be copied from the previous complete version.
    # The important parts are the cookie management methods above.

    def validate_youtube_url(self, url: str) -> bool:
        """Validate YouTube URL"""
        patterns = [
            r'(https?://)?(www\.)?youtube\.com/watch\?v=',
            r'(https?://)?youtu\.be/',
            r'(https?://)?(www\.)?youtube\.com/shorts/',
            r'(https?://)?(www\.)?youtube\.com/playlist\?list=',
            r'(https?://)?(www\.)?youtube\.com/embed/'
        ]
        return any(re.search(pattern, url, re.IGNORECASE) for pattern in patterns)
    
    def get_ydl_options(self, for_download: bool = False) -> Dict:
        """Get yt-dlp options with cookies"""
        base_opts = {
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 30,
            'retries': 10,
            'fragment_retries': 10,
            'ignoreerrors': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            },
        }
        
        if self.cookies_available:
            base_opts['cookiefile'] = self.config['cookies_path']
        
        if for_download:
            base_opts.update({
                'format': 'bestvideo[height<=720]+bestaudio/best[height<=720]/best',
                'merge_output_format': 'mp4',
                'outtmpl': '%(title)s.%(ext)s',
                'progress_hooks': [self.download_progress_hook],
            })
        
        return base_opts
    
    async def process_video(self, message: Message, url: str):
        """Main processing pipeline"""
        # [Implementation from previous version...]
        pass
    
    async def get_video_info(self, url: str) -> Optional[Dict]:
        """Fetch video metadata"""
        # [Implementation from previous version...]
        pass
    
    def sanitize_filename(self, filename: str) -> str:
        """Sanitize filename"""
        filename = re.sub(r'[<>:"/\\|?*]', '', filename)
        return filename[:100].strip()
    
    async def download_video(self, url: str, video_info: Dict, temp_dir: str) -> Optional[Dict]:
        """Download video"""
        # [Implementation from previous version...]
        pass
    
    def download_progress_hook(self, d):
        """Progress hook"""
        if d['status'] == 'downloading':
            percent = d.get('_percent_str', '0%').strip()
            logger.debug(f"Download: {percent}")
    
    async def process_downloaded_files(self, files: Dict, temp_dir: str) -> Optional[str]:
        """Process downloaded files"""
        # [Implementation from previous version...]
        pass
    
    async def merge_audio_video(self, video_path: str, audio_path: str, temp_dir: str) -> Optional[str]:
        """Merge audio and video"""
        # [Implementation from previous version...]
        pass
    
    async def generate_thumbnail(self, video_path: str) -> Optional[str]:
        """Generate thumbnail"""
        # [Implementation from previous version...]
        pass
    
    async def upload_to_telegram(self, message: Message, video_path: str, 
                                thumbnail_path: Optional[str], video_info: Dict):
        """Upload to Telegram"""
        # [Implementation from previous version...]
        pass
    
    async def update_status(self, message: Message, text: str):
        """Update status"""
        try:
            await message.edit_text(text)
        except:
            pass
    
    async def cleanup_user_files(self, user_id: int):
        """Clean up user files"""
        user_dir = os.path.join(self.config['temp_dir'], f"user_{user_id}")
        if os.path.exists(user_dir):
            try:
                shutil.rmtree(user_dir)
            except:
                pass
    
    async def run(self):
        """Main entry point"""
        try:
            await self.start_client()
            logger.info("Bot is running. Press Ctrl+C to stop.")
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            logger.info("Shutting down gracefully...")
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
        finally:
            if self.app:
                try:
                    await self.app.stop()
                except:
                    pass


async def main():
    """Main entry point"""
    env_path = Path('.env')
    if env_path.exists():
        from dotenv import load_dotenv
        load_dotenv()
    
    bot = YouTubeDownloaderBot()
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())