import os
import asyncio
import re
import shutil
import logging
from pathlib import Path
from typing import Optional, Dict, Any

from pyrogram import Client, filters
from pyrogram.types import Message
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
        self.temp_dir = None
        self.active_downloads = {}
        
    def load_config(self) -> Dict[str, Any]:
        """Load configuration from environment variables"""
        config = {
            'api_id': int(os.getenv('TELEGRAM_API_ID', 0)),
            'api_hash': os.getenv('TELEGRAM_API_HASH', ''),
            'bot_token': os.getenv('TELEGRAM_BOT_TOKEN', ''),
            'cookies_path': os.getenv('YOUTUBE_COOKIES_PATH', ''),
            'max_duration': int(os.getenv('MAX_DURATION', '3600')),
            'max_file_size': int(os.getenv('MAX_FILE_SIZE', '2000000000')),
            'allowed_users': os.getenv('ALLOWED_USERS', '').split(',') if os.getenv('ALLOWED_USERS') else [],
            'max_concurrent': int(os.getenv('MAX_CONCURRENT_DOWNLOADS', '2')),
            'temp_dir': os.getenv('TEMP_DIR', '/tmp/ytdl'),
        }
        
        # Create temp directory
        os.makedirs(config['temp_dir'], exist_ok=True)
        
        # Validate required config
        if not all([config['api_id'], config['api_hash'], config['bot_token']]):
            raise ValueError("Missing required Telegram configuration. Check TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_BOT_TOKEN.")
            
        logger.info("Configuration loaded successfully")
        return config
    
    async def check_user_access(self, user_id: int) -> bool:
        """Check if user is allowed to use bot"""
        if not self.config['allowed_users']:
            return True
        
        return str(user_id) in self.config['allowed_users']
    
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
            
            await message.reply(
                "🎬 **YouTube Video Downloader Bot**\n\n"
                "**Commands:**\n"
                "• /yt - Download a YouTube video\n"
                "• /status - Check bot status\n"
                "• /help - Show this message\n\n"
                "**Usage:**\n"
                "1. Send /yt\n"
                "2. Reply with a YouTube URL\n\n"
                "**Limits:**\n"
                "• Max duration: 60 minutes\n"
                "• Max resolution: 720p\n"
                "• Format: MP4\n\n"
                "**Notes:**\n"
                "• Processing may take time for long videos\n"
                "• Age-restricted videos require cookies setup\n"
                "• Videos are automatically deleted after sending"
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
                "• youtube.com/playlist?list=... (first video only)"
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
                    f"**Temp Directory:** {self.config['temp_dir']}\n\n"
                    "✅ Bot is running normally"
                )
                
                await message.reply(status_text)
            except Exception as e:
                logger.error(f"Error in status command: {e}")
                await message.reply("🤖 Bot is running normally")
        
        @self.app.on_message(filters.command("cancel"))
        async def cancel_command(client, message: Message):
            """Cancel current operation"""
            user_id = message.from_user.id
            if user_id in self.user_states:
                del self.user_states[user_id]
                await message.reply("❌ Operation cancelled.")
        
        # FIXED: Added parentheses to filters.command()
        @self.app.on_message(filters.text & ~filters.command())
        async def handle_message(client, message: Message):
            """Handle user messages"""
            if not await self.check_user_access(message.from_user.id):
                return
            
            user_id = message.from_user.id
            
            if user_id not in self.user_states:
                return
            
            if self.user_states[user_id] == "waiting_for_url":
                url = message.text.strip()
                
                # Validate URL
                if not self.validate_youtube_url(url):
                    await message.reply("❌ Invalid YouTube URL. Please send a valid YouTube link.")
                    del self.user_states[user_id]
                    return
                
                # Start processing in background
                asyncio.create_task(self.process_video(message, url))
    
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
    
    async def process_video(self, message: Message, url: str):
        """Main processing pipeline"""
        user_id = message.from_user.id
        task_id = f"{user_id}_{int(asyncio.get_event_loop().time())}"
        
        try:
            # Remove user from waiting state
            if user_id in self.user_states:
                del self.user_states[user_id]
            
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
                await status_msg.edit_text("❌ Failed to fetch video information. The video might be private or removed.")
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
                await status_msg.edit_text("❌ Failed to download video. It might be age-restricted or unavailable.")
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
                await message.reply(f"❌ Error: {str(e)[:200]}")
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
        if self.config['cookies_path'] and os.path.exists(self.config['cookies_path']):
            ydl_opts['cookiefile'] = self.config['cookies_path']
        
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
                    'description': info.get('description', '')[:100],
                    'webpage_url': info.get('webpage_url', url),
                    'thumbnail': info.get('thumbnail'),
                }
                
                logger.info(f"Video info fetched: {video_info['title']} ({video_info['duration']}s)")
                return video_info
                
        except Exception as e:
            logger.error(f"Error getting video info: {e}")
            return None
    
    def sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for safe filesystem usage"""
        # Remove invalid characters
        filename = re.sub(r'[<>:"/\\|?*]', '', filename)
        # Limit length
        filename = filename[:100]
        return filename.strip()
    
    async def download_video(self, url: str, video_info: Dict, temp_dir: str) -> Optional[Dict]:
        """Download video using yt-dlp with fallback logic"""
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
        
        # Try without cookies first
        formats = self.select_format(video_info['formats'])
        ydl_opts = base_ydl_opts.copy()
        ydl_opts['format'] = formats['primary']
        
        try:
            return await self._download_with_opts(url, ydl_opts, temp_dir)
            
        except yt_dlp.utils.DownloadError as e:
            error_msg = str(e).lower()
            logger.warning(f"Download failed without cookies: {error_msg[:100]}")
            
            # Check if cookies might help
            if any(keyword in error_msg for keyword in ['sign in', 'age verification', 'private', 'members only', 'login']):
                if self.config['cookies_path'] and os.path.exists(self.config['cookies_path']):
                    logger.info("Trying with cookies...")
                    ydl_opts = base_ydl_opts.copy()
                    ydl_opts['format'] = formats['fallback']
                    ydl_opts['cookiefile'] = self.config['cookies_path']
                    
                    try:
                        return await self._download_with_opts(url, ydl_opts, temp_dir)
                    except Exception as e2:
                        logger.error(f"Failed even with cookies: {e2}")
                        raise Exception("Video requires login but cookies didn't work")
                else:
                    raise Exception("Video requires login. Cookies file not configured.")
            else:
                raise
    
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
            
            # FIXED: Changed qscale:v=2 to qscale_v=2
            ffmpeg.input(video_path, ss=frame_time)\
                  .output(thumbnail_path, vframes=1, qscale_v=2)\
                  .run(quiet=True, overwrite_output=True, capture_stdout=True, capture_stderr=True)
            
            if os.path.exists(thumbnail_path) and os.path.getsize(thumbnail_path) > 0:
                return thumbnail_path
            else:
                logger.warning("Thumbnail generation failed, using default")
                
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
            
            # Check file size limit (Telegram has 2GB limit for bots)
            max_size = min(self.config['max_file_size'], 2000 * 1024 * 1024)  # 2GB or config
            if file_size > max_size:
                await message.reply(f"❌ Video file too large ({file_size//(1024*1024)}MB). Max allowed: {max_size//(1024*1024)}MB.")
                return
            
            caption = f"**{video_info['title']}**\n\n📹 {video_info['uploader']}"
            
            # Progress callback
            def progress(current, total):
                percent = (current / total) * 100
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
        """Main entry point"""
        try:
            await self.start_client()
            
            # Keep the bot running
            logger.info("Bot is running. Press Ctrl+C to stop.")
            await asyncio.Event().wait()
            
        except KeyboardInterrupt:
            logger.info("Shutting down gracefully...")
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
        finally:
            # Cleanup all temp files
            if os.path.exists(self.config['temp_dir']):
                try:
                    shutil.rmtree(self.config['temp_dir'])
                except:
                    pass
            
            if self.app:
                try:
                    await self.app.stop()
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