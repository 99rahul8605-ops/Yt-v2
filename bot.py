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
        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ]
        
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
            'use_yt_dlp_cache': os.getenv('USE_YT_DLP_CACHE', 'true').lower() == 'true',
            'yt_dlp_timeout': int(os.getenv('YT_DLP_TIMEOUT', '30')),
            'max_retries': int(os.getenv('MAX_RETRIES', '3')),
        }
        
        # Create directories
        os.makedirs(config['temp_dir'], exist_ok=True)
        os.makedirs(config['cookies_backup_dir'], exist_ok=True)
        
        # Validate required config
        if not all([config['api_id'], config['api_hash'], config['bot_token']]):
            raise ValueError("Missing required Telegram configuration. Check TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_BOT_TOKEN.")
            
        logger.info("Configuration loaded successfully")
        logger.info(f"Web server will run on port: {config['port']}")
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
                lines = f.readlines()[:10]
                
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
                    "• /cookies_upload - Upload new cookies file\n"
                    "• /cookies_backup - Backup current cookies\n"
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
                "2. Then send the URL"
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
            await message.reply(
                "🔗 **Please send me a YouTube URL**\n\n"
                "**Send /cancel to abort the operation.**\n\n"
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
                    f"**Temp Directory:** {self.config['temp_dir']}\n"
                    f"**Web Server:** ✅ Running on port {self.config['port']}\n\n"
                    "✅ Bot is running normally"
                )
                
                await message.reply(status_text)
            except Exception as e:
                logger.error(f"Error in status command: {e}")
                await message.reply("🤖 Bot is running normally")
        
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
        
        # ========== COOKIE UPLOAD COMMANDS ==========
        
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
                test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"  # Use a known video
                
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'extract_flat': True,
                    'cookiefile': self.config['cookies_path'],
                    'user_agent': self.user_agents[0],
                    'socket_timeout': 30,
                }
                
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
            if user_id in self.cookie_upload_states:
                if self.cookie_upload_states[user_id] == "waiting_for_file":
                    await self.handle_cookies_upload(message)
        
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
                
                if isinstance(user_state, dict) and user_state.get("state") == "waiting_for_url":
                    logger.info(f"User {user_id} is waiting for URL, processing...")
                    
                    # Validate URL
                    if not self.validate_youtube_url(text):
                        await message.reply("❌ Invalid YouTube URL. Please send a valid YouTube link.")
                        del self.user_states[user_id]
                        return
                    
                    logger.info(f"URL validated: {text}")
                    
                    # Remove user from waiting state immediately
                    del self.user_states[user_id]
                    
                    # Start processing in background
                    asyncio.create_task(self.process_video(message, text))
                    return
            
            # If not waiting for URL and text looks like a YouTube URL, suggest using /yt
            if self.validate_youtube_url(text):
                await message.reply(
                    "🔗 **I see you sent a YouTube URL!**\n\n"
                    "To download videos, please use the /yt command first:\n"
                    "1. Send /yt\n"
                    "2. Then send the URL\n\n"
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
    
    async def process_video(self, message: Message, url: str):
        """Main processing pipeline"""
        user_id = message.from_user.id
        task_id = f"{user_id}_{int(asyncio.get_event_loop().time())}"
        
        try:
            # Track active download
            self.active_downloads[task_id] = user_id
            
            status_msg = await message.reply("⏳ Processing your request...")
            
            # Create user-specific temp directory
            user_temp_dir = os.path.join(self.config['temp_dir'], f"user_{user_id}")
            os.makedirs(user_temp_dir, exist_ok=True)
            
            # Step 1: Fetch video info with retry mechanism
            await self.update_status(status_msg, "📥 Fetching video information...")
            video_info = await self.get_video_info_with_retry(url, max_retries=3)
            
            if not video_info:
                error_msg = "❌ Failed to fetch video information."
                if not self.cookies_available:
                    error_msg += "\n\n⚠️ **Cookies not configured!**\nSome videos (especially age-restricted ones) require cookies to work."
                else:
                    error_msg += "\n\nPossible reasons:\n• Video is private/removed\n• Region restricted\n• Requires age verification\n• YouTube bot protection is active"
                    if self.cookies_available:
                        error_msg += "\n• Cookies may be expired or invalid"
                
                await status_msg.edit_text(error_msg)
                return
            
            # Step 2: Check duration limit
            duration = video_info.get('duration', 0)
            max_duration = self.config['max_duration']
            if duration > max_duration:
                await status_msg.edit_text(
                    f"❌ Video is too long ({duration//60} minutes). "
                    f"Maximum allowed duration is {max_duration//60} minutes."
                )
                return
            
            # Step 3: Download video with retry
            await self.update_status(status_msg, f"⬇️ Downloading: {video_info['title'][:50]}...")
            downloaded_files = await self.download_video_with_retry(url, video_info, user_temp_dir, max_retries=3)
            
            if not downloaded_files:
                error_msg = "❌ Failed to download video."
                if not self.cookies_available and video_info.get('age_limit', 0) > 0:
                    error_msg += "\n\n⚠️ This appears to be an age-restricted video. Cookies are required to download age-restricted content."
                elif self.cookies_available:
                    error_msg += "\n\n⚠️ YouTube may be blocking this video due to bot detection. Try:\n1. Updating cookies file\n2. Using a different video\n3. Waiting some time"
                await status_msg.edit_text(error_msg)
                return
            
            # Step 4: Process files
            await self.update_status(status_msg, "🔄 Processing video...")
            final_video = await self.process_downloaded_files(downloaded_files, user_temp_dir)
            
            if not final_video:
                await status_msg.edit_text("❌ Failed to process video files.")
                return
            
            # Step 5: Generate thumbnail
            await self.update_status(status_msg, "🖼️ Generating thumbnail...")
            thumbnail = await self.generate_thumbnail(final_video)
            
            # Step 6: Upload to Telegram
            await self.update_status(status_msg, "📤 Uploading to Telegram...")
            await self.upload_to_telegram(message, final_video, thumbnail, video_info)
            
            await status_msg.edit_text("✅ Video sent successfully!")
            
        except Exception as e:
            logger.error(f"Error processing video: {e}", exc_info=True)
            try:
                error_msg = f"❌ Error: {str(e)[:200]}"
                if "cookies" in str(e).lower() or "sign in" in str(e).lower() or "bot" in str(e).lower():
                    error_msg += "\n\n⚠️ **Cookies Issue Detected**\nYouTube is requiring sign-in for this video.\nPossible solutions:\n1. Update cookies file with fresh cookies\n2. Try a different video\n3. Wait and try again later"
                await message.reply(error_msg)
            except:
                pass
        
        finally:
            # Cleanup
            if task_id in self.active_downloads:
                del self.active_downloads[task_id]
            await self.cleanup_user_files(user_id)
    
    async def get_video_info_with_retry(self, url: str, max_retries: int = 3) -> Optional[Dict]:
        """Fetch video metadata with retry logic"""
        for attempt in range(max_retries):
            try:
                ydl_opts = self.get_ydl_opts(for_info=True, attempt=attempt)
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    
                    if not info:
                        continue
                    
                    # Handle playlists (take first video)
                    if 'entries' in info:
                        info = info['entries'][0]
                    
                    # Format the info
                    video_info = {
                        'id': info.get('id'),
                        'title': self.sanitize_filename(info.get('title', 'video')),
                        'duration': info.get('duration', 0),
                        'uploader': info.get('uploader', 'Unknown'),
                        'formats': info.get('formats', []),
                        'description': info.get('description', '')[:200],
                        'webpage_url': info.get('webpage_url', url),
                        'thumbnail': info.get('thumbnail'),
                        'age_limit': info.get('age_limit', 0),
                    }
                    
                    logger.info(f"Video info fetched on attempt {attempt+1}: {video_info['title']} ({video_info['duration']}s), Age limit: {video_info['age_limit']}")
                    return video_info
                    
            except Exception as e:
                logger.warning(f"Attempt {attempt+1} failed: {str(e)[:200]}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"All {max_retries} attempts failed to fetch video info")
        
        return None
    
    def get_ydl_opts(self, for_info: bool = False, attempt: int = 0) -> Dict:
        """Get yt-dlp options with intelligent configuration"""
        # Base options
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': self.config['yt_dlp_timeout'],
            'extractor_args': {'youtube': {'skip': ['hls', 'dash']}},
            'user_agent': self.user_agents[attempt % len(self.user_agents)],
        }
        
        # Add cookies if available
        if self.cookies_available:
            ydl_opts['cookiefile'] = self.config['cookies_path']
        
        if for_info:
            ydl_opts['extract_flat'] = False
        else:
            # Download options
            ydl_opts.update({
                'retries': 3,
                'fragment_retries': 3,
                'ignoreerrors': False,
                'no_overwrites': True,
                'continue_dl': True,
            })
            
            # Cache configuration
            if self.config['use_yt_dlp_cache']:
                cache_dir = os.path.join(self.config['temp_dir'], 'yt_dlp_cache')
                os.makedirs(cache_dir, exist_ok=True)
                ydl_opts['cachedir'] = cache_dir
        
        return ydl_opts
    
    async def download_video_with_retry(self, url: str, video_info: Dict, temp_dir: str, max_retries: int = 3) -> Optional[Dict]:
        """Download video with retry logic"""
        for attempt in range(max_retries):
            try:
                base_ydl_opts = self.get_ydl_opts(for_info=False, attempt=attempt)
                base_ydl_opts['outtmpl'] = os.path.join(temp_dir, '%(title)s.%(ext)s')
                base_ydl_opts['progress_hooks'] = [self.download_progress_hook]
                
                formats = self.select_format(video_info['formats'], attempt)
                ydl_opts = base_ydl_opts.copy()
                ydl_opts['format'] = formats['primary']
                
                return await self._download_with_opts(url, ydl_opts, temp_dir)
                
            except Exception as e:
                error_msg = str(e).lower()
                logger.warning(f"Download attempt {attempt+1} failed: {error_msg[:200]}")
                
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff
                    await asyncio.sleep(wait_time)
                    
                    # Try fallback format on retry
                    try:
                        ydl_opts = base_ydl_opts.copy()
                        ydl_opts['format'] = formats['fallback']
                        return await self._download_with_opts(url, ydl_opts, temp_dir)
                    except Exception as e2:
                        logger.warning(f"Fallback download also failed: {e2}")
                        continue
                else:
                    logger.error(f"All {max_retries} download attempts failed")
                    raise Exception(f"Download failed after {max_retries} attempts: {str(e)[:100]}")
        
        return None
    
    def select_format(self, formats: list, attempt: int = 0) -> Dict:
        """Select best format based on rules and attempt"""
        if attempt == 0:
            # First attempt: try for 720p with audio
            format_primary = "bestvideo[height<=720]+bestaudio/best[height<=720]"
        elif attempt == 1:
            # Second attempt: try for any video with audio
            format_primary = "bestvideo+bestaudio/best"
        else:
            # Third attempt: just get whatever works
            format_primary = "best"
        
        # Fallback: simplest format
        format_fallback = "best"
        
        return {
            'primary': format_primary,
            'fallback': format_fallback
        }
    
    def sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for safe filesystem usage"""
        # Remove invalid characters
        filename = re.sub(r'[<>:"/\\|?*]', '', filename)
        # Limit length
        filename = filename[:100]
        return filename.strip()
    
    async def _download_with_opts(self, url: str, ydl_opts: Dict, temp_dir: str) -> Dict:
        """Execute yt-dlp with given options"""
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            # Find downloaded files
            downloaded_files = {
                'video': None,
                'audio': None,
                'merged': None
            }
            
            for file in Path(temp_dir).glob('*'):
                if file.is_file():
                    ext = file.suffix.lower()
                    if ext in ['.mp4', '.mkv', '.webm', '.flv', '.avi']:
                        # Check if it's likely a video file by size (> 100KB)
                        if file.stat().st_size > 1024 * 100:
                            downloaded_files['video'] = str(file)
                    elif ext in ['.m4a', '.mp3', '.webm', '.opus', '.aac']:
                        downloaded_files['audio'] = str(file)
            
            logger.info(f"Downloaded files: {downloaded_files}")
            return downloaded_files
    
    def download_progress_hook(self, d):
        """Progress hook for yt-dlp"""
        if d['status'] == 'downloading':
            percent = d.get('_percent_str', '0%').strip()
            speed = d.get('_speed_str', 'N/A')
            eta = d.get('_eta_str', 'N/A')
            logger.debug(f"Download: {percent} at {speed}, ETA: {eta}")
        elif d['status'] == 'finished':
            logger.info("Download completed")
    
    async def process_downloaded_files(self, files: Dict, temp_dir: str) -> Optional[str]:
        """Process downloaded files (merge if needed)"""
        if files['video'] and files['audio']:
            # Need to merge audio and video
            logger.info("Merging audio and video streams")
            return await self.merge_audio_video(files['video'], files['audio'], temp_dir)
        elif files['video']:
            # Already a single file
            logger.info("Using single video file")
            return files['video']
        else:
            logger.error("No video file found")
            return None
    
    async def merge_audio_video(self, video_path: str, audio_path: str, temp_dir: str) -> Optional[str]:
        """Merge audio and video streams using ffmpeg"""
        output_path = os.path.join(temp_dir, "merged_video.mp4")
        
        try:
            logger.info(f"Merging {video_path} and {audio_path} -> {output_path}")
            
            # Use ffmpeg to merge without re-encoding
            input_video = ffmpeg.input(video_path)
            input_audio = ffmpeg.input(audio_path)
            
            ffmpeg.output(
                input_video,
                input_audio,
                output_path,
                vcodec='copy',
                acodec='copy',
                **{'strict': 'experimental'}
            ).run(quiet=True, overwrite_output=True, capture_stdout=True, capture_stderr=True)
            
            # Verify output
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                logger.info(f"Merged file created: {output_path} ({os.path.getsize(output_path)} bytes)")
                return output_path
            else:
                logger.error("Merged file is empty or doesn't exist")
            
        except ffmpeg.Error as e:
            logger.error(f"FFmpeg error: {e.stderr.decode() if e.stderr else str(e)}")
        except Exception as e:
            logger.error(f"Error merging files: {e}")
        
        return None
    
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
    
    async def upload_to_telegram(self, message: Message, video_path: str, 
                                thumbnail_path: Optional[str], video_info: Dict):
        """Upload video to Telegram with fallback"""
        try:
            # Get video duration and size
            probe = ffmpeg.probe(video_path)
            duration = int(float(probe['format']['duration']))
            file_size = os.path.getsize(video_path)
            
            logger.info(f"Uploading video: {file_size} bytes, {duration} seconds")
            
            # Check file size limit
            max_size = self.config['max_file_size']
            if file_size > max_size:
                await message.reply(f"❌ Video file too large ({file_size//(1024*1024)}MB). Max allowed: {max_size//(1024*1024)}MB.")
                return
            
            caption = f"**{video_info['title']}**\n\n📹 {video_info['uploader']}"
            
            # Progress callback
            def progress(current, total):
                percent = (current / total) * 100
                if int(percent) % 10 == 0:  # Log every 10%
                    logger.debug(f"Upload progress: {percent:.1f}% ({current}/{total})")
            
            # Try to send as video first
            try:
                await self.app.send_video(
                    chat_id=message.chat.id,
                    video=video_path,
                    caption=caption,
                    duration=duration,
                    thumb=thumbnail_path,
                    supports_streaming=True,
                    progress=progress
                )
                logger.info("Video sent successfully")
                
            except FloodWait as e:
                logger.warning(f"Flood wait: {e.value} seconds")
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
                
            except RPCError as e:
                # Fallback to document
                logger.warning(f"Video upload failed, sending as document: {e}")
                await self.app.send_document(
                    chat_id=message.chat.id,
                    document=video_path,
                    caption=caption,
                    thumb=thumbnail_path,
                    progress=progress
                )
                
        except Exception as e:
            logger.error(f"Upload error: {e}")
            raise Exception(f"Upload failed: {str(e)[:100]}")
    
    async def update_status(self, message: Message, text: str):
        """Update status message"""
        try:
            await message.edit_text(text)
        except Exception as e:
            logger.debug(f"Could not update status: {e}")
    
    async def cleanup_user_files(self, user_id: int):
        """Clean up user's temporary files"""
        user_dir = os.path.join(self.config['temp_dir'], f"user_{user_id}")
        if os.path.exists(user_dir):
            try:
                shutil.rmtree(user_dir)
                logger.info(f"Cleaned up temp files for user {user_id}")
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