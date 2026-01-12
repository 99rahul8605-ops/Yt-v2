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
import threading

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
        self.web_app = None
        self.runner = None
        
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
            'port': int(os.getenv('PORT', '10000')),  # Render provides PORT
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
                    
                    <h2>⚙️ Configuration</h2>
                    <p><strong>Cookies:</strong> <span id="cookies-status">Checking...</span></p>
                    <p><strong>Port:</strong> <code id="port">Loading...</code></p>
                </div>
                
                <script>
                    async function updateStatus() {
                        try {
                            const response = await fetch('/health');
                            const data = await response.json();
                            document.getElementById('status').innerHTML = 
                                `<div class="status-ok">✅ Bot is running (Active downloads: ${data.active_downloads})</div>`;
                            document.getElementById('cookies-status').textContent = 
                                data.cookies_available ? '✅ Available' : '❌ Not configured';
                            document.getElementById('port').textContent = window.location.port || '10000';
                        } catch (error) {
                            document.getElementById('status').innerHTML = 
                                `<div class="status-error">❌ Error: ${error.message}</div>`;
                        }
                    }
                    
                    updateStatus();
                    setInterval(updateStatus, 30000); // Update every 30 seconds
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
        
        @self.app.on_message(filters.command("cancel"))
        async def cancel_command(client, message: Message):
            """Cancel current operation"""
            user_id = message.from_user.id
            if user_id in self.user_states:
                del self.user_states[user_id]
                await message.reply("❌ Operation cancelled.")
        
        # FIXED: Handle ALL text messages - not just non-commands
        @self.app.on_message(filters.text & ~filters.command)
        async def handle_all_text_messages(client, message: Message):
            """Handle all text messages except commands"""
            user_id = message.from_user.id
            text = message.text.strip()
            
            logger.info(f"Received text from user {user_id}: {text[:50]}...")
            
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
                logger.info(f"User {user_id} sent YouTube URL but not in waiting state")
                await message.reply(
                    "🔗 **I see you sent a YouTube URL!**\n\n"
                    "To download videos, please use the /yt command first:\n"
                    "1. Send /yt\n"
                    "2. Then send the URL\n\n"
                    "This helps me keep track of your request."
                )
    
    def validate_youtube_url(self, url: str) -> bool:
        """Validate YouTube URL with improved patterns"""
        # Clean the URL
        url = url.strip()
        
        # Remove any extra spaces or quotes
        url = url.replace('"', '').replace("'", "")
        
        patterns = [
            # Standard watch URLs
            r'(?:https?://)?(?:www\.)?youtube\.com/watch\?(?:.*&)?v=[\w-]+',
            # Short URLs
            r'(?:https?://)?youtu\.be/[\w-]+',
            # Shorts URLs
            r'(?:https?://)?(?:www\.)?youtube\.com/shorts/[\w-]+',
            # Playlist URLs
            r'(?:https?://)?(?:www\.)?youtube\.com/playlist\?list=[\w-]+',
            # Embed URLs
            r'(?:https?://)?(?:www\.)?youtube\.com/embed/[\w-]+',
            # Mobile URLs
            r'(?:https?://)?m\.youtube\.com/watch\?v=[\w-]+',
            # With additional parameters
            r'(?:https?://)?(?:www\.)?youtube\.com/watch\?[\w=&%-]+v=[\w-]+[\w=&%-]*',
        ]
        
        for pattern in patterns:
            if re.match(pattern, url, re.IGNORECASE):
                logger.info(f"URL matched pattern: {pattern}")
                return True
        
        # Also check if it contains youtube.com or youtu.be even if pattern didn't match exactly
        if 'youtube.com' in url.lower() or 'youtu.be' in url.lower():
            logger.info(f"URL contains youtube domain: {url}")
            return True
        
        logger.warning(f"URL validation failed for: {url}")
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
            
            # Step 1: Fetch video info
            await self.update_status(status_msg, "📥 Fetching video information...")
            video_info = await self.get_video_info(url)
            
            if not video_info:
                error_msg = "❌ Failed to fetch video information."
                if not self.cookies_available:
                    error_msg += "\n\n⚠️ **Cookies not configured!**\nSome videos (especially age-restricted ones) require cookies to work."
                else:
                    error_msg += "\n\nPossible reasons:\n• Video is private/removed\n• Region restricted\n• Requires age verification"
                
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
            
            # Step 3: Download video
            await self.update_status(status_msg, f"⬇️ Downloading: {video_info['title'][:50]}...")
            downloaded_files = await self.download_video(url, video_info, user_temp_dir)
            
            if not downloaded_files:
                error_msg = "❌ Failed to download video."
                if not self.cookies_available and video_info.get('age_limit', 0) > 0:
                    error_msg += "\n\n⚠️ This appears to be an age-restricted video. Cookies are required to download age-restricted content."
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
                if "cookies" in str(e).lower() or "sign in" in str(e).lower():
                    error_msg += "\n\n⚠️ **Cookies Issue Detected**\nThis video may require cookies or the cookies file may be invalid."
                await message.reply(error_msg)
            except:
                pass
        
        finally:
            # Cleanup
            if task_id in self.active_downloads:
                del self.active_downloads[task_id]
            await self.cleanup_user_files(user_id)
    
    async def get_video_info(self, url: str) -> Optional[Dict]:
        """Fetch video metadata using yt-dlp"""
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'socket_timeout': 30,
            'extractor_args': {'youtube': {'skip': ['hls', 'dash']}},
        }
        
        # Add cookies if available
        if self.cookies_available:
            ydl_opts['cookiefile'] = self.config['cookies_path']
            logger.info("Using cookies for video info fetch")
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                if not info:
                    return None
                
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
                
                logger.info(f"Video info fetched: {video_info['title']} ({video_info['duration']}s), Age limit: {video_info['age_limit']}")
                return video_info
                
        except Exception as e:
            logger.error(f"Error getting video info: {e}")
            
            # Try without cookies if cookies were used
            if self.cookies_available:
                logger.info("Retrying without cookies...")
                try:
                    ydl_opts_without_cookies = ydl_opts.copy()
                    if 'cookiefile' in ydl_opts_without_cookies:
                        del ydl_opts_without_cookies['cookiefile']
                    
                    with yt_dlp.YoutubeDL(ydl_opts_without_cookies) as ydl:
                        info = ydl.extract_info(url, download=False)
                        if info:
                            if 'entries' in info:
                                info = info['entries'][0]
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
                            logger.info(f"Video info fetched without cookies: {video_info['title']}")
                            return video_info
                except Exception as e2:
                    logger.error(f"Error getting video info without cookies: {e2}")
            
            return None
    
    def sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for safe filesystem usage"""
        # Remove invalid characters
        filename = re.sub(r'[<>:"/\\|?*]', '', filename)
        # Limit length
        filename = filename[:100]
        return filename.strip()
    
    async def download_video(self, url: str, video_info: Dict, temp_dir: str) -> Optional[Dict]:
        """Download video using yt-dlp"""
        base_ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'progress_hooks': [self.download_progress_hook],
            'socket_timeout': 30,
            'retries': 3,
            'fragment_retries': 3,
            'extractor_args': {'youtube': {'skip': ['hls', 'dash']}},
        }
        
        # Add cookies if available
        if self.cookies_available:
            base_ydl_opts['cookiefile'] = self.config['cookies_path']
            logger.info("Using cookies for download")
        
        formats = self.select_format(video_info['formats'])
        ydl_opts = base_ydl_opts.copy()
        ydl_opts['format'] = formats['primary']
        
        try:
            return await self._download_with_opts(url, ydl_opts, temp_dir)
            
        except Exception as e:
            error_msg = str(e).lower()
            logger.error(f"Download failed: {error_msg[:200]}")
            
            # Try fallback format
            try:
                ydl_opts = base_ydl_opts.copy()
                ydl_opts['format'] = formats['fallback']
                return await self._download_with_opts(url, ydl_opts, temp_dir)
            except Exception as e2:
                logger.error(f"Fallback download also failed: {e2}")
                raise Exception(f"Download failed: {str(e)[:100]}")
    
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
    
    def select_format(self, formats: list) -> Dict:
        """Select best format based on rules"""
        # Try: bestvideo(height<=720) + bestaudio
        format_primary = "bestvideo[height<=720]+bestaudio/best[height<=720]"
        # Fallback: best
        format_fallback = "best"
        
        return {
            'primary': format_primary,
            'fallback': format_fallback
        }
    
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