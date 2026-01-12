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
from aiohttp import web

from pyrogram import Client, filters, idle
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
        self.user_states = {}  # Stores user states for URL input
        self.active_downloads = {}  # Tracks active downloads
        self.cookies_available = False
        self.cookies_metadata = {}
        self.admin_ids = self.get_admin_ids()
        self.runner = None
        self.cookie_upload_states = {}  # Track cookie upload states
        self.download_states = {}  # Track download states for each user
        
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
            'port': int(os.getenv('PORT', '10000')),
            'proxy_url': os.getenv('PROXY_URL', ''),
            'max_retries': int(os.getenv('MAX_RETRIES', '3')),
            'fragment_retries': int(os.getenv('FRAGMENT_RETRIES', '25')),
        }
        
        # Create directories
        os.makedirs(config['temp_dir'], exist_ok=True)
        os.makedirs(config['cookies_backup_dir'], exist_ok=True)
        
        # Validate required config
        if not all([config['api_id'], config['api_hash'], config['bot_token']]):
            raise ValueError("Missing required Telegram configuration. Check TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_BOT_TOKEN.")
            
        logger.info("Configuration loaded successfully")
        logger.info(f"Web server will run on port: {config['port']}")
        if config['proxy_url']:
            logger.info(f"Proxy configured: {config['proxy_url']}")
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
                    content = f.read()
                    first_line = content.split('\n')[0].strip() if content else ""
                    
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
                content = f.read()
                first_line = content.split('\n')[0].strip()
            
            # Check for Netscape format header
            is_netscape = first_line.startswith('# Netscape HTTP Cookie File')
            
            # Check for YouTube-specific cookies
            youtube_domains = ['.youtube.com', 'youtube.com', '.youtu.be']
            has_youtube_cookies = any(domain in content for domain in youtube_domains)
            
            # Check for important YouTube cookies
            important_cookies = ['LOGIN_INFO', 'SID', 'HSID', 'SSID', 'APISID', 'SAPISID', 'YSC', 'VISITOR_INFO1_LIVE']
            has_important_cookies = any(cookie in content for cookie in important_cookies)
            
            if not has_youtube_cookies:
                return False, "No YouTube cookies found in file"
            
            # Count cookie lines
            cookie_lines = 0
            for line in content.split('\n'):
                line = line.strip()
                if line and not line.startswith('#'):
                    parts = line.split('\t')
                    if len(parts) >= 7:
                        cookie_lines += 1
            
            if cookie_lines == 0 and not is_netscape:
                return False, "File doesn't appear to be a valid cookies.txt format"
            
            validation_msg = (
                f"Valid cookies file. Size: {file_size} bytes, "
                f"Format: {'Netscape' if is_netscape else 'Unknown'}, "
                f"Cookie lines: {cookie_lines}, "
                f"YouTube domains: {has_youtube_cookies}, "
                f"Important cookies: {has_important_cookies}"
            )
            
            return True, validation_msg
            
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
    
    async def start_web_server(self):
        """Start a simple HTTP server for Render health checks"""
        app = web.Application()
        
        # Health check endpoint
        async def health_check(request):
            return web.json_response({
                'status': 'ok',
                'service': 'youtube-downloader-bot',
                'cookies_available': self.cookies_available,
                'active_downloads': len(self.active_downloads),
                'user_states': len(self.user_states),
                'timestamp': datetime.now().isoformat()
            })
        
        # Status endpoint
        async def status(request):
            try:
                import psutil
                import platform
                
                status_info = {
                    'status': 'running',
                    'bot': 'Telegram YouTube Downloader',
                    'system': f"{platform.system()} {platform.release()}",
                    'cpu_percent': psutil.cpu_percent(),
                    'memory_percent': psutil.virtual_memory().percent,
                    'disk_percent': psutil.disk_usage('/').percent,
                    'active_downloads': len(self.active_downloads),
                    'user_states': len(self.user_states),
                    'cookies_available': self.cookies_available,
                    'cookies_size': self.cookies_metadata.get('size', 0),
                    'temp_dir': self.config['temp_dir'],
                    'timestamp': datetime.now().isoformat()
                }
                return web.json_response(status_info)
            except Exception as e:
                return web.json_response({'status': 'error', 'message': str(e)}, status=500)
        
        # Root endpoint
        async def root(request):
            html = """
            <!DOCTYPE html>
            <html>
            <head>
                <title>YouTube Downloader Telegram Bot</title>
                <style>
                    body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; line-height: 1.6; }
                    .container { background: #f5f5f5; padding: 30px; border-radius: 10px; margin-top: 20px; }
                    .status { padding: 15px; border-radius: 5px; margin: 10px 0; }
                    .status-ok { background: #d4edda; color: #155724; }
                    .status-error { background: #f8d7da; color: #721c24; }
                    .endpoints { background: #fff; padding: 20px; border-radius: 5px; margin-top: 20px; }
                    code { background: #eee; padding: 2px 5px; border-radius: 3px; }
                </style>
            </head>
            <body>
                <h1>🎬 YouTube Downloader Telegram Bot</h1>
                <p>This is a Telegram bot that downloads YouTube videos and sends them to users.</p>
                
                <div class="container">
                    <h2>📊 Bot Status</h2>
                    <div class="status" id="status">Loading status...</div>
                    
                    <h2>🔗 Endpoints</h2>
                    <div class="endpoints">
                        <p><strong>Health Check:</strong> <code><a href="/health">/health</a></code></p>
                        <p><strong>Status:</strong> <code><a href="/status">/status</a></code></p>
                        <p><strong>Home:</strong> <code><a href="/">/</a></code></p>
                    </div>
                    
                    <h2>📱 How to Use</h2>
                    <p>1. Find the bot on Telegram: <code>@YourBotUsername</code></p>
                    <p>2. Send <code>/start</code> to begin</p>
                    <p>3. Use <code>/yt</code> to download videos</p>
                </div>
                
                <script>
                    async function updateStatus() {
                        try {
                            const response = await fetch('/health');
                            const data = await response.json();
                            document.getElementById('status').innerHTML = 
                                `<div class="status-ok">✅ Bot is running (Active downloads: ${data.active_downloads})</div>`;
                        } catch (error) {
                            document.getElementById('status').innerHTML = 
                                `<div class="status-error">❌ Error: ${error.message}</div>`;
                        }
                    }
                    
                    updateStatus();
                    setInterval(updateStatus, 30000);
                </script>
            </body>
            </html>
            """
            return web.Response(text=html, content_type='text/html')
        
        # Add routes
        app.router.add_get('/', root)
        app.router.add_get('/health', health_check)
        app.router.add_get('/status', status)
        
        # Start server
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', self.config['port'])
        await site.start()
        
        logger.info(f"🌐 Web server started on port {self.config['port']}")
        self.runner = runner
        
        return runner
    
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
        logger.info(f"✅ Bot started successfully! Username: @{me.username}")
        
        return me
    
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
                    "• /cookies_upload - Upload new cookies file (in private chat)\n"
                    "• /getcookies - Download current cookies file (in private chat)\n"
                    "• /cookies_backup - Backup current cookies\n"
                    "• /cookies_test - Test cookies with YouTube\n"
                    "• /cookies_delete - Delete cookies file\n"
                    "• /cookies_refresh - Refresh cookies status\n"
                )
            
            await message.reply(
                "🎬 **YouTube Video Downloader Bot**\n\n"
                "**📋 Commands:**\n"
                "• /yt - Download a YouTube video\n"
                "• /batch - Download multiple videos from text file\n"
                "• /status - Check bot status\n"
                "• /cookies_status - Check cookies status\n"
                "• /stop - Cancel current download\n"
                "• /help - Show this message"
                f"{admin_commands}"
                "\n\n**⚙️ Limits:**\n"
                f"• Max duration: {self.config['max_duration']//60} minutes\n"
                "• Supported resolutions: 144p, 240p, 360p, 480p, 720p, 1080p\n"
                "• Format: MP4/MKV\n\n"
                f"**🍪 Cookies Status:** {cookies_status}\n"
                "• Age-restricted videos require cookies\n\n"
                "**📖 Usage:**\n"
                "1. Send /yt\n"
                "2. Reply with a YouTube URL\n"
                "3. Select resolution (default: 720p)"
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
            
            self.user_states[user_id] = {"state": "waiting_for_url", "message_id": message.id}
            
            # Ask for resolution
            resolution_keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("144p", callback_data="res_144"),
                     InlineKeyboardButton("240p", callback_data="res_240"),
                     InlineKeyboardButton("360p", callback_data="res_360")],
                    [InlineKeyboardButton("480p", callback_data="res_480"),
                     InlineKeyboardButton("720p", callback_data="res_720"),
                     InlineKeyboardButton("1080p", callback_data="res_1080")],
                    [InlineKeyboardButton("Best Available", callback_data="res_best"),
                     InlineKeyboardButton("Cancel", callback_data="res_cancel")]
                ]
            )
            
            await message.reply(
                "📏 **Please select video resolution:**\n\n"
                "Or send the resolution number (144, 240, 360, 480, 720, 1080)\n"
                "Send 'best' for best available quality\n\n"
                f"**Cookies:** {'✅ Active' if self.cookies_available else '❌ Not configured'}\n"
                "**Send /cancel to abort.**",
                reply_markup=resolution_keyboard
            )
        
        @self.app.on_message(filters.command("batch"))
        async def batch_command(client, message: Message):
            """Handle batch download from text file"""
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
            
            self.user_states[user_id] = {"state": "waiting_for_batch", "message_id": message.id}
            
            await message.reply(
                "📁 **Batch Download**\n\n"
                "Please send me a text file (.txt) containing YouTube URLs (one per line).\n\n"
                "**Format:**\n"
                "https://youtube.com/watch?v=...\n"
                "https://youtu.be/...\n"
                "https://youtube.com/shorts/...\n\n"
                "**Send /cancel to abort.**"
            )
        
        @self.app.on_message(filters.command("stop"))
        async def stop_command(client, message: Message):
            """Stop current download"""
            user_id = message.from_user.id
            
            if user_id in self.download_states:
                self.download_states[user_id]['cancelled'] = True
                await message.reply("⏹️ Download stopped. Cleaning up...")
            elif user_id in self.user_states:
                del self.user_states[user_id]
                await message.reply("❌ Operation cancelled.")
            else:
                await message.reply("❌ No active download to stop.")
        
        @self.app.on_callback_query()
        async def handle_callback_query(client, callback_query):
            """Handle callback queries"""
            user_id = callback_query.from_user.id
            data = callback_query.data
            
            if data.startswith("res_"):
                if user_id not in self.user_states:
                    await callback_query.answer("Session expired. Please start again with /yt", show_alert=True)
                    return
                
                if data == "res_cancel":
                    del self.user_states[user_id]
                    await callback_query.message.edit_text("❌ Operation cancelled.")
                    await callback_query.answer()
                    return
                
                # Store resolution and ask for URL
                resolution = data.replace("res_", "")
                if resolution == "best":
                    resolution = "best"
                else:
                    resolution = resolution.replace("p", "")
                
                self.user_states[user_id] = {
                    "state": "waiting_for_url",
                    "resolution": resolution,
                    "message_id": callback_query.message.id
                }
                
                await callback_query.message.edit_text(
                    f"📏 **Resolution selected:** {resolution if resolution == 'best' else resolution + 'p'}\n\n"
                    "🔗 **Now send me the YouTube URL**\n\n"
                    "**Supported URLs:**\n"
                    "• youtube.com/watch?v=...\n"
                    "• youtu.be/...\n"
                    "• youtube.com/shorts/...\n"
                    "• youtube.com/playlist?list=... (first video only)\n\n"
                    "**Send /cancel to abort.**"
                )
                await callback_query.answer()
            
            elif data == "delete_cookies_yes":
                if not await self.check_admin_access(user_id):
                    await callback_query.answer("Admin access required.", show_alert=True)
                    return
                
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
                    
            elif data == "delete_cookies_no":
                await callback_query.message.edit_text("✅ Cookies deletion cancelled.")
            
            await callback_query.answer()
        
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
                    f"**Temp Directory:** {self.config['temp_dir']}\n"
                    f"**Web Server:** ✅ Running on port {self.config['port']}\n\n"
                    "✅ Bot is running normally"
                )
                
                await message.reply(status_text)
            except Exception as e:
                logger.error(f"Error in status command: {e}")
                await message.reply("🤖 Bot is running normally")
        
        @self.app.on_message(filters.command("cookies_status"))
        async def cookies_status_command(client, message: Message):
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
        
        # ========== SIMPLIFIED COOKIE UPLOAD COMMANDS ==========
        
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
                "• File must be named `cookies.txt` or have .txt extension\n"
                "• Minimum size: 100 bytes\n"
                "• Maximum size: 1MB\n"
                "• Must contain YouTube cookies\n\n"
                "Send /cancel to abort the upload."
            )
        
        @self.app.on_message(filters.command("getcookies"))
        async def getcookies_handler(client, message: Message):
            """Handle cookies download"""
            if not await self.check_admin_access(message.from_user.id):
                await message.reply("❌ Admin access required for this command.")
                return
            
            if not self.cookies_available:
                await message.reply("❌ No cookies file available to download.")
                return
            
            try:
                await client.send_document(
                    chat_id=message.chat.id,
                    document=self.config['cookies_path'],
                    caption=f"📁 **YouTube Cookies File**\n\n"
                           f"**Size:** {self.cookies_metadata.get('size', 0)} bytes\n"
                           f"**Modified:** {self.cookies_metadata.get('modified', 'Unknown')}\n"
                           f"**Format:** {self.cookies_metadata.get('format', 'Unknown')}\n\n"
                           "⚠️ **Keep this file secure!**"
                )
            except Exception as e:
                await message.reply_text(f"⚠️ An error occurred: {str(e)[:200]}")
        
        # ========== OTHER COOKIE ADMIN COMMANDS ==========
        
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
                    await message.reply(
                        f"✅ **Cookies Backup Created**\n\n"
                        f"**Backup Location:** `{backup_path}`\n\n"
                        "Cookies have been backed up successfully."
                    )
                else:
                    await message.reply("❌ Failed to create backup.")
                    
            except Exception as e:
                logger.error(f"Error creating backup: {e}")
                await message.reply(f"❌ Error creating backup: {str(e)[:200]}")
        
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
                # Test with a simple YouTube request
                test_url = "https://www.youtube.com/"
                
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'extract_flat': True,
                    'cookiefile': self.config['cookies_path'],
                    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'http_headers': {
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        'Accept-Language': 'en-US,en;q=0.5',
                        'Accept-Encoding': 'gzip, deflate',
                        'DNT': '1',
                        'Connection': 'keep-alive',
                        'Upgrade-Insecure-Requests': '1',
                    }
                }
                
                # Add proxy if configured
                if self.config.get('proxy_url'):
                    ydl_opts['proxy'] = self.config['proxy_url']
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(test_url, download=False)
                    
                    if info:
                        await status_msg.edit_text(
                            "✅ **Cookies Test Successful!**\n\n"
                            "Your cookies are working correctly with YouTube.\n\n"
                            "**Details:**\n"
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
                        if i < 10:  # First 10 lines
                            sample_lines.append(line.rstrip())
                        else:
                            break
                
                info_text = (
                    "🍪 **Detailed Cookies Information**\n\n"
                    f"**Path:** `{self.config['cookies_path']}`\n"
                    f"**Size:** {self.cookies_metadata.get('size', 0)} bytes\n"
                    f"**Modified:** {self.cookies_metadata.get('modified', 'Unknown')}\n"
                    f"**Format:** {self.cookies_metadata.get('format', 'Unknown')}\n"
                    f"**Total Lines:** {self.cookies_metadata.get('line_count', 0)}\n"
                    f"**Unique Domains:** {self.cookies_metadata.get('domain_count', 0)}\n\n"
                    "**Sample Lines:**\n"
                )
                
                for i, line in enumerate(sample_lines, 1):
                    info_text += f"{i}. `{line[:50]}{'...' if len(line) > 50 else ''}`\n"
                
                await message.reply(info_text[:4000])  # Telegram limit
                
            except Exception as e:
                logger.error(f"Error reading cookies info: {e}")
                await message.reply(f"❌ Error reading cookies file: {str(e)[:200]}")
        
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
        
        @self.app.on_message(filters.command("cookies_refresh"))
        async def cookies_refresh_command(client, message: Message):
            """Manually refresh cookies status (Admin only)"""
            if not await self.check_admin_access(message.from_user.id):
                await message.reply("❌ Admin access required for this command.")
                return
            
            status_msg = await message.reply("🔄 Refreshing cookies status...")
            
            old_status = self.cookies_available
            self.check_cookies_file()
            
            if self.cookies_available:
                if old_status:
                    await status_msg.edit_text(
                        "✅ **Cookies Refreshed**\n\n"
                        f"Cookies are still active.\n"
                        f"Size: {self.cookies_metadata.get('size', 0)} bytes\n"
                        f"Modified: {self.cookies_metadata.get('modified', 'Unknown')}"
                    )
                else:
                    await status_msg.edit_text(
                        "✅ **Cookies Restored**\n\n"
                        f"Cookies are now active!\n"
                        f"Size: {self.cookies_metadata.get('size', 0)} bytes\n"
                        f"Modified: {self.cookies_metadata.get('modified', 'Unknown')}"
                    )
            else:
                await status_msg.edit_text("❌ No valid cookies file found.")
        
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
        
        # Handle document messages (for cookies upload and batch upload)
        @self.app.on_message(filters.document)
        async def handle_document(client, message: Message):
            """Handle document uploads (for cookies.txt and batch .txt files)"""
            user_id = message.from_user.id
            
            # Check if user is in cookie upload state
            if user_id in self.cookie_upload_states:
                if self.cookie_upload_states[user_id] == "waiting_for_file":
                    await self.handle_cookies_upload(message)
                    return
            
            # Check if user is in batch upload state
            if user_id in self.user_states and self.user_states[user_id].get("state") == "waiting_for_batch":
                await self.handle_batch_upload(message)
        
        # Handle text messages
        @self.app.on_message(filters.text)
        async def handle_text_messages(client, message: Message):
            """Handle text messages"""
            user_id = message.from_user.id
            text = message.text.strip()
            
            # Skip if it's a command
            if text.startswith('/'):
                return
            
            # Check if user is waiting for URL
            if user_id in self.user_states:
                user_state = self.user_states[user_id]
                
                if user_state.get("state") == "waiting_for_url":
                    logger.info(f"User {user_id} is waiting for URL, processing...")
                    
                    # Check if text is a resolution number
                    if text.isdigit() and int(text) in [144, 240, 360, 480, 720, 1080]:
                        # Store resolution and ask for URL
                        self.user_states[user_id] = {
                            "state": "waiting_for_url",
                            "resolution": text,
                            "message_id": user_state.get("message_id")
                        }
                        
                        await message.reply(
                            f"📏 **Resolution set to {text}p**\n\n"
                            "🔗 **Now send me the YouTube URL**\n\n"
                            "**Supported URLs:**\n"
                            "• youtube.com/watch?v=...\n"
                            "• youtu.be/...\n"
                            "• youtube.com/shorts/...\n"
                            "• youtube.com/playlist?list=... (first video only)\n\n"
                            "**Send /cancel to abort.**"
                        )
                        return
                    elif text.lower() == "best":
                        # Store best resolution and ask for URL
                        self.user_states[user_id] = {
                            "state": "waiting_for_url",
                            "resolution": "best",
                            "message_id": user_state.get("message_id")
                        }
                        
                        await message.reply(
                            "📏 **Resolution set to Best Available**\n\n"
                            "🔗 **Now send me the YouTube URL**\n\n"
                            "**Supported URLs:**\n"
                            "• youtube.com/watch?v=...\n"
                            "• youtu.be/...\n"
                            "• youtube.com/shorts/...\n"
                            "• youtube.com/playlist?list=... (first video only)\n\n"
                            "**Send /cancel to abort.**"
                        )
                        return
                    
                    # Validate URL
                    if not self.validate_youtube_url(text):
                        await message.reply("❌ Invalid YouTube URL. Please send a valid YouTube link.")
                        return
                    
                    logger.info(f"URL validated: {text}")
                    
                    # Get resolution from user state or default to 720
                    resolution = user_state.get("resolution", "720")
                    
                    # Remove user from waiting state immediately
                    del self.user_states[user_id]
                    
                    # Start processing in background
                    asyncio.create_task(self.process_video(message, text, resolution))
                    return
            
            # If not waiting for URL and text looks like a YouTube URL, suggest using /yt
            if self.validate_youtube_url(text):
                await message.reply(
                    "🔗 **I see you sent a YouTube URL!**\n\n"
                    "To download videos, please use the /yt command first:\n"
                    "1. Send /yt\n"
                    "2. Select resolution\n"
                    "3. Then send the URL\n\n"
                    "This helps me keep track of your request."
                )
    
    async def handle_cookies_upload(self, message: Message):
        """Handle cookies.txt file upload"""
        user_id = message.from_user.id
        
        # Check if document is valid
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
    
    async def handle_batch_upload(self, message: Message):
        """Handle batch .txt file upload"""
        user_id = message.from_user.id
        
        # Check if document is valid
        document = message.document
        if not document:
            await message.reply("❌ Please send a file, not text.")
            del self.user_states[user_id]
            return
        
        # Check file name
        if not document.file_name.endswith('.txt'):
            await message.reply("❌ File must be a .txt file")
            del self.user_states[user_id]
            return
        
        status_msg = await message.reply("📥 Downloading batch file...")
        
        try:
            # Download the file
            temp_dir = tempfile.mkdtemp()
            temp_path = os.path.join(temp_dir, "batch_temp.txt")
            
            await message.download(temp_path)
            
            # Read URLs from file
            with open(temp_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            lines = [line.strip() for line in content.split('\n') if line.strip()]
            youtube_urls = [line for line in lines if self.validate_youtube_url(line)]
            
            if not youtube_urls:
                await status_msg.edit_text("❌ No valid YouTube URLs found in the file.")
                shutil.rmtree(temp_dir)
                del self.user_states[user_id]
                return
            
            # Ask for resolution
            resolution_keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("144p", callback_data="batch_res_144"),
                     InlineKeyboardButton("240p", callback_data="batch_res_240"),
                     InlineKeyboardButton("360p", callback_data="batch_res_360")],
                    [InlineKeyboardButton("480p", callback_data="batch_res_480"),
                     InlineKeyboardButton("720p", callback_data="batch_res_720"),
                     InlineKeyboardButton("1080p", callback_data="batch_res_1080")],
                    [InlineKeyboardButton("Best Available", callback_data="batch_res_best"),
                     InlineKeyboardButton("Cancel", callback_data="batch_res_cancel")]
                ]
            )
            
            # Store batch data
            self.user_states[user_id] = {
                "state": "waiting_for_batch_resolution",
                "urls": youtube_urls,
                "temp_dir": temp_dir,
                "temp_path": temp_path
            }
            
            await status_msg.edit_text(
                f"✅ **Found {len(youtube_urls)} valid YouTube URLs**\n\n"
                "📏 **Please select video resolution for all downloads:**\n\n"
                "Or send the resolution number (144, 240, 360, 480, 720, 1080)\n"
                "Send 'best' for best available quality\n\n"
                "**Send /cancel to abort.**",
                reply_markup=resolution_keyboard
            )
            
        except Exception as e:
            logger.error(f"Error handling batch upload: {e}")
            await status_msg.edit_text(f"❌ Error processing batch file: {str(e)[:200]}")
            if 'temp_dir' in locals():
                try:
                    shutil.rmtree(temp_dir)
                except:
                    pass
            del self.user_states[user_id]
    
    def validate_youtube_url(self, url: str) -> bool:
        """Validate YouTube URL with improved patterns"""
        # Clean the URL
        url = url.strip()
        
        # Remove any extra spaces or quotes
        url = url.replace('"', '').replace("'", "")
        
        patterns = [
            r'(?:https?://)?(?:www\.)?youtube\.com/watch\?(?:.*&)?v=[\w-]+',
            r'(?:https?://)?youtu\.be/[\w-]+',
            r'(?:https?://)?(?:www\.)?youtube\.com/shorts/[\w-]+',
            r'(?:https?://)?(?:www\.)?youtube\.com/playlist\?list=[\w-]+',
            r'(?:https?://)?(?:www\.)?youtube\.com/embed/[\w-]+',
            r'(?:https?://)?m\.youtube\.com/watch\?v=[\w-]+',
            r'(?:https?://)?(?:www\.)?youtube\.com/watch\?[\w=&%-]+v=[\w-]+[\w=&%-]*',
        ]
        
        for pattern in patterns:
            if re.match(pattern, url, re.IGNORECASE):
                return True
        
        # Also check if it contains youtube.com or youtu.be even if pattern didn't match exactly
        if 'youtube.com' in url.lower() or 'youtu.be' in url.lower():
            return True
        
        return False
    
    async def process_video(self, message: Message, url: str, resolution: str = "720"):
        """Main processing pipeline with enhanced logic from drm_handler.py"""
        user_id = message.from_user.id
        task_id = f"{user_id}_{int(asyncio.get_event_loop().time())}"
        
        try:
            # Track active download
            self.active_downloads[task_id] = user_id
            self.download_states[user_id] = {'cancelled': False}
            
            # Sanitize URL
            url = url.replace("file/d/", "uc?export=download&id=").replace("www.youtube-nocookie.com/embed", "youtu.be")
            
            # Get video title from oembed for YouTube
            video_title = ""
            if "youtu" in url:
                try:
                    oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
                    import requests
                    response = requests.get(oembed_url)
                    if response.status_code == 200:
                        video_title = response.json().get('title', 'YouTube Video')
                        video_title = video_title.replace("_", " ")[:60]
                except:
                    pass
            
            # Create user-specific temp directory
            user_temp_dir = os.path.join(self.config['temp_dir'], f"user_{user_id}_{int(time.time())}")
            os.makedirs(user_temp_dir, exist_ok=True)
            
            # Build yt-dlp command based on resolution
            if resolution == "best":
                ytf = "bv*+ba/b"
            else:
                # Enhanced format selection from drm_handler.py
                ytf = f"bv*[height<={resolution}][ext=mp4]+ba[ext=m4a]/b[height<=?{resolution}]"
            
            # Create filename
            if video_title:
                name = self.sanitize_filename(video_title)
            else:
                # Extract from URL
                name = "video"
                if "youtu.be/" in url:
                    name = url.split("youtu.be/")[1][:50]
                elif "youtube.com/watch?v=" in url:
                    name = url.split("youtube.com/watch?v=")[1][:50]
                name = self.sanitize_filename(name)
            
            # Build yt-dlp command
            cmd_parts = [
                'yt-dlp',
                '--no-warnings',
                '-R', str(self.config['max_retries']),
                '--fragment-retries', str(self.config['fragment_retries']),
                '-f', f'"{ytf}"',
                '--merge-output-format', 'mp4',
                '-o', f'"{user_temp_dir}/{name}.%(ext)s"',
            ]
            
            # Add cookies if available
            if self.cookies_available:
                cmd_parts.extend(['--cookies', self.config['cookies_path']])
            
            # Add proxy if configured
            if self.config.get('proxy_url'):
                cmd_parts.extend(['--proxy', self.config['proxy_url']])
            
            # Add URL
            cmd_parts.append(f'"{url}"')
            
            cmd = ' '.join(cmd_parts)
            
            # Send progress message
            progress_msg = await message.reply(
                f"⏳ **Processing Video**\n\n"
                f"**Title:** {video_title or 'Fetching...'}\n"
                f"**Resolution:** {resolution if resolution == 'best' else resolution + 'p'}\n"
                f"**URL:** [Click Here]({url})\n\n"
                f"🔄 Downloading...\n\n"
                f"**Send /stop to cancel**"
            )
            
            # Execute download
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                shell=True
            )
            
            # Monitor process
            while True:
                if self.download_states.get(user_id, {}).get('cancelled'):
                    process.terminate()
                    await progress_msg.edit_text("⏹️ Download cancelled. Cleaning up...")
                    break
                
                try:
                    await asyncio.wait_for(process.communicate(), timeout=1)
                except asyncio.TimeoutError:
                    continue
                else:
                    break
            
            if self.download_states.get(user_id, {}).get('cancelled'):
                # Cleanup
                shutil.rmtree(user_temp_dir, ignore_errors=True)
                if user_id in self.download_states:
                    del self.download_states[user_id]
                if task_id in self.active_downloads:
                    del self.active_downloads[task_id]
                return
            
            # Check if download was successful
            output_files = list(Path(user_temp_dir).glob('*.mp4'))
            if not output_files:
                # Try other extensions
                output_files = list(Path(user_temp_dir).glob('*.*'))
            
            if not output_files:
                await progress_msg.edit_text("❌ Download failed. No video file found.")
                return
            
            video_path = str(output_files[0])
            
            # Get video info for Telegram upload
            try:
                probe = ffmpeg.probe(video_path)
                duration = int(float(probe['format']['duration']))
                file_size = os.path.getsize(video_path)
                
                # Generate thumbnail
                thumbnail_path = await self.generate_thumbnail(video_path)
                
                # Check file size limit
                max_size = self.config['max_file_size']
                if file_size > max_size:
                    await progress_msg.edit_text(
                        f"❌ Video file too large ({file_size//(1024*1024)}MB). "
                        f"Max allowed: {max_size//(1024*1024)}MB."
                    )
                    return
                
                # Upload to Telegram
                caption = f"**{video_title or name}**\n\n"
                caption += f"📏 **Resolution:** {resolution if resolution == 'best' else resolution + 'p'}\n"
                caption += f"⏱️ **Duration:** {duration//60}:{duration%60:02d}\n"
                caption += f"📊 **Size:** {file_size//(1024*1024)}MB\n"
                caption += f"🔗 **Source:** [YouTube]({url})"
                
                await progress_msg.edit_text("📤 Uploading to Telegram...")
                
                # Try to send as video first
                try:
                    await self.app.send_video(
                        chat_id=message.chat.id,
                        video=video_path,
                        caption=caption,
                        duration=duration,
                        thumb=thumbnail_path,
                        supports_streaming=True
                    )
                    await progress_msg.edit_text("✅ Video sent successfully!")
                    
                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                    # Retry once
                    await self.app.send_video(
                        chat_id=message.chat.id,
                        video=video_path,
                        caption=caption,
                        duration=duration,
                        thumb=thumbnail_path,
                        supports_streaming=True
                    )
                    await progress_msg.edit_text("✅ Video sent successfully!")
                    
                except RPCError:
                    # Fallback to document
                    await self.app.send_document(
                        chat_id=message.chat.id,
                        document=video_path,
                        caption=caption,
                        thumb=thumbnail_path
                    )
                    await progress_msg.edit_text("✅ Video sent as document!")
                    
            except Exception as upload_error:
                logger.error(f"Upload error: {upload_error}")
                await progress_msg.edit_text(f"❌ Upload failed: {str(upload_error)[:200]}")
            
        except Exception as e:
            logger.error(f"Error processing video: {e}", exc_info=True)
            try:
                error_msg = f"❌ Error: {str(e)[:200]}"
                
                # Specific handling for sign-in errors
                error_lower = str(e).lower()
                if any(term in error_lower for term in ['sign in', 'confirm', 'not a bot', 'age verification']):
                    error_msg = (
                        "🔒 **YouTube Sign-In Required**\n\n"
                        "YouTube is asking for sign-in verification for this video.\n\n"
                        "**Possible reasons:**\n"
                        "• The video is age-restricted\n"
                        "• YouTube detected unusual activity\n"
                        "• Region/country restrictions\n\n"
                        "**Solutions to try:**\n"
                        "1. Use a fresh cookies.txt file from a logged-in YouTube account\n"
                        "2. Try downloading a different video\n"
                        "3. Wait a few hours and try again\n"
                        "4. Use a VPN/proxy (if supported)\n\n"
                        f"**Cookies status:** {'✅ Active' if self.cookies_available else '❌ Not configured'}"
                    )
                elif "cookies" in error_lower:
                    error_msg += "\n\n⚠️ **Cookies Issue Detected**\nThis video may require valid cookies or the cookies file may be expired/invalid."
                
                await message.reply(error_msg)
            except:
                pass
        
        finally:
            # Cleanup
            if task_id in self.active_downloads:
                del self.active_downloads[task_id]
            if user_id in self.download_states:
                del self.download_states[user_id]
            await self.cleanup_user_files(user_id)
    
    def sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for safe filesystem usage"""
        # Remove invalid characters
        filename = re.sub(r'[<>:"/\\|?*]', '', filename)
        # Remove emojis and special characters
        filename = re.sub(r'[^\w\s-]', '', filename)
        # Replace multiple spaces with single space
        filename = re.sub(r'\s+', ' ', filename)
        # Limit length
        filename = filename[:100]
        return filename.strip()
    
    async def generate_thumbnail(self, video_path: str) -> Optional[str]:
        """Generate thumbnail from video"""
        thumbnail_path = video_path + "_thumb.jpg"
        
        try:
            logger.info(f"Generating thumbnail for {video_path}")
            
            # Extract frame at 10 seconds or 1/4 of duration
            probe = ffmpeg.probe(video_path)
            duration = float(probe['format']['duration'])
            frame_time = min(10, duration / 4)
            
            # Use qscale_v instead of qscale:v
            ffmpeg.input(video_path, ss=frame_time)\
                  .output(thumbnail_path, vframes=1, qscale_v=2)\
                  .run(quiet=True, overwrite_output=True, capture_stdout=True, capture_stderr=True)
            
            if os.path.exists(thumbnail_path) and os.path.getsize(thumbnail_path) > 0:
                return thumbnail_path
            else:
                logger.warning("Thumbnail generation failed")
                
        except Exception as e:
            logger.error(f"Error generating thumbnail: {e}")
        
        return None
    
    async def cleanup_user_files(self, user_id: int):
        """Clean up user's temporary files"""
        # Find all user directories
        temp_dir = self.config['temp_dir']
        for item in os.listdir(temp_dir):
            if item.startswith(f"user_{user_id}_"):
                user_dir = os.path.join(temp_dir, item)
                if os.path.exists(user_dir):
                    try:
                        shutil.rmtree(user_dir)
                        logger.info(f"Cleaned up temp files: {user_dir}")
                    except Exception as e:
                        logger.error(f"Error cleaning user files: {e}")
    
    async def run(self):
        """Main entry point - runs both web server and Telegram bot"""
        try:
            # Start web server first (for Render health checks)
            logger.info("Starting web server...")
            await self.start_web_server()
            
            # Start Telegram bot
            logger.info("Starting Telegram bot...")
            me = await self.start_client()
            
            # Keep both running
            logger.info(f"✅ Bot is running! Telegram: @{me.username}, Web: http://0.0.0.0:{self.config['port']}")
            logger.info("Press Ctrl+C to stop.")
            
            # Run forever
            await idle()
            
        except KeyboardInterrupt:
            logger.info("Shutting down gracefully...")
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
        finally:
            # Cleanup web server
            if self.runner:
                await self.runner.cleanup()
                logger.info("Web server stopped")
            
            # Cleanup Telegram client
            if self.app:
                try:
                    await self.app.stop()
                    logger.info("Telegram bot stopped")
                except:
                    pass


async def main():
    """Main entry point with error handling"""
    # Load environment from .env if exists
    env_path = Path('.env')
    if env_path.exists():
        from dotenv import load_dotenv
        load_dotenv()
    
    # Try to update yt-dlp to latest version
    try:
        import subprocess
        import sys
        logger.info("Checking for yt-dlp updates...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"])
        logger.info("yt-dlp updated to latest version")
        
        # Reload yt_dlp module after update
        import importlib
        importlib.reload(yt_dlp)
    except Exception as e:
        logger.warning(f"Could not update yt-dlp: {e}")
    
    bot = YouTubeDownloaderBot()
    await bot.run()


if __name__ == "__main__":
    # Run with asyncio
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user")
    except Exception as e:
        print(f"Fatal error: {e}")